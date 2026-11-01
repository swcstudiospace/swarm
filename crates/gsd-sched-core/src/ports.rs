//! The ports contract of the Rust scheduler runtime (Phase 14.4, unit S2.1).
//!
//! The decision loop in this crate never touches the Task Store, telemetry, leases, handoffs or sessions. Everything it needs from
//! the host is one method of [`SchedulerPorts`], and everything that crosses the boundary is one of the plain data types below. The
//! host is the Python scheduler (`scripts/swarm_run.py`, `swarm/rust_scheduler.py`), which stays the oracle: each port wraps the
//! Python code named in its documentation, and each type carries exactly what that code passes around.
//!
//! # Threads, the interpreter lock and blocking
//!
//! Two kinds of threads call ports:
//!
//! - the **loop thread**, the thread that called the run entry (the interpreter's main thread, so Python signal handlers run);
//! - **pool threads**, owned by this crate, one per running worker.
//!
//! Every call into the Python side attaches the interpreter lock (the GIL), so **any port may block**: for as long as the GIL is
//! contended, for a SQLite commit, for a network round trip to the lease substrate, and for [`SchedulerPorts::worker`] for the whole
//! length of a model session (minutes). While a loop-thread port runs, the loop does not poll the cancel state: a slow port delays a
//! cancel by its own duration, exactly as in the Python loop.
//!
//! **Lock-order rule: no Rust lock, channel guard or condition-variable guard may be held across a port call.** A port that waits for
//! the GIL while the calling thread holds a Rust lock that another GIL-holding thread is waiting for is a deadlock. Callers copy what
//! they need out of shared state, release the guard, then call the port.
//!
//! Ports are methods on one shared object, so the trait is `Send + Sync` and every method takes `&self`; an implementation guards its
//! own mutable state. A port that fails returns [`PortError`], which keeps the original Python exception so the binding can re-raise
//! it with its type and traceback. The loop stops on an `Err` from any loop-thread port; see [`RunError`].
//!
//! # Errors of the core itself
//!
//! Faults of this crate (an invalid [`RunConfig`], an internal invariant) are a [`SchedError`]: a [`SchedCode`] and a message with
//! no echoed input. The binding turns one into `ValueError("<wire code>: run_scheduler failed: <detail>")` with
//! [`SchedError::wire_message`], the convention of the other `swarm._native` functions.

use std::any::Any;
use std::error::Error;
use std::fmt;

/// The longest duration, in seconds, [`RunConfig::validate`] accepts: about 31 years. It keeps every `Duration` and `Instant`
/// computation of the runtime free of overflow; no real grace, tick or backoff comes near it.
pub const MAX_SECONDS: f64 = 1.0e9;

/// Longest `reason` text, in characters, that the loop hands to [`SchedulerPorts::block`] (the Python `[:500]` slice).
pub const BLOCK_REASON_MAX_CHARS: usize = 500;

/// The `reason` of [`SchedulerPorts::block`] for a task whose agent cannot be resolved (`select_batch` in `swarm_run.py`).
pub const BLOCK_REASON_NO_AGENT: &str = "no agent for capability";

/// A fault code of the scheduler core. These are the wire codes of the `gsd-codes` crate that the binding already uses; this crate
/// is std-only and does not depend on it, so the mapping is by spelling and is checked by [`SchedCode::as_str`]'s test and by the
/// binding's tests.
///
/// | variant | `gsd_codes::ErrorCode` | wire text |
/// |---|---|---|
/// | [`SchedCode::Input`] | `Input` | `E-INPUT` |
/// | [`SchedCode::Internal`] | `Internal` | `E-INTERNAL` |
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[non_exhaustive]
pub enum SchedCode {
    /// A value handed to the run entry is invalid (`E-INPUT`): the configuration.
    Input,
    /// A fault of the runtime itself (`E-INTERNAL`): a panic or a broken invariant. Its message never carries a panic payload.
    Internal,
}

impl SchedCode {
    /// The wire code, for example `"E-INPUT"`.
    pub fn as_str(self) -> &'static str {
        match self {
            SchedCode::Input => "E-INPUT",
            SchedCode::Internal => "E-INTERNAL",
        }
    }
}

/// A fault of the scheduler core: a [`SchedCode`] and a message that names the rule at fault and never echoes task ids, paths or
/// any value supplied by Python.
///
/// ```
/// use gsd_sched_core::ports::{SchedCode, SchedError};
///
/// let error = SchedError::new(SchedCode::Input, "max_concurrent must be >= 1");
/// assert_eq!(
///     error.wire_message("run_scheduler"),
///     "E-INPUT: run_scheduler failed: max_concurrent must be >= 1"
/// );
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SchedError {
    code: SchedCode,
    message: String,
}

impl SchedError {
    /// A fault with `code` and `message`.
    pub fn new(code: SchedCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    /// The fault code.
    pub fn code(&self) -> SchedCode {
        self.code
    }

    /// The message, without the code.
    pub fn message(&self) -> &str {
        &self.message
    }

    /// The text of the `ValueError` the binding raises: `"<wire code>: <operation> failed: <detail>"`. A message that already starts
    /// with `"<wire code>: "` keeps only what follows, as `wire_error_message` in `gsd-python` does.
    pub fn wire_message(&self, operation: &str) -> String {
        let code = self.code.as_str();
        let detail = self
            .message
            .strip_prefix(code)
            .and_then(|rest| rest.strip_prefix(": "))
            .unwrap_or(&self.message);
        format!("{code}: {operation} failed: {detail}")
    }
}

impl fmt::Display for SchedError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code.as_str(), self.message)
    }
}

impl Error for SchedError {}

/// A failure that came back through a port: a Python exception the binding caught, kept opaque.
///
/// `summary` is a single line chosen by the binding (exception type and message, already free of anything the run must not
/// log); the core uses it in [`SchedulerPorts::internal_failure`] and in its own log lines. `payload` is whatever the binding needs to
/// re-raise the original exception with its type and traceback (a `Py<PyBaseException>` in the binding); the core never opens it
/// and moves it out unchanged with [`PortError::into_payload`]. The payload is `Send` because a pool thread may produce it and the
/// loop thread re-raises it.
pub struct PortError {
    summary: String,
    payload: Box<dyn Any + Send>,
}

impl PortError {
    /// A port failure with a one-line `summary` and the opaque `payload` to re-raise.
    pub fn new(summary: impl Into<String>, payload: Box<dyn Any + Send>) -> Self {
        Self {
            summary: summary.into(),
            payload,
        }
    }

    /// The one-line description of the failure.
    pub fn summary(&self) -> &str {
        &self.summary
    }

    /// Takes the opaque payload out, to be re-raised by the binding.
    pub fn into_payload(self) -> Box<dyn Any + Send> {
        self.payload
    }
}

impl fmt::Debug for PortError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PortError")
            .field("summary", &self.summary)
            .finish_non_exhaustive()
    }
}

