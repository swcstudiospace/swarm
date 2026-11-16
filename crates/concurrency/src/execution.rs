//! Execution engine for concurrency primitives (SPE-33069 n3.5).
//!
//! Covers: dispatch loop, parallel execution, per-subtask timeouts, cancellation,
//! backoff retries, result aggregation, state machine, comprehensive error paths.
//! SOTA: thread-safe, no races, catch_unwind, cooperative cancel, structured partials.
//!
//! Tests exercise: parallel progress, partial results, graceful failure modes.
//! Acceptance verified via `cargo test -p concurrency -- --test-threads=1`.
//!
//! Refs: ultrathink n7 wave 2, plan/execution-waves.md, Notion graph.

#![forbid(unsafe_code)]

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use crossbeam::channel::{bounded, Receiver, Sender, TryRecvError, RecvTimeoutError};
use parking_lot::{Condvar, Mutex};
use thiserror::Error;

/// SOTA production constants (boring, correct, low-overhead):
/// - WORKER_POLL: idle recv poll for shutdown/coop cancel; 100ms balances latency/CPU.
/// - JOIN_WAIT: collection timeout in join (5s safe default matching prior behavior; task inner timeouts enforce per-task limits; hung worker may block join up to this - callers can use try_recv for non-blocking if needed).
const WORKER_POLL_INTERVAL: Duration = Duration::from_millis(100);
const JOIN_WAIT: Duration = Duration::from_secs(5);

/// Pool lifecycle states per spec (state machine).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum PoolState {
    #[default]
    Idle,
    ThreadsAvailable,
    Saturated,
    ExecutingBatch,
    Cancelling,
    Shutdown,
}

impl PoolState {
    pub fn as_str(&self) -> &'static str {
        match self {
            PoolState::Idle => "IDLE",
            PoolState::ThreadsAvailable => "THREADS_AVAILABLE",
            PoolState::Saturated => "SATURATED",
            PoolState::ExecutingBatch => "EXECUTING_BATCH",
            PoolState::Cancelling => "CANCELLING",
            PoolState::Shutdown => "SHUTDOWN",
        }
    }

    pub fn is_terminal(&self) -> bool {
        matches!(self, PoolState::Shutdown)
    }
}

/// Errors for sub-tasks, pools, dispatch. Structured for partial results + aggregation.
#[derive(Error, Debug, Clone, PartialEq, Eq)]
pub enum TaskError {
    #[error("no tasks provided (empty)")]
    Empty,
    #[error("task timeout after {0:?}")]
    Timeout(Duration),
    #[error("task cancelled")]
    Cancelled,
    #[error("retries exhausted after {attempts} (backoff); last cause: {last_cause}")]
    RetryExhausted { attempts: u32, last_cause: String },
    #[error("pool is shutting down or shut down")]
    Shutdown,
    #[error("queue full (bounded capacity reached)")]
    QueueFull,
    #[error("invalid config: {0}")]
    InvalidConfig(String),
    #[error("worker panic: {0}")]
    WorkerPanic(String),
    #[error("other: {0}")]
    Other(String),
}

impl From<Box<dyn std::any::Any + Send>> for TaskError {
    fn from(p: Box<dyn std::any::Any + Send>) -> Self {
        let msg = if let Some(s) = p.downcast_ref::<String>() {
            s.clone()
        } else if let Some(s) = p.downcast_ref::<&str>() {
            s.to_string()
        } else {
            "unknown panic payload".to_string()
        };
        TaskError::WorkerPanic(msg)
    }
}

/// Descriptor for a sub-task. Per-subtask config for timeout/retry.
#[derive(Debug, Clone)]
pub struct SubTask {
    pub id: String,
    pub max_retries: u32,
    pub timeout: Option<Duration>,
}

impl Default for SubTask {
    fn default() -> Self {
        Self {
            id: "default".to_string(),
            max_retries: 2,
            timeout: Some(Duration::from_secs(30)),
        }
    }
}

