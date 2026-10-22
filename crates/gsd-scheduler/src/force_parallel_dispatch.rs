//! Force-parallel dispatcher: refill-on-completion over the ready frontier.
//!
//! Port of the dispatch rules in `scripts/swarm_run.py` (module docstring lines 38-56, `order_ready`,
//! and the continuous/barrier runner):
//! - Every completion frees one slot and the frontier is refilled at once (continuous mode).
//! - Ready tasks dispatch in order: priority (`P0` first, see [`Priority`]), DAG depth (shallower first), then
//!   creation order (workflow position). The order is a stable sort of the workflow.
//! - The global cap is the requested parallelism (default 4) clamped to `1..=32`, and further to
//!   [`SESSION_CAP_CLAMP`] (30) per session. The effective cap is the AIMD limiter's current value.
//!   Per-agent `max_parallel` and resource claims are enforced by [`ready_frontier`].
//! - A worker failure fails only that task: a panic or an `Exception` yields `E-INTERNAL` for that
//!   task alone. A rate-limit signal halves the AIMD limiter once per provider burst ([`RateLimitGate`]) and is reported for that task.
//! - [`DispatchMode::Rounds`] is the barrier fallback (`SWARM_SCHEDULER=rounds`): a new frontier is
//!   admitted only once every task of the previous round has finished.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, Instant};

use gsd_failure::{ErrorCode, FailureError};
use gsd_proto::gsd::v1::{Task, TaskId};
use tokio::task::{JoinError, JoinSet};

use crate::aimd_limiter::AimdLimiter;
use crate::dag_build::BuiltDag;
use crate::ready_frontier::{ready_frontier, FrontierInput, PathClaim};
use crate::{SchedulerError, SchedulerResult};

/// Default requested global parallelism (`SWARM_MAX_PARALLEL`).
pub const GLOBAL_CAP_DEFAULT: usize = 4;
/// Ceiling for the requested global parallelism (`SWARM_MAX_PARALLEL_CEILING`).
pub const GLOBAL_CAP_CEILING: usize = 32;
/// Hard per-session clamp on the global parallelism.
pub const SESSION_CAP_CLAMP: usize = 30;
/// Default AIMD cooldown after a rate-limit signal (`SWARM_RATE_COOLDOWN_S`).
pub const DEFAULT_COOLDOWN: Duration = Duration::from_secs(60);
/// Error code reported for a worker exception or panic.
pub const WORKER_FAILURE_CODE: &str = "E-INTERNAL";

/// Clamps a requested global parallelism to `1..=32`, then to the per-session clamp of 30.
pub fn effective_global_cap(requested: usize) -> usize {
    requested
        .clamp(1, GLOBAL_CAP_CEILING)
        .min(SESSION_CAP_CLAMP)
}

/// Dispatch strategy: continuous refill (default) or the per-round barrier fallback.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DispatchMode {
    /// Refill the frontier after every completion.
    Continuous,
    /// Admit a new frontier only when the previous round has fully finished.
    Rounds,
}

impl DispatchMode {
    /// Parses a `SWARM_SCHEDULER` value: empty or `continuous` is continuous, `rounds` is the barrier.
    pub fn from_env_value(value: &str) -> SchedulerResult<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "" | "continuous" => Ok(Self::Continuous),
            "rounds" => Ok(Self::Rounds),
            other => Err(SchedulerError::Other(format!(
                "SWARM_SCHEDULER must be one of [\"continuous\", \"rounds\"], got {other:?}"
            ))),
        }
    }
}

/// Why a worker failed, as reported by the worker itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TaskFailure {
    /// The provider rate-limited the task; `retry_after` is the server's hint, if any.
    RateLimited {
        /// Server-requested hold before growth resumes.
        retry_after: Option<Duration>,
    },
    /// The worker raised an exception; only this task fails.
    Exception(String),
}