impl fmt::Display for PortError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.summary)
    }
}

impl Error for PortError {}

/// Why a run ended without a summary: the core's own fault, or a port failure that must reach Python with its original type.
#[derive(Debug)]
pub enum RunError {
    /// An invalid configuration or an internal fault; raised as `ValueError` through [`SchedError::wire_message`].
    Sched(SchedError),
    /// A loop-thread port failed: the loop stopped, and the binding re-raises the original exception (`SwarmError` keeps its exit
    /// code mapping that way).
    Port(PortError),
}

impl From<SchedError> for RunError {
    fn from(error: SchedError) -> Self {
        RunError::Sched(error)
    }
}

impl From<PortError> for RunError {
    fn from(error: PortError) -> Self {
        RunError::Port(error)
    }
}

impl fmt::Display for RunError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RunError::Sched(error) => error.fmt(f),
            RunError::Port(error) => error.fmt(f),
        }
    }
}

impl Error for RunError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            RunError::Sched(error) => Some(error),
            RunError::Port(error) => Some(error),
        }
    }
}

/// One dispatchable task as the Python `dispatchable` returns it, reduced to what the pass needs.
///
/// Rust orders rows with the Python `order_ready` rule (priority rank with an unknown or missing priority ranked as `P2`, then
/// `dag_depth` ascending, then `created_at` ascending, stable) and filters them by `retry_after` and the internal-failure count.
#[derive(Debug, Clone, PartialEq)]
pub struct ReadyRow {
    /// The task id. Unique among the rows of one pass.
    pub task_id: String,
    /// The `priority` column, verbatim: `P0` to `P3` normally, but the column is unvalidated text, so anything (and `None`) can
    /// arrive. Rust ranks every value outside `P0` to `P3` as `P2`.
    pub priority: Option<String>,
    /// The `dag_depth` column; `None` ranks as 0 (the Python `t.get("dag_depth") or 0`).
    pub dag_depth: Option<i64>,
    /// The `created_at` column in seconds; `None` ranks as 0 (the Python `... or 0`).
    pub created_at: Option<f64>,
    /// `notes.retry_after`, a wall-clock time in seconds since the epoch, only when it is a real number (Python checks
    /// `isinstance(..., (int, float))` and excludes `bool`, so the Python side sends `None` for anything else). The task is
    /// held back while this is greater than the current wall time; a NaN never holds a task back.
    pub retry_after: Option<f64>,
}

/// The manifest agent a task is dispatched to (the Python `resolve_agent` result, reduced).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentInfo {
    /// The agent id (`agent["id"]`): the key of the per-class replica count and the argument of [`SchedulerPorts::lease`],
    /// [`SchedulerPorts::submit`] and [`SchedulerPorts::worker`].
    pub agent_id: String,
    /// The agent slug (`agent["slug"]`), only for the telemetry the Python side writes.
    pub slug: String,
    /// The class ceiling: how many sessions of this agent may run at once. The Python side sends
    /// `int(agent.get("max_parallel") or 1)`; Rust treats a value below 1 as 1 (`max(1, ...)`).
    pub max_parallel: u32,
}

/// One claim of a task: a normalized path pattern and its mode (the Python `Claim`).
///
/// The Python parser (`swarm.claims.parse_claims`) is the only parser and has already normalized the pattern; Rust feeds the
/// pair to its claim table as is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClaimSpec {
    /// The normalized glob pattern (`Claim.pattern`).
    pub pattern: String,
    /// `true` for mode `shared`, `false` for `exclusive` (`Claim.mode`).
    pub shared: bool,
}

/// The answer of [`SchedulerPorts::claims_for`]: what `parse_claims(task["notes_json"])` produced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClaimsVerdict {
    /// The task's claims, in notes order; empty when the task declares none.
    Claims(Vec<ClaimSpec>),
    /// `parse_claims` raised `ValueError`; the text is `str(error)`. The loop blocks the task with the reason
    /// `E-INPUT: invalid notes.claims: <text>`, cut to [`BLOCK_REASON_MAX_CHARS`] characters (a malformed claim blocks the task
    /// rather than running it unclaimed).
    Invalid(String),
}

/// The answer of [`SchedulerPorts::lease`] when leases are on (the Python `Claim` of `leases.acquire`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LeaseVerdict {
    /// `claim.dispatch`: the task holds its node (or runs unleased because the substrate cannot answer) and may start. When
    /// `false` the task is left where it is, takes no slot, and its claims are released.
    pub dispatch: bool,
    /// `claim.status` (`held`, `denied`, `busy`, ...). Rust uses it only for the skip reason `lease:<status>` and the waiting
    /// line `<task id>: <status>` (plus ` (held by <holder>)` when a holder is known).
    pub status: String,
    /// `claim.holder["session_id"]` when a holder is known.
    pub holder: Option<String>,
}

/// One task left out of a pass, with the reason (the values of the Python `skipped` dict).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkippedTask {
    /// The task id.
    pub task_id: String,
    /// `backoff:<s>s`, `internal-failure`, `no-free-slot`, `class-ceiling:<agent>`, `claim-conflict:<holder>` or `lease:<status>`.
    pub reason: String,
}

/// What a finished pass tells the observers ([`SchedulerPorts::observe_pass`]): the numbers of the Python `_tick` and
/// `detect_parallel`.
///
/// The Python side derives `running = in_flight.len()`, `queued = max(0, ready - submitted)` and
/// `free_slots = max(0, free - submitted)` itself, so the values stay the ones of `scheduler_tick`.
#[derive(Debug, Clone)]
pub struct PassInfo<'a> {
    /// The 1-based pass number (the Python `self.rounds`).
    pub round: u64,
    /// How many dispatchable tasks the pass saw (`len(ready)`, before the backoff and internal-failure filters).
    pub ready: usize,
    /// How many tasks the pass submitted (`len(batch)`).
    pub submitted: usize,
    /// The effective parallelism of the pass: `min(max_concurrent, limiter)`, at least 1.
    pub effective_parallel: u32,
    /// The free slots the selection started from (`eff - in flight`, further capped by the dispatch budget in continuous mode).
    pub free: usize,
    /// Every task in flight once the pass has submitted its batch, in submission order.
    pub in_flight: &'a [&'a str],
    /// Every task the pass left out, one entry per task with its final reason, in first-skipped order. The Python side reports only
    /// the first `SKIPPED_REPORT_MAX` (64) entries, so the order matters.
    pub skipped: &'a [SkippedTask],
    /// The `(task_id, agent_id)` pairs, in selection order, whose lease this pass was granted but which were not submitted, because
    /// a stop was observed or because a port error ended the pass. The host settles each one (the Python `_release_unsubmitted`).
    /// Empty when leases are off or every granted dispatch was submitted.
    pub unsubmitted: Vec<(String, String)>,
}

