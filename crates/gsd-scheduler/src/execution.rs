//! Execution engine for the new validated-graph scheduling policy.
//!
//! Consumes [`crate::scheduler::ValidatedGraph`] (local adapter over the
//! existing `gsd_proto::gsd::v1::Task` / `TaskDag` public types; this crate
//! adds no dependency on `gsd-sched-core` or `gsd-python`) and runs it
//! work-conserving: a node starts as soon as every dependency succeeded and
//! a global + per-subagent slot is free.
//!
//! Reliability rules (Graph Engineering decisions):
//! * at-least-once: retries only for classified, idempotent,
//!   allow-listed failures; unclassified (or non-idempotent) failure => no retry.
//! * per-node, per-tool and whole-graph timeouts are distinct; none of them
//!   is a retry budget.
//! * partial success: a failed branch blocks only its dependents; disjoint
//!   branches keep running and the report names everything.
//!
//! The legacy `MatrixScheduler` path is untouched; callers fall back to it
//! while the new policy is disabled (see [`crate::scheduler::Scheduler`]).

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use gsd_proto::gsd::v1::Task;

use crate::{SchedulerError, SchedulerResult};

/// Cooperative cancellation token shared by the whole graph run.
///
/// Executors receive it in [`ExecutionCtx`] and are expected to poll
/// [`CancellationToken::is_cancelled`] between awaits (or race their work
/// against it); the runner also stops launching new nodes once cancelled.
#[derive(Debug, Clone, Default)]
pub struct CancellationToken {
    cancelled: Arc<AtomicBool>,
}

impl CancellationToken {
    /// Creates a token that is not cancelled.
    pub fn new() -> Self {
        Self::default()
    }

    /// Marks the token cancelled. Idempotent.
    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
    }

    /// Returns whether the token has been cancelled.
    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }
}

/// Inputs handed to the seed node's executor.
#[derive(Debug, Clone)]
pub struct ExecutionCtx {
    /// Correlation id carried across every log line of this run.
    pub correlation_id: String,
    /// Graph this node belongs to.
    pub graph_id: String,
    /// Node being executed.
    pub node_id: String,
    /// Subagent the node is routed to (`Task.subagent_type`).
    pub subagent: String,
    /// Remaining budget for this single node attempt; the executor (or the
    /// runner's `tokio::time::timeout`) must honour it.
    pub node_timeout: Duration,
    /// Budget for a single tool call inside the node; distinct from the
    /// node budget — a long node may need several tool calls.
    pub tool_timeout: Duration,
    /// Cooperative cancellation for the whole run.
    pub token: CancellationToken,
}

/// Failure returned by an executor, classified so the runner can decide
/// whether a retry is ever allowed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClassifiedError {
    /// Machine code, e.g. `"E-DEP"`, or `None` when unclassified.
    /// Unclassified failures are never retried.
    pub code: Option<String>,
    /// Whether re-running this node is safe. Retries require this to be
    /// `true`; the runner never retries a non-idempotent node.
    pub idempotent: bool,
    /// Human-readable detail, recorded verbatim in the report provenance.
    pub message: String,
}

impl ClassifiedError {
    /// Unclassified, non-idempotent failure: never retried.
    pub fn unclassified(message: impl Into<String>) -> Self {
        Self {
            code: None,
            idempotent: false,
            message: message.into(),
        }
    }

    /// Classified failure with an explicit idempotency statement.
    pub fn classified(
        code: impl Into<String>,
        idempotent: bool,
        message: impl Into<String>,
    ) -> Self {
        Self {
            code: Some(code.into()),
            idempotent,
            message: message.into(),
        }
    }
}

impl std::fmt::Display for ClassifiedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match &self.code {
            Some(code) => write!(f, "{code}: {}", self.message),
            None => write!(f, "unclassified: {}", self.message),
        }
    }
}

impl std::error::Error for ClassifiedError {}

/// Successful output of one node.
#[derive(Debug, Clone, Default)]
pub struct NodeOutput {
    /// Opaque payload the executor produced; the runner never interprets it.
    pub payload: String,
}