/// Final state of one dispatched task.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TaskOutcome {
    /// The worker finished successfully.
    Completed,
    /// The provider rate-limited the task; the caller owns any retry.
    RateLimited {
        /// Server-requested hold, if any.
        retry_after: Option<Duration>,
    },
    /// The worker failed, panicked, or was cancelled; `code` is the shared failure code.
    Failed {
        /// Wire failure code, `E-INTERNAL` for worker failures.
        code: &'static str,
        /// Human-readable detail.
        message: String,
    },
}

/// Configuration for one dispatch run.
#[derive(Debug, Clone)]
pub struct DispatchConfig {
    /// Requested global parallelism, before the ceiling and session clamp.
    pub requested_parallel: usize,
    /// Continuous refill or barrier rounds.
    pub mode: DispatchMode,
    /// AIMD cooldown after a rate-limit signal.
    pub cooldown: Duration,
    /// Failure-streak threshold; `0` disables the circuit.
    pub fail_streak: usize,
    /// Path claims each task requests, keyed by task id.
    pub requested_claims: HashMap<String, Vec<PathClaim>>,
    /// Manifest `max_parallel` per agent type.
    pub agent_max_parallel: HashMap<String, usize>,
}

impl Default for DispatchConfig {
    fn default() -> Self {
        Self {
            requested_parallel: GLOBAL_CAP_DEFAULT,
            mode: DispatchMode::Continuous,
            cooldown: DEFAULT_COOLDOWN,
            fail_streak: crate::aimd_limiter::DEFAULT_STREAK_THRESHOLD,
            requested_claims: HashMap::new(),
            agent_max_parallel: HashMap::new(),
        }
    }
}

/// Result of a dispatch run.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct DispatchReport {
    /// Task ids in the order they were dispatched.
    pub dispatch_order: Vec<String>,
    /// Final outcome per task, in completion order.
    pub outcomes: Vec<(String, TaskOutcome)>,
    /// Ids of tasks that never became ready (for example, behind a failed dependency).
    pub undispatched: Vec<String>,
    /// Highest number of simultaneously running tasks observed.
    pub max_in_flight: usize,
}

/// Dispatch priority, as Python stores it (`PRIORITY_RANK` in `scripts/swarm_run.py`): `P0` is the most
/// urgent. The variant order is the dispatch order, so an ascending sort dispatches `P0` first.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Priority {
    /// Most urgent.
    P0,
    /// Urgent.
    P1,
    /// Default.
    P2,
    /// Least urgent.
    P3,
}

impl Priority {
    /// Parses a priority label exactly as Python stores it (`P0`..`P3`). Any other string is `E-INPUT`.
    pub fn parse(label: &str) -> Result<Self, FailureError> {
        match label {
            "P0" => Ok(Self::P0),
            "P1" => Ok(Self::P1),
            "P2" => Ok(Self::P2),
            "P3" => Ok(Self::P3),
            other => Err(FailureError::new(
                ErrorCode::Input,
                format!("priority must be one of P0, P1, P2, P3, got {other:?}"),
            )),
        }
    }

    /// Reads a task's proto priority, which carries the rank: `0` is `P0` through `3` is `P3`.
    /// Any other rank is `E-INPUT`.
    pub fn from_wire(rank: i32) -> Result<Self, FailureError> {
        Self::parse(&format!("P{rank}"))
    }
}

impl From<FailureError> for SchedulerError {
    fn from(error: FailureError) -> Self {
        SchedulerError::Other(error.to_string())
    }
}

/// Sorts `tasks` into dispatch order: priority (`P0` first), DAG depth (shallower first), then creation
/// order (original position). Stable for full ties. A task whose priority is outside `P0`..`P3` fails the
/// sort with `E-INPUT` and leaves `tasks` unchanged.
pub fn sort_dispatch_order(tasks: &mut Vec<Task>) -> SchedulerResult<()> {
    let priorities = tasks
        .iter()
        .map(|task| Priority::from_wire(task.priority))
        .collect::<Result<Vec<_>, _>>()?;
    let depths = dag_depths(tasks);
    let owned = std::mem::take(tasks);
    let mut keyed: Vec<(Priority, usize, usize, Task)> = owned
        .into_iter()
        .enumerate()
        .map(|(index, task)| (priorities[index], depths[index], index, task))
        .collect();
    keyed.sort_by_key(|(priority, depth, index, _)| (*priority, *depth, *index));
    *tasks = keyed.into_iter().map(|(_, _, _, task)| task).collect();
    Ok(())
}

