//! Worker threads, dispatch, tickets and shutdown for the chat-thread runtime (CR-02).
//!
//! Crate-private machinery behind [`crate::ChatPool`]. Workers are plain `std::thread` threads
//! that wait on a `Condvar`. This module has no async code (GA-3).
//!
//! Lock order (DD-12): the `ChatPool` lifecycle lock is always taken before the runtime lock
//! ([`Runtime::state`]). Nothing in this module takes the lifecycle lock. [`Runtime::submit`],
//! the workers and [`Runtime::shutdown`] take only the runtime lock. The budget port and the
//! executor run with the runtime lock released.
//!
//! Dispatch (GA-4): each thread has at most one job in flight, and [`FairQueue`] serves ready
//! threads round-robin. Every accepted message gets a [`Ticket`] that is completed exactly once.

use std::any::Any;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use parking_lot::{Condvar, Mutex, MutexGuard};

use crate::cancel::{CancelOutcome, CancelReport, CancelToken};
use crate::chat::{BudgetPort, BudgetRefusal, BudgetReservation, ChatError, ChatRuntimeConfig};
use crate::fair_queue::{FairQueue, OfferOutcome};

/// Milliseconds the built-in executor sleeps between checks of its hold time.
const HOLD_SLICE_MS: u64 = 10;

/// Fallback text for a caught executor panic whose payload is not a string.
const EXECUTOR_PANIC_TEXT: &str = "executor panicked";
/// Fallback text for a caught budget-port panic whose payload is not a string.
const BUDGET_PANIC_TEXT: &str = "budget port panicked";

/// One message to run on a chat thread. Build it with [`ChatMessage::new`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChatMessage {
    /// Caller-supplied id. Unique per thread within the dedupe window (DEC-6).
    pub msg_id: String,
    /// Chat thread the message runs on.
    pub thread_id: String,
    /// Session the thread belongs to. Budget reservations are charged to it.
    pub session_id: String,
    /// Opaque payload that the executor returns on success.
    pub payload: Vec<u8>,
    /// Wall-clock milliseconds the message reserves from the budget. Zero for every public
    /// submit (DEC-5). Only the crate's tests set it.
    pub(crate) hold_ms: u64,
}

impl ChatMessage {
    /// Creates a message with a zero hold time.
    pub fn new(
        msg_id: impl Into<String>,
        thread_id: impl Into<String>,
        session_id: impl Into<String>,
        payload: Vec<u8>,
    ) -> Self {
        Self {
            msg_id: msg_id.into(),
            thread_id: thread_id.into(),
            session_id: session_id.into(),
            payload,
            hold_ms: 0,
        }
    }
}

/// Names one message: its thread and its id. Message ids are unique per thread, not globally
/// (DEC-6).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MessageRef {
    /// Thread the message was submitted to.
    pub thread_id: String,
    /// Message id on that thread.
    pub msg_id: String,
}

/// The outcome of one accepted message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Completed {
    /// The executor returned this payload.
    Ok(Vec<u8>),
    /// The message failed with this error.
    Failed(ChatError),
    /// The message was cancelled. A cancelled message's executor result is discarded.
    Cancelled,
}

/// Handle to the eventual outcome of one accepted message.
///
/// The outcome is set once. A second completion is ignored, so a late executor result cannot
/// replace the outcome that a shutdown already recorded.
#[derive(Debug, Clone)]
pub struct Ticket {
    slot: Arc<TicketSlot>,
}

/// Shared outcome cell behind every clone of a [`Ticket`].
#[derive(Debug)]
struct TicketSlot {
    outcome: Mutex<Option<Completed>>,
    done: Condvar,
}

impl Ticket {
    /// Creates a ticket whose outcome is not yet set.
    pub(crate) fn pending() -> Self {
        Self {
            slot: Arc::new(TicketSlot {
                outcome: Mutex::new(None),
                done: Condvar::new(),
            }),
        }
    }

    /// Sets the outcome if none is set yet. Returns `false` when the ticket already completed.
    pub(crate) fn complete(&self, outcome: Completed) -> bool {
        let mut slot = self.slot.outcome.lock();
        if slot.is_some() {
            return false;
        }
        *slot = Some(outcome);
        self.slot.done.notify_all();
        true
    }

    /// Waits up to `timeout` for the outcome and returns a copy of it.
    ///
    /// Returns `None` if the message is still pending when the timeout elapses (GA-5). A timeout
    /// does not cancel the message.
    pub fn wait(&self, timeout: Duration) -> Option<Completed> {
        let mut slot = self.slot.outcome.lock();
        if slot.is_none() {
            match Instant::now().checked_add(timeout) {
                Some(deadline) => {
                    while slot.is_none() {
                        if self.slot.done.wait_until(&mut slot, deadline).timed_out() {
                            break;
                        }
                    }
                }
                // A timeout too large for an `Instant` never elapses, so the wait lasts until the
                // outcome arrives. parking_lot's `wait_for` makes the same choice.
                None => {
                    while slot.is_none() {
                        self.slot.done.wait(&mut slot);
                    }
                }
            }
        }
        slot.clone()
    }
}

/// Result of [`crate::ChatPool::submit`] for a message the queue accepted or deduplicated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SubmitOutcome {
    /// The message is queued at sequence number `seq` on its thread.
    Enqueued {
        /// Per-thread sequence number of the accepted message.
        seq: u64,
    },
    /// The message id is inside the thread's dedupe window. Nothing was enqueued.
    AlreadyAccepted {
        /// Sequence number the message was first accepted at.
        seq: u64,
    },
}

/// A submit result: the outcome, and the ticket when the message was enqueued.
#[derive(Debug, Clone)]
pub struct Submitted {
    /// Whether the message was enqueued or was a duplicate.
    pub outcome: SubmitOutcome,
    /// Ticket for the enqueued message. `None` for [`SubmitOutcome::AlreadyAccepted`].
    pub ticket: Option<Ticket>,
}

/// What [`crate::ChatPool::shutdown`] did with every message queued or in flight at the call
/// (GA-10). Messages that completed before the call are not listed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShutdownReport {
    /// Messages that completed before the grace deadline, in completion order.
    pub flushed: Vec<MessageRef>,
    /// Queued messages that had not started at the deadline, in queue order. Each ticket
    /// completes `Failed(ShuttingDown)`.
    pub dropped: Vec<MessageRef>,
    /// In-flight messages still running at the deadline, in start order. Each ticket completes
    /// `Failed(ShuttingDown)`, and the late executor result is ignored.
    pub abandoned: Vec<MessageRef>,
    /// Queued messages that a cancel removed while shutdown waited, in cancel order. Each ticket
    /// completes `Cancelled`. A message listed here is in none of `flushed`, `dropped` or
    /// `abandoned`. A message that was running when cancelled is reported by how it ended.
    pub cancelled: Vec<MessageRef>,
}

