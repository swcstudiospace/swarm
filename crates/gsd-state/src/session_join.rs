//! U3.4 session join: collects a session's completions through the B5 matrix join or the B6 nexus
//! join, applies each outcome to its thread's phase, and runs the cancel window.
//!
//! The barrier, merge and dispatch mechanics stay in `gsd-scheduler`; this module adds none of them.
//! Each run's stage log is emitted to a [`CompletionLedger`] under [`JoinKey`] `(stage, join seq,
//! attempt)`. An emission under a key already collected is dropped, and a run under such a key is not
//! repeated, so a re-emitted completion never applies twice.
//!
//! Cancel follows the runner's flag rule. A [`CancelMarker`] is written at `cancel/<corr>` with the
//! sanitized correlation id. Each running task then has [`CancelTiming::cooperative`] (10 s) to finish
//! inside [`CancelTiming::budget`] (15 s). A task still running when that window closes is stopped and
//! ends `CANCELLED_TIMED_OUT` with `metadata["terminal_reason"] = "timed_out"`. A task that returns a
//! failure after the marker was set, inside the window, ends `CANCELLED_TIMED_OUT` with reason
//! `cancelled`. A task that returns success keeps `SUCCEEDED`.
//!
//! A nexus run has no cooperative exit of its own. Its join future is dropped at the end of the window,
//! and each started thread ends `CANCELLED_TIMED_OUT` with reason `timed_out`. Stage merges that had not
//! returned by then are discarded; no retention semantics are implemented.
//!
//! A nexus participant's `E-TIMEOUT` straggler ends `FAILED`, as the n3 hook 3 "thread-timeout ⇒ FAILED"
//! fixture requires. A `RateLimited` matrix outcome leaves its thread `RUNNING`; retry belongs to the
//! caller and its backoff rule is not implemented.

use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use gsd_pool::thread_state_transition_table::ALL_PHASES;
use gsd_pool::{transition, ThreadPhase};
use gsd_proto::gsd::v1::{SessionState, Task, ThreadState, ThreadStateKind};
use gsd_scheduler::{
    run_matrix_join, CompletionLog, CompletionRecord, DispatchConfig, MatrixBatch, NexusJoinReport,
    NexusPlan, SchedulerError, SchedulerResult, SemaphoreManager, TaskFailure, TaskOutcome,
};
use parking_lot::Mutex;
use tokio::sync::watch;
use tokio::time::sleep;

use crate::session_create::{thread_for_task, PHASE_KEY};
use crate::session_dispatch::{admit_thread, handoff_thread, DispatchError};

/// Cooperative wait a cancelled task has before it is stopped. `SWARM_CANCEL_GRACE_S`.
pub const CANCEL_GRACE: Duration = Duration::from_secs(10);
/// Budget the cooperative wait sits inside. `SWARM_SHUTDOWN_BUDGET_S`.
pub const SHUTDOWN_BUDGET: Duration = Duration::from_secs(15);
/// Metadata key on a [`ThreadState`] holding the [`TerminalReason`] of a `CANCELLED_TIMED_OUT` thread.
pub const TERMINAL_REASON_KEY: &str = "terminal_reason";
/// Prefix of the cancel marker key, `cancel/<correlation id>`.
pub const CANCEL_KEY_PREFIX: &str = "cancel/";

/// Result type for session join operations.
pub type JoinResult<T> = Result<T, JoinError>;

/// Typed refusal or failure from a session join.
#[derive(Debug, thiserror::Error)]
pub enum JoinError {
    /// The cooperative window is longer than the budget it must sit inside.
    #[error(
        "cancel timing invalid: cooperative window {cooperative:?} exceeds the budget {budget:?}"
    )]
    Timing {
        /// Cooperative window requested.
        cooperative: Duration,
        /// Budget it must fit in.
        budget: Duration,
    },
    /// Join attempts start at 1.
    #[error("join attempt must be at least 1")]
    InvalidAttempt,
    /// A thread of the join is not IDLE, so the join cannot start it as a fresh thread.
    #[error("thread {thread} is {phase}, not IDLE; a join starts only fresh threads")]
    NotIdle {
        /// Thread that is not idle.
        thread: String,
        /// Its current phase.
        phase: &'static str,
    },
    /// The nexus plan has more stages than a [`JoinKey`] stage index can hold.
    #[error("nexus plan has {0} stages, more than a u32 stage index holds")]
    TooManyStages(usize),
    /// A thread lookup, thread-phase change or start refused. See [`DispatchError`].
    #[error("dispatch: {0}")]
    Dispatch(#[from] DispatchError),
    /// The B5 or B6 join refused or failed.
    #[error("scheduler: {0}")]
    Scheduler(#[from] SchedulerError),
}

/// Idempotency key of one emitted stage completion: `(stage id, join seq, attempt)`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct JoinKey {
    /// Stage the completion belongs to.
    pub stage: u32,
    /// Join sequence number within the session, supplied by the caller.
    pub join_seq: u64,
    /// Attempt of the stage, starting at 1. A partial join is re-run as `attempt + 1`.
    pub attempt: u32,
}

/// Whether an emission was collected or dropped as a duplicate of an earlier key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Emit {
    /// First emission under its key; the completion was stored.
    Collected,
    /// The key was already collected; nothing was stored or applied.
    Deduplicated,
}

