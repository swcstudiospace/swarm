//! The bounded worker pool of the scheduler runtime (Phase 14.4, unit S2.2).
//!
//! [`Pool::submit`] runs a job closure on a worker thread, at most `max_workers` at a time; further jobs wait in submission order and
//! start, first in first out, the moment a running job finishes. A finished job leaves a [`Completion`] in a queue that the loop thread
//! reads with [`Pool::next_completion`] (blocking with a timeout) or [`Pool::try_next_completion`]. Completions come out in the order
//! the jobs finished.
//!
//! # Threads and blocking
//!
//! - Worker threads are **detached and never joined**. A job blocked inside a Python callback for minutes must not block shutdown, and
//!   the Python `WorkerPool` (`scripts/swarm_run.py`) uses daemon threads for the same reason. A worker thread is created when a job is
//!   submitted with a free slot, runs that job, then takes the next queued job directly (no new thread) and ends when the queue is empty.
//!   At most `max_workers` threads exist at once, so a long run does not accumulate thread identities on the host side. Each thread has
//!   an 8 MiB stack, the Linux default of a Python thread, because jobs call into the interpreter.
//! - **No lock is held while a job closure runs**, while a result or a panic payload is dropped, or while a queued job is dropped. A job
//!   may therefore call back into the pool (`in_flight`, even `submit`) and may block for as long as it likes; a caller of the pool may
//!   hold other locks around a port call without any of them being a pool lock. The internal mutex is held only for a few map and queue
//!   operations and never across a user callback, so it cannot take part in a lock-order inversion with the interpreter lock.
//! - [`Pool::submit`], [`Pool::in_flight`], [`Pool::queued`], [`Pool::try_next_completion`] never block beyond that short critical
//!   section. [`Pool::next_completion`] and [`Pool::shutdown`] block the calling thread, bounded by their `Duration`, on a condition
//!   variable (so no CPU is spent waiting); they are meant for the loop thread, which waits with the interpreter lock detached.
//!
//! # Panics
//!
//! A job runs under `catch_unwind`. A job that panics yields [`Outcome::Crashed`]: the completion carries **no payload text** (the
//! payload is dropped, itself guarded against a panicking drop), the thread goes on with the next queued job, and every other job and the
//! pool are unaffected. The default panic hook still prints its message to stderr; that is the host's to configure.
//!
//! # Model checking
//!
//! The pool's mutex, condition variables and threads come from a small `sync` shim: `std` normally and `loom` under `--cfg loom`, where
//! `pool::loom_tests` model-checks submit, completion and shutdown. Under `--cfg loom` the pool only works inside `loom::model`.

use std::collections::{BTreeMap, VecDeque};
use std::error::Error;
use std::fmt;
use std::io;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::PoisonError;
use std::time::{Duration, Instant};

use self::sync::{thread, Arc, Condvar, Mutex, MutexGuard};

#[cfg(all(test, loom))]
mod loom_tests;
#[cfg(all(test, not(loom)))]
mod tests;

/// The threading primitives of the pool: `std` normally, `loom` under `--cfg loom` so the model checker sees every lock and wakeup.
mod sync {
    #[cfg(loom)]
    pub(super) use loom::sync::{Arc, Condvar, Mutex, MutexGuard};
    #[cfg(loom)]
    pub(super) use loom::thread;
    #[cfg(not(loom))]
    pub(super) use std::sync::{Arc, Condvar, Mutex, MutexGuard};
    #[cfg(not(loom))]
    pub(super) use std::thread;
}

/// The stack of a worker thread: 8 MiB, what a Python thread gets by default on Linux, because jobs call into the interpreter.
const WORKER_STACK_BYTES: usize = 8 * 1024 * 1024;

/// How a job ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome<R> {
    /// The closure returned this value.
    Done(R),
    /// The closure panicked. The panic payload is dropped on purpose: no text of it is kept.
    Crashed,
}

/// The end of one job, as read from the completion queue.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Completion<R> {
    /// The id given to [`Pool::submit`].
    pub task_id: String,
    /// What the job returned, or `Crashed` when it panicked.
    pub outcome: Outcome<R>,
}

/// Why [`Pool::submit`] refused a job. The job was not run and is dropped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SubmitError {
    /// [`Pool::shutdown`] was called (or the pool is being dropped): the pool accepts no more jobs.
    Closed,
    /// The operating system refused a new thread (resource exhaustion). The kind of the OS error is kept; its text is not.
    Spawn(io::ErrorKind),
}