/// Runs one message on a worker and returns its payload, or the failure text. The executor
/// receives the message's cancel token and must check it cooperatively (GA-9).
pub(crate) type Executor =
    Arc<dyn Fn(&ChatMessage, &CancelToken) -> Result<Vec<u8>, String> + Send + Sync>;

/// The executor every [`crate::ChatPool`] uses. It holds for `hold_ms`, checks the token after
/// each slice, and returns the payload. No Python callable runs on a worker (DEC-5).
pub(crate) fn builtin_executor() -> Executor {
    Arc::new(run_builtin)
}

/// Holds for `hold_ms`, checking `cancel` after each slice, and returns the payload.
fn run_builtin(msg: &ChatMessage, cancel: &CancelToken) -> Result<Vec<u8>, String> {
    let mut remaining = msg.hold_ms;
    while remaining > 0 {
        let slice = remaining.min(HOLD_SLICE_MS);
        thread::sleep(Duration::from_millis(slice));
        remaining -= slice;
        if cancel.is_cancelled() {
            return Err("cancelled".to_string());
        }
    }
    Ok(msg.payload.clone())
}

/// A queued message and the ticket its outcome completes.
pub(crate) struct Job {
    msg: ChatMessage,
    ticket: Ticket,
}

/// A job a worker is running.
struct InFlight {
    msg_ref: MessageRef,
    ticket: Ticket,
    /// Set by a cancel or by shutdown. The worker reads it when the executor returns.
    cancel: CancelToken,
}

/// The report of a shutdown in progress: the messages it covers, and those already flushed or
/// cancelled.
#[derive(Default)]
struct Watch {
    pending: Vec<MessageRef>,
    flushed: Vec<MessageRef>,
    cancelled: Vec<MessageRef>,
}

impl RuntimeState {
    /// Whether shutdown or abort has closed intake. Callers read it under the runtime lock they
    /// already hold, so the answer is consistent with the queue they touch.
    pub(crate) fn is_closed(&self) -> bool {
        self.closed
    }

    /// Takes a queued message that a cancel removed out of the shutdown's pending set, so the
    /// wait does not hold for it, and lists it as cancelled. Does nothing when no shutdown is
    /// waiting. The caller notifies the shutdown.
    fn note_cancelled(&mut self, msg_ref: MessageRef) {
        let Some(watch) = self.watch.as_mut() else {
            return;
        };
        if let Some(position) = watch.pending.iter().position(|pending| *pending == msg_ref) {
            watch.pending.remove(position);
            watch.cancelled.push(msg_ref);
        }
    }
}

/// State behind the runtime lock.
pub(crate) struct RuntimeState {
    /// Per-thread queues and the ready ring.
    pub(crate) queue: FairQueue<Job>,
    /// Set by shutdown. Later submits return `ShuttingDown`.
    closed: bool,
    /// Jobs running on workers, in start order.
    in_flight: Vec<InFlight>,
    /// Report of the shutdown in progress, if any.
    watch: Option<Watch>,
}

/// Data shared by the runtime handle and every worker.
struct Shared {
    state: Mutex<RuntimeState>,
    /// Wakes idle workers when work arrives or the runtime closes.
    work: Condvar,
    /// Wakes a shutdown in progress after each completion.
    progress: Condvar,
    budget: Arc<dyn BudgetPort>,
    executor: Executor,
    /// Workers that have not exited. Changed under the runtime lock (see [`WorkerExit`]).
    live: Arc<AtomicUsize>,
}

/// Worker threads and the queue they serve. Crate-private.
pub(crate) struct Runtime {
    shared: Arc<Shared>,
    workers: Mutex<Vec<JoinHandle<()>>>,
}

impl Runtime {
    /// Starts `config.workers` workers that run `executor` under `budget`.
    ///
    /// Returns [`ChatError::WorkerSpawn`] when the operating system refuses a thread. The workers
    /// already started then exit, because the runtime is closed.
    pub(crate) fn start(
        config: &ChatRuntimeConfig,
        budget: Arc<dyn BudgetPort>,
        executor: Executor,
    ) -> Result<Self, ChatError> {
        let shared = Arc::new(Shared {
            state: Mutex::new(RuntimeState {
                queue: FairQueue::new(config.per_thread_queue, config.dedupe_window),
                closed: false,
                in_flight: Vec::new(),
                watch: None,
            }),
            work: Condvar::new(),
            progress: Condvar::new(),
            budget,
            executor,
            live: Arc::new(AtomicUsize::new(0)),
        });
        let mut workers = Vec::with_capacity(config.workers);
        for index in 0..config.workers {
            shared.live.fetch_add(1, Ordering::AcqRel);
            let worker_shared = Arc::clone(&shared);
            let spawned = thread::Builder::new()
                .name(format!("gsd-chat-worker-{index}"))
                .spawn(move || worker_loop(worker_shared));
            match spawned {
                Ok(handle) => workers.push(handle),
                Err(err) => {
                    // The closure never ran, so no worker exits to count this one out.
                    shared.live.fetch_sub(1, Ordering::AcqRel);
                    shared.state.lock().closed = true;
                    shared.work.notify_all();
                    return Err(ChatError::WorkerSpawn(err.to_string()));
                }
            }
        }
        Ok(Self {
            shared,
            workers: Mutex::new(workers),
        })
    }