/// The emission of one stage, with its key and outcome.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StageEmit {
    /// Key the stage was emitted under.
    pub key: JoinKey,
    /// Whether it was collected or dropped.
    pub emit: Emit,
}

/// Store of collected stage completions, keyed by [`JoinKey`]. Holds each key's log exactly once.
#[derive(Debug, Default)]
pub struct CompletionLedger {
    collected: BTreeMap<JoinKey, CompletionLog>,
}

impl CompletionLedger {
    /// Returns `true` when `key` has been collected.
    pub fn contains(&self, key: &JoinKey) -> bool {
        self.collected.contains_key(key)
    }

    /// Returns the collected log for `key`, if any.
    pub fn log(&self, key: &JoinKey) -> Option<&CompletionLog> {
        self.collected.get(key)
    }

    /// Number of collected keys.
    pub fn len(&self) -> usize {
        self.collected.len()
    }

    /// Returns `true` when no key has been collected.
    pub fn is_empty(&self) -> bool {
        self.collected.is_empty()
    }

    /// Stores `log` under `key` if the key is new. A second emission under the same key is dropped.
    pub fn emit(&mut self, key: JoinKey, log: CompletionLog) -> Emit {
        if self.collected.contains_key(&key) {
            return Emit::Deduplicated;
        }
        self.collected.insert(key, log);
        Emit::Collected
    }
}

/// Why a thread ended `CANCELLED_TIMED_OUT`. Stored as `metadata["terminal_reason"]`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TerminalReason {
    /// The thread failed on its way out of a cancel, inside the cooperative window.
    Cancelled,
    /// The cooperative window closed while the thread was still running; it was stopped.
    TimedOut,
}

impl TerminalReason {
    /// Wire value stored in `metadata["terminal_reason"]`.
    pub const fn as_str(self) -> &'static str {
        match self {
            TerminalReason::Cancelled => "cancelled",
            TerminalReason::TimedOut => "timed_out",
        }
    }
}

/// Cooperative window and budget a cancel runs under.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CancelTiming {
    /// Time a running task has to finish after the marker is set.
    pub cooperative: Duration,
    /// Budget the cooperative window must fit inside.
    pub budget: Duration,
}

impl Default for CancelTiming {
    fn default() -> Self {
        Self {
            cooperative: CANCEL_GRACE,
            budget: SHUTDOWN_BUDGET,
        }
    }
}

impl CancelTiming {
    /// Refuses a cooperative window longer than the budget.
    pub fn validate(self) -> JoinResult<()> {
        if self.cooperative > self.budget {
            return Err(JoinError::Timing {
                cooperative: self.cooperative,
                budget: self.budget,
            });
        }
        Ok(())
    }
}

/// The cancel marker: the value written at `cancel/<corr>`.
#[derive(Debug, Clone, PartialEq)]
pub struct CancelMarker {
    /// Marker key, `cancel/<sanitized correlation id>`.
    pub key: String,
    /// Wall-clock time of the request, in seconds since the epoch. Supplied by the caller.
    pub requested_wall: f64,
    /// Human-readable reason for the request.
    pub reason: String,
    /// Cooperative window, in seconds.
    pub grace_s: f64,
}

/// Returns the marker key for `correlation_id`: `cancel/` followed by the id with every character outside
/// `[A-Za-z0-9._-]` replaced by `_`. An id that is empty or only dots is spelled with underscores, so it
/// never addresses a directory. This is the rule `shared_runtime.cancel_flag_path` applies.
pub fn cancel_marker_key(correlation_id: &str) -> String {
    let name: String = correlation_id
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
                c
            } else {
                '_'
            }
        })
        .collect();
    let file = if name.chars().all(|c| c == '.') {
        if name.is_empty() {
            "_".to_string()
        } else {
            name.replace('.', "_")
        }
    } else {
        name
    };
    format!("{CANCEL_KEY_PREFIX}{file}")
}

/// Sets the cancel marker. The sender side of [`cancel_channel`].
pub struct CancelControl {
    tx: watch::Sender<bool>,
}

/// Observes the cancel marker. Cloneable; every running task holds one.
#[derive(Clone)]
pub struct CancelSignal {
    rx: watch::Receiver<bool>,
}

/// Creates a cancel control and its signal. The signal starts not cancelled.
pub fn cancel_channel() -> (CancelControl, CancelSignal) {
    let (tx, rx) = watch::channel(false);
    (CancelControl { tx }, CancelSignal { rx })
}

impl CancelControl {
    /// Sets the marker for `correlation_id` and returns the value written at its key.
    pub fn request(
        &self,
        correlation_id: &str,
        reason: &str,
        requested_wall: f64,
        timing: CancelTiming,
    ) -> CancelMarker {
        let _ = self.tx.send_replace(true);
        CancelMarker {
            key: cancel_marker_key(correlation_id),
            requested_wall,
            reason: reason.to_string(),
            grace_s: timing.cooperative.as_secs_f64(),
        }
    }
}

impl CancelSignal {
    /// Returns `true` once the marker has been set.
    pub fn is_cancelled(&self) -> bool {
        *self.rx.borrow()
    }

    /// Resolves once the marker is set. Pends forever if the control was dropped without cancelling.
    pub async fn cancelled(&self) {
        let mut rx = self.rx.clone();
        if rx.wait_for(|cancelled| *cancelled).await.is_err() {
            std::future::pending::<()>().await;
        }
    }
}