/// The answer of [`SchedulerPorts::submit`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Submission {
    /// The dispatch is set up. Rust now runs [`SchedulerPorts::worker`] on a pool thread with this subagent id.
    Started {
        /// The subagent id (`mgr.new_subagent_id`), passed back to `worker`.
        sid: String,
    },
    /// The dispatch could not even be set up (the `except Exception` branch of `Scheduler._submit`). The Python side has already
    /// run its `_internal_failure`; the task counts as dispatched and in flight, its `worker` never runs, and this result goes to
    /// [`SchedulerPorts::complete`] on the next completion pass like any other.
    Failed(WorkerDone),
}

/// What a worker hands back: the Python `Done` (`task_id`, `agent_id`, `outcome`, `error`). A worker never raises; one task's fault
/// stays that task's.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkerDone {
    /// The task id.
    pub task_id: String,
    /// The agent id the task ran as.
    pub agent_id: String,
    /// The task state the dispatch ended in (`DONE`, `FAILED`, `SKIPPED`, `BLOCKED`, `ESCALATED`, ...). The core never interprets it;
    /// [`SchedulerPorts::complete`] does.
    pub outcome: String,
    /// Set only when the worker itself failed (an internal fault), not when the agent did.
    pub error: Option<String>,
}

/// What the Python completion handling reports back ([`SchedulerPorts::complete`]); the decisions that need only these flags (AIMD,
/// the failure circuit, the backoff delay, the `extension_down` stop) are Rust's.
///
/// Rust applies them in the order of `Scheduler._complete`: AIMD feed from `rate_limited` first, then `failed` (circuit), else
/// success; then the retry write; then `extension_down`.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Completion {
    /// `done.error is not None or outcome == FAILED`.
    pub failed: bool,
    /// `run.rate_limited`: `None` when the session hit no provider limit, `Some(0.0)` for a bare marker without a delay, otherwise
    /// the seconds the provider asked to wait. A rate-limited completion feeds the limiter whether or not it also `failed`; it
    /// neither counts toward the failure streak nor resets it.
    pub rate_limited: Option<f64>,
    /// `Some(attempt)` when a retry delay must be scheduled: the task failed, no cancel is under way, attempts are left and the
    /// failure is transient (`is_transient`). `attempt` is the task's current attempt count (at least 1 is used for the delay).
    /// `None` otherwise. The store lookup and the transient classification stay on the Python side.
    pub retry_attempt: Option<u32>,
    /// The task's notes carry an extension error: no further session can start this run (`_extension_failed`). Rust latches it, closes
    /// the run to new dispatches and logs the stop line once.
    pub extension_down: bool,
    /// A cancel was already requested when the Python side handled this completion. The failure circuit counts nothing then
    /// (`SWARM_FAIL_STREAK` ignores sessions killed by a shutdown).
    pub cancelled: bool,
}

/// The state of the run's cancel request (the Python `CancelRequest`), polled by the loop once per `wait_tick_s` and at the top of
/// every iteration.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct CancelState {
    /// `cancel.event.is_set()`: a cancel was requested (signal or swarm timeout).
    pub is_set: bool,
    /// `cancel.force.is_set()`: a second signal arrived; skip the grace and kill now.
    pub force: bool,
    /// `cancel.reason` (`signal SIGTERM`, `swarm-timeout`, ...). `None` reads as `cancelled`.
    pub reason: Option<String>,
    /// Seconds since the first request (`time.monotonic() - cancel.at`, never negative; 0 when `is_set` is false). The shutdown
    /// budget is counted from the request, and Rust never reads Python's clock.
    pub elapsed_s: f64,
}

/// How a run ended (the `stopped` key of the result).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopReason {
    /// The run was closed to new dispatches (`--once`, the budget, the rounds limit or `extension_down`) and everything in flight
    /// finished.
    Closed,
    /// A pass found no dispatchable task and nothing is in flight. Tasks may remain that are not dispatchable; the Python facade
    /// reads the store and logs `stalled: [(task id, state), ...]` when any are not terminal.
    NothingReady,
    /// Nothing runs and every ready task waits on a lease; [`RunSummary::waiting`] has the lines the facade logs as
    /// `waiting on leases: [...]`.
    LeaseWait,
    /// `internal_failure_limit` consecutive idle passes could not dispatch the ready tasks; the facade logs
    /// `stalled: ready tasks could not be dispatched`.
    Stalled,
    /// A cancel was requested and the drain ran.
    Cancelled,
}

impl StopReason {
    /// The value of the `stopped` key of the dict `run_scheduler` returns.
    pub fn as_str(self) -> &'static str {
        match self {
            StopReason::Closed => "closed",
            StopReason::NothingReady => "nothing-ready",
            StopReason::LeaseWait => "lease-wait",
            StopReason::Stalled => "stalled",
            StopReason::Cancelled => "cancelled",
        }
    }
}

/// The result of a run: the keys of the dict `run_scheduler` returns, plus `waiting`.
///
/// The lines that quote Python values (`waiting on leases: [...]`, `stalled: [...]`, `abandoned after kill: [...]`) are not written
/// by Rust: they contain Python `repr` text, so the facade formats them from these fields.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunSummary {
    /// The number of scheduling passes (`Scheduler.rounds`).
    pub rounds: u64,
    /// The number of tasks dispatched (`Scheduler.dispatched`), including a [`Submission::Failed`] one.
    pub dispatched: u64,
    /// The effective parallelism at the end of the run (`Scheduler.effective_parallel()`).
    pub effective_parallel: u32,
    /// No further passes ran: the run only drained what was in flight (`Scheduler.closed`).
    pub closed: bool,
    /// An extension error stopped the run (`Scheduler.extension_down`).
    pub extension_down: bool,
    /// The tasks still in flight when the drain gave up after the kill, sorted ascending (`sorted(self.inflight)`). Their pool
    /// threads stay detached.
    pub abandoned: Vec<String>,
    /// How the run ended.
    pub stopped: StopReason,
    /// For [`StopReason::LeaseWait`]: the waiting lines of the last pass (`<task id>: <status>[ (held by <holder>)]`); empty
    /// otherwise.
    pub waiting: Vec<String>,
}

