//! Chat-thread facade: the public entry point to the chat-thread pool.
//!
//! The pool machinery (`ThreadPool`, the warm pool and the handle registry) is crate-private.
//! `ChatPool` is the only way in, and the types here are
//! the only ones a consumer sees. Budget enforcement is supplied by the consumer through
//! [`BudgetPort`]; `gsd-pool` never implements it and never depends on `gsd-governor` (DEC-1).
//!
//! Lock order (DD-12): every operation that touches the handle registry, the warm pool or the
//! thread map takes the lifecycle lock first. No path takes the two in reverse.

use std::sync::Arc;
use std::time::{Duration, Instant};

use gsd_proto::gsd::v1::{ChatThreadId, SubagentType};
use parking_lot::Mutex;

use crate::cancel::{CancelOutcome, CancelReport};
use crate::chat_runtime::{
    builtin_executor, ChatMessage, Executor, Runtime, ShutdownReport, Submitted,
};
use crate::handle_registry::{ChatThreadHandle, HandleRegistry};
use crate::{PoolConfig, PoolError, ThreadPool};

/// Largest accepted [`ChatRuntimeConfig::workers`]. Each worker is an operating-system thread,
/// and the default is 4, so the bound stops a mistyped count from exhausting the process's
/// thread limit.
pub const MAX_WORKERS: usize = 256;

/// Largest accepted thread, message or session id, in bytes. `gsd-governor` caps its ids at the same size: keep them equal.
pub const MAX_ID_BYTES: usize = 256;
/// Largest accepted message payload, in bytes: 1 MiB.
pub const MAX_PAYLOAD_BYTES: usize = 1024 * 1024;

/// Refuses an id over [`MAX_ID_BYTES`] with [`ChatError::TooLarge`] (E-INPUT).
fn check_id(what: &str, id: &str) -> Result<(), ChatError> {
    if id.len() > MAX_ID_BYTES {
        return Err(ChatError::TooLarge(format!(
            "{what} is {} bytes; the limit is {MAX_ID_BYTES}",
            id.len()
        )));
    }
    Ok(())
}

/// Refuses a message whose ids or payload are over their byte caps, before anything is queued.
fn check_message(msg: &ChatMessage) -> Result<(), ChatError> {
    check_id("message id", &msg.msg_id)?;
    check_id("thread id", &msg.thread_id)?;
    check_id("session id", &msg.session_id)?;
    if msg.payload.len() > MAX_PAYLOAD_BYTES {
        return Err(ChatError::TooLarge(format!(
            "payload is {} bytes; the limit is {MAX_PAYLOAD_BYTES}",
            msg.payload.len()
        )));
    }
    Ok(())
}

/// Configuration for [`ChatPool::new`]. Every field must be greater than zero, and `workers`
/// must not exceed [`MAX_WORKERS`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChatRuntimeConfig {
    /// Number of worker threads, at most [`MAX_WORKERS`].
    pub workers: usize,
    /// Capacity of each thread's message queue.
    pub per_thread_queue: usize,
    /// Number of recent message ids remembered per thread, each recorded when it is accepted.
    /// A queued or in-flight id is refused as already accepted whatever the window holds, and the
    /// window covers the ids that are no longer live. A closed or parked thread forgets its
    /// window. `validate` refuses `dedupe_window <= per_thread_queue` (a DD-16 config rule).
    pub dedupe_window: usize,
    /// Maximum number of live chat threads. Parked threads do not count.
    pub max_threads: usize,
    /// Maximum number of parked threads kept warm for reuse.
    pub warm_pool_size: usize,
    /// Milliseconds a dropped pool waits for its workers to exit before it returns. Workers
    /// still running then are detached, and a warning records how many. Default 2000.
    pub drop_grace_ms: u64,
}

impl Default for ChatRuntimeConfig {
    fn default() -> Self {
        let pool = PoolConfig::default();
        Self {
            workers: 4,
            per_thread_queue: 64,
            dedupe_window: 256,
            max_threads: pool.max_threads,
            warm_pool_size: pool.warm_pool_size,
            drop_grace_ms: 2000,
        }
    }
}

impl ChatRuntimeConfig {
    /// Refuses a config with any zero field, naming the first such field; a `workers` count
    /// above [`MAX_WORKERS`], naming the bound; and a `dedupe_window` not greater than
    /// `per_thread_queue`.
    pub(crate) fn validate(&self) -> Result<(), ChatError> {
        let zero = [
            ("workers", self.workers == 0),
            ("per_thread_queue", self.per_thread_queue == 0),
            ("dedupe_window", self.dedupe_window == 0),
            ("max_threads", self.max_threads == 0),
            ("warm_pool_size", self.warm_pool_size == 0),
            ("drop_grace_ms", self.drop_grace_ms == 0),
        ];
        match zero.iter().find(|(_, is_zero)| *is_zero) {
            Some((name, _)) => Err(ChatError::InvalidConfig(format!(
                "{name} must be greater than zero"
            ))),
            None if self.workers > MAX_WORKERS => Err(ChatError::InvalidConfig(format!(
                "workers must be at most {MAX_WORKERS}"
            ))),
            None if self.dedupe_window <= self.per_thread_queue => Err(ChatError::InvalidConfig(
                "dedupe_window must be greater than per_thread_queue".to_string(),
            )),
            None => Ok(()),
        }
    }
}