/// Session, join identity and cancel state that one join run shares.
#[derive(Clone)]
pub struct JoinContext {
    /// Session whose threads the join drives.
    pub session: Arc<Mutex<SessionState>>,
    /// Join sequence number within the session.
    pub join_seq: u64,
    /// Attempt of the join, starting at 1.
    pub attempt: u32,
    /// Cooperative window and budget.
    pub timing: CancelTiming,
    /// Cancel marker the run observes.
    pub cancel: CancelSignal,
}

impl JoinContext {
    fn check(&self) -> JoinResult<()> {
        self.timing.validate()?;
        if self.attempt == 0 {
            return Err(JoinError::InvalidAttempt);
        }
        Ok(())
    }

    fn key(&self, stage: u32) -> JoinKey {
        JoinKey {
            stage,
            join_seq: self.join_seq,
            attempt: self.attempt,
        }
    }
}

/// Result of one join run.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct JoinCompletion {
    /// Emission of each stage. Empty when the run was cancelled before any stage merged.
    pub stages: Vec<StageEmit>,
    /// Tasks stopped when the cooperative window closed. Each ends `CANCELLED_TIMED_OUT` (`timed_out`).
    pub forced: Vec<String>,
    /// Tasks that failed inside the cooperative window after the marker. Each ends `CANCELLED_TIMED_OUT`
    /// (`cancelled`).
    pub cancelled: Vec<String>,
    /// Tasks not started because the marker was already set. Their threads stay as they were.
    pub not_started: Vec<String>,
    /// Tasks whose outcome was rate-limited. Their threads stay `RUNNING`; retry belongs to the caller.
    pub rate_limited: Vec<String>,
    /// Tasks the matrix join never dispatched, for example behind a failed dependency.
    pub undispatched: Vec<String>,
    /// Whether the cancel marker was set during the run.
    pub cancel_requested: bool,
}

impl JoinCompletion {
    fn deduplicated(key: JoinKey) -> Self {
        Self {
            stages: vec![StageEmit {
                key,
                emit: Emit::Deduplicated,
            }],
            ..Self::default()
        }
    }
}

/// Outcome a thread ends with, before it is written to the session.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Terminal {
    Succeeded,
    Failed,
    Cancelled(TerminalReason),
}

/// Per-run sets the matrix wrapper fills in as tasks run.
#[derive(Default)]
struct RunState {
    started: Mutex<HashSet<String>>,
    skipped: Mutex<HashSet<String>>,
    forced: Mutex<HashSet<String>>,
    cooperative: Mutex<HashSet<String>>,
}

/// Runs `batch` through the B5 flat matrix join and collects its completion once per key.
///
/// Every batch task must be bound to an IDLE thread. A task that starts is admitted and handed off to
/// `RUNNING` through the B7 helpers, and its end is written as its thread's terminal phase. If the cancel
/// marker is set before a task starts, that task is not run and is listed in `not_started`. A task still
/// running when the cooperative window closes is dropped and ends `CANCELLED_TIMED_OUT` (`timed_out`).
/// If the key is already in `ledger`, nothing runs and the call returns [`Emit::Deduplicated`].
pub async fn collect_matrix_completions<W, Fut>(
    ctx: &JoinContext,
    ledger: &mut CompletionLedger,
    batch: MatrixBatch,
    config: DispatchConfig,
    semaphores: Arc<SemaphoreManager>,
    worker: W,
) -> JoinResult<JoinCompletion>
where
    W: Fn(Task, CancelSignal) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<(), TaskFailure>> + Send + 'static,
{
    ctx.check()?;
    let key = ctx.key(batch.stage);
    if ledger.contains(&key) {
        return Ok(JoinCompletion::deduplicated(key));
    }
    let ids: Vec<String> = batch
        .tasks
        .iter()
        .map(|task| task_id_of(task).to_string())
        .collect();
    prepare_fresh(&ctx.session, &ids)?;

    let run = Arc::new(RunState::default());
    let worker = Arc::new(worker);
    let session = Arc::clone(&ctx.session);
    let cancel = ctx.cancel.clone();
    let timing = ctx.timing;
    let shared = Arc::clone(&run);
    let wrapped = move |task: Task| {
        run_task(
            Arc::clone(&session),
            Arc::clone(&worker),
            task,
            cancel.clone(),
            timing,
            Arc::clone(&shared),
        )
    };
    let report = run_matrix_join(batch, config, semaphores, wrapped).await?;

    let skipped = take(&run.skipped);
    let started = take(&run.started);
    let forced = take(&run.forced);
    let cooperative = take(&run.cooperative);
    let mut records: Vec<CompletionRecord> = Vec::new();
    let mut rate_limited = Vec::new();
    {
        let mut guard = ctx.session.lock();
        for record in report.log.records() {
            let id = record.key.task_id.as_str();
            if skipped.contains(id) {
                continue;
            }
            records.push(record.clone());
            if !started.contains(id) {
                continue;
            }
            let is_forced = forced.contains(id);
            let is_cooperative = cooperative.contains(id);
            match terminal_for(&record.outcome, is_forced, is_cooperative) {
                Some(terminal) => apply_terminal_to_task(&mut guard, id, terminal)?,
                None => rate_limited.push(id.to_string()),
            }
        }
    }

    let emit = ledger.emit(key, CompletionLog::from_records(records));
    Ok(JoinCompletion {
        stages: vec![StageEmit { key, emit }],
        forced: sorted(forced),
        cancelled: sorted(cooperative),
        not_started: sorted(skipped),
        rate_limited,
        undispatched: report.undispatched,
        cancel_requested: ctx.cancel.is_cancelled(),
    })
}