/// Everything the run needs as numbers: the environment (`SWARM_*`) and the manager are read by the Python helpers, never by Rust.
///
/// Counts are unsigned; the binding saturates a Python integer above the type's maximum and refuses a negative one with
/// `E-INPUT` in the same wire format. [`RunConfig::validate`] checks the rest. The `Default` is the Python default of every
/// field and is what tests start from; the binding always supplies every field.
#[derive(Debug, Clone, PartialEq)]
pub struct RunConfig {
    /// The global cap (`mgr.max_concurrent`), at least 1. Not clamped here: the Python manager already applied its ceiling.
    pub max_concurrent: u32,
    /// `true`: the rounds barrier (`SWARM_SCHEDULER=rounds`): no new pass while anything is in flight, at most `max_rounds` passes.
    pub barrier: bool,
    /// `--once`: one scheduling pass, then close.
    pub once: bool,
    /// Continuous mode's dispatch budget: `max_rounds * max_concurrent` (the Python `Scheduler.budget`), at least 1.
    pub budget: u64,
    /// `--max-rounds`, at least 1 (the Python `max(1, ...)`): the pass limit of the rounds barrier.
    pub max_rounds: u64,
    /// `SWARM_FAIL_STREAK`: consecutive failures of distinct tasks that halve the parallelism once; 0 turns the circuit off.
    /// There is no upper bound.
    pub fail_streak: u32,
    /// `SWARM_RATE_COOLDOWN_S`: seconds the limiter holds growth after a decrease, at least 0.
    pub cooldown_s: f64,
    /// `SWARM_RETRY_CAP_S` for the AIMD hold (`limiter._retry_cap_s`, default 120), greater than 0. Not the backoff cap.
    pub retry_cap_s: f64,
    /// `SWARM_RETRY_BASE_S` (`retry.DEFAULT_BASE_S`, 0.1): the first backoff delay, greater than 0.
    pub backoff_base_s: f64,
    /// `SWARM_RETRY_CAP_S` for the backoff (`retry.DEFAULT_CAP_S`, 5), greater than 0. Not the AIMD hold cap.
    pub backoff_cap_s: f64,
    /// `WAIT_TICK_S` (0.25): how often the loop looks at the cancel state and the clock while sessions run, greater than 0.
    pub wait_tick_s: f64,
    /// `INTERNAL_FAILURE_LIMIT` (3): internal failures of one task after which it is left alone, and idle passes after which the run
    /// stops as stalled; at least 1.
    pub internal_failure_limit: u32,
    /// `SWARM_CANCEL_GRACE_S` (10): how long sessions get to leave on their own after a cancel, at least 0.
    pub cancel_grace_s: f64,
    /// `SWARM_SHUTDOWN_BUDGET_S` (15): the whole cancel shutdown, counted from the request, at least 0.
    pub shutdown_budget_s: f64,
    /// The longest SIGTERM to SIGKILL window of the kill: `min(KILL_GRACE_S, STOP_GRACE_S)` on the Python side (5), at least 0.
    pub kill_grace_s: f64,
    /// `KILL_SETTLE_S` (3): what SIGKILL and the final records need after the kill window, at least 0.
    pub kill_settle_s: f64,
}

impl Default for RunConfig {
    fn default() -> Self {
        Self {
            max_concurrent: 4,
            barrier: false,
            once: false,
            budget: 200,
            max_rounds: 50,
            fail_streak: 3,
            cooldown_s: 60.0,
            retry_cap_s: 120.0,
            backoff_base_s: 0.1,
            backoff_cap_s: 5.0,
            wait_tick_s: 0.25,
            internal_failure_limit: 3,
            cancel_grace_s: 10.0,
            shutdown_budget_s: 15.0,
            kill_grace_s: 5.0,
            kill_settle_s: 3.0,
        }
    }
}

impl RunConfig {
    /// Checks every field; the first invalid one is an [`SchedCode::Input`] error naming the field and the rule, never the value.
    ///
    /// Counts `max_concurrent`, `budget`, `max_rounds` and `internal_failure_limit` must be at least 1. Durations must be finite
    /// numbers of seconds in `[0, MAX_SECONDS]` (`cooldown_s`, `cancel_grace_s`, `shutdown_budget_s`, `kill_grace_s`,
    /// `kill_settle_s`) or in `(0, MAX_SECONDS]` (`retry_cap_s`, `backoff_base_s`, `backoff_cap_s`, `wait_tick_s`); NaN, infinity
    /// and negatives are refused. `fail_streak` has no invalid value (0 is "off").
    ///
    /// The upper bound is the one deliberate difference from the Python scheduler, which would accept a grace of a thousand years.
    ///
    /// ```
    /// use gsd_sched_core::ports::RunConfig;
    ///
    /// let mut config = RunConfig::default();
    /// assert!(config.validate().is_ok());
    /// config.wait_tick_s = f64::NAN;
    /// let error = config.validate().unwrap_err();
    /// assert_eq!(
    ///     error.wire_message("run_scheduler"),
    ///     "E-INPUT: run_scheduler failed: wait_tick_s must be a number of seconds > 0 and at most 1000000000"
    /// );
    /// ```
    pub fn validate(&self) -> Result<(), SchedError> {
        let counts = [
            ("max_concurrent", u64::from(self.max_concurrent)),
            ("budget", self.budget),
            ("max_rounds", self.max_rounds),
            (
                "internal_failure_limit",
                u64::from(self.internal_failure_limit),
            ),
        ];
        for (name, value) in counts {
            if value == 0 {
                return Err(SchedError::new(
                    SchedCode::Input,
                    format!("{name} must be >= 1"),
                ));
            }
        }
        let durations = [
            ("cooldown_s", self.cooldown_s, true),
            ("retry_cap_s", self.retry_cap_s, false),
            ("backoff_base_s", self.backoff_base_s, false),
            ("backoff_cap_s", self.backoff_cap_s, false),
            ("wait_tick_s", self.wait_tick_s, false),
            ("cancel_grace_s", self.cancel_grace_s, true),
            ("shutdown_budget_s", self.shutdown_budget_s, true),
            ("kill_grace_s", self.kill_grace_s, true),
            ("kill_settle_s", self.kill_settle_s, true),
        ];
        for (name, value, zero_is_valid) in durations {
            let in_range = (0.0..=MAX_SECONDS).contains(&value);
            if !in_range || (value == 0.0 && !zero_is_valid) {
                let bound = if zero_is_valid { ">= 0" } else { "> 0" };
                return Err(SchedError::new(
                    SchedCode::Input,
                    format!("{name} must be a number of seconds {bound} and at most {MAX_SECONDS}"),
                ));
            }
        }
        Ok(())
    }
}