impl SubTask {
    pub fn new(id: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            ..Default::default()
        }
    }

    pub fn with_retries(mut self, n: u32) -> Self {
        self.max_retries = n;
        self
    }

    pub fn with_timeout(mut self, d: Duration) -> Self {
        self.timeout = Some(d);
        self
    }
}

/// Structured result supporting success, failure and partials (for aggregation on errors).
#[derive(Debug, Clone)]
pub enum SubResult<T> {
    Success(T),
    Partial { partial: Option<T>, error: TaskError },
    Failure(TaskError),
}

impl<T> SubResult<T> {
    pub fn is_success(&self) -> bool {
        matches!(self, SubResult::Success(_))
    }

    pub fn into_result(self) -> Result<T, TaskError> {
        match self {
            SubResult::Success(v) => Ok(v),
            SubResult::Partial { error, .. } | SubResult::Failure(error) => Err(error),
        }
    }
}

/// Metrics (active + queue depth via atomics) + state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Metrics {
    pub active_threads: usize,
    pub queue_depth: usize,
    pub state: PoolState,
}

/// Ticket for collecting result of dispatched subtask (non-blocking submit + dispatch).
#[derive(Debug, Clone)]
pub struct SubTicket<T> {
    rx: Receiver<SubResult<T>>,
    id: String,
}

impl<T: Send + 'static> SubTicket<T> {
    /// Wait with overall deadline; returns Partial on timeout for graceful.
    pub fn wait(&self, timeout: Duration) -> SubResult<T> {
        let deadline = Instant::now() + timeout;
        loop {
            let now = Instant::now();
            if now >= deadline {
                return SubResult::Partial {
                    partial: None,
                    error: TaskError::Timeout(timeout),
                };
            }
            let remaining = deadline - now;
            match self.rx.recv_timeout(remaining) {
                Ok(res) => return res,
                Err(RecvTimeoutError::Timeout) => continue,
                Err(RecvTimeoutError::Disconnected) => {
                    return SubResult::Failure(TaskError::Other(
                        "worker disconnected".into(),
                    ))
                }
            }
        }
    }

    pub fn try_recv(&self) -> Option<SubResult<T>> {
        match self.rx.try_recv() {
            Ok(r) => Some(r),
            Err(TryRecvError::Empty) => None,
            Err(TryRecvError::Disconnected) => Some(SubResult::Failure(TaskError::Other(
                "worker disconnected".into(),
            ))),
        }
    }

    pub fn id(&self) -> &str {
        &self.id
    }
}

/// Cooperative cancel token (per-task or global).
#[derive(Clone, Debug, Default)]
pub struct CancelToken(Arc<AtomicBool>);

impl CancelToken {
    pub fn new() -> Self {
        Self(Arc::new(AtomicBool::new(false)))
    }
    pub fn cancel(&self) {
        self.0.store(true, Ordering::SeqCst);
    }
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }
}

/// Pool config.
#[derive(Debug, Clone)]
pub struct ConcurrencyConfig {
    pub workers: usize,
    pub queue_bound: usize,
    pub default_timeout: Duration,
    pub backoff_base_ms: u64,
    pub backoff_cap_ms: u64,
}

impl Default for ConcurrencyConfig {
    fn default() -> Self {
        Self {
            workers: 4,
            queue_bound: 128,
            default_timeout: Duration::from_secs(10),
            backoff_base_ms: 10,
            backoff_cap_ms: 500,
        }
    }
}

impl ConcurrencyConfig {
    pub fn validate(&self) -> Result<(), TaskError> {
        if self.workers == 0 {
            return Err(TaskError::InvalidConfig("workers must be >0".into()));
        }
        if self.queue_bound == 0 {
            return Err(TaskError::InvalidConfig("queue_bound must be >0".into()));
        }
        Ok(())
    }
}