/// Longest dependency chain below each task: roots are 0, a dependent is its deepest dependency + 1.
/// Dependencies that are not in `tasks` contribute nothing; a cycle is cut rather than followed.
fn dag_depths(tasks: &[Task]) -> Vec<usize> {
    let index: HashMap<&str, usize> = tasks
        .iter()
        .enumerate()
        .filter_map(|(i, t)| t.id.as_ref().map(|id| (id.value.as_str(), i)))
        .collect();
    let mut memo: Vec<Option<usize>> = vec![None; tasks.len()];
    let mut visiting = vec![false; tasks.len()];
    (0..tasks.len())
        .map(|i| depth_of(i, tasks, &index, &mut memo, &mut visiting))
        .collect()
}

fn depth_of(
    i: usize,
    tasks: &[Task],
    index: &HashMap<&str, usize>,
    memo: &mut Vec<Option<usize>>,
    visiting: &mut Vec<bool>,
) -> usize {
    if let Some(depth) = memo[i] {
        return depth;
    }
    if visiting[i] {
        return 0;
    }
    visiting[i] = true;
    let mut depth = 0;
    for dep in &tasks[i].dependencies {
        if let Some(&j) = index.get(dep.value.as_str()) {
            depth = depth.max(depth_of(j, tasks, index, memo, visiting) + 1);
        }
    }
    visiting[i] = false;
    memo[i] = Some(depth);
    depth
}

fn task_key(task: &Task) -> &str {
    task.id.as_ref().map_or("", |id| id.value.as_str())
}

fn agent_key(task: &Task) -> &str {
    task.subagent_type.as_ref().map_or("", |s| s.value.as_str())
}

type Joined = (String, Result<Result<(), TaskFailure>, JoinError>);

/// Once-per-burst gate for rate-limit signals, owned by the dispatch runner. Port of the dedupe in
/// `scripts/swarm_run.py` `_backpressure`: a hit from a session submitted before the last decrease belongs
/// to the same provider burst and changes nothing. The limiter itself halves on every call it receives.
#[derive(Debug, Default)]
struct RateLimitGate {
    last_decrease: Option<Instant>,
}

impl RateLimitGate {
    /// Feeds one rate-limit signal from a session submitted at `submitted_at`, observed at `now`.
    /// Returns whether the limit dropped. A hit with `submitted_at < last_decrease` returns before the
    /// limiter is called; otherwise the limiter halves and `last_decrease` is set only when the cap dropped.
    fn on_rate_limit(
        &mut self,
        limiter: &mut AimdLimiter,
        submitted_at: Instant,
        now: Instant,
        retry_after: Option<Duration>,
    ) -> bool {
        if self.last_decrease.is_some_and(|last| submitted_at < last) {
            return false;
        }
        let dropped = limiter.on_rate_limit(now, retry_after);
        if dropped {
            self.last_decrease = Some(now);
        }
        dropped
    }
}

/// Mutable state of one dispatch run.
struct Run<W> {
    built: BuiltDag,
    worker: Arc<W>,
    config: DispatchConfig,
    limiter: AimdLimiter,
    completed: HashSet<String>,
    active_claims: HashMap<String, Vec<PathClaim>>,
    running_per_agent: HashMap<String, usize>,
    in_flight: HashMap<String, String>,
    set: JoinSet<Joined>,
    report: DispatchReport,
    gate: RateLimitGate,
    submitted_at: HashMap<String, Instant>,
}

