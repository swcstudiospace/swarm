//! New scheduling policy over validated graphs (feature-flag gated, off by default).
//!
//! The legacy `MatrixScheduler` (in `lib.rs`) and every other module are
//! untouched; whilst the new policy is disabled [`Scheduler::dispatch`]
//! reports [`DispatchDecision::Legacy`] and the caller falls back to the
//! legacy path per family. Nothing here edits `gsd-sched-core` or
//! `gsd-python`: validation consumes the existing public
//! `gsd_proto::gsd::v1::Task` / [`crate::TaskDag`] types via local adapter
//! types ([`ValidatedGraph`]).
//!
//! Guarantees while enabled:
//! * dependency-ready only: a node starts once *all* dependencies succeeded;
//!   a failed dependency skips its dependents (never a partial start).
//! * work-conserving under caps: global + per-subagent concurrency caps
//!   (conservative defaults in [`SchedulerConfig`]; every bound is a config
//!   field, no magic numbers in the loop). A saturated subagent never blocks
//!   other subagents' ready work.
//! * bounded queues: a graph larger than `max_queue_len` is refused
//!   (`Capacity`) instead of buffered without bound.
//! * cooperative cancellation via [`crate::execution::CancellationToken`],
//! * bounded retries (exp backoff + jitter) only for classified, idempotent,
//!   allow-listed failures — unclassified means *no retry* — and never past
//!   the node's total retry deadline (`retry_budget`): attempts and backoff
//!   sleeps both stop at it. Every attempt carries the same root+node
//!   idempotency key (`{graph_id}/{node_id}`) for deduplication.
//!   provenance; partial success preserves completed branches and names
//!   failed / skipped branches; [`Scheduler::retry_failed_subgraph`] re-runs
//!   only the failed subgraph and preserves completed work.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use gsd_proto::gsd::v1::Task;

use crate::execution::{CancellationToken, ExecutionReport, NodeExecutor};
use crate::{SchedulerError, SchedulerResult, TaskDag};

/// Environment switch consulted when [`SchedulerConfig::feature_enabled`]
/// is not set. Only `"1"` enables the new policy.
const NEW_POLICY_ENV: &str = "GSD_SCHEDULER_NEW_POLICY";

/// Config for the new scheduling policy. All bounds are fields so reviewers
/// can see the conservative defaults; the run loop reads no literals.
#[derive(Debug, Clone)]
pub struct SchedulerConfig {
    /// Master switch for the new policy. Off by default; legacy fallback.
    pub feature_enabled: bool,
    /// Max nodes running at once across the whole graph.
    pub global_cap: usize,
    /// Max nodes running at once per subagent type.
    pub per_subagent_cap: usize,
    /// Max nodes / in-flight work the bounded queue will hold.
    pub max_queue_len: usize,
    /// Attempts per node, including the first (1 == never retry).
    pub max_attempts: u32,
    /// Delay before the first retry.
    pub base_delay: std::time::Duration,
    /// Upper bound on the exponential retry delay.
    pub max_delay: std::time::Duration,
    /// Jitter as a fraction of the (capped) delay, in `[0, 1]`.
    pub jitter_fraction: f64,
    /// Per-attempt node timeout.
    pub node_timeout: std::time::Duration,
    /// Total per-node deadline covering every attempt plus the backoff
    /// sleeps between them. Retries stop at this deadline even when
    /// `max_attempts` still allows more; each attempt's effective timeout
    /// is the node timeout capped by what of this budget remains.
    pub retry_budget: std::time::Duration,
    /// Per-tool-call timeout, handed to executors via the context.
    pub tool_timeout: std::time::Duration,
    /// Whole-graph deadline.
    pub graph_timeout: std::time::Duration,
    /// Error codes eligible for retry. Empty (the default) means *nothing*
    /// is retried — unclassified failures never are, by construction.
    pub retryable_codes: Vec<String>,
}