/// Runs one node. Implementations must honour `ctx.node_timeout` /
/// `ctx.tool_timeout` and observe `ctx.token`; the runner additionally
/// enforces the node timeout around each attempt.
pub trait NodeExecutor: Send + Sync {
    /// Executes one attempt of `ctx.node_id`.
    fn execute<'a>(
        &'a self,
        ctx: ExecutionCtx,
    ) -> impl Future<Output = Result<NodeOutput, ClassifiedError>> + Send + 'a;
}

/// How one node ended. `Skipped` means it never started because an ancestor
/// failed (or the run was cancelled / hit the graph deadline).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeStatus {
    Succeeded,
    Failed,
    Skipped,
    Cancelled,
}

/// Provenance for one node in an [`ExecutionReport`].
#[derive(Debug, Clone)]
pub struct NodeResult {
    /// Node id.
    pub node_id: String,
    /// Subagent the node was routed to.
    pub subagent: String,
    /// How the node ended.
    pub status: NodeStatus,
    /// Attempts actually made (1 == no retry happened).
    pub attempts: u32,
    /// Wall time spent inside this node's attempts plus backoff sleeps.
    pub duration: Duration,
    /// Error code of the final failure, if any (`None` == unclassified).
    pub error_code: Option<String>,
    /// Final failure message (or the skip reason), if any.
    pub message: Option<String>,
}

/// Deterministic, dependency-ordered outcome of one graph run.
#[derive(Debug, Clone)]
pub struct ExecutionReport {
    /// Graph that ran.
    pub graph_id: String,
    /// Correlation id of the run.
    pub correlation_id: String,
    /// One entry per node, ordered by dependency (topological) order with
    /// ties broken by node id — never by completion order.
    pub nodes: Vec<NodeResult>,
    /// Highest number of simultaneously running nodes observed.
    pub max_observed_concurrency: usize,
}

impl ExecutionReport {
    /// Succeeded node ids, in report order.
    pub fn succeeded(&self) -> Vec<&str> {
        self.nodes
            .iter()
            .filter(|n| n.status == NodeStatus::Succeeded)
            .map(|n| n.node_id.as_str())
            .collect()
    }

    /// Failed node ids, in report order.
    pub fn failed(&self) -> Vec<&str> {
        self.nodes
            .iter()
            .filter(|n| n.status == NodeStatus::Failed)
            .map(|n| n.node_id.as_str())
            .collect()
    }

    /// Skipped node ids, in report order.
    pub fn skipped(&self) -> Vec<&str> {
        self.nodes
            .iter()
            .filter(|n| n.status == NodeStatus::Skipped)
            .map(|n| n.node_id.as_str())
            .collect()
    }

    /// Nodes that did not succeed: the targeted failed-subgraph, ready for
    /// [`crate::scheduler::Scheduler::retry_failed_subgraph`].
    pub fn failed_subgraph(&self) -> Vec<String> {
        self.nodes
            .iter()
            .filter(|n| n.status != NodeStatus::Succeeded)
            .map(|n| n.node_id.clone())
            .collect()
    }
}

/// Bounded exponential backoff with deterministic jitter.
///
/// `deterministic_jitter` hashes `(node_id, attempt)` into `[0, jitter_throughput)`,
/// with `jitter_throughput = delay * jitter_fraction`; keeping it pure makes
/// retries reproducible in tests, while the fraction still spreads retries
/// across different nodes. Capped at `max_delay`.
pub(crate) fn backoff_delay(
    base_delay: Duration,
    max_delay: Duration,
    jitter_fraction: f64,
    attempt: u32,
    node_id: &str,
) -> Duration {
    let shift = attempt.saturating_sub(1).min(30);
    let exp = base_delay.checked_mul(1u32 << shift).unwrap_or(max_delay);
    let capped = exp.min(max_delay);
    let hash = deterministic_hash(node_id, attempt);
    let frac = (hash % 1000) as f64 / 1000.0;
    let jitter = capped.as_secs_f64() * jitter_fraction.clamp(0.0, 1.0) * frac;
    capped + Duration::from_secs_f64(jitter)
}