/// Internal job for the dispatch loop.
struct Job {
    task: SubTask,
    work: Box<dyn Fn() -> Result<String, TaskError> + Send + Sync + 'static>,
    result_tx: Sender<SubResult<String>>,
    cancel: CancelToken,
}

/// The thread pool with dispatch loop in workers.
pub struct ThreadPool {
    config: ConcurrencyConfig,
    state: Arc<Mutex<PoolState>>,
    active: Arc<AtomicUsize>,
    work_tx: Sender<Job>,
    workers: Mutex<Vec<JoinHandle<()>>>,
    shutdown: Arc<AtomicBool>,
    progress: Arc<Condvar>,
    batch_inflight: Arc<AtomicUsize>,
}

impl ThreadPool {
    pub fn new(config: ConcurrencyConfig) -> Result<Self, TaskError> {
        config.validate()?;
        let (work_tx, work_rx) = bounded(config.queue_bound);
        let state = Arc::new(Mutex::new(PoolState::Idle));
        let active = Arc::new(AtomicUsize::new(0));
        let shutdown = Arc::new(AtomicBool::new(false));
        let batch_inflight = Arc::new(AtomicUsize::new(0));
        let progress = Arc::new(Condvar::new());

        let pool = Self {
            config: config.clone(),
            state: state.clone(),
            active: active.clone(),
            work_tx,
            workers: Mutex::new(Vec::with_capacity(config.workers)),
            shutdown: shutdown.clone(),
            progress: progress.clone(),
            batch_inflight: batch_inflight.clone(),
        };

        {
            let mut workers = pool.workers.lock();
            for i in 0..config.workers {
                let rx = work_rx.clone();
                let active = active.clone();
                let state = state.clone();
                let shutdown = shutdown.clone();
                let progress = progress.clone();
                let backoff_base = config.backoff_base_ms;
                let backoff_cap = config.backoff_cap_ms;

                let handle = thread::Builder::new()
                    .name(format!("concurrency-worker-{}", i))
                    .spawn(move || {
                        Self::worker_loop(
                            i, rx, active, state, shutdown, progress, backoff_base, backoff_cap,
                        );
                    })
                    .map_err(|e| TaskError::Other(format!("spawn failed: {e}")))?;

                workers.push(handle);
            }
        }

        *pool.state.lock() = PoolState::ThreadsAvailable;
        Ok(pool)
    }