impl fmt::Display for SubmitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Closed => f.write_str("worker pool is shut down"),
            Self::Spawn(kind) => write!(f, "could not start a worker thread: {kind}"),
        }
    }
}

impl Error for SubmitError {}

/// What [`Pool::shutdown`] found.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Shutdown {
    /// Ids of the jobs still running when the wait ended, sorted. Their threads stay detached and keep running; each still posts a
    /// completion when (if) its job returns.
    pub abandoned: Vec<String>,
    /// Ids of the jobs that were queued and never started, in submission order. They are dropped without running.
    pub dropped: Vec<String>,
}

type Job<R> = Box<dyn FnOnce() -> R + Send + 'static>;

/// A job waiting for a free slot.
struct Queued<R> {
    seq: u64,
    task_id: String,
    job: Job<R>,
}

/// Everything the mutex guards. Invariant at every lock release: `queue` is non-empty only while `running.len()` equals the cap.
struct State<R> {
    next_seq: u64,
    closed: bool,
    queue: VecDeque<Queued<R>>,
    /// Running jobs: submission sequence number to task id. One entry per live worker thread.
    running: BTreeMap<u64, String>,
    completions: VecDeque<Completion<R>>,
}

struct Shared<R> {
    state: Mutex<State<R>>,
    /// Signalled when a completion is queued.
    completed: Condvar,
    /// Signalled when the last running job leaves `running`.
    drained: Condvar,
}

impl<R> Shared<R> {
    /// Locks the state. A poisoned mutex is recovered: no critical section runs user code, so the state is never left half updated.
    fn lock(&self) -> MutexGuard<'_, State<R>> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Waits on `condvar` for at most `timeout`, with the same poison recovery as [`Shared::lock`].
    fn wait<'a>(
        &self,
        condvar: &Condvar,
        guard: MutexGuard<'a, State<R>>,
        timeout: Duration,
    ) -> MutexGuard<'a, State<R>> {
        condvar
            .wait_timeout(guard, timeout)
            .unwrap_or_else(PoisonError::into_inner)
            .0
    }
}

/// A bounded pool of detached worker threads; see the [module documentation](self).
///
/// `Pool` is `Send` and `Sync` (share it in an `Arc` to let jobs call back into it). Dropping it closes it like
/// [`Pool::shutdown`] with no wait: queued jobs are dropped, running jobs keep going on their detached threads.
pub struct Pool<R: Send + 'static> {
    shared: Arc<Shared<R>>,
    max_workers: usize,
}