/// Typed error from the chat facade.
///
/// Each variant maps to one wire code in the error-code table (`E-INPUT`, `E-TIMEOUT`,
/// `E-CAPACITY`, `E-INTERNAL`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ChatError {
    /// No live chat thread has this id. `E-INPUT`.
    #[error("unknown chat thread: {0}")]
    UnknownThread(String),
    /// A live chat thread already holds this id. `E-INPUT`.
    #[error("chat thread already exists: {0}")]
    ThreadExists(String),
    /// The thread has queued or in-flight work, so it cannot be closed or parked. `E-CAPACITY`.
    #[error("chat thread is busy: {0}")]
    ThreadBusy(String),
    /// The thread's queue is at capacity; the message was not accepted. `E-CAPACITY`.
    #[error("queue for chat thread {thread_id} is full (capacity {capacity})")]
    QueueFull {
        /// Thread whose queue is full.
        thread_id: String,
        /// Queue capacity.
        capacity: usize,
    },
    /// No message with this id is known on this thread. `E-INPUT`.
    #[error("unknown message {msg_id} on chat thread {thread_id}")]
    UnknownMessage {
        /// Thread the message was looked up on.
        thread_id: String,
        /// Message id that is not known.
        msg_id: String,
    },
    /// The budget port refused the reservation as exceeded. `E-TIMEOUT`.
    #[error("budget denied: {0}")]
    BudgetDenied(String),
    /// The budget port refused the reservation as rate limited. `E-CAPACITY`.
    #[error("rate limited: {0}")]
    RateLimited(String),
    /// An id or payload is over its byte cap. `E-INPUT`.
    #[error("input too large: {0}")]
    TooLarge(String),
    /// The budget port refused the reservation because a quota is spent. `E-CAPACITY`.
    #[error("quota exceeded: {0}")]
    QuotaExceeded(String),
    /// The budget port refused the reservation as unauthorized. `E-POLICY`.
    #[error("unauthorized: {0}")]
    Unauthorized(String),
    /// The budget port reported the reservation amounts as invalid. `E-INPUT`.
    #[error("invalid budget: {0}")]
    InvalidBudget(String),
    /// The pool is shutting down and accepts no new work. `E-CAPACITY`.
    #[error("chat pool is shutting down")]
    ShuttingDown,
    /// An executor panicked or failed. `E-INTERNAL`.
    #[error("chat executor failed: {0}")]
    Panicked(String),
    /// The budget port failed for a reason other than a refusal. `E-INTERNAL`.
    #[error("governor fault: {0}")]
    Governor(String),
    /// The runtime config is invalid. `E-INPUT`.
    #[error("invalid chat runtime config: {0}")]
    InvalidConfig(String),
    /// Admission refused: `max_threads` live threads already exist. `E-CAPACITY`.
    #[error("chat thread capacity reached: max_threads is {max_threads}")]
    Capacity {
        /// The configured live-thread cap.
        max_threads: usize,
    },
    /// A chat worker thread could not be started. `E-INTERNAL`.
    #[error("could not start a chat worker thread: {0}")]
    WorkerSpawn(String),
}

/// Outcome of [`ChatPool::release`] and [`ChatPool::park`].
///
/// This is the chat facade's placement type. The teardown report in `gsd-state` has its own
/// `Placement` enum; the two are different types in different crates.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Placement {
    /// The thread was parked in the warm pool for reuse.
    Parked,
    /// The warm pool was full, so the thread was closed and removed.
    Closed,
}

/// The amounts one thread reserves from the budget port.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BudgetReservation {
    /// Session the reservation belongs to.
    pub session_id: String,
    /// Thread the reservation belongs to.
    pub thread_id: String,
    /// Wall-clock milliseconds reserved.
    pub wall_time_ms: u64,
}

/// Why a [`BudgetPort`] refused a reservation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BudgetRefusal {
    /// A budget dimension would be exceeded.
    Exceeded(String),
    /// The amounts or ids are invalid.
    Invalid(String),
    /// The request rate limit refused the reservation.
    RateLimited(String),
    /// A quota is spent; the refusal is a capacity limit, not a budget overrun.
    Quota(String),
    /// The reservation is not authorized.
    Unauthorized(String),
    /// Any other refusal or fault.
    Other(String),
}

/// Budget enforcement supplied by the consumer.
///
/// `gsd-pool` defines this trait and never implements it (DEC-1). The consumer that holds the
/// governor provides the adapter, so `gsd-pool` has no edge to `gsd-governor`.
pub trait BudgetPort: Send + Sync {
    /// Reserves the amounts in `reservation`, or refuses with the reason.
    fn reserve(&self, reservation: &BudgetReservation) -> Result<(), BudgetRefusal>;
    /// Returns the amounts in `reservation` to the budget.
    fn release(&self, reservation: &BudgetReservation) -> Result<(), BudgetRefusal>;
}

/// The chat-thread pool facade.
///
/// Threads are identified by string ids. A caller may supply the id through
/// [`ChatPool::open_thread`] or let [`ChatPool::acquire`] generate one. A thread that has been
/// parked is not live: its id is free again and may be reopened. A live thread has an open
/// message queue. A parked or closed thread has none, so [`ChatPool::submit`] refuses it.
pub struct ChatPool {
    pool: ThreadPool,
    registry: HandleRegistry,
    config: ChatRuntimeConfig,
    /// Worker threads, the per-thread queues and the budget reservations they make.
    runtime: Runtime,
    /// Guards every multi-step operation over the registry, the warm pool, the thread map and
    /// the queues.
    lifecycle: Mutex<()>,
}