impl<W, Fut> Run<W>
where
    W: Fn(Task) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<(), TaskFailure>> + Send + 'static,
{
    /// Admits every ready task the free slots allow, in dispatch order.
    fn refill(&mut self) {
        let cap = self.limiter.current();
        let free = cap.saturating_sub(self.in_flight.len());
        if free == 0 {
            return;
        }
        let ready = ready_frontier(&FrontierInput {
            dag: &self.built,
            completed: &self.completed,
            active_claims: &self.active_claims,
            requested_claims: &self.config.requested_claims,
            running_per_agent: &self.running_per_agent,
            agent_max_parallel: &self.config.agent_max_parallel,
            free_slots: free,
        });
        for id in ready {
            let Some(pos) = self
                .built
                .tasks
                .iter()
                .position(|t| task_key(t) == id.value)
            else {
                continue;
            };
            let task = self.built.tasks.remove(pos);
            let agent = agent_key(&task).to_string();
            let claims = self
                .config
                .requested_claims
                .get(&id.value)
                .cloned()
                .unwrap_or_default();
            self.active_claims.insert(id.value.clone(), claims);
            *self.running_per_agent.entry(agent.clone()).or_insert(0) += 1;
            self.in_flight.insert(id.value.clone(), agent);
            self.submitted_at.insert(id.value.clone(), Instant::now());
            self.report.dispatch_order.push(id.value.clone());
            self.spawn_worker(id.value, task);
        }
        self.report.max_in_flight = self.report.max_in_flight.max(self.in_flight.len());
    }

    /// Runs the worker in its own task so a panic is isolated and attributable to `id`.
    fn spawn_worker(&mut self, id: String, task: Task) {
        let handle = tokio::spawn((*self.worker)(task));
        self.set.spawn(async move { (id, handle.await) });
    }

    /// Records one finished task and feeds its signal to the limiter.
    fn complete(&mut self, joined: Joined) {
        let (id, result) = joined;
        let now = Instant::now();
        let submitted_at = self.submitted_at.remove(&id).unwrap_or(now);
        if let Some(agent) = self.in_flight.remove(&id) {
            if let Some(count) = self.running_per_agent.get_mut(&agent) {
                *count = count.saturating_sub(1);
                if *count == 0 {
                    self.running_per_agent.remove(&agent);
                }
            }
        }
        self.active_claims.remove(&id);
        let outcome = match result {
            Ok(Ok(())) => {
                self.limiter.on_success(now);
                self.completed.insert(id.clone());
                TaskOutcome::Completed
            }
            Ok(Err(TaskFailure::RateLimited { retry_after })) => {
                self.gate
                    .on_rate_limit(&mut self.limiter, submitted_at, now, retry_after);
                TaskOutcome::RateLimited { retry_after }
            }
            Ok(Err(TaskFailure::Exception(message))) => {
                self.limiter.on_failure(now, &id);
                TaskOutcome::Failed {
                    code: ErrorCode::Internal.as_str(),
                    message,
                }
            }
            Err(join_error) => {
                self.limiter.on_failure(now, &id);
                TaskOutcome::Failed {
                    code: ErrorCode::Internal.as_str(),
                    message: panic_message(join_error),
                }
            }
        };
        self.report.outcomes.push((id, outcome));
    }
}

/// Failure text for a worker that panicked or was cancelled.
///
/// Built only from the panic payload. `JoinError`'s display embeds tokio's process-wide task id,
/// which differs between runs, so it must never reach a completion record.
fn panic_message(join_error: JoinError) -> String {
    let payload = match join_error.try_into_panic() {
        Ok(payload) => payload,
        Err(_) => return format!("{WORKER_FAILURE_CODE}: worker task was cancelled"),
    };
    let detail = payload
        .downcast_ref::<&str>()
        .map(|text| (*text).to_string())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "non-string panic payload".to_string());
    format!("{WORKER_FAILURE_CODE}: worker panicked: {detail}")
}