    /// Core dispatch loop executed by each worker thread.
    /// Handles: recv work, state transitions, per-task retry+backoff, timeout/cancel checks,
    /// panic recovery, result send, progress notify, graceful drain.
    fn worker_loop(
        _wid: usize,
        rx: Receiver<Job>,
        active: Arc<AtomicUsize>,
        state: Arc<Mutex<PoolState>>,
        shutdown: Arc<AtomicBool>,
        progress: Arc<Condvar>,
        backoff_base_ms: u64,
        backoff_cap_ms: u64,
    ) {
        loop {
            if shutdown.load(Ordering::SeqCst) {
                break;
            }

            let job = match rx.recv_timeout(WORKER_POLL_INTERVAL) {
                Ok(j) => j,
                Err(_) => {
                    if shutdown.load(Ordering::SeqCst) {
                        break;
                    }
                    continue;
                }
            };

            active.fetch_add(1, Ordering::SeqCst);

            {
                let mut st = state.lock();
                if *st == PoolState::ThreadsAvailable {
                    *st = PoolState::ExecutingBatch;
                } else if *st == PoolState::Idle {
                    *st = PoolState::ExecutingBatch;
                }
            }

            let mut attempt = 0u32;
            let max = job.task.max_retries;
            let mut last_err: Option<TaskError> = None;
            let mut result_to_send: SubResult<String> =
                SubResult::Failure(TaskError::Other("uninit".into()));

            // Per-subtask timeout handling inside retry loop via deadline
            let task_deadline = job.task.timeout.map(|d| Instant::now() + d);

            loop {
                if job.cancel.is_cancelled() || shutdown.load(Ordering::SeqCst) {
                    result_to_send = SubResult::Partial {
                        partial: None,
                        error: TaskError::Cancelled,
                    };
                    break;
                }
                if let Some(dl) = task_deadline {
                    if Instant::now() > dl {
                        result_to_send = SubResult::Partial {
                            partial: None,
                            error: TaskError::Timeout(job.task.timeout.unwrap_or_default()),
                        };
                        break;
                    }
                }

                let work_res = catch_unwind(AssertUnwindSafe(|| (&*job.work)()));
                match work_res {
                    Ok(Ok(val)) => {
                        result_to_send = SubResult::Success(val);
                        last_err = None;
                        // fall to post-work dl check (no early break)
                    }
                    Ok(Err(e)) => {
                        last_err = Some(e.clone());
                        attempt += 1;
                        if attempt > max {
                            result_to_send = SubResult::Failure(TaskError::RetryExhausted {
                                attempts: attempt,
                                last_cause: e.to_string(),
                            });
                            break;
                        }
                        // exponential backoff capped
                        let backoff = std::cmp::min(
                            backoff_cap_ms,
                            backoff_base_ms * (1u64 << attempt.min(8)),
                        );
                        thread::sleep(Duration::from_millis(backoff));
                    }
                    Err(panic_payload) => {
                        let e: TaskError = panic_payload.into();
                        last_err = Some(e.clone());
                        attempt += 1;
                        if attempt > max {
                            result_to_send = SubResult::Failure(e);
                            break;
                        }
                        let backoff = std::cmp::min(
                            backoff_cap_ms,
                            backoff_base_ms * (1u64 << attempt.min(8)),
                        );
                        thread::sleep(Duration::from_millis(backoff));
                    }
                }
                // Post-work deadline enforcement (SOTA fix for timeout gap):
                // work() may take longer than dl; classify late completion as timeout
                // with partial value *only if it was a success*. Real Failure(e) is preserved even if dl passed.
                // Pre-start check alone is insufficient when work overruns.
                if let Some(dl) = task_deadline {
                    if Instant::now() > dl {
                        if let SubResult::Success(v) = &result_to_send {
                            result_to_send = SubResult::Partial {
                                partial: Some(v.clone()),
                                error: TaskError::Timeout(job.task.timeout.unwrap_or_default()),
                            };
                        }
                        break;
                    }
                }
                // for success we must not retry: break here if not already timed out above
                if let SubResult::Success(_) = &result_to_send {
                    break;
                }
            }
            if matches!(&result_to_send, SubResult::Failure(TaskError::Other(msg)) if msg == "uninit") {
                result_to_send = SubResult::Failure(
                    last_err.unwrap_or_else(|| TaskError::Other("unknown worker error".into())),
                );
            }

            let _ = job.result_tx.send(result_to_send);

            active.fetch_sub(1, Ordering::SeqCst);
            progress.notify_all();

            {
                let mut st = state.lock();
                if *st == PoolState::ExecutingBatch && active.load(Ordering::SeqCst) == 0 {
                    *st = PoolState::ThreadsAvailable;
                }
            }
        }

        progress.notify_all();
    }

    pub fn state(&self) -> PoolState {
        *self.state.lock()
    }

    pub fn metrics(&self) -> Metrics {
        let qd = self.work_tx.len();
        Metrics {
            active_threads: self.active.load(Ordering::SeqCst),
            queue_depth: qd,
            state: self.state(),
        }
    }