impl ChatPool {
    /// Creates a pool from a validated config and a budget port, and starts `config.workers`
    /// worker threads.
    ///
    /// Returns [`ChatError::InvalidConfig`] naming the first field that is zero, and
    /// [`ChatError::WorkerSpawn`] when the operating system refuses a worker thread.
    pub fn new(config: ChatRuntimeConfig, budget: Arc<dyn BudgetPort>) -> Result<Self, ChatError> {
        Self::assemble(config, budget, builtin_executor())
    }

    /// Builds the pool around `executor`. [`ChatPool::new`] passes the built-in executor.
    pub(crate) fn assemble(
        config: ChatRuntimeConfig,
        budget: Arc<dyn BudgetPort>,
        executor: Executor,
    ) -> Result<Self, ChatError> {
        config.validate()?;
        let pool_config = PoolConfig {
            max_threads: config.max_threads,
            warm_pool_size: config.warm_pool_size,
            ..PoolConfig::default()
        };
        let registry = HandleRegistry::new(config.max_threads);
        let runtime = Runtime::start(&config, budget, executor)?;
        Ok(Self {
            pool: ThreadPool::new(&pool_config),
            registry,
            config,
            runtime,
            lifecycle: Mutex::new(()),
        })
    }

    /// Opens a live thread with the caller's `id` and the given subagent type. Its message queue
    /// opens with it.
    ///
    /// Returns [`ChatError::ShuttingDown`] after [`ChatPool::shutdown`]. That check comes first, so
    /// a closed pool refuses even a live id, and even when every live slot is taken. Returns
    /// [`ChatError::ThreadExists`] while a live thread holds `id`, even when the cap is also full.
    /// Returns [`ChatError::Capacity`] when `max_threads` live threads already exist.
    ///
    /// A parked id is not live, so it opens as a fresh thread with a new handle. Any warm entry
    /// for that id is discarded before the new thread is registered and created, so its
    /// termination is reported first. A refused call changes nothing.
    pub fn open_thread(&self, id: &str, subagent_type: SubagentType) -> Result<(), ChatError> {
        check_id("thread id", id)?;
        let _lifecycle = self.lifecycle.lock();
        // Shutdown is checked before the live-id lookup, so a closed pool changes nothing.
        if self.runtime.state().is_closed() {
            return Err(ChatError::ShuttingDown);
        }
        if self.registry.get(id).is_some() {
            return Err(ChatError::ThreadExists(id.to_string()));
        }
        // The queue opens first. Its open observes shutdown, so a refusal here takes no slot.
        self.open_queue(id)?;
        if let Err(err) = self.registry.reserve() {
            self.close_queue(id);
            return Err(self.admission_error(err, id));
        }
        let thread_id = ChatThreadId {
            value: id.to_string(),
        };
        if let Err(err) = self
            .registry
            .register_reserved(thread_id, subagent_type.clone())
        {
            // Cannot happen: the lifecycle lock is held and `get` found no handle for `id`.
            self.close_queue(id);
            return Err(self.admission_error(err, id));
        }
        if let Err(err) = self.pool.open_with_id(id, subagent_type) {
            // Cannot happen for the same reason: the live thread map holds only registered ids.
            self.close_queue(id);
            let _released = self.registry.release(id);
            return Err(self.admission_error(err, id));
        }
        Ok(())
    }

    /// Closes live thread `id`, removing it from the registry, the thread map and the queue.
    ///
    /// Returns [`ChatError::UnknownThread`] when no live thread holds `id`, and
    /// [`ChatError::ThreadBusy`] while the thread has queued or in-flight work. Nothing changes
    /// on either refusal.
    pub fn close_thread(&self, id: &str) -> Result<(), ChatError> {
        let _lifecycle = self.lifecycle.lock();
        self.runtime.state().queue.close(id)?;
        self.registry
            .release(id)
            .map_err(|_| ChatError::UnknownThread(id.to_string()))?;
        self.pool.forget(id);
        Ok(())
    }

    /// Takes a thread for `subagent_type`, reusing a warm one when available, and returns its id.
    ///
    /// A new thread gets a generated id. A call after shutdown changes nothing. Admission is
    /// checked first, so a call refused for capacity takes no warm entry. A call refused after a
    /// warm entry was popped, by a registry admission failure or by a shutdown that began
    /// meanwhile, discards that parked thread and returns its slot; the entry is not restored.
    /// Returns [`ChatError::Capacity`] when `max_threads` live threads already exist, and
    /// [`ChatError::ShuttingDown`] after [`ChatPool::shutdown`].
    pub async fn acquire(&self, subagent_type: SubagentType) -> Result<String, ChatError> {
        let _lifecycle = self.lifecycle.lock();
        // A call after shutdown takes no slot and pops no warm entry.
        if self.runtime.state().is_closed() {
            return Err(ChatError::ShuttingDown);
        }
        self.registry.reserve().map_err(|_| self.capacity_error())?;
        if let Some(handle) = self
            .pool
            .pop_warm(&subagent_type, gsd_runtime::clock::now())
        {
            let id = handle.id().value.clone();
            if let Err(err) = self
                .registry
                .register_reserved(handle.id().clone(), handle.subagent_type().clone())
            {
                // The registry returned the reserved slot. The popped thread is discarded.
                return Err(self.admission_error(err, &id));
            }
            if let Err(err) = self.open_queue(&id) {
                // Refused during shutdown. Free the slot and discard the popped parked thread.
                let _released = self.registry.release(&id);
                return Err(err);
            }
            self.pool.reopen_parked(&handle);
            return Ok(id);
        }
        let id = self.pool.create_thread(subagent_type.clone());
        if let Err(err) = self.registry.register_reserved(id.clone(), subagent_type) {
            // The created thread was never admitted, so drop it.
            self.pool.forget(&id.value);
            return Err(self.admission_error(err, &id.value));
        }
        if let Err(err) = self.open_queue(&id.value) {
            // Refused during shutdown. Undo the admission.
            let _released = self.registry.release(&id.value);
            self.pool.forget(&id.value);
            return Err(err);
        }
        Ok(id.value)
    }