/// Runs a B6 nexus barrier join through `run` and collects each stage once per key.
///
/// `run` performs the staged fan-out and fan-in, normally by calling `run_nexus_barrier_join` with the
/// caller's transport. This keeps the Nexus endpoints out of `gsd-state`. Every thread of `plan` must be
/// bound and IDLE. All of them are started before `run` is awaited. If the cancel window closes first,
/// `run` is dropped and each started thread ends `CANCELLED_TIMED_OUT` (`timed_out`). If any stage key is
/// already in `ledger`, the whole run is deduplicated and `run` is not called.
pub async fn collect_nexus_completions<F, Fut>(
    ctx: &JoinContext,
    ledger: &mut CompletionLedger,
    plan: &NexusPlan,
    run: F,
) -> JoinResult<JoinCompletion>
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = SchedulerResult<NexusJoinReport>>,
{
    ctx.check()?;
    let stage_count = u32::try_from(plan.stages.len())
        .map_err(|_| JoinError::TooManyStages(plan.stages.len()))?;
    let keys: Vec<JoinKey> = (0..stage_count).map(|stage| ctx.key(stage)).collect();
    if keys.iter().any(|key| ledger.contains(key)) {
        return Ok(JoinCompletion {
            stages: keys
                .iter()
                .map(|key| StageEmit {
                    key: *key,
                    emit: Emit::Deduplicated,
                })
                .collect(),
            ..JoinCompletion::default()
        });
    }

    let ids: Vec<String> = plan
        .stages
        .iter()
        .flatten()
        .map(|task| task_id_of(task).to_string())
        .collect::<BTreeSet<String>>()
        .into_iter()
        .collect();
    prepare_fresh(&ctx.session, &ids)?;
    for id in &ids {
        start_task(&ctx.session, id)?;
    }

    let window = forced_after(ctx.cancel.clone(), ctx.timing.cooperative);
    let joined = tokio::select! {
        biased;
        result = run() => Some(result),
        () = window => None,
    };

    let mut stages = Vec::new();
    let mut forced = Vec::new();
    match joined {
        Some(result) => {
            let report = result?;
            for merge in &report.stages {
                let key = ctx.key(merge.stage);
                {
                    let mut guard = ctx.session.lock();
                    for record in merge.log.records() {
                        if let Some(terminal) = terminal_for(&record.outcome, false, false) {
                            apply_terminal_to_task(&mut guard, &record.key.task_id, terminal)?;
                        }
                    }
                }
                let emit = ledger.emit(key, merge.log.clone());
                stages.push(StageEmit { key, emit });
            }
        }
        None => {
            {
                let mut guard = ctx.session.lock();
                for id in &ids {
                    apply_terminal_to_task(
                        &mut guard,
                        id,
                        Terminal::Cancelled(TerminalReason::TimedOut),
                    )?;
                }
            }
            forced = ids;
        }
    }

    Ok(JoinCompletion {
        stages,
        forced,
        cancel_requested: ctx.cancel.is_cancelled(),
        ..JoinCompletion::default()
    })
}

/// Runs one matrix task under the cancel window and returns its result to the matrix join.
async fn run_task<W, Fut>(
    session: Arc<Mutex<SessionState>>,
    worker: Arc<W>,
    task: Task,
    cancel: CancelSignal,
    timing: CancelTiming,
    run: Arc<RunState>,
) -> Result<(), TaskFailure>
where
    W: Fn(Task, CancelSignal) -> Fut + Send + Sync,
    Fut: Future<Output = Result<(), TaskFailure>> + Send,
{
    let id = task_id_of(&task).to_string();
    if cancel.is_cancelled() {
        run.skipped.lock().insert(id);
        return Ok(());
    }
    start_task(&session, &id).map_err(|error| TaskFailure::Exception(error.to_string()))?;
    run.started.lock().insert(id.clone());

    let window = forced_after(cancel.clone(), timing.cooperative);
    tokio::select! {
        biased;
        result = (*worker)(task, cancel.clone()) => {
            if result.is_err() && cancel.is_cancelled() {
                run.cooperative.lock().insert(id);
            }
            result
        }
        () = window => {
            run.forced.lock().insert(id);
            Err(TaskFailure::Exception(format!(
                "stopped when the cancel window of {:?} closed",
                timing.cooperative
            )))
        }
    }
}

/// Resolves once the marker is set and the cooperative window has elapsed after it.
async fn forced_after(cancel: CancelSignal, cooperative: Duration) {
    cancel.cancelled().await;
    sleep(cooperative).await;
}

/// Refuses unless every task in `ids` is bound to a thread that is IDLE.
fn prepare_fresh(session: &Mutex<SessionState>, ids: &[String]) -> JoinResult<()> {
    let guard = session.lock();
    for id in ids {
        let thread_id =
            thread_for_task(&guard, id).ok_or_else(|| DispatchError::Unbound(id.clone()))?;
        let thread = guard
            .threads
            .get(&thread_id)
            .ok_or_else(|| DispatchError::UnknownThread(thread_id.clone()))?;
        let phase = phase_of(thread, &thread_id)?;
        if phase != ThreadPhase::Idle {
            return Err(JoinError::NotIdle {
                thread: thread_id,
                phase: phase.as_str(),
            });
        }
    }
    Ok(())
}