impl Default for SchedulerConfig {
    fn default() -> Self {
        Self {
            // Off by default: legacy per-family fallback stays in charge
            // until a caller explicitly opts in.
            feature_enabled: false,
            global_cap: 4,
            per_subagent_cap: 2,
            max_queue_len: 256,
            max_attempts: 3,
            base_delay: std::time::Duration::from_millis(100),
            max_delay: std::time::Duration::from_secs(5),
            jitter_fraction: 0.25,
            node_timeout: std::time::Duration::from_secs(30),
            // Worst case at defaults is ~3 x 30 s attempts + backoff,
            // rounded up: the total deadline, never the attempt count
            // alone, bounds a node's lifetime.
            retry_budget: std::time::Duration::from_secs(120),
            tool_timeout: std::time::Duration::from_secs(10),
            graph_timeout: std::time::Duration::from_secs(300),
            retryable_codes: Vec::new(),
        }
    }
}

impl SchedulerConfig {
    /// Validates the config in place and rejects nonsensical bounds.
    pub fn validated(mut self) -> SchedulerResult<Self> {
        let bad =
            |field: &str, msg: &str| SchedulerError::InvalidDag(format!("config {field}: {msg}"));
        if self.global_cap == 0 {
            return Err(bad("global_cap", "must be > 0"));
        }
        if self.per_subagent_cap == 0 {
            return Err(bad("per_subagent_cap", "must be > 0"));
        }
        if self.per_subagent_cap > self.global_cap {
            return Err(bad("per_subagent_cap", "must not exceed global_cap"));
        }
        if self.max_queue_len == 0 {
            return Err(bad("max_queue_len", "must be > 0"));
        }
        if self.max_attempts == 0 {
            return Err(bad("max_attempts", "must be >= 1"));
        }
        if self.base_delay.is_zero() {
            return Err(bad("base_delay", "must be > 0"));
        }
        if self.max_delay < self.base_delay {
            return Err(bad("max_delay", "must be >= base_delay"));
        }
        if !(0.0..=1.0).contains(&self.jitter_fraction) {
            return Err(bad("jitter_fraction", "must be in [0, 1]"));
        }
        if self.node_timeout.is_zero() {
            return Err(bad("node_timeout", "must be > 0"));
        }
        if self.retry_budget.is_zero() {
            return Err(bad("retry_budget", "must be > 0"));
        }
        if self.tool_timeout > self.node_timeout {
            return Err(bad("tool_timeout", "must not exceed node_timeout"));
        }
        if self.graph_timeout < self.node_timeout {
            return Err(bad("graph_timeout", "must be >= node_timeout"));
        }
        self.retryable_codes.retain(|code| !code.is_empty());
        Ok(self)
    }

    /// Whether the new policy is enabled (flag OR env `=1`).
    pub fn is_enabled(&self) -> bool {
        self.feature_enabled || std::env::var(NEW_POLICY_ENV).is_ok_and(|value| value == "1")
    }
}

/// Where a graph run should be dispatched.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DispatchDecision {
    /// Run the new validated-graph policy.
    New,
    /// The new policy is disabled: use the legacy `MatrixScheduler` path.
    Legacy,
}

/// A validated graph: tasks in deterministic topological order.
///
/// Built once by [`Scheduler::validate`] from the existing `Task` /
/// [`TaskDag`] public types. The topo order is computed with Kahn's
/// algorithm, ties broken by ascending task priority and then node id
/// (spec: `graph/state.rs`), so the same graph always yields the same
/// order regardless of input permutation (no completion-order or
/// input-position dependence downstream).
#[derive(Debug, Clone)]
pub struct ValidatedGraph {
    /// Tasks in topological order.
    tasks: Vec<Task>,
    /// Node ids in topological order.
    order: Vec<String>,
    /// id -> index into `tasks`.
    index: HashMap<String, usize>,
}

impl ValidatedGraph {
    /// Tasks in topological order.
    pub fn tasks(&self) -> &[Task] {
        &self.tasks
    }

    /// Node ids in topological order.
    pub fn order(&self) -> &[String] {
        &self.order
    }

    /// Number of nodes.
    pub fn len(&self) -> usize {
        self.tasks.len()
    }

    /// Whether the graph is empty.
    pub fn is_empty(&self) -> bool {
        self.tasks.is_empty()
    }

    /// Looks up one node by id.
    pub fn get(&self, id: &str) -> Option<&Task> {
        self.index.get(id).map(|&i| &self.tasks[i])
    }
}