/// The host side of the scheduler: one method per port of the Phase 14.4 plan.
///
/// Implemented once, by the Python adapter in the binding (and by fakes in tests), and shared by the loop thread and the pool
/// threads, hence `Send + Sync` and `&self` everywhere. The trait is object safe; the runtime holds it as `dyn SchedulerPorts`.
///
/// Rules that hold for **every** method (they are not repeated in full on each):
///
/// - Every call attaches the Python interpreter lock, so it may block; see the module documentation.
/// - **No Rust lock or channel guard may be held across a call.**
/// - An `Err` is a Python exception (or a conversion failure of the adapter). From a loop-thread port it stops the loop and is
///   re-raised with its original type; from a pool-thread port it fails that task (see [`SchedulerPorts::worker`]).
/// - Task and agent ids always come from the latest [`SchedulerPorts::begin_pass`] rows and [`SchedulerPorts::agent_for`] answers;
///   the Python side keeps the full task and agent dicts it needs between those calls.
pub trait SchedulerPorts: Send + Sync {
    /// Starts scheduling pass `round` and returns the dispatchable tasks. **Loop thread.**
    ///
    /// Python side: `reconcile` (its lines go to the run log), `log_phase("reconcile", ...)`, `handoffs.flush`,
    /// `dispatchable(store, ..., skip=in_flight)` (which performs the FAILED to RETRY to ESCALATED ladder), and the telemetry
    /// registration of every task of the plan (`RunBook.seen`: ready tasks as queued, the others as waiting), which Python does
    /// before it selects a batch.
    ///
    /// `round` counts passes from 1. `in_flight` holds the ids of the tasks in flight, which `dispatchable` must leave exactly as they
    /// are. The result holds every dispatchable task not in flight, in any order; an empty vector means nothing is ready. A task
    /// is returned at most once.
    ///
    /// The pass has no cancel input of its own, so this call must honour the cancel flag: `reconcile`, `handoffs.flush` and
    /// `dispatchable` each receive the flag and return early once it is set (`swarm_run.py`, `_schedule`). A cancel raised while this
    /// call runs is otherwise seen only after it returns.
    fn begin_pass(&self, round: u64, in_flight: &[&str]) -> Result<Vec<ReadyRow>, PortError>;

    /// Resolves the manifest agent of `task_id` (`resolve_agent`). **Loop thread.**
    ///
    /// `Ok(None)` is the Python `KeyError`/`IndexError`: no agent matches, and the loop blocks the task with
    /// [`BLOCK_REASON_NO_AGENT`] (the task still consumes a slot of the pass, as in `select_batch`). Any other exception is an
    /// `Err`.
    fn agent_for(&self, task_id: &str) -> Result<Option<AgentInfo>, PortError>;

    /// Parses the claims of `task_id` (`parse_claims(task["notes_json"])`). **Loop thread.**
    ///
    /// See [`ClaimsVerdict`]. Called only when the task passed its class ceiling, once per selection attempt.
    fn claims_for(&self, task_id: &str) -> Result<ClaimsVerdict, PortError>;

    /// Moves `task_id` to BLOCKED with `reason` (`store.transition(tid, BLOCKED, reason=...)`). **Loop thread.**
    ///
    /// The reason is at most [`BLOCK_REASON_MAX_CHARS`] characters. Returns nothing; the task never comes back this run.
    fn block(&self, task_id: &str, reason: &str) -> Result<(), PortError>;

    /// Asks for the lease of `task_id` for `agent_id` (`leases.acquire`). **Loop thread.**
    ///
    /// `Ok(None)` means leases are off for this run (`--dry-run`, no substrate). That answer holds for the whole run, so the loop
    /// may remember it and stop calling. Otherwise see [`LeaseVerdict`]. Called once per task that passed its claims, and it can
    /// take a network round trip.
    fn lease(&self, task_id: &str, agent_id: &str) -> Result<Option<LeaseVerdict>, PortError>;