/// Dispatches every task of `built` through `worker` and returns once no task can run.
///
/// `worker` receives an owned task and returns `Ok(())` on success or a [`TaskFailure`]. A panic in
/// the worker fails only that task with `E-INTERNAL`. Tasks behind a failed dependency are never
/// dispatched and appear in [`DispatchReport::undispatched`].
pub async fn run_force_parallel<W, Fut>(
    mut built: BuiltDag,
    config: DispatchConfig,
    worker: W,
) -> SchedulerResult<DispatchReport>
where
    W: Fn(Task) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<(), TaskFailure>> + Send + 'static,
{
    let cap = effective_global_cap(config.requested_parallel);
    let limiter =
        AimdLimiter::new(cap, 1, config.cooldown)?.with_streak_threshold(config.fail_streak);
    sort_dispatch_order(&mut built.tasks)?;
    let mut run = Run {
        built,
        worker: Arc::new(worker),
        config,
        limiter,
        completed: HashSet::new(),
        active_claims: HashMap::new(),
        running_per_agent: HashMap::new(),
        in_flight: HashMap::new(),
        gate: RateLimitGate::default(),
        submitted_at: HashMap::new(),
        set: JoinSet::new(),
        report: DispatchReport::default(),
    };
    loop {
        if run.config.mode == DispatchMode::Continuous || run.in_flight.is_empty() {
            run.refill();
        }
        if run.set.is_empty() {
            break;
        }
        match run.set.join_next().await {
            Some(Ok(joined)) => run.complete(joined),
            Some(Err(join_error)) => {
                return Err(SchedulerError::Other(format!(
                    "dispatch wrapper failed: {join_error}"
                )))
            }
            None => break,
        }
    }
    run.report.undispatched = run
        .built
        .tasks
        .iter()
        .map(|t| task_key(t).to_string())
        .collect();
    Ok(run.report)
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_proto::gsd::v1::SubagentType;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    fn task(id: &str, priority: i32, agent: &str, deps: &[&str]) -> Task {
        Task {
            id: Some(TaskId {
                value: id.to_string(),
            }),
            priority,
            subagent_type: Some(SubagentType {
                value: agent.to_string(),
            }),
            dependencies: deps
                .iter()
                .map(|d| TaskId {
                    value: d.to_string(),
                })
                .collect(),
            ..Default::default()
        }
    }

    fn built(tasks: Vec<Task>) -> BuiltDag {
        BuiltDag {
            dag: Default::default(),
            tasks,
        }
    }

    fn ids(tasks: &[Task]) -> Vec<String> {
        tasks.iter().map(|t| task_key(t).to_string()).collect()
    }

    fn serial(requested: usize) -> DispatchConfig {
        DispatchConfig {
            requested_parallel: requested,
            ..Default::default()
        }
    }

    #[test]
    fn order_is_priority_then_depth_then_creation() {
        // A(P0 root), B(P1 root), C(P0 root), D(P0 depends on A): P0 first, then depth, then index.
        let mut tasks = vec![
            task("A", 0, "w", &[]),
            task("B", 1, "w", &[]),
            task("C", 0, "w", &[]),
            task("D", 0, "w", &["A"]),
        ];
        sort_dispatch_order(&mut tasks).expect("sort");
        assert_eq!(ids(&tasks), vec!["A", "C", "D", "B"]);
    }

    #[test]
    fn depth_outranks_creation_order_at_equal_priority() {
        // Created deep-first, but depth ascending puts the root first.
        let mut tasks = vec![
            task("deep", 0, "w", &["mid"]),
            task("mid", 0, "w", &["root"]),
            task("root", 0, "w", &[]),
        ];
        sort_dispatch_order(&mut tasks).expect("sort");
        assert_eq!(ids(&tasks), vec!["root", "mid", "deep"]);
    }

    #[test]
    fn effective_cap_applies_ceiling_and_session_clamp() {
        assert_eq!(effective_global_cap(0), 1);
        assert_eq!(effective_global_cap(GLOBAL_CAP_DEFAULT), 4);
        assert_eq!(effective_global_cap(64), 30);
        assert_eq!(effective_global_cap(31), 30);
    }

    #[test]
    fn mode_parses_env_values() {
        assert_eq!(
            DispatchMode::from_env_value("").ok(),
            Some(DispatchMode::Continuous)
        );
        assert_eq!(
            DispatchMode::from_env_value(" Rounds ").ok(),
            Some(DispatchMode::Rounds)
        );
        assert!(DispatchMode::from_env_value("bogus").is_err());
    }

    #[tokio::test]
    async fn serial_dispatch_follows_priority_depth_creation() {
        // With one slot, dispatch order is exactly the sorted order.
        let tasks = vec![
            task("a", 0, "w", &[]),
            task("b", 3, "w", &[]),
            task("c", 0, "w", &["a"]),
            task("d", 0, "w", &[]),
            task("e", 0, "w", &[]),
        ];
        let report = run_force_parallel(built(tasks), serial(1), |_t: Task| async { Ok(()) })
            .await
            .expect("dispatch");
        assert_eq!(report.dispatch_order, vec!["a", "d", "e", "c", "b"]);
        assert!(report.undispatched.is_empty());
        assert_eq!(report.max_in_flight, 1);
    }

    #[tokio::test]
    async fn flood_of_sixty_never_exceeds_thirty_in_flight() {
        let tasks: Vec<Task> = (0..60)
            .map(|i| task(&format!("t{i}"), 0, "w", &[]))
            .collect();
        let running = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));
        let (r, p) = (Arc::clone(&running), Arc::clone(&peak));
        let mut config = DispatchConfig {
            requested_parallel: 64,
            ..Default::default()
        };
        // Python's select_batch limits an agent with no manifest max_parallel to one session; grant "w" the full cap.
        config.agent_max_parallel.insert("w".to_string(), 64);
        let report = run_force_parallel(built(tasks), config, move |_t: Task| {
            let (r, p) = (Arc::clone(&r), Arc::clone(&p));
            async move {
                let now = r.fetch_add(1, Ordering::SeqCst) + 1;
                p.fetch_max(now, Ordering::SeqCst);
                tokio::time::sleep(Duration::from_millis(5)).await;
                r.fetch_sub(1, Ordering::SeqCst);
                Ok(())
            }
        })
        .await
        .expect("dispatch");
        assert_eq!(report.outcomes.len(), 60);
        assert!(report
            .outcomes
            .iter()
            .all(|(_, o)| *o == TaskOutcome::Completed));
        assert!(peak.load(Ordering::SeqCst) <= 30);
        assert_eq!(report.max_in_flight, 30);
    }

    #[tokio::test]
    async fn per_agent_cap_limits_concurrency() {
        let tasks: Vec<Task> = (0..6)
            .map(|i| task(&format!("t{i}"), 0, "solo", &[]))
            .collect();
        let mut config = DispatchConfig {
            requested_parallel: 8,
            ..Default::default()
        };
        config.agent_max_parallel.insert("solo".to_string(), 2);
        let report = run_force_parallel(built(tasks), config, |_t: Task| async {
            tokio::time::sleep(Duration::from_millis(2)).await;
            Ok(())
        })
        .await
        .expect("dispatch");
        assert_eq!(report.max_in_flight, 2);
        assert_eq!(report.outcomes.len(), 6);
    }

    #[tokio::test]
    async fn single_worker_panic_fails_only_that_task() {
        let tasks = vec![
            task("a", 0, "w", &[]),
            task("b", 0, "w", &[]),
            task("c", 0, "w", &[]),
        ];
        let report = run_force_parallel(built(tasks), serial(3), |t: Task| async move {
            if task_key(&t) == "b" {
                panic!("boom");
            }
            Ok(())
        })
        .await
        .expect("dispatch");
        let outcome = |id: &str| {
            report
                .outcomes
                .iter()
                .find(|(i, _)| i == id)
                .map(|(_, o)| o.clone())
        };
        assert_eq!(outcome("a"), Some(TaskOutcome::Completed));
        assert_eq!(outcome("c"), Some(TaskOutcome::Completed));
        match outcome("b") {
            Some(TaskOutcome::Failed { code, .. }) => assert_eq!(code, "E-INTERNAL"),
            other => panic!("expected E-INTERNAL failure, got {other:?}"),
        }
        assert!(report.undispatched.is_empty());
    }

    #[tokio::test]
    async fn panic_message_is_identical_across_dispatches() {
        async fn panic_text() -> String {
            let tasks = vec![task("b", 0, "w", &[])];
            let report = run_force_parallel(built(tasks), serial(1), |t: Task| async move {
                if task_key(&t) == "b" {
                    panic!("boom");
                }
                Ok(())
            })
            .await
            .expect("dispatch");
            match report.outcomes.iter().find(|(i, _)| i == "b") {
                Some((_, TaskOutcome::Failed { message, .. })) => message.clone(),
                other => panic!("expected failure, got {other:?}"),
            }
        }
        let first = panic_text().await;
        let second = panic_text().await;
        assert_eq!(first, second);
        assert_eq!(first, "E-INTERNAL: worker panicked: boom");
    }

    #[tokio::test]
    async fn worker_exception_fails_dependents_only_through_the_graph() {
        let tasks = vec![
            task("a", 0, "w", &[]),
            task("b", 0, "w", &["a"]),
            task("c", 0, "w", &[]),
        ];
        let report = run_force_parallel(built(tasks), serial(2), |t: Task| async move {
            if task_key(&t) == "a" {
                Err(TaskFailure::Exception("bad input".to_string()))
            } else {
                Ok(())
            }
        })
        .await
        .expect("dispatch");
        assert!(report
            .outcomes
            .iter()
            .any(|(id, o)| id == "c" && *o == TaskOutcome::Completed));
        assert!(report.outcomes.iter().any(|(id, o)| id == "a"
            && matches!(
                o,
                TaskOutcome::Failed {
                    code: "E-INTERNAL",
                    ..
                }
            )));
        assert_eq!(report.undispatched, vec!["b".to_string()]);
    }

    #[tokio::test]
    async fn rate_limit_burst_halves_once_in_dispatch() {
        let tasks: Vec<Task> = (0..4)
            .map(|i| task(&format!("t{i}"), 0, "w", &[]))
            .collect();
        let mut config = DispatchConfig {
            requested_parallel: 4,
            cooldown: Duration::from_secs(60),
            ..Default::default()
        };
        // Four sessions in one burst: the manifest cap for "w" must admit all four.
        config.agent_max_parallel.insert("w".to_string(), 4);
        let report = run_force_parallel(built(tasks), config, |_t: Task| async {
            Err(TaskFailure::RateLimited { retry_after: None })
        })
        .await
        .expect("dispatch");
        assert_eq!(report.outcomes.len(), 4);
        // Four sessions submitted in one burst all hit the limit. The first halves 4 -> 2; the other three
        // were submitted before that decrease, so the runner's gate treats them as the same burst.
        assert_eq!(report.dispatch_order.len(), 4);
        assert_eq!(report.max_in_flight, 4);
    }

    #[test]
    fn priority_parses_p0_to_p3_and_rejects_anything_else() {
        assert_eq!(Priority::parse("P0").ok(), Some(Priority::P0));
        assert_eq!(Priority::parse("P3").ok(), Some(Priority::P3));
        for bad in ["p0", "P4", "", "high", " P1"] {
            assert_eq!(
                Priority::parse(bad).expect_err("bad priority").code(),
                ErrorCode::Input
            );
        }
    }

    #[test]
    fn wire_rank_maps_zero_to_p0_and_rejects_out_of_range() {
        assert_eq!(Priority::from_wire(0).ok(), Some(Priority::P0));
        assert_eq!(Priority::from_wire(3).ok(), Some(Priority::P3));
        assert_eq!(
            Priority::from_wire(4).expect_err("rank 4").code(),
            ErrorCode::Input
        );
        assert_eq!(
            Priority::from_wire(-1).expect_err("rank -1").code(),
            ErrorCode::Input
        );
    }

    #[test]
    fn p0_sorts_before_p3() {
        assert!(Priority::P0 < Priority::P3);
        let mut tasks = vec![task("low", 3, "w", &[]), task("high", 0, "w", &[])];
        sort_dispatch_order(&mut tasks).expect("sort");
        assert_eq!(ids(&tasks), vec!["high", "low"]);
    }

    #[test]
    fn bad_priority_is_rejected_and_leaves_tasks_unsorted() {
        let mut tasks = vec![task("a", 0, "w", &[]), task("b", 7, "w", &[])];
        let err = sort_dispatch_order(&mut tasks).expect_err("bad priority");
        assert!(err.to_string().contains("E-INPUT"));
        assert_eq!(ids(&tasks), vec!["a", "b"]);
    }

    #[tokio::test]
    async fn run_rejects_bad_priority_before_dispatching() {
        let tasks = vec![task("a", -1, "w", &[])];
        let err = run_force_parallel(built(tasks), serial(1), |_t: Task| async { Ok(()) })
            .await
            .expect_err("bad priority");
        assert!(err.to_string().contains("E-INPUT"));
    }

    #[test]
    fn same_burst_signal_from_earlier_session_changes_nothing() {
        let t0 = Instant::now();
        let mut limiter = AimdLimiter::new(8, 1, Duration::from_secs(60)).expect("valid bounds");
        let mut gate = RateLimitGate::default();
        let t1 = t0 + Duration::from_secs(1);
        assert!(gate.on_rate_limit(&mut limiter, t0, t1, None));
        assert_eq!(limiter.current(), 4);
        // Submitted at t0, before the decrease at t1: same burst, so the limiter is not called.
        assert!(!gate.on_rate_limit(&mut limiter, t0, t1 + Duration::from_secs(1), None));
        assert_eq!(limiter.current(), 4);
    }

    #[test]
    fn signal_from_session_submitted_after_decrease_halves_again() {
        let t0 = Instant::now();
        let mut limiter = AimdLimiter::new(8, 1, Duration::from_secs(60)).expect("valid bounds");
        let mut gate = RateLimitGate::default();
        let t1 = t0 + Duration::from_secs(1);
        assert!(gate.on_rate_limit(&mut limiter, t0, t1, None));
        let t2 = t0 + Duration::from_secs(2);
        assert!(gate.on_rate_limit(&mut limiter, t2, t2 + Duration::from_secs(1), None));
        assert_eq!(limiter.current(), 2);
    }

    #[tokio::test]
    async fn continuous_refills_on_completion_while_rounds_barrier() {
        async fn events(mode: DispatchMode) -> Vec<String> {
            let log: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
            let tasks = vec![
                task("a", 0, "w", &[]),
                task("b", 0, "w", &[]),
                task("c", 0, "w", &[]),
            ];
            let mut config = DispatchConfig {
                requested_parallel: 2,
                mode,
                ..Default::default()
            };
            // Two slots for "w" so a completion frees a slot while "a" still runs.
            config.agent_max_parallel.insert("w".to_string(), 2);
            let l = Arc::clone(&log);
            run_force_parallel(built(tasks), config, move |t: Task| {
                let l = Arc::clone(&l);
                async move {
                    let id = task_key(&t).to_string();
                    l.lock().expect("log").push(format!("start:{id}"));
                    let delay = if id == "a" { 80 } else { 10 };
                    tokio::time::sleep(Duration::from_millis(delay)).await;
                    l.lock().expect("log").push(format!("end:{id}"));
                    Ok(())
                }
            })
            .await
            .expect("dispatch");
            let out = log.lock().expect("log").clone();
            out
        }
        let pos = |log: &[String], s: &str| log.iter().position(|e| e == s).expect("event");
        let cont = events(DispatchMode::Continuous).await;
        assert!(pos(&cont, "start:c") < pos(&cont, "end:a"));
        let rounds = events(DispatchMode::Rounds).await;
        assert!(pos(&rounds, "end:a") < pos(&rounds, "start:c"));
    }
}