/// Admits and hands off the thread serving `task_id`, as the B7 dispatch wrapper does.
fn start_task(session: &Mutex<SessionState>, task_id: &str) -> JoinResult<()> {
    let mut guard = session.lock();
    let thread_id = thread_for_task(&guard, task_id)
        .ok_or_else(|| DispatchError::Unbound(task_id.to_string()))?;
    let phase = {
        let thread = guard
            .threads
            .get(&thread_id)
            .ok_or_else(|| DispatchError::UnknownThread(thread_id.clone()))?;
        phase_of(thread, &thread_id)?
    };
    if phase == ThreadPhase::Idle {
        admit_thread(&mut guard, &thread_id)?;
    }
    handoff_thread(&mut guard, &thread_id)?;
    Ok(())
}

/// Returns the thread's phase, read from its [`PHASE_KEY`] metadata.
pub(crate) fn phase_of(
    thread: &ThreadState,
    thread_id: &str,
) -> Result<ThreadPhase, DispatchError> {
    let raw = thread.metadata.get(PHASE_KEY).map_or("", String::as_str);
    ALL_PHASES
        .into_iter()
        .find(|phase| phase.as_str() == raw)
        .ok_or_else(|| DispatchError::UnknownPhase {
            thread: thread_id.to_string(),
            value: raw.to_string(),
        })
}

/// Writes `terminal` to thread `thread_id` through the B1 transition table, and sets its proto state and
/// `terminal_reason` when it is a cancel.
pub(crate) fn apply_terminal(
    state: &mut SessionState,
    thread_id: &str,
    terminal: Terminal,
) -> JoinResult<()> {
    let thread = state
        .threads
        .get_mut(thread_id)
        .ok_or_else(|| DispatchError::UnknownThread(thread_id.to_string()))?;
    let from = phase_of(thread, thread_id)?;
    let (to, kind) = match terminal {
        Terminal::Succeeded => (ThreadPhase::Succeeded, ThreadStateKind::Completed),
        Terminal::Failed => (ThreadPhase::Failed, ThreadStateKind::Failed),
        Terminal::Cancelled(_) => (ThreadPhase::CancelledTimedOut, ThreadStateKind::Terminated),
    };
    let next = transition(from, to).map_err(DispatchError::from)?;
    thread
        .metadata
        .insert(PHASE_KEY.to_string(), next.as_str().to_string());
    if let Terminal::Cancelled(reason) = terminal {
        thread
            .metadata
            .insert(TERMINAL_REASON_KEY.to_string(), reason.as_str().to_string());
    }
    thread.state = kind as i32;
    Ok(())
}

/// Writes `terminal` to the thread that serves task `task_id`, resolved through the task's binding.
fn apply_terminal_to_task(
    state: &mut SessionState,
    task_id: &str,
    terminal: Terminal,
) -> JoinResult<()> {
    let thread_id = thread_for_task(state, task_id)
        .ok_or_else(|| DispatchError::Unbound(task_id.to_string()))?;
    apply_terminal(state, &thread_id, terminal)
}

/// Maps a completion outcome to its terminal phase. `None` leaves the thread `RUNNING`.
fn terminal_for(outcome: &TaskOutcome, forced: bool, cooperative: bool) -> Option<Terminal> {
    if forced {
        return Some(Terminal::Cancelled(TerminalReason::TimedOut));
    }
    if cooperative {
        return Some(Terminal::Cancelled(TerminalReason::Cancelled));
    }
    match outcome {
        TaskOutcome::Completed => Some(Terminal::Succeeded),
        TaskOutcome::Failed { .. } => Some(Terminal::Failed),
        TaskOutcome::RateLimited { .. } => None,
    }
}

fn task_id_of(task: &Task) -> &str {
    task.id.as_ref().map_or("", |id| id.value.as_str())
}

fn take(set: &Mutex<HashSet<String>>) -> HashSet<String> {
    std::mem::take(&mut *set.lock())
}

fn sorted(set: HashSet<String>) -> Vec<String> {
    let mut values: Vec<String> = set.into_iter().collect();
    values.sort();
    values
}

#[cfg(test)]
pub(crate) mod test_fixtures {
    //! Session fixtures shared by the join and teardown tests.

    use gsd_governor::{Governor, GovernorConfig};
    use gsd_proto::gsd::v1::{
        Budget, BudgetRequest, CapabilityManifest, ChatThreadId, SessionId, SessionState,
        SubagentType, Task, TaskId, WorkflowDef,
    };

    use crate::session_create::{bind_thread, create_session, ThreadBinding};

    /// Agent type every fixture task and thread uses.
    pub(crate) const AGENT: &str = "w";

    /// Builds a task `id` of the fixture agent type.
    pub(crate) fn task(id: &str) -> Task {
        Task {
            id: Some(TaskId {
                value: id.to_string(),
            }),
            subagent_type: Some(SubagentType {
                value: AGENT.to_string(),
            }),
            ..Default::default()
        }
    }

    /// Creates session `s1` over the tasks named in `pairs` and binds each `(thread, task)` pair, each
    /// reserving `tokens` from the governor.
    pub(crate) async fn bound_state(
        pairs: &[(&str, &str)],
        tokens: i64,
    ) -> Result<SessionState, Box<dyn std::error::Error>> {
        bound_state_with(&Governor::new(GovernorConfig::default()), pairs, tokens).await
    }