    /// Reports the finished pass (`scheduler_tick`, `ErrorHandler.detect_parallel`). **Loop thread.**
    ///
    /// Called once per pass after the batch was submitted, with [`PassInfo`]. Returns nothing. With `SWARM_ENGINE_CHECK=1` the
    /// Python side compares `in_flight` with its own mirror and raises on a difference. `info.unsubmitted` lists the pairs whose lease
    /// was granted but not submitted after a stop or a port error: the host settles each one, as `_release_unsubmitted` does.
    /// A pass that a port error ends, in its selection or in its submissions, is reported the same way before the error goes on:
    /// `info.submitted` counts the dispatches admitted before the error, and `info.unsubmitted` holds every granted pair not
    /// submitted. The run then returns the original error whatever this call answers. A pass that fails in `begin_pass` is not
    /// reported, since it granted nothing. The host settles every pair in `info.unsubmitted` on every call.
    fn observe_pass(&self, info: &PassInfo<'_>) -> Result<(), PortError>;

    /// Sets up the dispatch of `task_id` as `agent_id` (the body of `Scheduler._submit` before `pool.submit`). **Loop thread.**
    ///
    /// Python side: the run registration (`RunBook.opened`), the `RunHandle`, the subagent id and the immediate status streams. See
    /// [`Submission`]. Rust queues the worker only after `Started`.
    fn submit(&self, task_id: &str, agent_id: &str) -> Result<Submission, PortError>;

    /// Runs the dispatch of `task_id` to the end (`Scheduler._worker`, unchanged). **Pool thread, and it blocks for the whole
    /// session**: Python waits on the session with the GIL released.
    ///
    /// `sid` is the id from [`Submission::Started`]. The Python worker catches every `Exception` itself and answers with a
    /// [`WorkerDone`]; an `Err` here is a `BaseException` that escaped it (or an adapter fault). Rust then asks
    /// [`SchedulerPorts::internal_failure`] with `Err.summary()`. A panic of the call is treated the same way with a fixed
    /// `E-INTERNAL` text. A submitted worker is always called, even when a cancel is already set, so Python can release the slot.
    fn worker(&self, task_id: &str, agent_id: &str, sid: &str) -> Result<WorkerDone, PortError>;

    /// Records a structured failure of one task and returns its result (`Scheduler._internal_failure`). **Normally a pool thread**,
    /// after [`SchedulerPorts::worker`] failed or panicked; implementations must not assume which thread calls.
    ///
    /// `text` is the failure reason to record (at most [`BLOCK_REASON_MAX_CHARS`] characters are kept), for example
    /// `E-INTERNAL: <summary>`. Python counts the failure toward `INTERNAL_FAILURE_LIMIT`, fails the task in the store, emits
    /// `task.result.rejected` and settles the lease. It does not fail itself in practice; an `Err` is an adapter fault and stops the
    /// run like a loop-thread port failure.
    fn internal_failure(
        &self,
        task_id: &str,
        agent_id: &str,
        text: &str,
    ) -> Result<WorkerDone, PortError>;

    /// Handles one finished dispatch (`Scheduler._complete` without the AIMD feed and the retry write). **Loop thread.**
    ///
    /// Python side: lifecycle slot release, run outcome, telemetry `finish`, the `round N: <task> [<agent>] sid=... → <outcome>`
    /// log line, status streams. See [`Completion`] for the answer. Called exactly once per dispatch, with the [`WorkerDone`] of
    /// `worker`, of `internal_failure` or of [`Submission::Failed`].
    fn complete(&self, done: &WorkerDone) -> Result<Completion, PortError>;

    /// Stores `notes.retry_after = wall` for `task_id` (`store.set_notes`). **Loop thread.**
    ///
    /// `wall` is a wall-clock time in seconds since the epoch: the completion time plus the backoff delay, so another process
    /// reading the store sees it. Returns nothing.
    fn set_retry_after(&self, task_id: &str, wall: f64) -> Result<(), PortError>;

    /// One draw of `random.uniform(-1, 1)`, the jitter source of the backoff. **Loop thread.**
    ///
    /// A value in `[-1, 1]`. Rust owns no random generator; tests script this value.
    fn random_unit(&self) -> Result<f64, PortError>;

    /// Reads the cancel request (the Python `CancelRequest`). **Loop thread**, at the top of every loop iteration and once per
    /// `wait_tick_s` while sessions run. Cheap on the Python side (event flags and one clock read). See [`CancelState`].
    fn cancel_state(&self) -> Result<CancelState, PortError>;

    /// Starts the cancel drain (the first half of `Scheduler._cancel_drain`). **Loop thread**, once per run.
    ///
    /// Python side: the `cancelling: <reason>; <in_flight> session(s), grace <grace_s>s[ (capped...)]` log line, the
    /// `swarm.degraded` telemetry and `write_cancel_flag` (a failed flag write is logged and swallowed). `reason` is
    /// [`CancelState::reason`] (`cancelled` when absent), `grace_s` the cooperative wait Rust computed from the drain plan, and
    /// `in_flight` the number of sessions still running. Returns nothing.
    fn cancel_begin(&self, reason: &str, grace_s: f64, in_flight: usize) -> Result<(), PortError>;

    /// Kills the sessions (`kill_sessions`: SIGTERM to every session group, SIGKILL what outlives `grace_s`). **Loop thread**, at most
    /// once per run, after the cooperative grace ran out or a force request arrived while sessions remain.
    ///
    /// `grace_s` is the SIGTERM to SIGKILL window of the plan; `reason` is the cancel reason again, which the Python side maps to
    /// `E-TIMEOUT: swarm timeout` or `E-TIMEOUT: runner shutdown`. It returns after the signals are sent, not after the sessions
    /// are gone.
    fn kill(&self, grace_s: f64, reason: &str) -> Result<(), PortError>;

    /// Appends one line to the run log (`Scheduler.log.append`). **Loop thread.** Returns nothing.
    ///
    /// Rust writes only the lines it owns (the AIMD and retry lines, the extension stop line) with the exact Python text.
    fn log(&self, line: &str) -> Result<(), PortError>;
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;
    use std::thread;

    /// A fake host: every method records a line and answers with the scripted value.
    struct RecordingPorts {
        calls: Mutex<Vec<String>>,
        rows: Vec<ReadyRow>,
        agent: Option<AgentInfo>,
        claims: ClaimsVerdict,
        lease: Option<LeaseVerdict>,
        submission: Submission,
        completion: Completion,
        cancel: CancelState,
        unit: f64,
        failing: Option<&'static str>,
    }

    impl RecordingPorts {
        fn scripted() -> Self {
            Self {
                calls: Mutex::new(Vec::new()),
                rows: vec![ReadyRow {
                    task_id: "T-1".into(),
                    priority: Some("P0".into()),
                    dag_depth: Some(2),
                    created_at: Some(10.5),
                    retry_after: None,
                }],
                agent: Some(AgentInfo {
                    agent_id: "A05".into(),
                    slug: "backend".into(),
                    max_parallel: 2,
                }),
                claims: ClaimsVerdict::Claims(vec![ClaimSpec {
                    pattern: "src/**".into(),
                    shared: false,
                }]),
                lease: Some(LeaseVerdict {
                    dispatch: false,
                    status: "denied".into(),
                    holder: Some("s-9".into()),
                }),
                submission: Submission::Started {
                    sid: "sub-1".into(),
                },
                completion: Completion {
                    failed: true,
                    rate_limited: Some(0.0),
                    retry_attempt: Some(2),
                    extension_down: false,
                    cancelled: false,
                },
                cancel: CancelState {
                    is_set: true,
                    force: false,
                    reason: Some("signal SIGTERM".into()),
                    elapsed_s: 1.5,
                },
                unit: -0.5,
                failing: None,
            }
        }

        /// Records the call; the one method named in `failing` answers with a port error instead.
        fn record(&self, call: String) -> Result<(), PortError> {
            let name = call.split(' ').next().unwrap_or_default().to_owned();
            self.calls.lock().expect("calls mutex").push(call);
            if self.failing == Some(name.as_str()) {
                return Err(PortError::new(
                    format!("RuntimeError: {name} failed"),
                    Box::new(format!("original exception of {name}")),
                ));
            }
            Ok(())
        }

        fn recorded(&self) -> Vec<String> {
            self.calls.lock().expect("calls mutex").clone()
        }
    }

    impl SchedulerPorts for RecordingPorts {
        fn begin_pass(&self, round: u64, in_flight: &[&str]) -> Result<Vec<ReadyRow>, PortError> {
            self.record(format!("begin_pass {round} {in_flight:?}"))?;
            Ok(self.rows.clone())
        }

        fn agent_for(&self, task_id: &str) -> Result<Option<AgentInfo>, PortError> {
            self.record(format!("agent_for {task_id}"))?;
            Ok(self.agent.clone())
        }

        fn claims_for(&self, task_id: &str) -> Result<ClaimsVerdict, PortError> {
            self.record(format!("claims_for {task_id}"))?;
            Ok(self.claims.clone())
        }

        fn block(&self, task_id: &str, reason: &str) -> Result<(), PortError> {
            self.record(format!("block {task_id} {reason}"))
        }

        fn lease(&self, task_id: &str, agent_id: &str) -> Result<Option<LeaseVerdict>, PortError> {
            self.record(format!("lease {task_id} {agent_id}"))?;
            Ok(self.lease.clone())
        }

        fn observe_pass(&self, info: &PassInfo<'_>) -> Result<(), PortError> {
            self.record(format!(
                "observe_pass {} ready={} submitted={} eff={} free={} in_flight={:?} skipped={:?}",
                info.round,
                info.ready,
                info.submitted,
                info.effective_parallel,
                info.free,
                info.in_flight,
                info.skipped
                    .iter()
                    .map(|s| format!("{}={}", s.task_id, s.reason))
                    .collect::<Vec<_>>(),
            ))
        }

        fn submit(&self, task_id: &str, agent_id: &str) -> Result<Submission, PortError> {
            self.record(format!("submit {task_id} {agent_id}"))?;
            Ok(self.submission.clone())
        }

        fn worker(
            &self,
            task_id: &str,
            agent_id: &str,
            sid: &str,
        ) -> Result<WorkerDone, PortError> {
            self.record(format!("worker {task_id} {agent_id} {sid}"))?;
            Ok(WorkerDone {
                task_id: task_id.into(),
                agent_id: agent_id.into(),
                outcome: "DONE".into(),
                error: None,
            })
        }

        fn internal_failure(
            &self,
            task_id: &str,
            agent_id: &str,
            text: &str,
        ) -> Result<WorkerDone, PortError> {
            self.record(format!("internal_failure {task_id} {agent_id} {text}"))?;
            Ok(WorkerDone {
                task_id: task_id.into(),
                agent_id: agent_id.into(),
                outcome: "FAILED".into(),
                error: Some(text.into()),
            })
        }

        fn complete(&self, done: &WorkerDone) -> Result<Completion, PortError> {
            self.record(format!("complete {} {}", done.task_id, done.outcome))?;
            Ok(self.completion.clone())
        }

        fn set_retry_after(&self, task_id: &str, wall: f64) -> Result<(), PortError> {
            self.record(format!("set_retry_after {task_id} {wall}"))
        }

        fn random_unit(&self) -> Result<f64, PortError> {
            self.record("random_unit".into())?;
            Ok(self.unit)
        }

        fn cancel_state(&self) -> Result<CancelState, PortError> {
            self.record("cancel_state".into())?;
            Ok(self.cancel.clone())
        }

        fn cancel_begin(
            &self,
            reason: &str,
            grace_s: f64,
            in_flight: usize,
        ) -> Result<(), PortError> {
            self.record(format!("cancel_begin {reason} {grace_s} {in_flight}"))
        }

        fn kill(&self, grace_s: f64, reason: &str) -> Result<(), PortError> {
            self.record(format!("kill {grace_s} {reason}"))
        }

        fn log(&self, line: &str) -> Result<(), PortError> {
            self.record(format!("log {line}"))
        }
    }

    /// The loop of the contract in miniature: one pass, one worker, one completion, one cancel, all through `dyn SchedulerPorts`.
    #[test]
    fn every_port_answers_what_the_script_says_and_is_recorded_in_call_order() {
        let fake = RecordingPorts::scripted();
        let ports: &dyn SchedulerPorts = &fake;

        let rows = ports.begin_pass(1, &["T-0"]).unwrap();
        assert_eq!(rows[0].task_id, "T-1");
        let agent = ports.agent_for("T-1").unwrap().unwrap();
        assert_eq!((agent.agent_id.as_str(), agent.max_parallel), ("A05", 2));
        assert_eq!(
            ports.claims_for("T-1").unwrap(),
            ClaimsVerdict::Claims(vec![ClaimSpec {
                pattern: "src/**".into(),
                shared: false
            }])
        );
        ports.block("T-2", BLOCK_REASON_NO_AGENT).unwrap();
        let verdict = ports.lease("T-1", "A05").unwrap().unwrap();
        assert!(!verdict.dispatch);
        assert_eq!(verdict.holder.as_deref(), Some("s-9"));
        let skipped = [SkippedTask {
            task_id: "T-3".into(),
            reason: "no-free-slot".into(),
        }];
        ports
            .observe_pass(&PassInfo {
                round: 1,
                ready: 3,
                submitted: 1,
                effective_parallel: 2,
                free: 2,
                in_flight: &["T-0", "T-1"],
                skipped: &skipped,
                unsubmitted: Vec::new(),
            })
            .unwrap();
        let sid = match ports.submit("T-1", "A05").unwrap() {
            Submission::Started { sid } => sid,
            Submission::Failed(done) => panic!("scripted a started dispatch, got {done:?}"),
        };
        let done = ports.worker("T-1", "A05", &sid).unwrap();
        assert_eq!(
            (done.outcome.as_str(), done.error.as_deref()),
            ("DONE", None)
        );
        let crashed = ports
            .internal_failure("T-4", "A05", "E-INTERNAL: panic")
            .unwrap();
        assert_eq!(crashed.error.as_deref(), Some("E-INTERNAL: panic"));
        let completion = ports.complete(&done).unwrap();
        assert_eq!(completion.retry_attempt, Some(2));
        assert_eq!(completion.rate_limited, Some(0.0));
        ports.set_retry_after("T-1", 1_700_000_000.25).unwrap();
        assert_eq!(ports.random_unit().unwrap(), -0.5);
        let cancel = ports.cancel_state().unwrap();
        assert_eq!(
            (cancel.is_set, cancel.reason.as_deref(), cancel.elapsed_s),
            (true, Some("signal SIGTERM"), 1.5)
        );
        ports.cancel_begin("signal SIGTERM", 7.0, 2).unwrap();
        ports.kill(5.0, "signal SIGTERM").unwrap();
        ports.log("round 1: T-1 [A05] sid=sub-1 → DONE").unwrap();

        assert_eq!(
            fake.recorded(),
            [
                "begin_pass 1 [\"T-0\"]",
                "agent_for T-1",
                "claims_for T-1",
                "block T-2 no agent for capability",
                "lease T-1 A05",
                "observe_pass 1 ready=3 submitted=1 eff=2 free=2 in_flight=[\"T-0\", \"T-1\"] skipped=[\"T-3=no-free-slot\"]",
                "submit T-1 A05",
                "worker T-1 A05 sub-1",
                "internal_failure T-4 A05 E-INTERNAL: panic",
                "complete T-1 DONE",
                "set_retry_after T-1 1700000000.25",
                "random_unit",
                "cancel_state",
                "cancel_begin signal SIGTERM 7 2",
                "kill 5 signal SIGTERM",
                "log round 1: T-1 [A05] sid=sub-1 → DONE",
            ]
        );
    }

    /// `worker` and `internal_failure` are called from pool threads: the same shared object serves two threads at once.
    #[test]
    fn the_ports_object_is_shared_by_pool_threads_through_a_trait_object() {
        fn shared<T: Send + Sync + ?Sized>(_: &T) {}
        let fake = RecordingPorts::scripted();
        let ports: &dyn SchedulerPorts = &fake;
        shared(ports);

        let (worked, failed) = thread::scope(|scope| {
            let worker = scope.spawn(|| ports.worker("T-1", "A05", "sub-1"));
            let failure = scope.spawn(|| ports.internal_failure("T-2", "A05", "E-INTERNAL: panic"));
            (worker.join().unwrap(), failure.join().unwrap())
        });
        assert_eq!(worked.unwrap().outcome, "DONE");
        assert_eq!(failed.unwrap().outcome, "FAILED");
        let mut calls = fake.recorded();
        calls.sort();
        assert_eq!(
            calls,
            [
                "internal_failure T-2 A05 E-INTERNAL: panic",
                "worker T-1 A05 sub-1"
            ]
        );
    }

    /// A loop-thread port that fails hands the original exception back, untouched, through `RunError`.
    #[test]
    fn a_failing_port_returns_its_original_payload_and_the_run_error_keeps_it() {
        let mut fake = RecordingPorts::scripted();
        fake.failing = Some("lease");
        let ports: &dyn SchedulerPorts = &fake;
        assert!(ports.agent_for("T-1").is_ok());

        let error = ports.lease("T-1", "A05").unwrap_err();
        assert_eq!(error.summary(), "RuntimeError: lease failed");
        assert_eq!(error.to_string(), "RuntimeError: lease failed");

        let run_error = RunError::from(error);
        assert_eq!(run_error.to_string(), "RuntimeError: lease failed");
        let RunError::Port(port_error) = run_error else {
            panic!("a port failure must stay a port failure");
        };
        let payload = port_error.into_payload();
        assert_eq!(
            payload.downcast_ref::<String>().map(String::as_str),
            Some("original exception of lease")
        );
    }

    #[test]
    fn the_default_config_is_the_python_default_and_is_valid() {
        let config = RunConfig::default();
        assert_eq!(config.validate(), Ok(()));
        assert_eq!(
            config.budget,
            config.max_rounds * u64::from(config.max_concurrent)
        );
    }

    /// Each case breaks exactly one field of the default; the error names that field and is an `E-INPUT`.
    #[test]
    fn an_invalid_field_is_an_input_error_that_names_it() {
        type Break = fn(&mut RunConfig);
        let cases: &[(&str, Break)] = &[
            ("max_concurrent", |c| c.max_concurrent = 0),
            ("budget", |c| c.budget = 0),
            ("max_rounds", |c| c.max_rounds = 0),
            ("internal_failure_limit", |c| c.internal_failure_limit = 0),
            ("cooldown_s", |c| c.cooldown_s = -1.0),
            ("cooldown_s", |c| c.cooldown_s = f64::NAN),
            ("cooldown_s", |c| c.cooldown_s = f64::INFINITY),
            ("retry_cap_s", |c| c.retry_cap_s = 0.0),
            ("retry_cap_s", |c| c.retry_cap_s = -0.5),
            ("retry_cap_s", |c| c.retry_cap_s = f64::NAN),
            ("backoff_base_s", |c| c.backoff_base_s = 0.0),
            ("backoff_base_s", |c| c.backoff_base_s = f64::NEG_INFINITY),
            ("backoff_cap_s", |c| c.backoff_cap_s = 0.0),
            ("backoff_cap_s", |c| c.backoff_cap_s = f64::INFINITY),
            ("wait_tick_s", |c| c.wait_tick_s = 0.0),
            ("wait_tick_s", |c| c.wait_tick_s = f64::NAN),
            ("wait_tick_s", |c| c.wait_tick_s = -0.25),
            ("cancel_grace_s", |c| c.cancel_grace_s = -0.001),
            ("cancel_grace_s", |c| c.cancel_grace_s = f64::NAN),
            ("shutdown_budget_s", |c| c.shutdown_budget_s = -1.0),
            ("shutdown_budget_s", |c| c.shutdown_budget_s = f64::INFINITY),
            ("kill_grace_s", |c| c.kill_grace_s = -5.0),
            ("kill_grace_s", |c| c.kill_grace_s = f64::NAN),
            ("kill_settle_s", |c| c.kill_settle_s = -3.0),
            ("kill_settle_s", |c| c.kill_settle_s = f64::INFINITY),
            // too long for a `Duration` or an `Instant` addition to stay free of overflow
            ("shutdown_budget_s", |c| c.shutdown_budget_s = 1.0e300),
            ("wait_tick_s", |c| c.wait_tick_s = MAX_SECONDS * 2.0),
            ("backoff_cap_s", |c| c.backoff_cap_s = f64::MAX),
        ];
        for &(field, break_it) in cases {
            let mut config = RunConfig::default();
            break_it(&mut config);
            let error = config
                .validate()
                .expect_err(&format!("a broken {field} must be refused"));
            assert_eq!(error.code(), SchedCode::Input, "{field}");
            assert!(
                error.message().starts_with(&format!("{field} must be ")),
                "{field}: {}",
                error.message()
            );
        }
    }

    /// The boundary values are valid: zero where a zero means "off" or "no wait", the largest duration, a huge count, no streak.
    #[test]
    fn boundary_values_are_valid() {
        let config = RunConfig {
            max_concurrent: 1,
            max_rounds: 1,
            budget: 1,
            internal_failure_limit: 1,
            fail_streak: 0,
            cooldown_s: 0.0,
            cancel_grace_s: 0.0,
            shutdown_budget_s: 0.0,
            kill_grace_s: 0.0,
            kill_settle_s: 0.0,
            wait_tick_s: f64::MIN_POSITIVE,
            retry_cap_s: MAX_SECONDS,
            backoff_base_s: MAX_SECONDS,
            backoff_cap_s: MAX_SECONDS,
            ..RunConfig::default()
        };
        assert_eq!(config.validate(), Ok(()));
        let huge = RunConfig {
            max_concurrent: u32::MAX,
            budget: u64::MAX,
            max_rounds: u64::MAX,
            fail_streak: u32::MAX,
            internal_failure_limit: u32::MAX,
            ..RunConfig::default()
        };
        assert_eq!(huge.validate(), Ok(()));
    }

    /// The first invalid field wins, counts before durations, so the report does not depend on the order of the failures.
    #[test]
    fn validation_reports_counts_before_durations() {
        let config = RunConfig {
            wait_tick_s: f64::NAN,
            max_concurrent: 0,
            ..RunConfig::default()
        };
        assert_eq!(
            config.validate().unwrap_err().message(),
            "max_concurrent must be >= 1"
        );
    }

    #[test]
    fn wire_messages_follow_the_native_convention() {
        let cases = [
            (
                SchedError::new(SchedCode::Input, "wait_tick_s must be > 0"),
                "E-INPUT: run_scheduler failed: wait_tick_s must be > 0",
            ),
            (
                SchedError::new(SchedCode::Internal, "internal panic"),
                "E-INTERNAL: run_scheduler failed: internal panic",
            ),
            // a message that already carries its code is not prefixed twice
            (
                SchedError::new(SchedCode::Input, "E-INPUT: budget must be >= 1"),
                "E-INPUT: run_scheduler failed: budget must be >= 1",
            ),
            // another code's prefix is part of the detail
            (
                SchedError::new(SchedCode::Input, "E-INTERNAL: odd"),
                "E-INPUT: run_scheduler failed: E-INTERNAL: odd",
            ),
        ];
        for (error, expected) in cases {
            assert_eq!(error.wire_message("run_scheduler"), expected);
        }
        assert_eq!(
            SchedError::new(SchedCode::Internal, "x").to_string(),
            "E-INTERNAL: x"
        );
    }

    /// The spelling of both enums is a public contract: the `stopped` values of the result dict and the wire codes.
    #[test]
    fn the_wire_spellings_are_pinned() {
        let stops = [
            (StopReason::Closed, "closed"),
            (StopReason::NothingReady, "nothing-ready"),
            (StopReason::LeaseWait, "lease-wait"),
            (StopReason::Stalled, "stalled"),
            (StopReason::Cancelled, "cancelled"),
        ];
        for (reason, text) in stops {
            assert_eq!(reason.as_str(), text);
        }
        assert_eq!(SchedCode::Input.as_str(), "E-INPUT");
        assert_eq!(SchedCode::Internal.as_str(), "E-INTERNAL");
    }
}