    /// Releases thread `thread_id` back to the pool at the current clock.
    ///
    /// Equivalent to [`ChatPool::park`] with the clock read once here. The thread id names the
    /// thread: the handle itself is not exposed. Returns [`ChatError::UnknownThread`] for an id
    /// that is not live, and [`ChatError::ThreadBusy`] for one with queued or in-flight work.
    pub async fn release(&self, thread_id: &str) -> Result<Placement, ChatError> {
        self.park(thread_id, gsd_runtime::clock::now())
    }

    /// Parks live thread `thread_id` in the warm pool, aging it from `now`.
    ///
    /// The thread id names the thread, not a handle, because the handle type is crate-private.
    /// The thread leaves the live set in both outcomes, so its id is free afterwards, and its
    /// queue is closed. [`Placement::Parked`] keeps it warm for reuse. [`Placement::Closed`]
    /// means the warm pool was full: the thread was closed.
    ///
    /// Returns [`ChatError::ShuttingDown`] after [`ChatPool::shutdown`],
    /// [`ChatError::UnknownThread`] for an id that is not live, and
    /// [`ChatError::ThreadBusy`] while the thread has queued or in-flight work. Nothing changes
    /// on any refusal.
    pub fn park(&self, thread_id: &str, now: Instant) -> Result<Placement, ChatError> {
        let _lifecycle = self.lifecycle.lock();
        {
            // The closed check, the busy check and the close share one runtime lock, so no submit
            // can enqueue between them and no thread parks after shutdown.
            let mut state = self.runtime.state();
            if state.is_closed() {
                return Err(ChatError::ShuttingDown);
            }
            if state.queue.is_busy(thread_id)? {
                return Err(ChatError::ThreadBusy(thread_id.to_string()));
            }
            state.queue.close(thread_id)?;
        }
        let handle = self
            .registry
            .release(thread_id)
            .map_err(|_| ChatError::UnknownThread(thread_id.to_string()))?;
        Ok(self.pool.park_handle(handle, now))
    }

    /// Parks `id` as a thread of `subagent_type` in one step. The thread is never live, so
    /// a caller can never submit to it, and it holds no live slot: a parked thread does not
    /// count against `max_threads`, so this succeeds even when every live slot is taken.
    /// Teardown uses it to hand a retiring thread to the warm pool. Returns
    /// [`ChatError::ShuttingDown`] after [`ChatPool::shutdown`] and
    /// [`ChatError::ThreadExists`] while a live thread holds `id`. A refused call changes
    /// nothing.
    pub fn park_fresh(
        &self,
        id: &str,
        subagent_type: SubagentType,
        now: Instant,
    ) -> Result<Placement, ChatError> {
        let _lifecycle = self.lifecycle.lock();
        if self.runtime.state().is_closed() {
            return Err(ChatError::ShuttingDown);
        }
        if self.registry.get(id).is_some() {
            return Err(ChatError::ThreadExists(id.to_string()));
        }
        self.pool
            .open_with_id(id, subagent_type.clone())
            .map_err(|err| self.admission_error(err, id))?;
        let handle = ChatThreadHandle::new(
            ChatThreadId {
                value: id.to_string(),
            },
            subagent_type,
        );
        Ok(self.pool.park_handle(handle, now))
    }

    /// Queues `msg` on its thread and returns its outcome and, when enqueued, its ticket.
    ///
    /// Refusals, in order: [`ChatError::ShuttingDown`] after [`ChatPool::shutdown`];
    /// [`ChatError::UnknownThread`] for a thread that is not open; [`ChatError::QueueFull`] when
    /// the thread's queue is at capacity. A message id that is queued or in flight on the thread,
    /// or inside its dedupe window, returns [`crate::SubmitOutcome::AlreadyAccepted`] with no
    /// ticket (DEC-6). A refused call enqueues nothing.
    pub fn submit(&self, msg: ChatMessage) -> Result<Submitted, ChatError> {
        check_message(&msg)?;
        self.runtime.submit(msg)
    }

    /// Cancels message `msg_id` on thread `thread_id` (CR-03).
    ///
    /// A queued message is removed and never runs: [`CancelOutcome::RemovedQueued`]. A message a
    /// worker is running gets a cooperative token: [`CancelOutcome::SignalledInFlight`]. Its ticket
    /// completes with [`crate::Completed::Cancelled`] when the worker finishes, and the value the
    /// executor returned is discarded. The worker is free for its next message; no thread is
    /// killed.
    ///
    /// Returns [`ChatError::UnknownThread`] when the thread is not open, and
    /// [`ChatError::UnknownMessage`] when the message is neither queued nor running. Nothing
    /// changes on either refusal.
    pub fn cancel_message(
        &self,
        thread_id: &str,
        msg_id: &str,
    ) -> Result<CancelOutcome, ChatError> {
        self.runtime.cancel_message(thread_id, msg_id)
    }