    /// Locks the runtime state. Always taken after the lifecycle lock (DD-12).
    pub(crate) fn state(&self) -> MutexGuard<'_, RuntimeState> {
        self.shared.state.lock()
    }

    /// Queues `msg` on its thread. See [`crate::ChatPool::submit`] for the refusals.
    pub(crate) fn submit(&self, msg: ChatMessage) -> Result<Submitted, ChatError> {
        let mut state = self.shared.state.lock();
        if state.closed {
            return Err(ChatError::ShuttingDown);
        }
        let ticket = Ticket::pending();
        let thread_id = msg.thread_id.clone();
        let msg_id = msg.msg_id.clone();
        let job = Job {
            msg,
            ticket: ticket.clone(),
        };
        match state.queue.offer(&thread_id, &msg_id, job)? {
            OfferOutcome::Enqueued { seq } => {
                self.shared.work.notify_one();
                Ok(Submitted {
                    outcome: SubmitOutcome::Enqueued { seq },
                    ticket: Some(ticket),
                })
            }
            OfferOutcome::AlreadyAccepted { seq } => Ok(Submitted {
                outcome: SubmitOutcome::AlreadyAccepted { seq },
                ticket: None,
            }),
        }
    }

    /// Stops accepting work and reports on every message queued or in flight at the call.
    ///
    /// Messages that finish before `grace` elapses are flushed. At the deadline, queued messages
    /// are dropped and running messages are abandoned: each abandoned job's cancel token is set,
    /// and its ticket completes with `Failed(ShuttingDown)`. Workers exit once the closed queue
    /// is empty, and a worker still running an abandoned job is detached. A queued message that a
    /// cancel removes while the wait runs leaves the wait and is listed as `cancelled`.
    pub(crate) fn shutdown(&self, grace: Duration) -> ShutdownReport {
        let shared = &self.shared;
        let mut state = shared.state.lock();
        // One shutdown at a time: a concurrent call waits for the first report to finish.
        while state.watch.is_some() {
            shared.progress.wait(&mut state);
        }
        state.closed = true;
        let mut pending: Vec<MessageRef> = state
            .queue
            .queued_refs()
            .into_iter()
            .map(|(thread_id, msg_id)| MessageRef { thread_id, msg_id })
            .collect();
        pending.extend(state.in_flight.iter().map(|record| record.msg_ref.clone()));
        state.watch = Some(Watch {
            pending,
            flushed: Vec::new(),
            cancelled: Vec::new(),
        });
        shared.work.notify_all();

        let deadline = Instant::now().checked_add(grace);
        while state
            .watch
            .as_ref()
            .is_some_and(|watch| !watch.pending.is_empty())
        {
            match deadline {
                Some(deadline) => {
                    if shared.progress.wait_until(&mut state, deadline).timed_out() {
                        break;
                    }
                }
                None => shared.progress.wait(&mut state),
            }
        }

        let watch = state.watch.take().unwrap_or_default();
        let mut dropped = Vec::new();
        for (thread_id, msg_id, job) in state.queue.drain_queued() {
            job.ticket
                .complete(Completed::Failed(ChatError::ShuttingDown));
            dropped.push(MessageRef { thread_id, msg_id });
        }
        let mut abandoned = Vec::new();
        for record in std::mem::take(&mut state.in_flight) {
            // The token is set before the worker is detached (GA-10).
            record.cancel.cancel();
            record
                .ticket
                .complete(Completed::Failed(ChatError::ShuttingDown));
            abandoned.push(record.msg_ref);
        }
        let none_running = abandoned.is_empty();
        shared.progress.notify_all();
        drop(state);

        self.reap_workers(none_running);
        ShutdownReport {
            flushed: watch.flushed,
            dropped,
            abandoned,
            cancelled: watch.cancelled,
        }
    }

    /// Joins the workers. When no job is running, every worker exits promptly and is joined.
    /// Otherwise a worker that is still running is detached and exits on its own. A worker whose
    /// join reports a panic is logged at error level.
    fn reap_workers(&self, none_running: bool) {
        let handles = std::mem::take(&mut *self.workers.lock());
        for handle in handles {
            if none_running || handle.is_finished() {
                // `run_job` catches executor and budget panics, so a worker loop should not
                // unwind. If one does, the join reports it here.
                match handle.join() {
                    Ok(()) => {}
                    Err(payload) => tracing::error!(
                        error = %panic_text(payload.as_ref(), "worker panicked"),
                        "chat worker thread panicked"
                    ),
                }
            }
        }
    }

    /// Cancels message `msg_id` on thread `thread_id` (CR-03). The decision and the action share
    /// one runtime lock, in this order:
    ///
    /// 1. `UnknownThread` when the thread is not open.
    /// 2. A queued message is removed and its ticket completes `Cancelled`: `RemovedQueued`. A
    ///    removal during a shutdown wait is recorded in the shutdown report as `cancelled`.
    /// 3. The message a worker is running has its token set: `SignalledInFlight`.
    /// 4. Anything else is `UnknownMessage`, and nothing changes.
    pub(crate) fn cancel_message(
        &self,
        thread_id: &str,
        msg_id: &str,
    ) -> Result<CancelOutcome, ChatError> {
        let mut state = self.shared.state.lock();
        let running = state
            .queue
            .in_flight_msg(thread_id)?
            .is_some_and(|running| running == msg_id);
        if let Some(job) = state.queue.remove(thread_id, msg_id) {
            job.ticket.complete(Completed::Cancelled);
            state.note_cancelled(MessageRef {
                thread_id: thread_id.to_string(),
                msg_id: msg_id.to_string(),
            });
            self.shared.progress.notify_all();
            return Ok(CancelOutcome::RemovedQueued);
        }
        if running {
            if let Some(record) = find_in_flight(&state.in_flight, thread_id, msg_id) {
                record.cancel.cancel();
                return Ok(CancelOutcome::SignalledInFlight);
            }
        }
        Err(ChatError::UnknownMessage {
            thread_id: thread_id.to_string(),
            msg_id: msg_id.to_string(),
        })
    }

    /// Cancels every message of thread `thread_id` (CR-03). Each queued message is removed in
    /// queue order and its ticket completes `Cancelled`. A message a worker is running has its
    /// token set. Both kinds are reported. Returns `UnknownThread` when the thread is not open.
    pub(crate) fn cancel_thread(&self, thread_id: &str) -> Result<CancelReport, ChatError> {
        let mut state = self.shared.state.lock();
        let running = state.queue.in_flight_msg(thread_id)?.map(str::to_owned);
        let mut report = CancelReport::default();
        for (msg_id, job) in state.queue.drain_thread(thread_id) {
            job.ticket.complete(Completed::Cancelled);
            let msg_ref = MessageRef {
                thread_id: thread_id.to_string(),
                msg_id,
            };
            state.note_cancelled(msg_ref.clone());
            report.removed.push(msg_ref);
        }
        self.shared.progress.notify_all();
        if let Some(msg_id) = running {
            if let Some(record) = find_in_flight(&state.in_flight, thread_id, &msg_id) {
                record.cancel.cancel();
                report.signalled.push(record.msg_ref.clone());
            }
        }
        Ok(report)
    }

    /// Stops the runtime for its owner's drop. Nothing is reported. Closes intake, fails every
    /// queued message and every in-flight ticket with `ShuttingDown`, sets each running job's
    /// token, and wakes the workers. Waits up to `grace` for them to exit. Returns how many are
    /// still running. Those are detached and exit on their own.
    pub(crate) fn abort(&self, grace: Duration) -> usize {
        let shared = &self.shared;
        let mut state = shared.state.lock();
        state.closed = true;
        for (_thread_id, _msg_id, job) in state.queue.drain_queued() {
            job.ticket
                .complete(Completed::Failed(ChatError::ShuttingDown));
        }
        for record in std::mem::take(&mut state.in_flight) {
            record.cancel.cancel();
            record
                .ticket
                .complete(Completed::Failed(ChatError::ShuttingDown));
        }
        shared.work.notify_all();
        let deadline = Instant::now().checked_add(grace);
        while shared.live.load(Ordering::Acquire) > 0 {
            match deadline {
                Some(deadline) => {
                    if shared.progress.wait_until(&mut state, deadline).timed_out() {
                        break;
                    }
                }
                None => shared.progress.wait(&mut state),
            }
        }
        let running = shared.live.load(Ordering::Acquire);
        drop(state);
        self.reap_workers(running == 0);
        running
    }

    /// The count of workers that have not exited. Tests read it after the pool is dropped.
    #[cfg(test)]
    pub(crate) fn live_counter(&self) -> Arc<AtomicUsize> {
        Arc::clone(&self.shared.live)
    }
}