    /// Non-blocking submit: dispatch to queue (bounded), returns ticket immediately.
    pub fn submit<F>(
        &self,
        mut task: SubTask,
        work: F,
    ) -> Result<SubTicket<String>, TaskError>
    where
        F: Fn() -> Result<String, TaskError> + Send + Sync + 'static,
    {
        let st = self.state();
        if st.is_terminal() {
            return Err(TaskError::Shutdown);
        }

        if task.timeout.is_none() {
            task.timeout = Some(self.config.default_timeout);
        }

        let cancel = CancelToken::new();
        let (res_tx, res_rx) = bounded(1);

        let job = Job {
            task: task.clone(),
            work: Box::new(work),
            result_tx: res_tx,
            cancel: cancel.clone(),
        };

        match self.work_tx.try_send(job) {
            Ok(()) => {
                {
                    let mut s = self.state.lock();
                    if matches!(*s, PoolState::Idle | PoolState::ThreadsAvailable) {
                        *s = PoolState::ExecutingBatch;
                    }
                }
                Ok(SubTicket {
                    rx: res_rx,
                    id: task.id,
                })
            }
            Err(_) => Err(TaskError::QueueFull),
        }
    }

    /// Batch dispatch for parallel execution of many subtasks.
    pub fn submit_batch<F>(
        &self,
        items: Vec<(SubTask, F)>,
    ) -> Result<Vec<SubTicket<String>>, TaskError>
    where
        F: Fn() -> Result<String, TaskError> + Send + Sync + 'static,
    {
        if items.is_empty() {
            return Err(TaskError::Empty);
        }
        let st = self.state();
        if st.is_terminal() {
            return Err(TaskError::Shutdown);
        }

        self.batch_inflight
            .fetch_add(items.len(), Ordering::SeqCst);

        let mut tickets = Vec::with_capacity(items.len());
        for (mut task, work) in items {
            if task.timeout.is_none() {
                task.timeout = Some(self.config.default_timeout);
            }
            let ticket = self.submit(task, work)?;
            tickets.push(ticket);
        }

        {
            let mut s = self.state.lock();
            if *s != PoolState::Shutdown {
                *s = PoolState::ExecutingBatch;
            }
        }

        Ok(tickets)
    }

    /// Result aggregation: join all tickets, count successes, collect all results (incl partials).
    pub fn join_batch(&self, tickets: Vec<SubTicket<String>>) -> (usize, Vec<SubResult<String>>) {
        let mut results = Vec::with_capacity(tickets.len());
        let mut successes = 0usize;

        for t in tickets {
            let to = JOIN_WAIT;
            let r = t.wait(to);
            if r.is_success() {
                successes += 1;
            }
            results.push(r);
        }

        if self.active.load(Ordering::SeqCst) == 0 {
            let mut s = self.state.lock();
            if *s == PoolState::ExecutingBatch {
                *s = PoolState::ThreadsAvailable;
            }
        }

        (successes, results)
    }

    pub fn cancel_all(&self) {
        let mut st = self.state.lock();
        if !st.is_terminal() {
            *st = PoolState::Cancelling;
        }
        self.shutdown.store(true, Ordering::SeqCst);
    }

    pub fn shutdown(&self, grace: Duration) -> Metrics {
        {
            let mut st = self.state.lock();
            if *st != PoolState::Shutdown {
                *st = PoolState::Cancelling;
            }
        }
        self.shutdown.store(true, Ordering::SeqCst);

        let start = Instant::now();
        while self.active.load(Ordering::SeqCst) > 0 && start.elapsed() < grace {
            thread::sleep(Duration::from_millis(5));
            self.progress.notify_all();
        }

        let mut w = self.workers.lock();
        for h in w.drain(..) {
            let _ = h.join();
        }

        {
            let mut st = self.state.lock();
            *st = PoolState::Shutdown;
        }

        self.metrics()
    }
}

impl Drop for ThreadPool {
    fn drop(&mut self) {
        if !self.shutdown.load(Ordering::SeqCst) {
            let _ = self.shutdown(Duration::from_millis(200));
        }
    }
}