    /// Cancels every message of thread `thread_id` (CR-03). The report lists each removed
    /// message in queue order and each signalled message, so no cancelled message goes
    /// unaccounted for. Returns [`ChatError::UnknownThread`] when the thread is not open.
    pub fn cancel_thread(&self, thread_id: &str) -> Result<CancelReport, ChatError> {
        self.runtime.cancel_thread(thread_id)
    }

    /// Stops accepting work and reports every message queued or in flight at the call (GA-10).
    ///
    /// Messages that finish within `grace` are listed in `flushed`. Queued messages still waiting
    /// at the deadline are `dropped`, and running messages still running are `abandoned`. Each of
    /// their tickets completes with [`ChatError::ShuttingDown`]. `cancelled` lists the queued
    /// messages cancelled while shutdown waited. Messages that completed before the call are not
    /// listed.
    pub fn shutdown(&self, grace: Duration) -> ShutdownReport {
        self.runtime.shutdown(grace)
    }

    /// Opens the queue of thread `id`. Returns [`ChatError::ShuttingDown`] once shutdown has
    /// closed intake, and [`ChatError::ThreadExists`] when the queue is already open. The
    /// closed check and the open share one runtime lock, so no queue opens after shutdown.
    fn open_queue(&self, id: &str) -> Result<(), ChatError> {
        let mut state = self.runtime.state();
        if state.is_closed() {
            return Err(ChatError::ShuttingDown);
        }
        state
            .queue
            .open(id)
            .map_err(|_| ChatError::ThreadExists(id.to_string()))
    }

    /// Closes the queue of thread `id` if it is idle. Used only to undo a failed open.
    fn close_queue(&self, id: &str) {
        let _closed = self.runtime.state().queue.close(id);
    }

    /// Maps a registry admission failure for thread `id` to a facade error.
    fn admission_error(&self, err: PoolError, id: &str) -> ChatError {
        match err {
            PoolError::AlreadyExists(_) => ChatError::ThreadExists(id.to_string()),
            // The registry's only other admission refusal is `Exhausted`, the live-thread cap.
            _ => self.capacity_error(),
        }
    }

    /// The capacity refusal for this pool's `max_threads`.
    fn capacity_error(&self) -> ChatError {
        ChatError::Capacity {
            max_threads: self.config.max_threads,
        }
    }
}