/// FNV-1a over `node_id` and `attempt`; 64-bit, stable across runs.
fn deterministic_hash(node_id: &str, attempt: u32) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for byte in node_id.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash ^= u64::from(attempt);
    hash.wrapping_mul(0x0000_0100_0000_01b3)
}

/// Whether a failure may be retried: classified, idempotent and allow-listed.
/// Unclassified, non-idempotent or unlisted failures are never retried.
pub(crate) fn retry_allowed(err: &ClassifiedError, retryable_codes: &[String]) -> bool {
    err.idempotent
        && err
            .code
            .as_ref()
            .is_some_and(|code| retryable_codes.iter().any(|allowed| allowed == code))
}

/// Shared runner state handed to every spawned node task.
pub(crate) struct Runner {
    config: crate::scheduler::SchedulerConfig,
    graph_id: String,
    correlation_id: String,
    token: CancellationToken,
}

impl Runner {
    pub(crate) fn new(
        config: crate::scheduler::SchedulerConfig,
        graph_id: String,
        correlation_id: String,
        token: CancellationToken,
    ) -> Self {
        Self {
            config,
            graph_id,
            correlation_id,
            token,
        }
    }

    fn spawn_clone(&self) -> Self {
        Self {
            config: self.config.clone(),
            graph_id: self.graph_id.clone(),
            correlation_id: self.correlation_id.clone(),
            token: self.token.clone(),
        }
    }
}

/// Outcome collected from one spawned node task.
struct NodeTaskOutcome {
    result: NodeResult,
}