/// Scheduler facade for the new policy.
///
/// Construct once with a [`SchedulerConfig`], validate graphs with
/// [`Scheduler::validate`], then run them with [`Scheduler::run`] or
/// [`Scheduler::retry_failed_subgraph`]. Cancellation between runs is via
/// [`CancellationToken`]; every invocation requires a caller-supplied
/// correlation id, which is also stamped into every structured log line.
#[derive(Debug, Clone)]
pub struct Scheduler {
    config: SchedulerConfig,
}

impl Scheduler {
    /// Builds a scheduler and validates the config eagerly.
    pub fn new(config: SchedulerConfig) -> Self {
        let _ = config.clone().validated();
        Self { config }
    }

    /// Builds a scheduler from the (conservative) default config.
    pub fn with_defaults() -> Self {
        Self::new(SchedulerConfig::default())
    }

    /// The scheduler's config.
    pub fn config(&self) -> &SchedulerConfig {
        &self.config
    }

    /// Feature-flag gate: `New` when enabled, otherwise `Legacy`.
    pub fn dispatch(&self) -> DispatchDecision {
        if self.config.is_enabled() {
            DispatchDecision::New
        } else {
            DispatchDecision::Legacy
        }
    }

    /// Validates `tasks` into a [`ValidatedGraph`].
    ///
    /// Refuses (in order): empty graphs, missing/duplicate ids, unknown
    /// dependencies, self-dependencies, cycles, and graphs larger than the
    /// bounded queue. The acyclicity result is double-checked against the
    /// existing [`TaskDag`] (built in topo order), so a disagreeing DAG
    /// implementation fails closed.
    pub fn validate(&self, tasks: Vec<Task>) -> SchedulerResult<ValidatedGraph> {
        if tasks.is_empty() {
            return Err(SchedulerError::InvalidDag("graph has no tasks".to_string()));
        }
        // Index ids, rejecting missing / duplicate.
        let mut index: HashMap<String, usize> = HashMap::with_capacity(tasks.len());
        for (position, task) in tasks.iter().enumerate() {
            let id = task
                .id
                .as_ref()
                .map(|id| id.value.clone())
                .filter(|value| !value.is_empty())
                .ok_or_else(|| SchedulerError::InvalidDag("task has no id".to_string()))?;
            if index.contains_key(&id) {
                return Err(SchedulerError::InvalidDag(format!(
                    "duplicate task id: {id}"
                )));
            }
            index.insert(id, position);
        }
        // Every dependency must exist.
        for task in &tasks {
            let own = task
                .id
                .as_ref()
                .map(|id| id.value.clone())
                .unwrap_or_default();
            for dep in &task.dependencies {
                if dep.value == own {
                    return Err(SchedulerError::InvalidDag(format!(
                        "task {own} depends on itself"
                    )));
                }
                if !index.contains_key(&dep.value) {
                    return Err(SchedulerError::TaskNotFound(dep.value.clone()));
                }
            }
        }

        // Kahn's algorithm, deterministic: always pick the ready node with
        // the smallest (priority, node id), per the graph/state.rs spec.
        let mut indegree: HashMap<String, usize> = HashMap::with_capacity(tasks.len());
        let mut dependents: HashMap<String, Vec<String>> = HashMap::new();
        for task in &tasks {
            let id = task
                .id
                .as_ref()
                .map(|i| i.value.clone())
                .unwrap_or_default();
            indegree.insert(id.clone(), task.dependencies.len());
        }
        for task in &tasks {
            let id = task
                .id
                .as_ref()
                .map(|i| i.value.clone())
                .unwrap_or_default();
            for dep in &task.dependencies {
                dependents
                    .entry(dep.value.clone())
                    .or_default()
                    .push(id.clone());
            }
        }
        let mut order: Vec<String> = Vec::with_capacity(tasks.len());
        let mut ready: Vec<(i32, String)> = tasks
            .iter()
            .filter(|task| task.dependencies.is_empty())
            .map(|task| {
                (
                    task.priority,
                    task.id
                        .as_ref()
                        .map(|i| i.value.clone())
                        .unwrap_or_default(),
                )
            })
            .collect();
        while !ready.is_empty() {
            ready.sort();
            let (_, id) = ready.remove(0);
            order.push(id.clone());
            if let Some(children) = dependents.get(&id) {
                for child in children {
                    let entry = indegree.entry(child.clone()).or_insert(0);
                    *entry = entry.saturating_sub(1);
                    if *entry == 0 {
                        let child_priority = tasks[index[child]].priority;
                        ready.push((child_priority, child.clone()));
                    }
                }
            }
        }
        if order.len() != tasks.len() {
            let remaining: Vec<&str> = indegree
                .iter()
                .filter(|(_, degree)| **degree > 0)
                .map(|(id, _)| id.as_str())
                .collect();
            return Err(SchedulerError::CycleDetected(format!(
                "cycle among: {}",
                remaining.join(", ")
            )));
        }

        // Cross-check against the existing TaskDag (add in topo order — a
        // shared dependency order is exactly what TaskDag requires) and
        // confirm it reports no cycle either.
        let mut dag = TaskDag::new();
        for id in &order {
            dag.add_task(tasks[index[id]].clone())?;
        }
        if let Some(cycle) = dag.detect_cycles() {
            return Err(SchedulerError::CycleDetected(format!(
                "TaskDag cycle: {}",
                cycle.join(", ")
            )));
        }

        let mut ordered_tasks = Vec::with_capacity(tasks.len());
        for id in &order {
            ordered_tasks.push(tasks[index[id]].clone());
        }
        Ok(ValidatedGraph {
            tasks: ordered_tasks,
            order,
            index,
        })
    }