impl Drop for ChatPool {
    /// Stops the pool without a report: closes intake, fails queued and in-flight tickets with
    /// `ShuttingDown`, sets the running jobs' cancel tokens and wakes the workers. Waits at most
    /// `drop_grace_ms`. Workers still running then are detached, and a warning records their count.
    fn drop(&mut self) {
        let running = self
            .runtime
            .abort(Duration::from_millis(self.config.drop_grace_ms));
        if running > 0 {
            tracing::warn!(
                workers_running = running,
                "chat pool dropped with workers still running; they are detached"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    use gsd_proto::gsd::v1::SubagentType;

    use super::*;
    use crate::chat_runtime::tests::{settle, Probe};
    use crate::Completed;
    use std::sync::atomic::Ordering;

    /// Budget port that accepts every reservation. The facade tests never reach a refusal.
    struct NoBudget;

    impl BudgetPort for NoBudget {
        fn reserve(&self, _reservation: &BudgetReservation) -> Result<(), BudgetRefusal> {
            Ok(())
        }

        fn release(&self, _reservation: &BudgetReservation) -> Result<(), BudgetRefusal> {
            Ok(())
        }
    }

    fn kind(value: &str) -> SubagentType {
        SubagentType {
            value: value.to_string(),
        }
    }

    fn config(max_threads: usize, warm_pool_size: usize) -> ChatRuntimeConfig {
        ChatRuntimeConfig {
            max_threads,
            warm_pool_size,
            ..ChatRuntimeConfig::default()
        }
    }

    fn make_pool(max_threads: usize, warm_pool_size: usize) -> Result<ChatPool, ChatError> {
        ChatPool::new(config(max_threads, warm_pool_size), Arc::new(NoBudget))
    }

    #[test]
    fn drop_grace_zero_is_refused_and_named() {
        assert_eq!(ChatRuntimeConfig::default().drop_grace_ms, 2000);
        let cfg = ChatRuntimeConfig {
            drop_grace_ms: 0,
            ..ChatRuntimeConfig::default()
        };
        assert_eq!(
            cfg.validate(),
            Err(ChatError::InvalidConfig(
                "drop_grace_ms must be greater than zero".to_string()
            ))
        );
    }

    #[test]
    fn zero_field_is_refused_and_named() {
        type Setter = fn(&mut ChatRuntimeConfig);
        let fields: [(&str, Setter); 5] = [
            ("workers", |c| c.workers = 0),
            ("per_thread_queue", |c| c.per_thread_queue = 0),
            ("dedupe_window", |c| c.dedupe_window = 0),
            ("max_threads", |c| c.max_threads = 0),
            ("warm_pool_size", |c| c.warm_pool_size = 0),
        ];
        for (name, zero) in fields {
            let mut cfg = ChatRuntimeConfig::default();
            zero(&mut cfg);
            match ChatPool::new(cfg, Arc::new(NoBudget)).err() {
                Some(ChatError::InvalidConfig(message)) => {
                    assert!(message.contains(name), "{message} should name {name}");
                }
                other => panic!("expected InvalidConfig for {name}, got {other:?}"),
            }
        }
    }

    #[test]
    fn dedupe_window_below_per_thread_queue_is_refused_and_named() {
        let cfg = ChatRuntimeConfig {
            per_thread_queue: 8,
            dedupe_window: 4,
            ..ChatRuntimeConfig::default()
        };
        match ChatPool::new(cfg, Arc::new(NoBudget)).err() {
            Some(ChatError::InvalidConfig(message)) => {
                assert!(
                    message.contains("dedupe_window"),
                    "{message} should name dedupe_window"
                );
                assert!(
                    message.contains("per_thread_queue"),
                    "{message} should name per_thread_queue"
                );
            }
            other => panic!("expected InvalidConfig, got {other:?}"),
        }
    }

    #[test]
    fn dedupe_window_equal_to_per_thread_queue_is_refused_and_named() {
        let cfg = ChatRuntimeConfig {
            per_thread_queue: 8,
            dedupe_window: 8,
            ..ChatRuntimeConfig::default()
        };
        match ChatPool::new(cfg, Arc::new(NoBudget)).err() {
            Some(ChatError::InvalidConfig(message)) => {
                assert!(
                    message.contains("dedupe_window"),
                    "{message} should name dedupe_window"
                );
            }
            other => panic!("expected InvalidConfig, got {other:?}"),
        }
    }

    #[test]
    fn dedupe_window_one_above_per_thread_queue_is_accepted() {
        let cfg = ChatRuntimeConfig {
            per_thread_queue: 8,
            dedupe_window: 9,
            ..ChatRuntimeConfig::default()
        };
        assert!(ChatPool::new(cfg, Arc::new(NoBudget)).is_ok());
    }

    #[test]
    fn default_config_is_accepted() {
        assert!(ChatPool::new(ChatRuntimeConfig::default(), Arc::new(NoBudget)).is_ok());
    }

    #[test]
    fn park_fresh_parks_a_new_id_and_frees_it_for_a_later_open() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        let placement = pool.park_fresh("fresh", kind("w"), Instant::now())?;
        assert!(matches!(placement, Placement::Parked));
        // A parked id is not live, so it opens again as a fresh thread.
        pool.open_thread("fresh", kind("w"))
    }

    #[test]
    fn park_fresh_refuses_a_live_id_and_leaves_the_thread_open() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        pool.open_thread("live", kind("w"))?;
        assert!(matches!(
            pool.park_fresh("live", kind("w"), Instant::now()),
            Err(ChatError::ThreadExists(_))
        ));
        assert!(matches!(
            pool.open_thread("live", kind("w")),
            Err(ChatError::ThreadExists(_))
        ));
        Ok(())
    }

    #[test]
    fn park_fresh_succeeds_when_every_live_slot_is_taken() -> Result<(), ChatError> {
        let pool = make_pool(1, 5)?;
        pool.open_thread("a", kind("w"))?;
        assert_eq!(
            pool.park_fresh("th1", kind("w"), Instant::now()),
            Ok(Placement::Parked)
        );
        assert_eq!(pool.pool.warm_len(), 1);
        // The only live slot is held by "a", so "th1" opening is refused for capacity: the
        // parked id is not live.
        assert_eq!(
            pool.open_thread("th1", kind("w")),
            Err(ChatError::Capacity { max_threads: 1 })
        );
        assert_eq!(pool.registry.len(), 1);
        Ok(())
    }

    #[test]
    fn duplicate_live_id_is_thread_exists() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        pool.open_thread("t1", kind("w"))?;
        assert_eq!(
            pool.open_thread("t1", kind("w")),
            Err(ChatError::ThreadExists("t1".to_string()))
        );
        Ok(())
    }

    #[test]
    fn a_duplicate_open_at_full_capacity_is_thread_exists() -> Result<(), ChatError> {
        // The id is live, so the duplicate is ThreadExists even though the one live slot is
        // taken too. Capacity is for a new id only.
        let pool = make_pool(1, 5)?;
        pool.open_thread("a", kind("w"))?;
        assert_eq!(
            pool.open_thread("a", kind("w")),
            Err(ChatError::ThreadExists("a".to_string()))
        );
        Ok(())
    }

    #[test]
    fn third_thread_is_refused_at_max_threads() -> Result<(), ChatError> {
        let pool = make_pool(2, 5)?;
        pool.open_thread("a", kind("w"))?;
        pool.open_thread("b", kind("w"))?;
        assert_eq!(
            pool.open_thread("c", kind("w")),
            Err(ChatError::Capacity { max_threads: 2 })
        );
        // The refused call changed nothing: the registry still holds exactly two threads.
        assert_eq!(pool.registry.len(), 2);
        Ok(())
    }