/// Counts a worker out when its loop returns, including by unwinding. The decrement runs under
/// the runtime lock, so [`Runtime::abort`] cannot miss the wake-up.
struct WorkerExit(Arc<Shared>);

impl Drop for WorkerExit {
    fn drop(&mut self) {
        let state = self.0.state.lock();
        self.0.live.fetch_sub(1, Ordering::AcqRel);
        drop(state);
        self.0.progress.notify_all();
    }
}

/// Body of one worker thread: take a job, run it, report it, repeat until closed and empty.
fn worker_loop(shared: Arc<Shared>) {
    let _exit = WorkerExit(Arc::clone(&shared));
    while let Some((msg_ref, ticket, cancel, msg)) = next_job(&shared) {
        let outcome = run_job(&shared, &msg, &cancel);
        finish(&shared, &msg_ref, &ticket, &cancel, outcome);
    }
}

/// Waits for a job and records it as in flight with a fresh cancel token. Returns `None` when
/// the runtime is closed and nothing is queued.
fn next_job(shared: &Shared) -> Option<(MessageRef, Ticket, CancelToken, ChatMessage)> {
    let mut state = shared.state.lock();
    loop {
        if let Some((thread_id, msg_id, job)) = state.queue.take_next() {
            let Job { msg, ticket } = job;
            let msg_ref = MessageRef { thread_id, msg_id };
            let cancel = CancelToken::default();
            state.in_flight.push(InFlight {
                msg_ref: msg_ref.clone(),
                ticket: ticket.clone(),
                cancel: cancel.clone(),
            });
            return Some((msg_ref, ticket, cancel, msg));
        }
        if state.closed {
            return None;
        }
        shared.work.wait(&mut state);
    }
}

/// Reserves budget, runs the executor with `cancel`, and releases budget. Every refusal and
/// fault becomes a typed failure; nothing is dropped silently (GA-7). A panic in the budget port
/// is caught too, so it cannot unwind the worker and strand its job (G5). A panic in the
/// executor is caught the same way, and is reported as `Panicked`.
fn run_job(shared: &Shared, msg: &ChatMessage, cancel: &CancelToken) -> Completed {
    let reservation = BudgetReservation {
        session_id: msg.session_id.clone(),
        thread_id: msg.thread_id.clone(),
        wall_time_ms: msg.hold_ms,
    };
    match catch_unwind(AssertUnwindSafe(|| shared.budget.reserve(&reservation))) {
        Ok(Ok(())) => {}
        Ok(Err(refusal)) => return Completed::Failed(refusal_error(refusal)),
        Err(payload) => return budget_panic(payload.as_ref()),
    }
    let completed = match catch_unwind(AssertUnwindSafe(|| (shared.executor)(msg, cancel))) {
        Ok(Ok(payload)) => Completed::Ok(payload),
        Ok(Err(message)) => Completed::Failed(ChatError::Panicked(message)),
        Err(payload) => Completed::Failed(ChatError::Panicked(panic_text(
            payload.as_ref(),
            EXECUTOR_PANIC_TEXT,
        ))),
    };
    match catch_unwind(AssertUnwindSafe(|| shared.budget.release(&reservation))) {
        Ok(Ok(())) => completed,
        // A failed release is a governor fault, and so is a panic in it. When the executor also
        // failed, its text is kept after the release text (see `release_failed`).
        Ok(Err(refusal)) => release_failed(refusal_text(refusal), completed),
        Err(payload) => release_failed(panic_text(payload.as_ref(), BUDGET_PANIC_TEXT), completed),
    }
}

/// Records a finished job: frees its thread, updates the shutdown report, and completes the
/// ticket. The thread is freed first, so a caller woken by the ticket sees it idle. A job whose
/// token is set completes `Cancelled` whatever the executor returned (GA-9). The token is read
/// under the runtime lock, where cancel sets it, so the decision is exact.
fn finish(
    shared: &Shared,
    msg_ref: &MessageRef,
    ticket: &Ticket,
    cancel: &CancelToken,
    outcome: Completed,
) {
    let mut state = shared.state.lock();
    state.queue.complete(&msg_ref.thread_id);
    if let Some(position) = state
        .in_flight
        .iter()
        .position(|record| &record.msg_ref == msg_ref)
    {
        state.in_flight.remove(position);
    }
    if let Some(watch) = state.watch.as_mut() {
        if let Some(position) = watch.pending.iter().position(|pending| pending == msg_ref) {
            let done = watch.pending.remove(position);
            watch.flushed.push(done);
        }
    }
    let outcome = if cancel.is_cancelled() {
        Completed::Cancelled
    } else {
        outcome
    };
    ticket.complete(outcome);
    shared.progress.notify_all();
}

/// The in-flight record of message `msg_id` on `thread_id`, when a worker is running it.
fn find_in_flight<'a>(
    records: &'a [InFlight],
    thread_id: &str,
    msg_id: &str,
) -> Option<&'a InFlight> {
    records
        .iter()
        .find(|record| record.msg_ref.thread_id == thread_id && record.msg_ref.msg_id == msg_id)
}

/// Maps a budget refusal to the typed failure its message completes with (GA-7).
fn refusal_error(refusal: BudgetRefusal) -> ChatError {
    match refusal {
        BudgetRefusal::Exceeded(message) => ChatError::BudgetDenied(message),
        BudgetRefusal::RateLimited(message) => ChatError::RateLimited(message),
        BudgetRefusal::Invalid(message) => ChatError::InvalidBudget(message),
        BudgetRefusal::Other(message) => ChatError::Governor(message),
    }
}

/// The text inside a budget refusal.
fn refusal_text(refusal: BudgetRefusal) -> String {
    match refusal {
        BudgetRefusal::Exceeded(message)
        | BudgetRefusal::RateLimited(message)
        | BudgetRefusal::Invalid(message)
        | BudgetRefusal::Other(message) => message,
    }
}

/// The message carried by a caught panic payload. A payload that is neither a `&str` nor a
/// `String` yields `fallback`, which names the component that panicked.
fn panic_text(payload: &(dyn Any + Send), fallback: &str) -> String {
    if let Some(text) = payload.downcast_ref::<&str>() {
        (*text).to_string()
    } else if let Some(text) = payload.downcast_ref::<String>() {
        text.clone()
    } else {
        fallback.to_string()
    }
}