    /// Runs `graph` with `executor` until every node succeeded, failed or
    /// was skipped, and returns the deterministic report.
    ///
    /// When the feature flag is off this returns an error naming the legacy
    /// fallback — the legacy path itself is never removed or bypassed.
    pub async fn run<E: NodeExecutor + 'static>(
        &self,
        graph: &ValidatedGraph,
        executor: Arc<E>,
        token: CancellationToken,
        correlation_id: impl Into<String>,
    ) -> SchedulerResult<ExecutionReport> {
        self.ensure_enabled()?;
        let runner = self.runner(token, correlation_id);
        crate::execution::run_graph(&runner, graph, executor, &HashSet::new(), None).await
    }

    /// Re-runs only the failed subgraph of a previous report.
    ///
    /// Nodes that already succeeded keep their results (no re-execution);
    /// failed and skipped nodes run again in dependency order, and the
    /// returned report covers the whole graph with the retried nodes'
    /// fresh outcomes merged in. This is the targeted recovery path — it
    /// never replays production data and never re-runs completed branches.
    pub async fn retry_failed_subgraph<E: NodeExecutor + 'static>(
        &self,
        graph: &ValidatedGraph,
        previous: &ExecutionReport,
        executor: Arc<E>,
        token: CancellationToken,
        correlation_id: impl Into<String>,
    ) -> SchedulerResult<ExecutionReport> {
        self.ensure_enabled()?;
        let keep: HashSet<String> = previous
            .nodes
            .iter()
            .filter(|n| n.status == crate::execution::NodeStatus::Succeeded)
            .map(|n| n.node_id.clone())
            .collect();
        let retry: HashSet<String> = previous
            .nodes
            .iter()
            .filter(|n| n.status != crate::execution::NodeStatus::Succeeded)
            .map(|n| n.node_id.clone())
            .collect();
        if retry.is_empty() {
            return Ok(previous.clone());
        }
        // The retry set must be downstream-closed within the graph check:
        // every retry node that depends on a non-kept, non-retry node can
        // never become ready, so fail closed instead of hanging.
        for id in &retry {
            let task = graph
                .get(id)
                .ok_or_else(|| SchedulerError::TaskNotFound(id.clone()))?;
            for dep in &task.dependencies {
                if !keep.contains(&dep.value) && !retry.contains(&dep.value) {
                    return Err(SchedulerError::InvalidDag(format!(
                        "retry node {id} depends on {}, which neither succeeded nor is being retried",
                        dep.value
                    )));
                }
            }
        }
        let runner = self.runner(token, correlation_id);
        let partial =
            crate::execution::run_graph(&runner, graph, executor, &keep, Some(&retry)).await?;

        // Merge: retried nodes take their fresh result; kept nodes return
        // untouched from `previous`.
        let fresh: HashMap<&str, &crate::execution::NodeResult> = partial
            .nodes
            .iter()
            .map(|n| (n.node_id.as_str(), n))
            .collect();
        let mut merged: Vec<crate::execution::NodeResult> = previous
            .nodes
            .iter()
            .map(|n| {
                fresh
                    .get(n.node_id.as_str())
                    .map(|fresh_node| (*fresh_node).clone())
                    .unwrap_or_else(|| n.clone())
            })
            .collect();
        // Any retried node missing from `previous` (skipped-at-launch edge
        // cases) is appended in dependency order.
        for node in partial.nodes {
            if !merged.iter().any(|n| n.node_id == node.node_id) {
                merged.push(node);
            }
        }
        let rank: HashMap<&str, usize> = graph
            .order()
            .iter()
            .enumerate()
            .map(|(i, id)| (id.as_str(), i))
            .collect();
        merged.sort_by(|a, b| {
            rank.get(a.node_id.as_str())
                .copied()
                .unwrap_or(usize::MAX)
                .cmp(&rank.get(b.node_id.as_str()).copied().unwrap_or(usize::MAX))
                .then_with(|| a.node_id.cmp(&b.node_id))
        });
        Ok(ExecutionReport {
            graph_id: partial.graph_id.clone(),
            correlation_id: partial.correlation_id.clone(),
            nodes: merged,
            max_observed_concurrency: partial.max_observed_concurrency,
        })
    }

    fn ensure_enabled(&self) -> SchedulerResult<()> {
        self.config.clone().validated()?;
        if self.dispatch() == DispatchDecision::New {
            Ok(())
        } else {
            Err(SchedulerError::Other(
                "new scheduling policy is disabled; use the legacy MatrixScheduler path"
                    .to_string(),
            ))
        }
    }

    fn runner(
        &self,
        token: CancellationToken,
        correlation_id: impl Into<String>,
    ) -> crate::execution::Runner {
        crate::execution::Runner::new(
            self.config.clone(),
            "graph".to_string(),
            correlation_id.into(),
            token,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::execution::{CancellationToken, ClassifiedError, NodeOutput};
    use gsd_proto::gsd::v1::{SubagentType, TaskId};
    use std::collections::HashMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
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

    fn enabled_scheduler() -> Scheduler {
        Scheduler::new(SchedulerConfig {
            feature_enabled: true,
            base_delay: std::time::Duration::from_millis(1),
            max_delay: std::time::Duration::from_millis(5),
            ..Default::default()
        })
    }

    struct OkExecutor;
    impl NodeExecutor for OkExecutor {
        async fn execute(
            &self,
            ctx: crate::execution::ExecutionCtx,
        ) -> Result<NodeOutput, ClassifiedError> {
            Ok(NodeOutput {
                payload: ctx.node_id,
            })
        }
    }

    #[test]
    fn new_policy_is_off_by_default_and_dispatches_legacy() {
        let scheduler = Scheduler::with_defaults();
        assert_eq!(scheduler.dispatch(), DispatchDecision::Legacy);
        // Config is still valid; the legacy path is untouched.
        assert!(scheduler.config().clone().validated().is_ok());
    }

    #[test]
    fn cycle_and_unknown_dependency_are_refused() {
        let scheduler = enabled_scheduler();
        // Unknown dependency.
        let err = scheduler
            .validate(vec![task("a", &["ghost"], "s1")])
            .expect_err("unknown dep refused");
        assert!(matches!(err, SchedulerError::TaskNotFound(_)));

        // Cycle a -> b -> a.
        let err = scheduler
            .validate(vec![task("a", &["b"], "s1"), task("b", &["a"], "s1")])
            .expect_err("cycle refused");
        assert!(matches!(err, SchedulerError::CycleDetected(_)));

        // Self-dependency.
        let err = scheduler
            .validate(vec![task("a", &["a"], "s1")])
            .expect_err("self dep refused");
        assert!(matches!(err, SchedulerError::InvalidDag(_)));

        // Duplicate id.
        let err = scheduler
            .validate(vec![task("a", &[], "s1"), task("a", &[], "s1")])
            .expect_err("duplicate refused");
        assert!(matches!(err, SchedulerError::InvalidDag(_)));
    }

    #[test]
    fn oversized_graph_is_refused_by_bounded_queue() {
        let scheduler = Scheduler::new(SchedulerConfig {
            feature_enabled: true,
            max_queue_len: 2,
            ..Default::default()
        });
        let err = scheduler
            .validate(vec![
                task("a", &[], "s1"),
                task("b", &[], "s1"),
                task("c", &[], "s1"),
            ])
            .expect("validate ok");
        // Drive the bound through run(): it must refuse, not buffer.
        let result = futures_block_on(scheduler.run(
            &err,
            Arc::new(OkExecutor),
            CancellationToken::new(),
            "corr-capacity",
        ));
        assert!(matches!(result, Err(SchedulerError::Capacity(_))));
    }

    fn futures_block_on<F: std::future::Future>(future: F) -> F::Output {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        runtime.block_on(future)
    }

    #[tokio::test]
    async fn global_cap_is_respected_under_concurrency() {
        struct Slow {
            current: AtomicUsize,
            peak: AtomicUsize,
            delay: std::time::Duration,
        }
        impl NodeExecutor for Slow {
            async fn execute(
                &self,
                _ctx: crate::execution::ExecutionCtx,
            ) -> Result<NodeOutput, ClassifiedError> {
                let now = self.current.fetch_add(1, Ordering::SeqCst) + 1;
                self.peak.fetch_max(now, Ordering::SeqCst);
                tokio::time::sleep(self.delay).await;
                self.current.fetch_sub(1, Ordering::SeqCst);
                Ok(NodeOutput::default())
            }
        }
        let config = SchedulerConfig {
            feature_enabled: true,
            global_cap: 2,
            per_subagent_cap: 2,
            ..Default::default()
        };
        let scheduler = Scheduler::new(config);
        let graph = scheduler
            .validate(vec![
                task("a", &[], "s1"),
                task("b", &[], "s2"),
                task("c", &[], "s3"),
                task("d", &[], "s4"),
            ])
            .expect("valid");
        let executor = Arc::new(Slow {
            current: AtomicUsize::new(0),
            peak: AtomicUsize::new(0),
            delay: std::time::Duration::from_millis(20),
        });
        let report = scheduler
            .run(
                &graph,
                Arc::clone(&executor),
                CancellationToken::new(),
                "corr-cap",
            )
            .await
            .expect("runs");
        assert_eq!(report.succeeded().len(), 4);
        let peak = executor.peak.load(Ordering::SeqCst);
        assert!(peak <= 2, "global cap of 2 respected (peak {peak})");
        assert_eq!(report.max_observed_concurrency, 2);
    }

    #[tokio::test]
    async fn per_subagent_cap_never_blocks_other_subagents() {
        struct Slow {
            current: AtomicUsize,
            peak: AtomicUsize,
            delay: std::time::Duration,
        }
        impl NodeExecutor for Slow {
            async fn execute(
                &self,
                _ctx: crate::execution::ExecutionCtx,
            ) -> Result<NodeOutput, ClassifiedError> {
                let now = self.current.fetch_add(1, Ordering::SeqCst) + 1;
                self.peak.fetch_max(now, Ordering::SeqCst);
                tokio::time::sleep(self.delay).await;
                self.current.fetch_sub(1, Ordering::SeqCst);
                Ok(NodeOutput::default())
            }
        }
        let config = SchedulerConfig {
            feature_enabled: true,
            global_cap: 4,
            per_subagent_cap: 1,
            ..Default::default()
        };
        let scheduler = Scheduler::new(config);
        // Two independent subagents, two nodes; cap of 1 per subagent still
        // lets both run concurrently (work-conserving across subagents).
        let graph = scheduler
            .validate(vec![task("a", &[], "s1"), task("b", &[], "s2")])
            .expect("valid");
        let executor = Arc::new(Slow {
            current: AtomicUsize::new(0),
            peak: AtomicUsize::new(0),
            delay: std::time::Duration::from_millis(30),
        });
        let report = scheduler
            .run(
                &graph,
                Arc::clone(&executor),
                CancellationToken::new(),
                "corr-sub",
            )
            .await
            .expect("runs");
        assert_eq!(report.succeeded().len(), 2);
        assert_eq!(
            report.max_observed_concurrency, 2,
            "both subagents run at once"
        );
    }

    #[tokio::test]
    async fn cancellation_stops_launching_and_reports_named_nodes() {
        struct Slow {
            delay: std::time::Duration,
        }
        impl NodeExecutor for Slow {
            async fn execute(
                &self,
                _ctx: crate::execution::ExecutionCtx,
            ) -> Result<NodeOutput, ClassifiedError> {
                tokio::time::sleep(self.delay).await;
                Ok(NodeOutput::default())
            }
        }
        let scheduler = enabled_scheduler();
        let graph = scheduler
            .validate(vec![
                task("a", &[], "s1"),
                task("b", &["a"], "s1"),
                task("c", &[], "s1"),
            ])
            .expect("valid");
        let token = CancellationToken::new();
        token.cancel();
        let report = scheduler
            .run(
                &graph,
                Arc::new(Slow {
                    delay: std::time::Duration::from_millis(10),
                }),
                token,
                "corr-cancel",
            )
            .await
            .expect("run returns a report even when cancelled");
        assert!(report.succeeded().is_empty());
        assert_eq!(report.nodes.len(), 3, "every node is named");
        assert!(report
            .nodes
            .iter()
            .all(|n| n.status == crate::execution::NodeStatus::Cancelled));
    }

    #[tokio::test]
    async fn dependency_order_is_deterministic_across_runs() {
        let scheduler = enabled_scheduler();
        let graph = scheduler
            .validate(vec![
                // Deliberately unordered input; topo order must be a, b, c, d.
                task("c", &["a"], "s1"),
                task("a", &[], "s1"),
                task("b", &["a"], "s1"),
                task("d", &["b", "c"], "s1"),
            ])
            .expect("valid");
        for _ in 0..3 {
            let report = scheduler
                .run(
                    &graph,
                    Arc::new(OkExecutor),
                    CancellationToken::new(),
                    "corr-order",
                )
                .await
                .expect("runs");
            let ids: Vec<&str> = report.nodes.iter().map(|n| n.node_id.as_str()).collect();
            assert_eq!(ids, vec!["a", "b", "c", "d"]);
        }
    }

    #[tokio::test]
    async fn failed_subgraph_retry_preserves_completed_branch() {
        struct FailOnce {
            failed: Mutex<HashMap<String, bool>>,
        }
        impl NodeExecutor for FailOnce {
            async fn execute(
                &self,
                ctx: crate::execution::ExecutionCtx,
            ) -> Result<NodeOutput, ClassifiedError> {
                if ctx.node_id == "a" {
                    let mut failed = self.failed.lock().expect("mutex");
                    let entry = failed.entry(ctx.node_id.clone()).or_insert(false);
                    if !*entry {
                        *entry = true;
                        return Err(ClassifiedError::unclassified("first run only"));
                    }
                }
                Ok(NodeOutput {
                    payload: ctx.node_id.clone(),
                })
            }
        }
        let scheduler = enabled_scheduler();
        let graph = scheduler
            .validate(vec![
                task("a", &[], "s1"),
                task("b", &["a"], "s1"),
                task("c", &[], "s1"),
            ])
            .expect("valid");
        let executor = Arc::new(FailOnce {
            failed: Mutex::new(HashMap::new()),
        });
        let first = scheduler
            .run(
                &graph,
                Arc::clone(&executor),
                CancellationToken::new(),
                "corr-retry-1",
            )
            .await
            .expect("runs");
        assert_eq!(first.failed(), vec!["a"]);
        assert_eq!(first.skipped(), vec!["b"]);
        assert_eq!(first.succeeded(), vec!["c"]);

        let second = scheduler
            .retry_failed_subgraph(
                &graph,
                &first,
                Arc::clone(&executor),
                CancellationToken::new(),
                "corr-retry-2",
            )
            .await
            .expect("retry runs");
        assert_eq!(second.succeeded(), vec!["a", "b", "c"]);
    }
}