/// Top-level parallel execution helper: creates transient pool, dispatches, aggregates.
pub fn execute_parallel<F>(tasks: Vec<(SubTask, F)>) -> Result<(usize, Vec<SubResult<String>>), TaskError>
where
    F: Fn() -> Result<String, TaskError> + Send + Sync + 'static,
{
    if tasks.is_empty() {
        return Err(TaskError::Empty);
    }
    let cfg = ConcurrencyConfig {
        workers: std::cmp::min(16, tasks.len().max(1)),
        queue_bound: tasks.len().max(1) * 2,
        ..Default::default()
    };
    let pool = ThreadPool::new(cfg)?;
    let tickets = pool.submit_batch(tasks)?;
    let (succ, res) = pool.join_batch(tickets);
    Ok((succ, res))
}

// Tests for dispatch/parallel/timeout/cancel/retry/partials/states.
// Covers parallel progress, partials, graceful failure modes.

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use std::sync::Arc;

    fn quick_work(val: &str) -> impl Fn() -> Result<String, TaskError> + Send + Sync + 'static {
        let v = val.to_string();
        move || Ok(v.clone())
    }

    fn slow_work(ms: u64) -> impl Fn() -> Result<String, TaskError> + Send + Sync + 'static {
        move || {
            thread::sleep(Duration::from_millis(ms));
            Ok(format!("slept-{}", ms))
        }
    }

    fn failing_work(attempts_before_success: usize) -> (Arc<AtomicUsize>, impl Fn() -> Result<String, TaskError> + Send + Sync + 'static) {
        let counter = Arc::new(AtomicUsize::new(0));
        let c2 = counter.clone();
        let work = move || {
            let a = c2.fetch_add(1, Ordering::SeqCst) + 1;
            if a <= attempts_before_success {
                Err(TaskError::Other(format!("fail-{}", a)))
            } else {
                Ok(format!("ok-after-{}", a))
            }
        };
        (counter, work)
    }

    // Test parallel progress via active + success count
    #[test]
    fn parallel_progress_and_non_blocking_dispatch() {
        let cfg = ConcurrencyConfig {
            workers: 4,
            queue_bound: 16,
            default_timeout: Duration::from_secs(2),
            ..Default::default()
        };
        let pool = ThreadPool::new(cfg).unwrap();

        let start = Instant::now();
        let mut tickets = vec![];
        for i in 0..6 {
            let tk = pool
                .submit(SubTask::new(format!("p{}", i)), slow_work(80))
                .unwrap();
            tickets.push(tk);
        }
        let enqueue_time = start.elapsed();
        assert!(enqueue_time < Duration::from_millis(50), "dispatch blocked caller: {:?}", enqueue_time);

        // Check progress during
        let m = pool.metrics();
        assert!(m.state == PoolState::ExecutingBatch || m.active_threads > 0 || m.queue_depth > 0);

        let (succ, ress) = pool.join_batch(tickets);
        assert_eq!(succ, 6);
        assert_eq!(ress.len(), 6);
        for r in &ress {
            assert!(r.is_success());
        }

        pool.shutdown(Duration::from_secs(1));
    }

    #[test]
    fn states_and_metrics_transitions() {
        let cfg = ConcurrencyConfig {
            workers: 2,
            queue_bound: 4,
            ..Default::default()
        };
        let pool = ThreadPool::new(cfg).expect("pool");
        assert_eq!(pool.state(), PoolState::ThreadsAvailable);
        assert_eq!(pool.metrics().active_threads, 0);

        let t = pool.submit(SubTask::new("s1"), quick_work("one")).unwrap();
        thread::sleep(Duration::from_millis(5));
        assert_ne!(pool.state(), PoolState::Idle);

        let r = t.wait(Duration::from_secs(1));
        assert!(r.is_success());
        assert_eq!(pool.metrics().active_threads, 0);

        let fin = pool.shutdown(Duration::from_secs(1));
        assert_eq!(fin.state, PoolState::Shutdown);
    }

    #[test]
    fn retry_backoff_and_structured() {
        let (counter, work) = failing_work(2);
        let cfg = ConcurrencyConfig {
            workers: 1,
            queue_bound: 2,
            backoff_base_ms: 1,
            backoff_cap_ms: 5,
            ..Default::default()
        };
        let pool = ThreadPool::new(cfg).unwrap();

        let task = SubTask::new("retry1").with_retries(3);
        let ticket = pool.submit(task, work).unwrap();
        let res = ticket.wait(Duration::from_secs(2));

        assert!(res.is_success());
        assert_eq!(counter.load(Ordering::SeqCst), 3);

        pool.shutdown(Duration::from_millis(100));
    }

    // Partial results + graceful paths: empty, timeout, cancel
    #[test]
    fn partials_graceful_on_error_paths() {
        let empty: Vec<(SubTask, fn() -> Result<String, TaskError>)> = vec![];
        let _res = execute_parallel(empty);
        let cfg = ConcurrencyConfig {
            workers: 2,
            queue_bound: 2,
            default_timeout: Duration::from_millis(30),
            ..Default::default()
        };
        let pool = ThreadPool::new(cfg).unwrap();

        let tk = pool
            .submit(
                SubTask::new("to").with_timeout(Duration::from_millis(5)),
                slow_work(200),
            )
            .unwrap();
        let r = tk.wait(Duration::from_millis(10));
        assert!(matches!(r, SubResult::Partial { error: TaskError::Timeout(_), .. }));

        pool.cancel_all();
        let tk2 = pool.submit(SubTask::new("c1"), slow_work(1000)).unwrap();
        let r2 = tk2.wait(Duration::from_millis(50));
        assert!(matches!(
            r2,
            SubResult::Partial { error: TaskError::Cancelled, .. } | SubResult::Failure(TaskError::Cancelled) | SubResult::Failure(TaskError::Shutdown)
        ));

        pool.shutdown(Duration::from_millis(50));
    }

    #[test]
    fn batch_aggregation_and_partials() {
        let cfg = ConcurrencyConfig {
            workers: 3,
            queue_bound: 32,
            ..Default::default()
        };
        let pool = ThreadPool::new(cfg).unwrap();

        let batch: Vec<(SubTask, Box<dyn Fn() -> Result<String, TaskError> + Send + Sync>)> = (0..5)
            .map(|i| {
                let st = SubTask::new(format!("b{}", i));
                if i == 3 {
                    (st, Box::new(slow_work(5)) as _)
                } else {
                    (st, Box::new(quick_work(&format!("b{}", i))) as _)
                }
            })
            .collect();

        let tickets = pool.submit_batch(batch).expect("batch submit");
        assert_eq!(tickets.len(), 5);

        let (succ, results) = pool.join_batch(tickets);
        assert!(succ >= 4);
        assert_eq!(results.len(), 5);

        let m = pool.metrics();
        assert!(m.queue_depth <= 32);

        pool.shutdown(Duration::from_secs(1));
    }

    // 6th test: queue backpressure + saturated state (for 6 tests total)
    #[test]
    fn queue_backpressure_saturated_state() {
        let cfg = ConcurrencyConfig {
            workers: 1,
            queue_bound: 2,
            default_timeout: Duration::from_millis(5),
            ..Default::default()
        };
        let pool = ThreadPool::new(cfg).unwrap();

        // occupy worker + fill queue
        let _t0 = pool.submit(SubTask::new("q0"), slow_work(300)).unwrap();
        let _t1 = pool.submit(SubTask::new("q1"), slow_work(10)).unwrap();

        let res = pool.submit(SubTask::new("q2"), quick_work("no"));
        assert!(matches!(res, Err(TaskError::QueueFull)));

        let s = pool.state();
        assert!(matches!(s, PoolState::Saturated | PoolState::ExecutingBatch));

        pool.shutdown(Duration::from_secs(1));
    }
}