/// The governor fault that a caught panic in the budget port completes its message with.
fn budget_panic(payload: &(dyn Any + Send)) -> Completed {
    Completed::Failed(ChatError::Governor(panic_text(payload, BUDGET_PANIC_TEXT)))
}

/// The governor fault that a failed release completes its message with. When the executor had
/// also failed, its text is kept after the release text, so neither failure is lost.
fn release_failed(release_error: String, completed: Completed) -> Completed {
    let text = match completed {
        Completed::Failed(ChatError::Panicked(executor_error)) => {
            format!("{release_error}; executor: {executor_error}")
        }
        _ => release_error,
    };
    Completed::Failed(ChatError::Governor(text))
}

#[cfg(test)]
pub(crate) mod tests {
    use std::error::Error;

    use gsd_proto::gsd::v1::SubagentType;

    use super::*;
    use crate::chat::ChatPool;

    /// How long a test waits on a ticket or a probe before it fails.
    pub(crate) const WAIT: Duration = Duration::from_secs(5);
    /// How long a gated executor sleeps between checks of its token.
    const GATE_SLICE: Duration = Duration::from_millis(10);

    /// Test gate. Executor starts and finishes are recorded in order. A message waits at the
    /// gate until the test opens it.
    pub(crate) struct Probe {
        state: Mutex<ProbeState>,
        changed: Condvar,
    }

    #[derive(Default)]
    struct ProbeState {
        open: bool,
        started: Vec<String>,
        finished: Vec<String>,
        /// The token each started message was given, by message id.
        tokens: Vec<(String, CancelToken)>,
        cancelled_at_closed_gate: Vec<String>,
    }

    impl Probe {
        pub(crate) fn new(open: bool) -> Arc<Self> {
            Arc::new(Self {
                state: Mutex::new(ProbeState {
                    open,
                    ..ProbeState::default()
                }),
                changed: Condvar::new(),
            })
        }

        pub(crate) fn open_gate(&self) {
            self.state.lock().open = true;
            self.changed.notify_all();
        }

        /// Blocks until `count` executor starts have been recorded.
        pub(crate) fn wait_started(&self, count: usize) {
            let deadline = Instant::now() + WAIT;
            let mut state = self.state.lock();
            while state.started.len() < count {
                let timed_out = self.changed.wait_until(&mut state, deadline).timed_out();
                assert!(
                    !timed_out || state.started.len() >= count,
                    "timed out waiting for {count} executor starts"
                );
            }
        }

        /// Blocks until `count` executor runs have finished.
        pub(crate) fn wait_finished(&self, count: usize) {
            let deadline = Instant::now() + WAIT;
            let mut state = self.state.lock();
            while state.finished.len() < count {
                let timed_out = self.changed.wait_until(&mut state, deadline).timed_out();
                assert!(
                    !timed_out || state.finished.len() >= count,
                    "timed out waiting for {count} finished runs"
                );
            }
        }

        pub(crate) fn started(&self) -> Vec<String> {
            self.state.lock().started.clone()
        }

        pub(crate) fn finished(&self) -> Vec<String> {
            self.state.lock().finished.clone()
        }

        /// The cancel token the executor was given for message `msg_id`, if it started.
        pub(crate) fn token(&self, msg_id: &str) -> Option<CancelToken> {
            self.state
                .lock()
                .tokens
                .iter()
                .find(|(id, _)| id == msg_id)
                .map(|(_, token)| token.clone())
        }

        /// Message ids whose executor saw its token while the gate was still closed.
        pub(crate) fn cancelled_at_closed_gate(&self) -> Vec<String> {
            self.state.lock().cancelled_at_closed_gate.clone()
        }

        /// The executor. A message id of `boom` panics; `refused` returns an error. Otherwise a
        /// message waits at the gate until it is opened or its token is set.
        pub(crate) fn executor(self: &Arc<Self>) -> Executor {
            let probe = Arc::clone(self);
            Arc::new(move |msg: &ChatMessage, cancel: &CancelToken| {
                {
                    let mut state = probe.state.lock();
                    state.started.push(msg.msg_id.clone());
                    state.tokens.push((msg.msg_id.clone(), cancel.clone()));
                    probe.changed.notify_all();
                    // The token is set outside this lock, so the gate wait polls it.
                    while !state.open && !cancel.is_cancelled() {
                        probe.changed.wait_for(&mut state, GATE_SLICE);
                    }
                    if !state.open {
                        state.cancelled_at_closed_gate.push(msg.msg_id.clone());
                    }
                }
                if msg.msg_id == "boom" {
                    panic!("boom");
                }
                if msg.msg_id == "refused" {
                    return Err("refused by executor".to_string());
                }
                {
                    let mut state = probe.state.lock();
                    state.finished.push(msg.msg_id.clone());
                    probe.changed.notify_all();
                }
                Ok(msg.payload.clone())
            })
        }
    }

    /// Budget port with a configurable refusal. It records every reservation and release.
    #[derive(Default)]
    pub(crate) struct ScriptedBudget {
        refusal: Mutex<Option<BudgetRefusal>>,
        reserved: Mutex<Vec<BudgetReservation>>,
        released: Mutex<Vec<BudgetReservation>>,
        /// When set, `release` refuses with this text as an `Other` refusal.
        release_refusal: Option<String>,
        /// When set, `reserve` panics with a non-string payload after it records the reservation.
        non_string_reserve_panic: bool,
    }

    impl BudgetPort for ScriptedBudget {
        fn reserve(&self, reservation: &BudgetReservation) -> Result<(), BudgetRefusal> {
            self.reserved.lock().push(reservation.clone());
            if self.non_string_reserve_panic {
                std::panic::panic_any(7_u32);
            }
            let refusal = self.refusal.lock().clone();
            match refusal {
                Some(refusal) => Err(refusal),
                None => Ok(()),
            }
        }

        fn release(&self, reservation: &BudgetReservation) -> Result<(), BudgetRefusal> {
            self.released.lock().push(reservation.clone());
            match &self.release_refusal {
                Some(text) => Err(BudgetRefusal::Other(text.clone())),
                None => Ok(()),
            }
        }
    }

    pub(crate) fn config(workers: usize, per_thread_queue: usize) -> ChatRuntimeConfig {
        ChatRuntimeConfig {
            workers,
            per_thread_queue,
            ..ChatRuntimeConfig::default()
        }
    }

    pub(crate) fn start(
        workers: usize,
        per_thread_queue: usize,
        budget: Arc<ScriptedBudget>,
        probe: &Arc<Probe>,
    ) -> Result<Runtime, ChatError> {
        Runtime::start(&config(workers, per_thread_queue), budget, probe.executor())
    }

    fn msg(thread: &str, id: &str) -> ChatMessage {
        ChatMessage::new(id, thread, "s1", id.as_bytes().to_vec())
    }