    /// As [`bound_state`], but reserves through `governor`, so the caller can settle the same reservations.
    pub(crate) async fn bound_state_with(
        governor: &Governor,
        pairs: &[(&str, &str)],
        tokens: i64,
    ) -> Result<SessionState, Box<dyn std::error::Error>> {
        let workflow = WorkflowDef {
            name: "wf".to_string(),
            tasks: pairs.iter().map(|(_, task_id)| task(task_id)).collect(),
            budgets: Some(Budget {
                max_tokens: 10_000,
                max_wall_time_ms: 60_000,
                max_tool_calls: 100,
                max_cost_usd: 10.0,
            }),
            ..Default::default()
        };
        let mut state = create_session(
            SessionId {
                value: "s1".to_string(),
            },
            workflow,
        )?;
        for (thread, task_id) in pairs {
            bind_thread(
                &mut state,
                &governor,
                ThreadBinding {
                    thread_id: ChatThreadId {
                        value: thread.to_string(),
                    },
                    task_id: TaskId {
                        value: task_id.to_string(),
                    },
                    manifest: CapabilityManifest {
                        subagent_type: Some(SubagentType {
                            value: AGENT.to_string(),
                        }),
                        ..Default::default()
                    },
                    reservation: BudgetRequest {
                        tokens,
                        ..Default::default()
                    },
                },
            )
            .await?;
        }
        Ok(state)
    }
}

#[cfg(test)]
mod tests {
    use super::test_fixtures::{bound_state, task, AGENT};
    use super::*;
    use gsd_scheduler::{CompletionKey, JoinTerminal, Provenance, StageMerge, VerdictEnvelope};
    use std::collections::{BTreeMap, HashMap};
    use std::future::Ready;
    use std::sync::atomic::{AtomicUsize, Ordering};

    type TestResult = Result<(), Box<dyn std::error::Error>>;

    fn semaphores() -> Arc<SemaphoreManager> {
        Arc::new(SemaphoreManager::new())
    }

    fn shared(state: SessionState) -> Arc<Mutex<SessionState>> {
        Arc::new(Mutex::new(state))
    }

    fn context(
        session: &Arc<Mutex<SessionState>>,
        join_seq: u64,
        timing: CancelTiming,
        cancel: CancelSignal,
    ) -> JoinContext {
        JoinContext {
            session: Arc::clone(session),
            join_seq,
            attempt: 1,
            timing,
            cancel,
        }
    }

    fn phase(
        session: &Arc<Mutex<SessionState>>,
        thread: &str,
    ) -> Result<ThreadPhase, Box<dyn std::error::Error>> {
        let guard = session.lock();
        let state = guard
            .threads
            .get(thread)
            .ok_or("thread missing from session")?;
        Ok(phase_of(state, thread)?)
    }

    fn metadata_of(session: &Arc<Mutex<SessionState>>, thread: &str) -> HashMap<String, String> {
        let guard = session.lock();
        guard
            .threads
            .get(thread)
            .map(|state| state.metadata.clone())
            .unwrap_or_default()
    }

    fn state_kind(session: &Arc<Mutex<SessionState>>, thread: &str) -> i32 {
        let guard = session.lock();
        guard.threads.get(thread).map_or(-1, |state| state.state)
    }

    fn batch(stage: u32, ids: &[&str]) -> MatrixBatch {
        MatrixBatch {
            stage,
            tasks: ids.iter().map(|id| task(id)).collect(),
            attempts: HashMap::new(),
        }
    }