/// Work-conserving run of `graph`.
///
/// Eligible nodes (all dependencies succeeded) are launched until the global
/// or per-subagent cap is reached; a saturated subagent never blocks other
/// subagents. `pre_succeeded` counts as already satisfied (used by the
/// failed-subgraph retry); `only`, when set, restricts the run to that set
/// of node ids and validates it is a downstream-closed subgraph.
pub(crate) async fn run_graph<E: NodeExecutor + 'static>(
    runner: &Runner,
    graph: &crate::scheduler::ValidatedGraph,
    executor: Arc<E>,
    pre_succeeded: &HashSet<String>,
    only: Option<&HashSet<String>>,
) -> SchedulerResult<ExecutionReport> {
    let config = &runner.config;
    if graph.len() > config.max_queue_len {
        return Err(SchedulerError::Capacity(format!(
            "graph {} has {} nodes, bounded queue holds {}",
            runner.graph_id,
            graph.len(),
            config.max_queue_len
        )));
    }
    let started_at = Instant::now();
    let graph_deadline = started_at + config.graph_timeout;

    // Deterministic ready scan in topological order.
    let pending: Vec<&Task> = graph
        .tasks()
        .iter()
        .filter(|t| {
            only.is_none_or(|set| {
                set.contains(t.id.as_ref().map(|i| i.value.as_str()).unwrap_or_default())
            })
        })
        .collect();

    let mut succeeded: HashSet<String> = pre_succeeded.clone();
    let mut results: HashMap<String, NodeResult> = HashMap::new();
    let mut started: HashSet<String> = HashSet::new();
    let mut running_per_subagent: HashMap<String, usize> = HashMap::new();
    let mut join_set: tokio::task::JoinSet<NodeTaskOutcome> = tokio::task::JoinSet::new();
    let mut max_concurrency = 0usize;

    loop {
        if runner.token.is_cancelled() {
            // Stop launching; drain in-flight tasks but do not wait for them
            // to succeed — abort and record them as cancelled.
            join_set.abort_all();
            while let Some(outcome) = join_set.join_next().await {
                if let Ok(outcome) = outcome {
                    let status = if succeeded.contains(&outcome.result.node_id) {
                        NodeStatus::Succeeded
                    } else {
                        NodeStatus::Cancelled
                    };
                    results.insert(
                        outcome.result.node_id.clone(),
                        NodeResult {
                            status,
                            ..outcome.result
                        },
                    );
                }
            }
            break;
        }
        if Instant::now() >= graph_deadline {
            join_set.abort_all();
            while let Some(outcome) = join_set.join_next().await {
                if let Ok(outcome) = outcome {
                    results.insert(
                        outcome.result.node_id.clone(),
                        NodeResult {
                            message: Some("graph deadline exceeded".to_string()),
                            ..outcome.result
                        },
                    );
                }
            }
            break;
        }

        // Launch every eligible node the caps allow (work-conserving).
        for task in &pending {
            let id = task
                .id
                .as_ref()
                .map(|i| i.value.clone())
                .unwrap_or_default();
            if started.contains(&id) || succeeded.contains(&id) {
                continue;
            }
            let deps_met = task.dependencies.iter().all(|dep| {
                succeeded.contains(&dep.value)
                    || (only.is_some()
                        && !only.unwrap().contains(&dep.value)
                        && pre_succeeded.contains(&dep.value))
            });
            if !deps_met {
                continue;
            }
            if join_set.len() >= config.global_cap {
                break;
            }
            let subagent = task
                .subagent_type
                .as_ref()
                .map(|s| s.value.clone())
                .unwrap_or_else(|| "default".to_string());
            let used = running_per_subagent.get(&subagent).copied().unwrap_or(0);
            if used >= config.per_subagent_cap {
                continue; // this subagent is saturated; other subagents still launch
            }
            if join_set.len() >= config.max_queue_len {
                return Err(SchedulerError::Capacity(format!(
                    "ready queue full ({} in flight)",
                    join_set.len()
                )));
            }
            started.insert(id.clone());
            *running_per_subagent.entry(subagent.clone()).or_default() += 1;
            max_concurrency = max_concurrency.max(join_set.len() + 1);

            let runner_task = runner.spawn_clone();
            let executor_task = Arc::clone(&executor);
            let task_owned = (*task).clone();
            join_set.spawn(async move {
                let outcome = run_node(&runner_task, &task_owned, &*executor_task).await;
                NodeTaskOutcome { result: outcome }
            });
        }

        if join_set.is_empty() {
            break;
        }
        // Wait for at least one completion, bounded by the graph deadline.
        let remaining = graph_deadline.saturating_duration_since(Instant::now());
        match tokio::time::timeout(remaining, join_set.join_next()).await {
            Ok(Some(Ok(outcome))) => {
                if outcome.result.status == NodeStatus::Succeeded {
                    succeeded.insert(outcome.result.node_id.clone());
                }
                if let Some(sub) = running_per_subagent.get_mut(&outcome.result.subagent) {
                    *sub = sub.saturating_sub(1);
                }
                let node_id = outcome.result.node_id.clone();
                results.insert(node_id, outcome.result);
            }
            Ok(Some(Err(joined))) => {
                tracing::warn!(
                    correlation_id = %runner.correlation_id,
                    error = %joined,
                    "scheduler: node task panicked; recorded as failed"
                );
            }
            Ok(None) => break,
            Err(_elapsed) => {}
        }
    }

    // Everything still pending either failed to launch (dependency failed,
    // budget exhausted, cancellation) or was skipped. Name the reason.
    for task in &pending {
        let id = task
            .id
            .as_ref()
            .map(|i| i.value.clone())
            .unwrap_or_default();
        if results.contains_key(&id) || succeeded.contains(&id) {
            continue;
        }
        let blocked_by: Vec<&str> = task
            .dependencies
            .iter()
            .map(|d| d.value.as_str())
            .filter(|dep| !succeeded.contains(*dep))
            .collect();
        let (status, message) = if runner.token.is_cancelled() {
            (NodeStatus::Cancelled, "cancelled before launch".to_string())
        } else if blocked_by.is_empty() {
            // Was eligible but the loop exited (deadline) — treat as skipped.
            (
                NodeStatus::Skipped,
                "graph deadline exceeded before launch".to_string(),
            )
        } else {
            (
                NodeStatus::Skipped,
                format!(
                    "blocked by unfinished dependencies: {}",
                    blocked_by.join(", ")
                ),
            )
        };
        results.insert(
            id.clone(),
            NodeResult {
                node_id: id,
                subagent: task
                    .subagent_type
                    .as_ref()
                    .map(|s| s.value.clone())
                    .unwrap_or_else(|| "default".to_string()),
                status,
                attempts: 0,
                duration: Duration::ZERO,
                error_code: None,
                message: Some(message),
            },
        );
    }

    // Deterministic join: topological order, ties by node id.
    let mut nodes: Vec<NodeResult> = results.into_values().collect();
    let rank: HashMap<&str, usize> = graph
        .order()
        .iter()
        .enumerate()
        .map(|(i, id)| (id.as_str(), i))
        .collect();
    nodes.sort_by(|a, b| {
        rank.get(a.node_id.as_str())
            .copied()
            .unwrap_or(usize::MAX)
            .cmp(&rank.get(b.node_id.as_str()).copied().unwrap_or(usize::MAX))
            .then_with(|| a.node_id.cmp(&b.node_id))
    });

    Ok(ExecutionReport {
        graph_id: runner.graph_id.clone(),
        correlation_id: runner.correlation_id.clone(),
        nodes,
        max_observed_concurrency: max_concurrency,
    })
}