impl<R: Send + 'static> Pool<R> {
    /// Creates a pool that runs at most `max_workers` jobs at once (a value of 0 is raised to 1, like the Python pool). No thread is
    /// started until a job is submitted.
    pub fn new(max_workers: usize) -> Self {
        let state = State {
            next_seq: 0,
            closed: false,
            queue: VecDeque::new(),
            running: BTreeMap::new(),
            completions: VecDeque::new(),
        };
        Self {
            shared: Arc::new(Shared {
                state: Mutex::new(state),
                completed: Condvar::new(),
                drained: Condvar::new(),
            }),
            max_workers: max_workers.max(1),
        }
    }

    /// Queues `job` under `task_id`: it starts on its own worker thread at once when fewer than `max_workers` jobs run, otherwise it
    /// waits for a slot behind the jobs submitted before it. Returns without waiting for the job.
    ///
    /// # Errors
    ///
    /// [`SubmitError::Closed`] after [`Pool::shutdown`]; [`SubmitError::Spawn`] when the operating system cannot start a thread.
    pub fn submit<F>(&self, task_id: impl Into<String>, job: F) -> Result<(), SubmitError>
    where
        F: FnOnce() -> R + Send + 'static,
    {
        let task_id = task_id.into();
        let seq = {
            let mut state = self.shared.lock();
            if state.closed {
                return Err(SubmitError::Closed);
            }
            let seq = state.next_seq;
            state.next_seq += 1;
            if state.running.len() >= self.max_workers {
                state.queue.push_back(Queued {
                    seq,
                    task_id,
                    job: Box::new(job),
                });
                return Ok(());
            }
            state.running.insert(seq, task_id.clone());
            seq
        };
        let shared = Arc::clone(&self.shared);
        let spawned = thread::Builder::new()
            .name(format!("sched-worker-{seq}"))
            .stack_size(WORKER_STACK_BYTES)
            .spawn(move || run_worker(&shared, seq, task_id, Box::new(job)));
        if let Err(error) = spawned {
            let mut state = self.shared.lock();
            state.running.remove(&seq);
            let drained = state.running.is_empty();
            drop(state);
            if drained {
                self.shared.drained.notify_all();
            }
            return Err(SubmitError::Spawn(error.kind()));
        }
        Ok(())
    }

    /// Waits up to `timeout` for a finished job and returns its completion; `None` when none arrived in time. A zero `timeout` only
    /// looks. Blocks the calling thread on a condition variable and holds no lock while blocked, so a completion wakes it at once; a
    /// `timeout` too large to add to the clock waits without a bound.
    pub fn next_completion(&self, timeout: Duration) -> Option<Completion<R>> {
        let deadline = Instant::now().checked_add(timeout);
        let mut state = self.shared.lock();
        loop {
            if let Some(completion) = state.completions.pop_front() {
                return Some(completion);
            }
            let left = match deadline {
                Some(deadline) => deadline.saturating_duration_since(Instant::now()),
                None => Duration::MAX,
            };
            if left.is_zero() {
                return None;
            }
            state = self.shared.wait(&self.shared.completed, state, left);
        }
    }

    /// Returns the oldest unread completion without waiting.
    pub fn try_next_completion(&self) -> Option<Completion<R>> {
        self.shared.lock().completions.pop_front()
    }

    /// The jobs that have not finished: running plus queued. A job leaves this count in the same step that queues its completion.
    pub fn in_flight(&self) -> usize {
        let state = self.shared.lock();
        state.running.len() + state.queue.len()
    }

    /// The jobs waiting for a free slot (submitted, not started).
    pub fn queued(&self) -> usize {
        self.shared.lock().queue.len()
    }

    /// Stops accepting jobs, drops the queued ones without running them, and waits up to `wait` for the running ones.
    ///
    /// Returns the ids dropped from the queue and the ids still running when the wait ended (`abandoned`). It returns as soon as no
    /// job is running, so a generous `wait` costs nothing when the jobs are quick. Abandoned threads are never joined or killed: they
    /// keep running detached, and their completions can still be read afterwards. Blocks the calling thread for at most `wait` on a
    /// condition variable. Calling it again is harmless.
    pub fn shutdown(&self, wait: Duration) -> Shutdown {
        let deadline = Instant::now().checked_add(wait);
        let queued = {
            let mut state = self.shared.lock();
            state.closed = true;
            std::mem::take(&mut state.queue)
        };
        let dropped: Vec<String> = queued.into_iter().map(|queued| queued.task_id).collect();
        let mut state = self.shared.lock();
        while !state.running.is_empty() {
            let left = match deadline {
                Some(deadline) => deadline.saturating_duration_since(Instant::now()),
                None => Duration::MAX,
            };
            if left.is_zero() {
                break;
            }
            state = self.shared.wait(&self.shared.drained, state, left);
        }
        let mut abandoned: Vec<String> = state.running.values().cloned().collect();
        drop(state);
        abandoned.sort();
        Shutdown { abandoned, dropped }
    }
}

impl<R: Send + 'static> Drop for Pool<R> {
    fn drop(&mut self) {
        self.shutdown(Duration::ZERO);
    }
}

/// The body of a worker thread: runs `job`, publishes its completion and takes the next queued job, until the queue is empty.
/// The state lock is taken only to publish and to take the next job, never around a job.
fn run_worker<R: Send + 'static>(
    shared: &Shared<R>,
    mut seq: u64,
    mut task_id: String,
    mut job: Job<R>,
) {
    loop {
        let outcome = match catch_unwind(AssertUnwindSafe(job)) {
            Ok(value) => Outcome::Done(value),
            Err(payload) => {
                // A payload whose own drop panics must not unwind out of the worker.
                let _ = catch_unwind(AssertUnwindSafe(move || drop(payload)));
                Outcome::Crashed
            }
        };
        let mut state = shared.lock();
        state.running.remove(&seq);
        state.completions.push_back(Completion { task_id, outcome });
        let next = state.queue.pop_front();
        if let Some(queued) = &next {
            state.running.insert(queued.seq, queued.task_id.clone());
        }
        let drained = state.running.is_empty();
        drop(state);
        shared.completed.notify_all();
        if drained {
            shared.drained.notify_all();
        }
        let Some(queued) = next else { return };
        seq = queued.seq;
        task_id = queued.task_id;
        job = queued.job;
    }
}