    /// Worker that counts its calls and succeeds at once.
    fn counting(
        calls: Arc<AtomicUsize>,
    ) -> impl Fn(Task, CancelSignal) -> Ready<Result<(), TaskFailure>> + Send + Sync + 'static {
        move |_task: Task, _cancel: CancelSignal| {
            calls.fetch_add(1, Ordering::SeqCst);
            std::future::ready(Ok(()))
        }
    }

    fn stage_report(stage: u32, records: Vec<(&str, TaskOutcome)>) -> NexusJoinReport {
        let records = records
            .into_iter()
            .map(|(id, outcome)| CompletionRecord {
                key: CompletionKey {
                    stage,
                    depth: 0,
                    creation: 0,
                    task_id: id.to_string(),
                },
                provenance: Provenance {
                    producer: AGENT.to_string(),
                    attempt: 1,
                    verdict: VerdictEnvelope::from_outcome(&outcome),
                },
                outcome,
            })
            .collect();
        NexusJoinReport {
            stages: vec![StageMerge {
                stage,
                log: CompletionLog::from_records(records),
                outputs: BTreeMap::new(),
            }],
            partial_timeouts: Vec::new(),
            failed: Vec::new(),
            terminal: JoinTerminal::PartialSuccess,
        }
    }

    #[test]
    fn cancel_marker_key_sanitizes_like_the_runner() {
        assert_eq!(cancel_marker_key("run/1 x"), "cancel/run_1_x");
        assert_eq!(cancel_marker_key("a.b-c_d"), "cancel/a.b-c_d");
        assert_eq!(cancel_marker_key(".."), "cancel/__");
        assert_eq!(cancel_marker_key(""), "cancel/_");
    }

    #[test]
    fn default_timing_is_ten_inside_fifteen() {
        let timing = CancelTiming::default();
        assert_eq!(timing.cooperative, Duration::from_secs(10));
        assert_eq!(timing.budget, Duration::from_secs(15));
        assert!(timing.validate().is_ok());
        let inverted = CancelTiming {
            cooperative: Duration::from_secs(16),
            budget: Duration::from_secs(15),
        };
        assert!(inverted.validate().is_err());
    }

    #[test]
    fn ledger_drops_a_second_emission_under_the_same_key() {
        let mut ledger = CompletionLedger::default();
        let key = JoinKey {
            stage: 0,
            join_seq: 1,
            attempt: 1,
        };
        let first = CompletionLog::from_records(Vec::new());
        assert_eq!(ledger.emit(key, first.clone()), Emit::Collected);
        let late = CompletionRecord {
            key: CompletionKey {
                stage: 0,
                depth: 0,
                creation: 0,
                task_id: "late".to_string(),
            },
            provenance: Provenance {
                producer: AGENT.to_string(),
                attempt: 1,
                verdict: VerdictEnvelope::from_outcome(&TaskOutcome::Completed),
            },
            outcome: TaskOutcome::Completed,
        };
        assert_eq!(
            ledger.emit(key, CompletionLog::from_records(vec![late])),
            Emit::Deduplicated
        );
        assert_eq!(ledger.len(), 1);
        assert_eq!(ledger.log(&key), Some(&first));
        // A different attempt is a different key and is collected.
        let retry = JoinKey { attempt: 2, ..key };
        assert_eq!(ledger.emit(retry, first), Emit::Collected);
        assert_eq!(ledger.len(), 2);
    }

    #[tokio::test]
    async fn completion_is_collected_once_and_duplicates_are_dropped() -> TestResult {
        let session = shared(bound_state(&[("th1", "t1"), ("th2", "t2")], 1).await?);
        let (_control, signal) = cancel_channel();
        let ctx = context(&session, 1, CancelTiming::default(), signal);
        let mut ledger = CompletionLedger::default();
        let calls = Arc::new(AtomicUsize::new(0));

        let first = collect_matrix_completions(
            &ctx,
            &mut ledger,
            batch(0, &["t1", "t2"]),
            DispatchConfig::default(),
            semaphores(),
            counting(Arc::clone(&calls)),
        )
        .await?;
        assert_eq!(
            first.stages,
            vec![StageEmit {
                key: JoinKey {
                    stage: 0,
                    join_seq: 1,
                    attempt: 1,
                },
                emit: Emit::Collected,
            }]
        );
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert_eq!(phase(&session, "th1")?, ThreadPhase::Succeeded);
        assert_eq!(phase(&session, "th2")?, ThreadPhase::Succeeded);

        // Re-emitting the same (stage, join seq, attempt) key neither reruns nor reapplies.
        let second = collect_matrix_completions(
            &ctx,
            &mut ledger,
            batch(0, &["t1", "t2"]),
            DispatchConfig::default(),
            semaphores(),
            counting(Arc::clone(&calls)),
        )
        .await?;
        assert_eq!(second.stages[0].emit, Emit::Deduplicated);
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert_eq!(ledger.len(), 1);
        Ok(())
    }

    #[tokio::test]
    async fn cancel_past_the_cooperative_window_ends_cancelled_timed_out() -> TestResult {
        let session = shared(bound_state(&[("th1", "t1")], 1).await?);
        let (control, signal) = cancel_channel();
        let timing = CancelTiming {
            cooperative: Duration::from_millis(30),
            budget: Duration::from_millis(500),
        };
        let ctx = context(&session, 7, timing, signal);
        let mut ledger = CompletionLedger::default();
        let never =
            |_task: Task, _cancel: CancelSignal| std::future::pending::<Result<(), TaskFailure>>();
        let request = async {
            sleep(Duration::from_millis(10)).await;
            control.request("corr-1", "test", 0.0, timing)
        };

        let (joined, marker) = tokio::join!(
            collect_matrix_completions(
                &ctx,
                &mut ledger,
                batch(0, &["t1"]),
                DispatchConfig::default(),
                semaphores(),
                never,
            ),
            request
        );
        let completion = joined?;

        assert_eq!(marker.key, "cancel/corr-1");
        assert_eq!(completion.forced, vec!["t1".to_string()]);
        assert!(completion.cancel_requested);
        assert_eq!(phase(&session, "th1")?, ThreadPhase::CancelledTimedOut);
        assert_eq!(
            metadata_of(&session, "th1")
                .get(TERMINAL_REASON_KEY)
                .map(String::as_str),
            Some("timed_out")
        );
        assert_eq!(
            state_kind(&session, "th1"),
            ThreadStateKind::Terminated as i32
        );
        Ok(())
    }

    #[tokio::test]
    async fn worker_that_stops_inside_the_window_ends_cancelled_with_reason() -> TestResult {
        let session = shared(bound_state(&[("th1", "t1")], 1).await?);
        let (control, signal) = cancel_channel();
        let timing = CancelTiming {
            cooperative: Duration::from_secs(5),
            budget: Duration::from_secs(6),
        };
        let ctx = context(&session, 2, timing, signal);
        let mut ledger = CompletionLedger::default();
        let cooperative = |_task: Task, cancel: CancelSignal| async move {
            cancel.cancelled().await;
            Err::<(), TaskFailure>(TaskFailure::Exception("stopped at cancel".to_string()))
        };
        let request = async {
            sleep(Duration::from_millis(10)).await;
            control.request("corr-2", "test", 0.0, timing)
        };

        let (joined, _marker) = tokio::join!(
            collect_matrix_completions(
                &ctx,
                &mut ledger,
                batch(0, &["t1"]),
                DispatchConfig::default(),
                semaphores(),
                cooperative,
            ),
            request
        );
        let completion = joined?;

        assert_eq!(completion.cancelled, vec!["t1".to_string()]);
        assert!(completion.forced.is_empty());
        assert_eq!(phase(&session, "th1")?, ThreadPhase::CancelledTimedOut);
        assert_eq!(
            metadata_of(&session, "th1")
                .get(TERMINAL_REASON_KEY)
                .map(String::as_str),
            Some("cancelled")
        );
        Ok(())
    }

    #[tokio::test]
    async fn task_not_started_after_cancel_is_left_untouched() -> TestResult {
        let session = shared(bound_state(&[("th1", "t1")], 1).await?);
        let (control, signal) = cancel_channel();
        let timing = CancelTiming::default();
        let _marker = control.request("corr-3", "test", 0.0, timing);
        let ctx = context(&session, 3, timing, signal);
        let mut ledger = CompletionLedger::default();
        let calls = Arc::new(AtomicUsize::new(0));

        let completion = collect_matrix_completions(
            &ctx,
            &mut ledger,
            batch(0, &["t1"]),
            DispatchConfig::default(),
            semaphores(),
            counting(Arc::clone(&calls)),
        )
        .await?;

        assert_eq!(calls.load(Ordering::SeqCst), 0);
        assert_eq!(completion.not_started, vec!["t1".to_string()]);
        assert_eq!(phase(&session, "th1")?, ThreadPhase::Idle);
        Ok(())
    }

    #[tokio::test]
    async fn nexus_completion_applies_outcomes_and_dedups_the_rerun() -> TestResult {
        let session = shared(bound_state(&[("th1", "t1"), ("th2", "t2")], 1).await?);
        let (_control, signal) = cancel_channel();
        let ctx = context(&session, 4, CancelTiming::default(), signal);
        let mut ledger = CompletionLedger::default();
        let plan = NexusPlan {
            join_id: "j".to_string(),
            stages: vec![vec![task("t1"), task("t2")]],
        };
        let calls = Arc::new(AtomicUsize::new(0));
        let report = || {
            stage_report(
                0,
                vec![
                    ("t1", TaskOutcome::Completed),
                    (
                        "t2",
                        TaskOutcome::Failed {
                            code: "E-TIMEOUT",
                            message: "partial-join timeout".to_string(),
                        },
                    ),
                ],
            )
        };

        let counter = Arc::clone(&calls);
        let first = collect_nexus_completions(&ctx, &mut ledger, &plan, || {
            counter.fetch_add(1, Ordering::SeqCst);
            std::future::ready(Ok::<NexusJoinReport, SchedulerError>(report()))
        })
        .await?;
        assert_eq!(first.stages[0].emit, Emit::Collected);
        assert_eq!(phase(&session, "th1")?, ThreadPhase::Succeeded);
        // A nexus straggler is FAILED, not CANCELLED_TIMED_OUT (n3 hook 3).
        assert_eq!(phase(&session, "th2")?, ThreadPhase::Failed);

        let counter = Arc::clone(&calls);
        let second = collect_nexus_completions(&ctx, &mut ledger, &plan, || {
            counter.fetch_add(1, Ordering::SeqCst);
            std::future::ready(Ok::<NexusJoinReport, SchedulerError>(report()))
        })
        .await?;
        assert_eq!(second.stages[0].emit, Emit::Deduplicated);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        Ok(())
    }

    #[tokio::test]
    async fn nexus_cancel_past_the_window_stops_every_started_thread() -> TestResult {
        let session = shared(bound_state(&[("th1", "t1"), ("th2", "t2")], 1).await?);
        let (control, signal) = cancel_channel();
        let timing = CancelTiming {
            cooperative: Duration::from_millis(30),
            budget: Duration::from_millis(500),
        };
        let ctx = context(&session, 5, timing, signal);
        let mut ledger = CompletionLedger::default();
        let plan = NexusPlan {
            join_id: "j".to_string(),
            stages: vec![vec![task("t1")], vec![task("t2")]],
        };
        let request = async {
            sleep(Duration::from_millis(10)).await;
            control.request("corr-4", "test", 0.0, timing)
        };

        let (joined, _marker) = tokio::join!(
            collect_nexus_completions(&ctx, &mut ledger, &plan, || {
                std::future::pending::<SchedulerResult<NexusJoinReport>>()
            }),
            request
        );
        let completion = joined?;

        assert_eq!(completion.forced, vec!["t1".to_string(), "t2".to_string()]);
        assert!(completion.stages.is_empty());
        assert!(ledger.is_empty());
        for thread in ["th1", "th2"] {
            assert_eq!(phase(&session, thread)?, ThreadPhase::CancelledTimedOut);
            assert_eq!(
                metadata_of(&session, thread)
                    .get(TERMINAL_REASON_KEY)
                    .map(String::as_str),
                Some("timed_out")
            );
        }
        Ok(())
    }

    #[test]
    fn every_terminal_this_module_writes_is_legal_from_running() {
        for to in [
            ThreadPhase::Succeeded,
            ThreadPhase::Failed,
            ThreadPhase::CancelledTimedOut,
        ] {
            assert!(gsd_pool::is_legal_transition(ThreadPhase::Running, to));
        }
    }
}