    #[tokio::test]
    async fn release_of_unknown_thread_is_unknown_and_not_counted() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        assert_eq!(
            pool.release("unknown").await,
            Err(ChatError::UnknownThread("unknown".to_string()))
        );
        Ok(())
    }

    #[test]
    fn full_warm_pool_closes_the_thread_and_forgets_it() -> Result<(), ChatError> {
        let pool = make_pool(30, 1)?;
        let now = Instant::now();
        pool.open_thread("a", kind("w"))?;
        pool.open_thread("b", kind("w"))?;
        assert_eq!(pool.park("a", now)?, Placement::Parked);
        assert_eq!(pool.park("b", now)?, Placement::Closed);
        assert!(!pool.pool.threads.contains_key("b"));
        // The closed thread is not live, so its id opens again.
        pool.open_thread("b", kind("w"))?;
        Ok(())
    }

    #[test]
    fn eviction_removes_the_stale_thread_from_the_thread_map() -> Result<(), ChatError> {
        let pool = make_pool(30, 1)?;
        let t0 = Instant::now();
        pool.open_thread("a", kind("w"))?;
        pool.open_thread("b", kind("w"))?;
        assert_eq!(pool.park("a", t0)?, Placement::Parked);
        // `a` is 31 s old, past the 30 s grace, so parking `b` evicts it.
        assert_eq!(
            pool.park("b", t0 + Duration::from_secs(31))?,
            Placement::Parked
        );
        assert!(!pool.pool.threads.contains_key("a"));
        assert!(!pool.pool.threads.contains_key("b"));
        assert_eq!(pool.pool.warm_len(), 1);
        pool.open_thread("a", kind("w"))?;
        Ok(())
    }

    #[tokio::test]
    async fn acquire_then_release_then_acquire_reuses_the_warm_thread() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        let first = pool.acquire(kind("w")).await?;
        assert_eq!(pool.release(&first).await?, Placement::Parked);
        let second = pool.acquire(kind("w")).await?;
        assert_eq!(second, first);
        Ok(())
    }

    #[tokio::test]
    async fn parked_threads_do_not_count_against_max_threads() -> Result<(), ChatError> {
        let pool = make_pool(1, 5)?;
        let first = pool.acquire(kind("w")).await?;
        pool.release(&first).await?;
        // The only live slot is free again, so a second acquire fits and reuses the warm thread.
        let second = pool.acquire(kind("w")).await?;
        assert_eq!(second, first);
        assert_eq!(
            pool.open_thread("x", kind("w")),
            Err(ChatError::Capacity { max_threads: 1 })
        );
        Ok(())
    }

    #[test]
    fn park_of_never_opened_id_is_unknown() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        assert_eq!(
            pool.park("never-opened", Instant::now()),
            Err(ChatError::UnknownThread("never-opened".to_string()))
        );
        Ok(())
    }

    #[test]
    fn an_id_over_the_byte_cap_is_refused_and_changes_nothing() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        let long = "a".repeat(MAX_ID_BYTES + 1);
        assert!(matches!(
            pool.open_thread(&long, kind("w")),
            Err(ChatError::TooLarge(_))
        ));
        assert_eq!(pool.registry.len(), 0);
        // Exactly at the cap is accepted.
        pool.open_thread(&"a".repeat(MAX_ID_BYTES), kind("w"))?;
        Ok(())
    }

    #[test]
    fn a_message_over_a_byte_cap_is_refused_before_it_is_queued() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        pool.open_thread("t", kind("w"))?;
        let long_id = "m".repeat(MAX_ID_BYTES + 1);
        let refused = [
            ChatMessage::new(long_id.clone(), "t", "s", Vec::new()),
            ChatMessage::new("m", long_id.clone(), "s", Vec::new()),
            ChatMessage::new("m", "t", long_id, Vec::new()),
            ChatMessage::new("m", "t", "s", vec![0; MAX_PAYLOAD_BYTES + 1]),
        ];
        for msg in refused {
            assert!(matches!(pool.submit(msg), Err(ChatError::TooLarge(_))));
        }
        Ok(())
    }

    #[tokio::test]
    async fn parked_id_reopens_as_a_fresh_thread_and_drops_the_stale_warm_entry(
    ) -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        let now = Instant::now();
        pool.open_thread("a", kind("w"))?;
        assert_eq!(pool.park("a", now)?, Placement::Parked);
        assert_eq!(pool.pool.warm_len(), 1);

        pool.open_thread("a", kind("w"))?;
        assert_eq!(
            pool.pool.warm_len(),
            0,
            "stale warm entry must be discarded"
        );

        // The warm pool is empty, so acquire creates a new thread rather than returning "a".
        let generated = pool.acquire(kind("w")).await?;
        assert_ne!(generated, "a");
        assert_eq!(
            pool.open_thread("a", kind("w")),
            Err(ChatError::ThreadExists("a".to_string()))
        );
        Ok(())
    }

    #[test]
    fn close_thread_of_unknown_id_is_unknown() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        assert_eq!(
            pool.close_thread("nope"),
            Err(ChatError::UnknownThread("nope".to_string()))
        );
        Ok(())
    }

    #[test]
    fn closed_thread_frees_its_admission_slot() -> Result<(), ChatError> {
        let pool = make_pool(1, 5)?;
        pool.open_thread("a", kind("w"))?;
        pool.close_thread("a")?;
        pool.open_thread("b", kind("w"))?;
        assert_eq!(pool.registry.len(), 1);
        Ok(())
    }

    #[test]
    fn dropping_a_pool_wakes_its_parked_workers_and_none_stay_alive() -> Result<(), ChatError> {
        let pool = ChatPool::new(
            ChatRuntimeConfig {
                workers: 3,
                ..ChatRuntimeConfig::default()
            },
            Arc::new(NoBudget),
        )?;
        let live = pool.runtime.live_counter();
        // Every worker is idle, parked on the work condvar, when the pool is dropped.
        assert_eq!(live.load(Ordering::Acquire), 3);
        drop(pool);
        assert_eq!(live.load(Ordering::Acquire), 0);
        Ok(())
    }

    #[test]
    fn dropping_a_pool_cancels_a_running_job_and_fails_its_ticket(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let probe = Probe::new(false);
        let pool = ChatPool::assemble(
            ChatRuntimeConfig {
                workers: 1,
                ..ChatRuntimeConfig::default()
            },
            Arc::new(NoBudget),
            probe.executor(),
        )?;
        pool.open_thread("a", kind("w"))?;
        let live = pool.runtime.live_counter();
        let ticket = match pool
            .submit(ChatMessage::new("a1", "a", "s1", b"a1".to_vec()))?
            .ticket
        {
            Some(ticket) => ticket,
            None => return Err("a1 must be enqueued".into()),
        };
        probe.wait_started(1);
        drop(pool);
        // The gate never opened, so the executor returned only because the drop set its token.
        assert!(probe.token("a1").is_some_and(|token| token.is_cancelled()));
        assert_eq!(settle(&ticket), Completed::Failed(ChatError::ShuttingDown));
        assert_eq!(live.load(Ordering::Acquire), 0);
        Ok(())
    }

    #[tokio::test]
    async fn a_thread_cannot_be_opened_after_shutdown() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        let report = pool.shutdown(Duration::ZERO);
        assert!(report.dropped.is_empty());
        assert_eq!(
            pool.open_thread("a", kind("w")),
            Err(ChatError::ShuttingDown)
        );
        assert_eq!(pool.acquire(kind("w")).await, Err(ChatError::ShuttingDown));
        // Neither refused call left a live thread, a reserved slot or a thread-map entry.
        assert_eq!(pool.registry.len(), 0);
        assert_eq!(pool.pool.threads.len(), 0);
        Ok(())
    }

    #[tokio::test]
    async fn a_refused_acquire_after_shutdown_takes_no_warm_entry_and_creates_nothing(
    ) -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        pool.open_thread("a", kind("w"))?;
        assert_eq!(pool.release("a").await?, Placement::Parked);
        pool.shutdown(Duration::ZERO);
        assert_eq!(pool.acquire(kind("w")).await, Err(ChatError::ShuttingDown));
        // The refused call pops no warm entry and creates no thread.
        assert_eq!(pool.pool.warm_len(), 1);
        Ok(())
    }

    #[tokio::test]
    async fn a_release_after_shutdown_is_refused_and_parks_nothing() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        pool.open_thread("a", kind("w"))?;
        pool.shutdown(Duration::ZERO);
        assert_eq!(pool.release("a").await, Err(ChatError::ShuttingDown));
        // The thread stays where it was: still open, and nothing parked.
        assert_eq!(pool.registry.len(), 1);
        assert_eq!(pool.pool.warm_len(), 0);
        Ok(())
    }

    #[tokio::test]
    async fn a_fresh_park_after_shutdown_is_refused_and_creates_nothing() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        pool.shutdown(Duration::ZERO);
        assert_eq!(
            pool.park_fresh("a", kind("w"), Instant::now()),
            Err(ChatError::ShuttingDown)
        );
        assert_eq!(pool.pool.warm_len(), 0);
        assert_eq!(pool.registry.len(), 0);
        Ok(())
    }

    #[test]
    fn a_full_pool_refuses_an_open_with_shutting_down_after_shutdown() -> Result<(), ChatError> {
        let pool = make_pool(1, 5)?;
        pool.open_thread("a", kind("w"))?;
        pool.shutdown(Duration::ZERO);
        // The one live slot is taken, but shutdown is the reason the open must be refused.
        assert_eq!(
            pool.open_thread("b", kind("w")),
            Err(ChatError::ShuttingDown)
        );
        assert_eq!(pool.registry.len(), 1);
        Ok(())
    }

    #[test]
    fn open_thread_after_shutdown_refuses_a_live_id() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        pool.open_thread("live", kind("w"))?;
        pool.shutdown(Duration::ZERO);
        assert_eq!(
            pool.open_thread("live", kind("w")),
            Err(ChatError::ShuttingDown)
        );
        // The refused call changed nothing: the live thread is still the only one.
        assert_eq!(pool.registry.len(), 1);
        assert_eq!(pool.pool.threads.len(), 1);
        Ok(())
    }

    #[test]
    fn open_thread_after_shutdown_leaves_a_parked_entry_in_place() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        pool.open_thread("p", kind("w"))?;
        assert_eq!(pool.park("p", Instant::now())?, Placement::Parked);
        pool.shutdown(Duration::ZERO);
        assert_eq!(
            pool.open_thread("p", kind("w")),
            Err(ChatError::ShuttingDown)
        );
        // The refused open discards no warm entry and creates no thread.
        assert_eq!(pool.pool.warm_len(), 1);
        assert_eq!(pool.registry.len(), 0);
        Ok(())
    }

    #[test]
    fn a_worker_count_above_the_maximum_is_refused() {
        let cfg = ChatRuntimeConfig {
            workers: 257,
            ..ChatRuntimeConfig::default()
        };
        assert!(matches!(cfg.validate(), Err(ChatError::InvalidConfig(_))));
    }

    #[test]
    fn reopening_a_parked_thread_id_displaces_its_warm_entry() -> Result<(), ChatError> {
        let pool = make_pool(30, 5)?;
        pool.open_thread("a", kind("w"))?;
        assert_eq!(pool.park("a", Instant::now())?, Placement::Parked);
        // Reopening "a" displaces its parked entry, so the warm pool no longer holds it.
        pool.open_thread("a", kind("w"))?;
        assert_eq!(pool.pool.warm_len(), 0);
        Ok(())
    }
}