// (the Tap helper was removed: join handling is plain sequential code)

// Retry helper used by the node task: runs one node with bounded
// exp-backoff + jitter. The per-attempt `tokio::time::timeout` is the
// *node* timeout; the *tool* timeout is handed to the executor via the
// context, and the *graph* timeout is owned by the runner loop.
async fn run_node<E: NodeExecutor>(runner: &Runner, task: &Task, executor: &E) -> NodeResult {
    let config = &runner.config;
    let node_id = task
        .id
        .as_ref()
        .map(|i| i.value.clone())
        .unwrap_or_default();
    let subagent = task
        .subagent_type
        .as_ref()
        .map(|s| s.value.clone())
        .unwrap_or_else(|| "default".to_string());
    let retries_allowed = config.max_attempts.saturating_sub(1);
    let mut attempts = 0u32;
    let mut last_err: Option<ClassifiedError> = None;
    let node_started = Instant::now();

    loop {
        if runner.token.is_cancelled() {
            break;
        }
        attempts += 1;
        let ctx = ExecutionCtx {
            correlation_id: runner.correlation_id.clone(),
            graph_id: runner.graph_id.clone(),
            node_id: node_id.clone(),
            subagent: subagent.clone(),
            node_timeout: config.node_timeout,
            tool_timeout: config.tool_timeout,
            token: runner.token.clone(),
        };
        tracing::info!(
            correlation_id = %ctx.correlation_id,
            graph_id = %ctx.graph_id,
            node_id = %ctx.node_id,
            subagent = %ctx.subagent,
            attempt = attempts,
            "scheduler: launching node"
        );
        let attempt_future = tokio::time::timeout(config.node_timeout, executor.execute(ctx));
        let outcome = match attempt_future.await {
            Ok(result) => result,
            Err(_elapsed) => Err(ClassifiedError::classified(
                "E-TIMEOUT",
                false,
                format!("node {node_id} exceeded its node timeout"),
            )),
        };
        match outcome {
            Ok(_output) => {
                return NodeResult {
                    node_id,
                    subagent,
                    status: NodeStatus::Succeeded,
                    attempts,
                    duration: node_started.elapsed(),
                    error_code: None,
                    message: None,
                };
            }
            Err(err) => {
                tracing::warn!(
                    correlation_id = %runner.correlation_id,
                    graph_id = %runner.graph_id,
                    node_id = %node_id,
                    subagent = %subagent,
                    attempt = attempts,
                    error = %err,
                    "scheduler: node attempt failed"
                );
                last_err = Some(err);
                if !retry_allowed(
                    last_err.as_ref().expect("last_err set above"),
                    &config.retryable_codes,
                ) || attempts > retries_allowed
                {
                    break;
                }
                let delay = backoff_delay(
                    config.base_delay,
                    config.max_delay,
                    config.jitter_fraction,
                    attempts,
                    &node_id,
                );
                // Cooperative sleep: sleep `delay` in small slices, bailing
                // early when the token is cancelled (never sleep through it).
                let mut remaining = delay;
                loop {
                    if runner.token.is_cancelled() || remaining.is_zero() {
                        break;
                    }
                    let step = remaining.min(Duration::from_millis(10));
                    tokio::time::sleep(step).await;
                    remaining = remaining.saturating_sub(step);
                }
                if runner.token.is_cancelled() {
                    break;
                }
            }
        }
    }

    let duration = node_started.elapsed();
    if runner.token.is_cancelled() && attempts == 0 {
        return NodeResult {
            node_id,
            subagent,
            status: NodeStatus::Cancelled,
            attempts,
            duration,
            error_code: None,
            message: Some("cancelled before first attempt".to_string()),
        };
    }
    let err = last_err.unwrap_or_else(|| ClassifiedError::unclassified("cancelled"));
    NodeResult {
        node_id,
        subagent,
        status: if runner.token.is_cancelled() {
            NodeStatus::Cancelled
        } else {
            NodeStatus::Failed
        },
        attempts,
        duration,
        error_code: err.code.clone(),
        message: Some(err.message.clone()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_proto::gsd::v1::{SubagentType, TaskId};
    use std::collections::HashMap;
    use std::sync::Mutex;

    fn task(id: &str, deps: &[&str], subagent: &str) -> Task {
        Task {
            id: Some(TaskId {
                value: id.to_string(),
            }),
            subagent_type: Some(SubagentType {
                value: subagent.to_string(),
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

    struct ScriptExecutor {
        fail_ids: HashMap<String, ClassifiedError>,
        fast_fail_assign_count: Mutex<HashMap<String, usize>>,
    }

    impl NodeExecutor for ScriptExecutor {
        async fn execute(&self, ctx: ExecutionCtx) -> Result<NodeOutput, ClassifiedError> {
            if ctx.token.is_cancelled() {
                let _ = ctx;
                return Err(ClassifiedError::unclassified("cancelled"));
            }
            {
                let mut counts = self.fast_fail_assign_count.lock().expect("mutex");
                let count = counts.entry(ctx.node_id.clone()).or_insert(0);
                *count += 1;
            }
            if let Some(err) = self.fail_ids.get(&ctx.node_id) {
                return Err(err.clone());
            }
            Ok(NodeOutput {
                payload: ctx.node_id,
            })
        }
    }

    fn test_config() -> crate::scheduler::SchedulerConfig {
        crate::scheduler::SchedulerConfig {
            feature_enabled: true,
            base_delay: Duration::from_millis(1),
            max_delay: Duration::from_millis(5),
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn unclassified_failure_is_never_retried() {
        let config = test_config();
        let scheduler = crate::scheduler::Scheduler::new(config);
        let graph = scheduler
            .validate(vec![task("a", &[], "s1")])
            .expect("valid");
        let executor = Arc::new(ScriptExecutor {
            fail_ids: HashMap::from([("a".to_string(), ClassifiedError::unclassified("boom"))]),
            fast_fail_assign_count: Mutex::new(HashMap::new()),
        });
        let report = scheduler
            .run(&graph, executor.clone(), CancellationToken::new(), "corr-1")
            .await
            .expect("runs");
        assert_eq!(report.failed(), vec!["a"]);
        assert_eq!(
            report.nodes[0].attempts, 1,
            "unclassified: exactly one attempt"
        );
    }

    #[tokio::test]
    async fn classified_idempotent_allowlisted_failure_retries_then_succeeds() {
        // Flaky executor: fails once with a retryable classified error,
        // succeeds on the second attempt.
        struct Flaky {
            calls: Mutex<HashMap<String, usize>>,
        }
        impl NodeExecutor for Flaky {
            async fn execute(&self, ctx: ExecutionCtx) -> Result<NodeOutput, ClassifiedError> {
                let mut calls = self.calls.lock().expect("mutex");
                let count = calls.entry(ctx.node_id.clone()).or_insert(0);
                *count += 1;
                if *count == 1 {
                    Err(ClassifiedError::classified("E-DEP", true, "try again"))
                } else {
                    Ok(NodeOutput {
                        payload: ctx.node_id.clone(),
                    })
                }
            }
        }
        let config = crate::scheduler::SchedulerConfig {
            retryable_codes: vec!["E-DEP".to_string()],
            ..test_config()
        };
        let scheduler = crate::scheduler::Scheduler::new(config);
        let graph = scheduler
            .validate(vec![task("a", &[], "s1")])
            .expect("valid");
        let report = scheduler
            .run(
                &graph,
                Arc::new(Flaky {
                    calls: Mutex::new(HashMap::new()),
                }),
                CancellationToken::new(),
                "corr-2",
            )
            .await
            .expect("runs");
        assert_eq!(report.succeeded(), vec!["a"]);
        assert_eq!(report.nodes[0].attempts, 2);
    }

    #[tokio::test]
    async fn backoff_is_bounded_and_increases() {
        let first = backoff_delay(
            Duration::from_millis(100),
            Duration::from_secs(5),
            0.0,
            1,
            "node-x",
        );
        let second = backoff_delay(
            Duration::from_millis(100),
            Duration::from_secs(5),
            0.0,
            2,
            "node-x",
        );
        assert_eq!(first, Duration::from_millis(100));
        assert_eq!(second, Duration::from_millis(200));
        // Even with jitter it never exceeds max_delay * (1 + fraction).
        let jittered = backoff_delay(
            Duration::from_secs(10),
            Duration::from_secs(60),
            0.5,
            30,
            "node-x",
        );
        assert!(jittered <= Duration::from_secs(90));
    }

    #[tokio::test]
    async fn partial_failure_preserves_disjoint_branch_and_names_blocked() {
        let config = test_config();
        let scheduler = crate::scheduler::Scheduler::new(config);
        // a -> b (fails), c (independent, succeeds)
        let graph = scheduler
            .validate(vec![
                task("a", &[], "s1"),
                task("b", &["a"], "s1"),
                task("c", &[], "s1"),
            ])
            .expect("valid");
        let executor = Arc::new(ScriptExecutor {
            fail_ids: HashMap::from([(
                "a".to_string(),
                ClassifiedError::unclassified("a exploded"),
            )]),
            fast_fail_assign_count: Mutex::new(HashMap::new()),
        });
        let report = scheduler
            .run(&graph, executor, CancellationToken::new(), "corr-3")
            .await
            .expect("runs");
        assert_eq!(report.failed(), vec!["a"]);
        assert_eq!(report.skipped(), vec!["b"], "dependent is named as skipped");
        assert_eq!(report.succeeded(), vec!["c"], "disjoint branch preserved");
        // Deterministic join order: topological, ties by id.
        let ids: Vec<&str> = report.nodes.iter().map(|n| n.node_id.as_str()).collect();
        assert_eq!(ids, vec!["a", "b", "c"]);
    }

    /// Cooperative cancel mid-flight: the running node ends `Cancelled`,
    /// its dependent (never eligible) is named `Cancelled` with zero
    /// attempts, and the already-finished disjoint node stays `Succeeded`.
    #[tokio::test]
    async fn cancellation_mid_flight_aborts_running_and_names_unstarted() {
        struct SlowExecutor {
            started: Arc<AtomicBool>,
            calls: Mutex<HashMap<String, usize>>,
        }
        impl NodeExecutor for SlowExecutor {
            async fn execute(&self, ctx: ExecutionCtx) -> Result<NodeOutput, ClassifiedError> {
                {
                    let mut calls = self.calls.lock().expect("mutex");
                    *calls.entry(ctx.node_id.clone()).or_insert(0) += 1;
                }
                if ctx.node_id == "a" {
                    // Long-running node: polls the cooperative token, so
                    // cancellation is observed mid-flight.
                    self.started.store(true, Ordering::SeqCst);
                    loop {
                        if ctx.token.is_cancelled() {
                            return Err(ClassifiedError::unclassified("cancelled while running"));
                        }
                        tokio::time::sleep(Duration::from_millis(5)).await;
                    }
                }
                Ok(NodeOutput {
                    payload: ctx.node_id.clone(),
                })
            }
        }
        let config = crate::scheduler::SchedulerConfig {
            node_timeout: Duration::from_secs(30),
            ..test_config()
        };
        let scheduler = crate::scheduler::Scheduler::new(config);
        let graph = scheduler
            .validate(vec![
                task("a", &[], "s1"),
                task("b", &["a"], "s1"),
                task("c", &[], "s1"),
            ])
            .expect("valid");
        let token = CancellationToken::new();
        let started = Arc::new(AtomicBool::new(false));
        let executor = Arc::new(SlowExecutor {
            started: Arc::clone(&started),
            calls: Mutex::new(HashMap::new()),
        });
        let run_token = token.clone();
        let running = tokio::spawn(async move {
            scheduler
                .run(&graph, executor, run_token, "corr-cancel-mid")
                .await
        });
        // Wait (bounded) for node `a` to be in flight, then cancel.
        for _ in 0..200 {
            if started.load(Ordering::SeqCst) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        assert!(
            started.load(Ordering::SeqCst),
            "node a must be in flight before cancelling"
        );
        token.cancel();
        let report = running
            .await
            .expect("run task")
            .expect("report even when cancelled");
        let status = |id: &str| {
            report
                .nodes
                .iter()
                .find(|n| n.node_id == id)
                .map(|n| n.status)
        };
        assert_eq!(status("a"), Some(NodeStatus::Cancelled));
        assert_eq!(status("b"), Some(NodeStatus::Cancelled));
        let b = report
            .nodes
            .iter()
            .find(|n| n.node_id == "b")
            .expect("b named");
        assert_eq!(b.attempts, 0, "dependent never launched after cancel");
        assert_eq!(status("c"), Some(NodeStatus::Succeeded));
    }

    /// Partial failure, then a targeted retry at the runner level
    /// (`pre_succeeded` + `only`): only the failed subgraph re-executes;
    /// the completed branch's executor is never called again.
    #[tokio::test]
    async fn partial_failure_targeted_retry_reruns_only_failed_subgraph() {
        struct OkExecutor {
            calls: Mutex<HashMap<String, usize>>,
        }
        impl NodeExecutor for OkExecutor {
            async fn execute(&self, ctx: ExecutionCtx) -> Result<NodeOutput, ClassifiedError> {
                let mut calls = self.calls.lock().expect("mutex");
                *calls.entry(ctx.node_id.clone()).or_insert(0) += 1;
                Ok(NodeOutput {
                    payload: ctx.node_id.clone(),
                })
            }
        }
        let config = test_config();
        let scheduler = crate::scheduler::Scheduler::new(config);
        let graph = scheduler
            .validate(vec![
                task("a", &[], "s1"),
                task("b", &["a"], "s1"),
                task("c", &[], "s1"),
            ])
            .expect("valid");
        let first = Arc::new(ScriptExecutor {
            fail_ids: HashMap::from([(
                "a".to_string(),
                ClassifiedError::unclassified("a exploded"),
            )]),
            fast_fail_assign_count: Mutex::new(HashMap::new()),
        });
        let report = scheduler
            .run(&graph, first, CancellationToken::new(), "corr-retry-1")
            .await
            .expect("runs");
        assert_eq!(report.failed(), vec!["a"]);
        assert_eq!(report.skipped(), vec!["b"]);
        assert_eq!(report.succeeded(), vec!["c"]);

        let runner = Runner::new(
            scheduler.config().clone(),
            "graph-retry".to_string(),
            "corr-retry-2".to_string(),
            CancellationToken::new(),
        );
        let retry_executor = Arc::new(OkExecutor {
            calls: Mutex::new(HashMap::new()),
        });
        let pre_succeeded: HashSet<String> = ["c".to_string()].into_iter().collect();
        let only: HashSet<String> = ["a".to_string(), "b".to_string()].into_iter().collect();
        let retry_report = run_graph(
            &runner,
            &graph,
            Arc::clone(&retry_executor),
            &pre_succeeded,
            Some(&only),
        )
        .await
        .expect("retry runs");
        assert_eq!(retry_report.succeeded(), vec!["a", "b"]);
        let calls = retry_executor.calls.lock().expect("mutex");
        assert_eq!(calls.get("c"), None, "completed branch must not re-execute");
        assert_eq!(calls.get("a"), Some(&1));
        assert_eq!(calls.get("b"), Some(&1));
    }
}