    pub(crate) fn open(runtime: &Runtime, thread: &str) {
        assert!(runtime.state().queue.open(thread).is_ok());
    }

    /// Submits and returns the ticket of an enqueued message.
    pub(crate) fn enqueue(runtime: &Runtime, thread: &str, id: &str) -> Result<Ticket, ChatError> {
        let submitted = runtime.submit(msg(thread, id))?;
        match submitted.ticket {
            Some(ticket) => Ok(ticket),
            None => Err(ChatError::UnknownMessage {
                thread_id: thread.to_string(),
                msg_id: id.to_string(),
            }),
        }
    }

    /// Waits for a ticket and returns its outcome, failing the test if it stays pending.
    pub(crate) fn settle(ticket: &Ticket) -> Completed {
        match ticket.wait(WAIT) {
            Some(completed) => completed,
            None => panic!("ticket still pending after {WAIT:?}"),
        }
    }

    fn position(list: &[String], id: &str) -> Option<usize> {
        list.iter().position(|entry| entry == id)
    }

    pub(crate) fn msg_ref(thread: &str, id: &str) -> MessageRef {
        MessageRef {
            thread_id: thread.to_string(),
            msg_id: id.to_string(),
        }
    }

    #[test]
    fn fairness_a_ready_thread_runs_before_a_flooded_threads_backlog() -> Result<(), Box<dyn Error>>
    {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        open(&runtime, "b");
        let mut tickets = Vec::new();
        for index in 1..=20 {
            tickets.push(enqueue(&runtime, "a", &format!("a{index}"))?);
        }
        // a1 is running at the closed gate before b1 is offered, so b1 queues behind it.
        probe.wait_started(1);
        let b1 = enqueue(&runtime, "b", "b1")?;
        probe.open_gate();
        assert_eq!(settle(&b1), Completed::Ok(b"b1".to_vec()));
        for ticket in &tickets {
            assert!(matches!(settle(ticket), Completed::Ok(_)));
        }
        let started = probe.started();
        let b_at = position(&started, "b1");
        let last_a_at = position(&started, "a20");
        assert!(matches!((b_at, last_a_at), (Some(b), Some(a)) if b < a));
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn per_thread_completion_order_is_enqueue_order() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(false);
        let runtime = start(4, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let mut tickets = Vec::new();
        for index in 1..=5 {
            tickets.push(enqueue(&runtime, "a", &format!("a{index}"))?);
        }
        probe.open_gate();
        for ticket in &tickets {
            assert!(matches!(settle(ticket), Completed::Ok(_)));
        }
        assert_eq!(probe.finished(), ["a1", "a2", "a3", "a4", "a5"]);
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn full_queue_refuses_with_queue_full_and_enqueues_nothing() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(false);
        let runtime = start(1, 2, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let first = enqueue(&runtime, "a", "a1")?;
        probe.wait_started(1);
        let second = enqueue(&runtime, "a", "a2")?;
        let third = enqueue(&runtime, "a", "a3")?;
        assert_eq!(
            runtime.submit(msg("a", "a4")).err(),
            Some(ChatError::QueueFull {
                thread_id: "a".to_string(),
                capacity: 2,
            })
        );
        assert_eq!(runtime.state().queue.queued_count(), 2);
        probe.open_gate();
        for ticket in [&first, &second, &third] {
            assert!(matches!(settle(ticket), Completed::Ok(_)));
        }
        assert!(!probe.started().iter().any(|id| id == "a4"));
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn resubmission_in_the_window_is_already_accepted_and_runs_once() -> Result<(), Box<dyn Error>>
    {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let first = runtime.submit(msg("a", "a1"))?;
        assert_eq!(first.outcome, SubmitOutcome::Enqueued { seq: 0 });
        let again = runtime.submit(msg("a", "a1"))?;
        assert_eq!(again.outcome, SubmitOutcome::AlreadyAccepted { seq: 0 });
        assert!(again.ticket.is_none());
        probe.open_gate();
        let Some(ticket) = first.ticket else {
            return Err("the first submit must return a ticket".into());
        };
        assert!(matches!(settle(&ticket), Completed::Ok(_)));
        runtime.shutdown(WAIT);
        assert_eq!(probe.finished(), ["a1"]);
        Ok(())
    }

    #[test]
    fn the_same_id_on_two_threads_is_two_messages() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(true);
        let runtime = start(2, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        open(&runtime, "b");
        let on_a = enqueue(&runtime, "a", "m1")?;
        let on_b = enqueue(&runtime, "b", "m1")?;
        assert!(matches!(settle(&on_a), Completed::Ok(_)));
        assert!(matches!(settle(&on_b), Completed::Ok(_)));
        assert_eq!(probe.started().iter().filter(|id| *id == "m1").count(), 2);
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn budget_refusals_complete_with_their_typed_failure_and_never_run(
    ) -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(true);
        let budget = Arc::new(ScriptedBudget::default());
        let runtime = start(1, 64, Arc::clone(&budget), &probe)?;
        open(&runtime, "a");
        let cases = [
            (
                BudgetRefusal::Exceeded("over".to_string()),
                ChatError::BudgetDenied("over".to_string()),
            ),
            (
                BudgetRefusal::RateLimited("slow".to_string()),
                ChatError::RateLimited("slow".to_string()),
            ),
            (
                BudgetRefusal::Invalid("bad".to_string()),
                ChatError::InvalidBudget("bad".to_string()),
            ),
            (
                BudgetRefusal::Other("x".to_string()),
                ChatError::Governor("x".to_string()),
            ),
        ];
        for (index, (refusal, expected)) in cases.into_iter().enumerate() {
            *budget.refusal.lock() = Some(refusal);
            let id = format!("r{index}");
            let ticket = enqueue(&runtime, "a", &id)?;
            assert_eq!(settle(&ticket), Completed::Failed(expected));
        }
        *budget.refusal.lock() = None;
        assert!(!probe.started().iter().any(|id| id.starts_with('r')));
        assert_eq!(budget.reserved.lock().len(), 4);
        assert!(budget.released.lock().is_empty());
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn executor_panic_completes_as_panicked_and_the_worker_serves_on() -> Result<(), Box<dyn Error>>
    {
        let probe = Probe::new(true);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let boom = enqueue(&runtime, "a", "boom")?;
        let after = enqueue(&runtime, "a", "ok1")?;
        match settle(&boom) {
            Completed::Failed(ChatError::Panicked(message)) => assert!(message.contains("boom")),
            other => return Err(format!("expected Panicked, got {other:?}").into()),
        }
        assert_eq!(settle(&after), Completed::Ok(b"ok1".to_vec()));
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn executor_error_completes_as_panicked_with_its_text() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(true);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let refused = enqueue(&runtime, "a", "refused")?;
        assert_eq!(
            settle(&refused),
            Completed::Failed(ChatError::Panicked("refused by executor".to_string()))
        );
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn busy_thread_refuses_close_and_park_and_stays_open() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(false);
        let pool = ChatPool::assemble(
            config(1, 64),
            Arc::new(ScriptedBudget::default()),
            probe.executor(),
        )?;
        let kind = SubagentType {
            value: "w".to_string(),
        };
        pool.open_thread("a", kind.clone())?;
        pool.open_thread("b", kind)?;
        let a1 = pool.submit(msg("a", "a1"))?;
        probe.wait_started(1);
        let a2 = pool.submit(msg("a", "a2"))?;
        let b1 = pool.submit(msg("b", "b1"))?;
        // `a` has one job in flight and one queued. `b` has one queued and none running.
        assert_eq!(
            pool.close_thread("a"),
            Err(ChatError::ThreadBusy("a".to_string()))
        );
        assert_eq!(
            pool.park("a", Instant::now()),
            Err(ChatError::ThreadBusy("a".to_string()))
        );
        assert_eq!(
            pool.close_thread("b"),
            Err(ChatError::ThreadBusy("b".to_string()))
        );
        // The refused calls left `a` open: it still takes work.
        assert!(pool.submit(msg("a", "a3")).is_ok());
        probe.open_gate();
        for submitted in [a1, a2, b1] {
            if let Some(ticket) = submitted.ticket {
                assert!(matches!(settle(&ticket), Completed::Ok(_)));
            }
        }
        pool.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn shutdown_past_the_deadline_abandons_running_and_drops_queued() -> Result<(), Box<dyn Error>>
    {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let a1 = enqueue(&runtime, "a", "a1")?;
        probe.wait_started(1);
        let a2 = enqueue(&runtime, "a", "a2")?;
        let a3 = enqueue(&runtime, "a", "a3")?;
        let report = runtime.shutdown(Duration::from_millis(50));
        assert_eq!(report.abandoned, [msg_ref("a", "a1")]);
        assert_eq!(report.dropped, [msg_ref("a", "a2"), msg_ref("a", "a3")]);
        assert!(report.flushed.is_empty());
        for ticket in [&a1, &a2, &a3] {
            assert_eq!(settle(ticket), Completed::Failed(ChatError::ShuttingDown));
        }
        // The abandoned worker still finishes its run. Its late result must not change a1.
        probe.open_gate();
        probe.wait_finished(1);
        assert_eq!(settle(&a1), Completed::Failed(ChatError::ShuttingDown));
        Ok(())
    }

    #[test]
    fn shutdown_with_the_gate_open_flushes_every_message_in_the_set() -> Result<(), Box<dyn Error>>
    {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let a1 = enqueue(&runtime, "a", "a1")?;
        probe.wait_started(1);
        let a2 = enqueue(&runtime, "a", "a2")?;
        let a3 = enqueue(&runtime, "a", "a3")?;
        // The report set is {a1 in flight, a2, a3 queued}. The gate stays closed until the call
        // has started, so nothing in the set can finish before the call.
        let report = std::thread::scope(|scope| {
            let shutdown = scope.spawn(|| runtime.shutdown(WAIT));
            let deadline = Instant::now() + WAIT;
            while !runtime.state().closed {
                assert!(Instant::now() < deadline, "shutdown did not start");
                std::thread::yield_now();
            }
            probe.open_gate();
            match shutdown.join() {
                Ok(report) => report,
                Err(_) => panic!("shutdown thread panicked"),
            }
        });
        assert_eq!(
            report.flushed,
            [msg_ref("a", "a1"), msg_ref("a", "a2"), msg_ref("a", "a3")]
        );
        assert!(report.dropped.is_empty());
        assert!(report.abandoned.is_empty());
        for ticket in [&a1, &a2, &a3] {
            assert!(matches!(settle(ticket), Completed::Ok(_)));
        }
        assert!(matches!(
            runtime.submit(msg("a", "a4")),
            Err(ChatError::ShuttingDown)
        ));
        Ok(())
    }

    #[test]
    fn parked_and_closed_threads_have_no_queue_and_a_second_open_changes_nothing(
    ) -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(true);
        let pool = ChatPool::assemble(
            config(1, 64),
            Arc::new(ScriptedBudget::default()),
            probe.executor(),
        )?;
        let kind = SubagentType {
            value: "w".to_string(),
        };
        pool.open_thread("a", kind.clone())?;
        pool.open_thread("c", kind.clone())?;
        let ticket = match pool.submit(msg("a", "a1"))?.ticket {
            Some(ticket) => ticket,
            None => return Err("a1 must be enqueued".into()),
        };
        assert!(matches!(settle(&ticket), Completed::Ok(_)));

        // `a` is idle, so it parks. Its id then has no queue.
        assert_eq!(
            pool.park("a", Instant::now())?,
            crate::chat::Placement::Parked
        );
        assert_eq!(
            pool.submit(msg("a", "a2")).err(),
            Some(ChatError::UnknownThread("a".to_string()))
        );
        // `c` closes while idle, and then it has no queue either.
        pool.close_thread("c")?;
        assert_eq!(
            pool.submit(msg("c", "c1")).err(),
            Some(ChatError::UnknownThread("c".to_string()))
        );

        // A second open of a live thread is refused, and the queue keeps its dedupe window.
        pool.open_thread("d", kind.clone())?;
        let d1 = match pool.submit(msg("d", "d1"))?.ticket {
            Some(ticket) => ticket,
            None => return Err("d1 must be enqueued".into()),
        };
        assert!(matches!(settle(&d1), Completed::Ok(_)));
        assert_eq!(
            pool.open_thread("d", kind),
            Err(ChatError::ThreadExists("d".to_string()))
        );
        assert_eq!(
            pool.submit(msg("d", "d1"))?.outcome,
            SubmitOutcome::AlreadyAccepted { seq: 0 }
        );
        pool.shutdown(WAIT);
        Ok(())
    }

    /// Budget port whose `reserve` or `release` panics once, when armed.
    #[derive(Default)]
    struct PanickingBudget {
        panic_on_reserve: Mutex<bool>,
        panic_on_release: Mutex<bool>,
    }

    impl BudgetPort for PanickingBudget {
        fn reserve(&self, _reservation: &BudgetReservation) -> Result<(), BudgetRefusal> {
            if std::mem::take(&mut *self.panic_on_reserve.lock()) {
                panic!("reserve blew up");
            }
            Ok(())
        }

        fn release(&self, _reservation: &BudgetReservation) -> Result<(), BudgetRefusal> {
            if std::mem::take(&mut *self.panic_on_release.lock()) {
                panic!("release blew up");
            }
            Ok(())
        }
    }

    #[test]
    fn queued_cancel_during_shutdown_ends_the_wait_as_cancelled() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let a1 = enqueue(&runtime, "a", "a1")?;
        probe.wait_started(1);
        let a2 = enqueue(&runtime, "a", "a2")?;
        // The shutdown holds for a1, which is in flight at the closed gate. The queued a2 is
        // cancelled during that wait. Opening the gate then ends the wait well before the grace.
        let (waited, report) = std::thread::scope(|scope| {
            let shutdown = scope.spawn(|| {
                let started = Instant::now();
                let report = runtime.shutdown(WAIT);
                (started.elapsed(), report)
            });
            let deadline = Instant::now() + WAIT;
            while !runtime.state().closed {
                assert!(Instant::now() < deadline, "shutdown did not start");
                std::thread::yield_now();
            }
            assert_eq!(
                runtime.cancel_message("a", "a2"),
                Ok(CancelOutcome::RemovedQueued)
            );
            probe.open_gate();
            match shutdown.join() {
                Ok(outcome) => outcome,
                Err(_) => panic!("shutdown thread panicked"),
            }
        });
        assert!(waited < WAIT, "shutdown held for {waited:?}");
        assert_eq!(report.cancelled, [msg_ref("a", "a2")]);
        assert_eq!(report.flushed, [msg_ref("a", "a1")]);
        assert!(report.dropped.is_empty());
        assert!(report.abandoned.is_empty());
        assert_eq!(settle(&a1), Completed::Ok(b"a1".to_vec()));
        assert_eq!(settle(&a2), Completed::Cancelled);
        assert!(!probe.started().iter().any(|id| id == "a2"));
        Ok(())
    }

    #[test]
    fn thread_cancel_during_shutdown_lists_queued_as_cancelled() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(false);
        let runtime = start(1, 64, Arc::new(ScriptedBudget::default()), &probe)?;
        open(&runtime, "a");
        let a1 = enqueue(&runtime, "a", "a1")?;
        probe.wait_started(1);
        let a2 = enqueue(&runtime, "a", "a2")?;
        let a3 = enqueue(&runtime, "a", "a3")?;
        // The thread cancel sets a1's token too, so a1 finishes at once and the wait can end.
        let (waited, report, cancel) = std::thread::scope(|scope| {
            let shutdown = scope.spawn(|| {
                let started = Instant::now();
                let report = runtime.shutdown(WAIT);
                (started.elapsed(), report)
            });
            let deadline = Instant::now() + WAIT;
            while !runtime.state().closed {
                assert!(Instant::now() < deadline, "shutdown did not start");
                std::thread::yield_now();
            }
            let cancel = runtime.cancel_thread("a");
            match shutdown.join() {
                Ok((waited, report)) => (waited, report, cancel),
                Err(_) => panic!("shutdown thread panicked"),
            }
        });
        let cancel = cancel?;
        assert!(waited < WAIT, "shutdown held for {waited:?}");
        assert_eq!(cancel.removed, [msg_ref("a", "a2"), msg_ref("a", "a3")]);
        assert_eq!(cancel.signalled, [msg_ref("a", "a1")]);
        assert_eq!(report.cancelled, [msg_ref("a", "a2"), msg_ref("a", "a3")]);
        assert_eq!(report.flushed, [msg_ref("a", "a1")]);
        assert!(report.dropped.is_empty());
        assert!(report.abandoned.is_empty());
        for ticket in [&a1, &a2, &a3] {
            assert_eq!(settle(ticket), Completed::Cancelled);
        }
        Ok(())
    }

    #[test]
    fn panicking_reserve_fails_one_message_and_serves_on() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(true);
        let budget = Arc::new(PanickingBudget {
            panic_on_reserve: Mutex::new(true),
            panic_on_release: Mutex::new(false),
        });
        let runtime = Runtime::start(&config(1, 64), budget, probe.executor())?;
        open(&runtime, "a");
        let p1 = enqueue(&runtime, "a", "p1")?;
        let ok1 = enqueue(&runtime, "a", "ok1")?;
        match settle(&p1) {
            Completed::Failed(ChatError::Governor(message)) => {
                assert!(message.contains("reserve blew up"));
            }
            other => return Err(format!("expected Governor, got {other:?}").into()),
        }
        assert_eq!(settle(&ok1), Completed::Ok(b"ok1".to_vec()));
        // The reserve panic never reached the executor, and the job left no in-flight record.
        assert_eq!(probe.started(), ["ok1"]);
        assert!(runtime.state().in_flight.is_empty());
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn panicking_release_fails_one_message_and_serves_on() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(true);
        let budget = Arc::new(PanickingBudget {
            panic_on_reserve: Mutex::new(false),
            panic_on_release: Mutex::new(true),
        });
        let runtime = Runtime::start(&config(1, 64), budget, probe.executor())?;
        open(&runtime, "a");
        let r1 = enqueue(&runtime, "a", "r1")?;
        let ok1 = enqueue(&runtime, "a", "ok1")?;
        match settle(&r1) {
            Completed::Failed(ChatError::Governor(message)) => {
                assert!(message.contains("release blew up"));
            }
            other => return Err(format!("expected Governor, got {other:?}").into()),
        }
        assert_eq!(settle(&ok1), Completed::Ok(b"ok1".to_vec()));
        assert!(runtime.state().in_flight.is_empty());
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn non_string_budget_panic_is_attributed_to_the_budget_port() -> Result<(), Box<dyn Error>> {
        let probe = Probe::new(true);
        let budget = Arc::new(ScriptedBudget {
            non_string_reserve_panic: true,
            ..ScriptedBudget::default()
        });
        let runtime = start(1, 64, budget, &probe)?;
        open(&runtime, "a");
        let p1 = enqueue(&runtime, "a", "p1")?;
        assert_eq!(
            settle(&p1),
            Completed::Failed(ChatError::Governor("budget port panicked".to_string()))
        );
        // The reserve panic happened before the executor, so the executor never started.
        assert!(probe.started().is_empty());
        runtime.shutdown(WAIT);
        Ok(())
    }

    #[test]
    fn failed_release_keeps_executor_failure_text() -> Result<(), Box<dyn Error>> {
        let budget = Arc::new(ScriptedBudget {
            release_refusal: Some("release refused".to_string()),
            ..ScriptedBudget::default()
        });
        let executor: Executor = Arc::new(
            |_msg: &ChatMessage, _cancel: &CancelToken| -> Result<Vec<u8>, String> {
                panic!("executor boom")
            },
        );
        let runtime = Runtime::start(&config(1, 64), budget, executor)?;
        open(&runtime, "a");
        let failed = enqueue(&runtime, "a", "e1")?;
        let Completed::Failed(error) = settle(&failed) else {
            return Err("expected a failed completion".into());
        };
        let text = error.to_string();
        assert!(text.contains("release refused"), "{text}");
        assert!(text.contains("executor boom"), "{text}");
        runtime.shutdown(WAIT);
        Ok(())
    }
}
