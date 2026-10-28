//! Flat fan-out matrix join: dispatch a batch, collect results as they arrive, and emit an ordered
//! completion log.
//!
//! Dispatch runs through [`run_force_parallel`] (B4), which draws every admission from
//! [`ready_frontier`](crate::ready_frontier::ready_frontier) (B3). The flat path has no round barrier:
//! a finished task frees its slot at once. Results are kept in arrival order for diagnostics only.
//! The [`CompletionLog`] is ordered by `(stage, DAG depth, creation, task id)`, so it does not depend
//! on arrival order. Each record carries its provenance: producer, attempt and verdict envelope.
//!
//! The dispatch order is recorded in [`MatrixJoinReport::dispatch_order`].
//! [`replay_matrix_join`] runs that recorded order again and rebuilds the same log.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::Arc;

use gsd_proto::gsd::v1::Task;
use tokio::task::JoinError;

use crate::dag_build::{build_dag_from, BuiltDag};
use crate::force_parallel_dispatch::{
    run_force_parallel, DispatchConfig, DispatchMode, TaskFailure, TaskOutcome, WORKER_FAILURE_CODE,
};
use crate::semaphore::SemaphoreManager;
use crate::{MatrixScheduler, SchedulerError, SchedulerResult};

/// One flat fan-out batch.
#[derive(Debug, Clone, Default)]
pub struct MatrixBatch {
    /// Stage the batch belongs to. It is the primary completion-log key.
    pub stage: u32,
    /// Tasks in creation (workflow) order. Dependencies must name tasks of this batch.
    pub tasks: Vec<Task>,
    /// Attempt number per task id. A task absent from this map runs as attempt 1.
    pub attempts: HashMap<String, u32>,
}

/// Verdict recorded for one attempt, derived from its outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerdictEnvelope {
    /// `completed`, `rate_limited`, or `failed`.
    pub verdict: &'static str,
    /// Failure code (`E-INTERNAL` for a worker failure); `None` unless the attempt failed.
    pub code: Option<&'static str>,
    /// Failure detail; `None` unless the attempt failed.
    pub detail: Option<String>,
}

impl VerdictEnvelope {
    /// Derives the verdict envelope for an attempt that ended with `outcome`.
    pub fn from_outcome(outcome: &TaskOutcome) -> Self {
        match outcome {
            TaskOutcome::Completed => Self {
                verdict: "completed",
                code: None,
                detail: None,
            },
            TaskOutcome::RateLimited { .. } => Self {
                verdict: "rate_limited",
                code: None,
                detail: None,
            },
            TaskOutcome::Failed { code, message } => Self {
                verdict: "failed",
                code: Some(*code),
                detail: Some(message.clone()),
            },
        }
    }
}

/// Who produced a completion record and how the attempt ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Provenance {
    /// Producing agent: the task's subagent type.
    pub producer: String,
    /// Attempt number, starting at 1.
    pub attempt: u32,
    /// Verdict recorded for this attempt.
    pub verdict: VerdictEnvelope,
}

/// Completion-log sort key. Fields compare in declaration order: stage, DAG depth, creation, task id.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct CompletionKey {
    /// Stage the task belongs to.
    pub stage: u32,
    /// Longest dependency chain below the task; roots are 0.
    pub depth: usize,
    /// Position of the task in the batch's creation order.
    pub creation: usize,
    /// Task id.
    pub task_id: String,
}

/// One completed attempt in the completion log.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompletionRecord {
    /// Sort key of the record.
    pub key: CompletionKey,
    /// Final outcome of the attempt.
    pub outcome: TaskOutcome,
    /// Provenance of the attempt.
    pub provenance: Provenance,
}

/// Completion log: records sorted by [`CompletionKey`].
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CompletionLog {
    records: Vec<CompletionRecord>,
}

impl CompletionLog {
    /// Builds a log from `records`, sorted by their keys.
    pub fn from_records(mut records: Vec<CompletionRecord>) -> Self {
        records.sort_by(|a, b| a.key.cmp(&b.key));
        Self { records }
    }

    /// Returns the records in key order.
    pub fn records(&self) -> &[CompletionRecord] {
        &self.records
    }
}

/// Result of one matrix join run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MatrixJoinReport {
    /// Task ids in dispatch order. Replay consumes this sequence.
    pub dispatch_order: Vec<String>,
    /// Task ids in the order their results arrived. Diagnostic only; not part of the log.
    pub arrival_order: Vec<String>,
    /// Task ids that never became ready, for example behind a failed dependency.
    pub undispatched: Vec<String>,
    /// Highest number of simultaneously running tasks.
    pub max_in_flight: usize,
    /// Completion log in key order.
    pub log: CompletionLog,
}

/// Per-task facts that the key and provenance need.
struct TaskMeta {
    depth: usize,
    creation: usize,
    producer: String,
    attempt: u32,
}

/// Dispatches `batch` through `worker` on the flat fan-out path and returns once no task can run.
///
/// Every task holds one permit of the global pool in `semaphores` while it runs, so concurrent joins
/// that share a pool stay within its size. `config.mode` must be [`DispatchMode::Continuous`]; the
/// round barrier is rejected because this join path never waits for a round to finish.
pub async fn run_matrix_join<W, Fut>(
    batch: MatrixBatch,
    config: DispatchConfig,
    semaphores: Arc<SemaphoreManager>,
    worker: W,
) -> SchedulerResult<MatrixJoinReport>
where
    W: Fn(Task) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<(), TaskFailure>> + Send + 'static,
{
    if config.mode != DispatchMode::Continuous {
        return Err(SchedulerError::Other(
            "matrix join is the flat fan-out path; the rounds barrier is not accepted here"
                .to_string(),
        ));
    }
    let built = build_dag_from(&batch.tasks, &[])?;
    let metas = join_metadata(&batch, &built)?;

    let worker = Arc::new(worker);
    let gated = move |task: Task| {
        let semaphores = Arc::clone(&semaphores);
        let worker = Arc::clone(&worker);
        async move {
            match semaphores.global().acquire().await {
                Ok(_permit) => run_isolated((*worker)(task)).await,
                Err(error) => Err(TaskFailure::Exception(error.to_string())),
            }
        }
    };
    let report = run_force_parallel(built, config, gated).await?;

    let mut records = Vec::with_capacity(report.outcomes.len());
    for (id, outcome) in &report.outcomes {
        let meta = metas
            .get(id)
            .ok_or_else(|| SchedulerError::TaskNotFound(id.clone()))?;
        records.push(completion_record(batch.stage, id, outcome.clone(), meta));
    }
    let arrival_order: Vec<String> = report.outcomes.iter().map(|(id, _)| id.clone()).collect();
    Ok(MatrixJoinReport {
        dispatch_order: report.dispatch_order,
        arrival_order,
        undispatched: report.undispatched,
        max_in_flight: report.max_in_flight,
        log: CompletionLog::from_records(records),
    })
}

/// Re-runs `batch` serially in `recorded_order` and rebuilds its completion log.
///
/// `recorded_order` must name each task at most once, and every batch dependency of a task must
/// appear before it. Tasks that are absent from it are reported as undispatched. Each task runs in
/// its own spawned task, so a panic fails only that task, as in the dispatcher. The worker must be
/// deterministic for the log to match the original run.
pub async fn replay_matrix_join<W, Fut>(
    batch: MatrixBatch,
    recorded_order: &[String],
    worker: W,
) -> SchedulerResult<MatrixJoinReport>
where
    W: Fn(Task) -> Fut,
    Fut: Future<Output = Result<(), TaskFailure>> + Send + 'static,
{
    let built = build_dag_from(&batch.tasks, &[])?;
    let metas = join_metadata(&batch, &built)?;
    let creation_order: Vec<String> = built
        .tasks
        .iter()
        .map(|task| task_id_of(task).to_string())
        .collect();
    let pool: HashMap<String, Task> = built
        .tasks
        .into_iter()
        .map(|task| (task_id_of(&task).to_string(), task))
        .collect();

    let mut seen: HashSet<String> = HashSet::with_capacity(recorded_order.len());
    for id in recorded_order {
        let Some(task) = pool.get(id) else {
            return Err(SchedulerError::TaskNotFound(id.clone()));
        };
        if seen.contains(id) {
            return Err(SchedulerError::InvalidDag(format!(
                "replay order repeats {id}"
            )));
        }
        if let Some(dep) = task
            .dependencies
            .iter()
            .find(|dep| pool.contains_key(&dep.value) && !seen.contains(&dep.value))
        {
            return Err(SchedulerError::InvalidDag(format!(
                "replay runs {id} before its dependency {}",
                dep.value
            )));
        }
        seen.insert(id.clone());
    }

    let mut records = Vec::with_capacity(recorded_order.len());
    let mut arrival_order = Vec::with_capacity(recorded_order.len());
    for id in recorded_order {
        let Some(task) = pool.get(id).cloned() else {
            return Err(SchedulerError::TaskNotFound(id.clone()));
        };
        let outcome = outcome_of(run_isolated(worker(task)).await);
        let meta = metas
            .get(id)
            .ok_or_else(|| SchedulerError::TaskNotFound(id.clone()))?;
        records.push(completion_record(batch.stage, id, outcome, meta));
        arrival_order.push(id.clone());
    }
    let undispatched: Vec<String> = creation_order
        .into_iter()
        .filter(|id| !seen.contains(id))
        .collect();
    Ok(MatrixJoinReport {
        dispatch_order: recorded_order.to_vec(),
        arrival_order,
        undispatched,
        max_in_flight: usize::from(!recorded_order.is_empty()),
        log: CompletionLog::from_records(records),
    })
}

impl MatrixScheduler {
    /// Runs a flat fan-out batch on this scheduler's global permit pool. See [`run_matrix_join`].
    pub async fn run_matrix_join<W, Fut>(
        &self,
        batch: MatrixBatch,
        config: DispatchConfig,
        worker: W,
    ) -> SchedulerResult<MatrixJoinReport>
    where
        W: Fn(Task) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<(), TaskFailure>> + Send + 'static,
    {
        run_matrix_join(batch, config, Arc::clone(&self.semaphores), worker).await
    }
}

/// Validates the batch and derives each task's depth, creation index, producer and attempt.
fn join_metadata(
    batch: &MatrixBatch,
    built: &BuiltDag,
) -> SchedulerResult<HashMap<String, TaskMeta>> {
    let ids: HashSet<&str> = built.tasks.iter().map(task_id_of).collect();
    for (id, attempt) in &batch.attempts {
        if *attempt == 0 || !ids.contains(id.as_str()) {
            return Err(SchedulerError::Other(format!(
                "attempt entry {id:?} must name a batch task with attempt >= 1"
            )));
        }
    }
    let depths = dependency_depths(&built.tasks);
    let mut metas = HashMap::with_capacity(built.tasks.len());
    for (creation, task) in built.tasks.iter().enumerate() {
        let id = task_id_of(task).to_string();
        let producer = agent_of(task).to_string();
        if producer.is_empty() {
            return Err(SchedulerError::InvalidDag(format!(
                "task {id} has no subagent type, so it has no producer"
            )));
        }
        metas.insert(
            id.clone(),
            TaskMeta {
                depth: depths.get(&id).copied().unwrap_or(0),
                creation,
                producer,
                attempt: batch.attempts.get(&id).copied().unwrap_or(1),
            },
        );
    }
    Ok(metas)
}

/// Longest dependency chain below each task: roots are 0, a dependent is its deepest dependency + 1.
///
/// Same rule as `force_parallel_dispatch`'s private depth function. Relaxation stops after at most
/// `tasks.len()` passes, which is enough for an acyclic graph.
fn dependency_depths(tasks: &[Task]) -> HashMap<String, usize> {
    let mut depths: HashMap<String, usize> = tasks
        .iter()
        .map(|task| (task_id_of(task).to_string(), 0))
        .collect();
    for _ in 0..tasks.len() {
        let mut changed = false;
        for task in tasks {
            let depth = task
                .dependencies
                .iter()
                .filter_map(|dep| depths.get(&dep.value))
                .map(|parent| *parent + 1)
                .max()
                .unwrap_or(0);
            if let Some(slot) = depths.get_mut(task_id_of(task)) {
                if *slot != depth {
                    *slot = depth;
                    changed = true;
                }
            }
        }
        if !changed {
            break;
        }
    }
    depths
}

fn completion_record(
    stage: u32,
    id: &str,
    outcome: TaskOutcome,
    meta: &TaskMeta,
) -> CompletionRecord {
    CompletionRecord {
        key: CompletionKey {
            stage,
            depth: meta.depth,
            creation: meta.creation,
            task_id: id.to_string(),
        },
        provenance: Provenance {
            producer: meta.producer.clone(),
            attempt: meta.attempt,
            verdict: VerdictEnvelope::from_outcome(&outcome),
        },
        outcome,
    }
}

/// Maps a finished worker's result to its outcome, as the dispatcher does.
fn outcome_of(result: Result<(), TaskFailure>) -> TaskOutcome {
    match result {
        Ok(()) => TaskOutcome::Completed,
        Err(TaskFailure::RateLimited { retry_after }) => TaskOutcome::RateLimited { retry_after },
        Err(TaskFailure::Exception(message)) => TaskOutcome::Failed {
            code: WORKER_FAILURE_CODE,
            message,
        },
    }
}

/// Runs one worker call in its own spawned task, so a panic fails only that task. The gated dispatch
/// path and the replay both use this, so a panic gives the same outcome in both.
async fn run_isolated<Fut>(attempt: Fut) -> Result<(), TaskFailure>
where
    Fut: Future<Output = Result<(), TaskFailure>> + Send + 'static,
{
    match tokio::spawn(attempt).await {
        Ok(result) => result,
        Err(join_error) => Err(TaskFailure::Exception(panic_message(join_error))),
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

fn task_id_of(task: &Task) -> &str {
    task.id.as_ref().map_or("", |id| id.value.as_str())
}

fn agent_of(task: &Task) -> &str {
    task.subagent_type.as_ref().map_or("", |s| s.value.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_proto::gsd::v1::{SubagentType, TaskId};
    use std::time::Duration;

    fn task(id: &str, deps: &[&str]) -> Task {
        Task {
            id: Some(TaskId {
                value: id.to_string(),
            }),
            subagent_type: Some(SubagentType {
                value: "w".to_string(),
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

    fn batch(stage: u32, tasks: Vec<Task>) -> MatrixBatch {
        MatrixBatch {
            stage,
            tasks,
            attempts: HashMap::new(),
        }
    }

    fn config() -> DispatchConfig {
        let mut config = DispatchConfig {
            requested_parallel: 6,
            ..Default::default()
        };
        config.agent_max_parallel.insert("w".to_string(), 6);
        config
    }

    fn permits() -> Arc<SemaphoreManager> {
        Arc::new(SemaphoreManager::new())
    }

    /// Numeric suffix of a `t<n>` task id.
    fn index(task: &Task) -> usize {
        task.id.as_ref().map_or(0, |id| {
            id.value
                .trim_start_matches('t')
                .parse::<usize>()
                .unwrap_or(0)
        })
    }

    fn ids(log: &CompletionLog) -> Vec<String> {
        log.records()
            .iter()
            .map(|r| r.key.task_id.clone())
            .collect()
    }

    fn record<'a>(log: &'a CompletionLog, id: &str) -> &'a CompletionRecord {
        log.records()
            .iter()
            .find(|r| r.key.task_id == id)
            .expect("record present")
    }

    /// t0 and t1 are roots; t4 depends on t0; t5 depends on t1. The worker makes t1 fail, t2 rate-limit
    /// and t3 panic, so t5 never dispatches.
    fn fixture() -> MatrixBatch {
        batch(
            0,
            vec![
                task("t0", &[]),
                task("t1", &[]),
                task("t2", &[]),
                task("t3", &[]),
                task("t4", &["t0"]),
                task("t5", &["t1"]),
            ],
        )
    }

    /// Sleeps `delay_ms`, then returns the outcome fixed for the task's index.
    async fn mixed(task: Task, delay_ms: u64) -> Result<(), TaskFailure> {
        tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        match index(&task) {
            1 => Err(TaskFailure::Exception("bad input".to_string())),
            2 => Err(TaskFailure::RateLimited { retry_after: None }),
            3 => panic!("boom"),
            _ => Ok(()),
        }
    }

    #[tokio::test]
    async fn arrival_order_shuffle_yields_identical_log() {
        let forward: [u64; 6] = [50, 40, 30, 20, 10, 0];
        let reversed: [u64; 6] = [0, 10, 20, 30, 40, 50];
        let a = run_matrix_join(fixture(), config(), permits(), move |t: Task| {
            let ms = forward[index(&t)];
            mixed(t, ms)
        })
        .await
        .expect("forward run");
        let b = run_matrix_join(fixture(), config(), permits(), move |t: Task| {
            let ms = reversed[index(&t)];
            mixed(t, ms)
        })
        .await
        .expect("reversed run");
        assert_ne!(
            a.arrival_order, b.arrival_order,
            "shuffle must permute arrivals"
        );
        assert_eq!(a.log, b.log);
        assert_eq!(ids(&a.log), vec!["t0", "t1", "t2", "t3", "t4"]);
        assert_eq!(a.undispatched, vec!["t5".to_string()]);
    }

    #[tokio::test]
    async fn log_orders_by_depth_then_creation_regardless_of_arrival() {
        // a(root), b(root), c(depends on a), d(root): depths 0,0,1,0; creation 0..3.
        let tasks = vec![
            task("a", &[]),
            task("b", &[]),
            task("c", &["a"]),
            task("d", &[]),
        ];
        let report = run_matrix_join(batch(0, tasks), config(), permits(), |t: Task| async move {
            let slow = t.id.as_ref().is_some_and(|id| id.value == "a");
            tokio::time::sleep(Duration::from_millis(if slow { 30 } else { 0 })).await;
            Ok(())
        })
        .await
        .expect("run");
        assert_eq!(ids(&report.log), vec!["a", "b", "d", "c"]);
        assert_ne!(report.arrival_order, ids(&report.log));
    }

    #[tokio::test]
    async fn every_record_carries_provenance() {
        let mut fx = fixture();
        fx.attempts.insert("t2".to_string(), 2);
        let report = run_matrix_join(fx, config(), permits(), move |t: Task| mixed(t, 0))
            .await
            .expect("run");
        for rec in report.log.records() {
            assert_eq!(rec.provenance.producer, "w");
            assert!(rec.provenance.attempt >= 1);
            assert_eq!(
                rec.provenance.verdict,
                VerdictEnvelope::from_outcome(&rec.outcome)
            );
        }
        assert_eq!(record(&report.log, "t2").provenance.attempt, 2);
        assert_eq!(
            record(&report.log, "t2").provenance.verdict.verdict,
            "rate_limited"
        );
        let failed = &record(&report.log, "t1").provenance.verdict;
        assert_eq!(failed.verdict, "failed");
        assert_eq!(failed.code, Some("E-INTERNAL"));
        assert_eq!(failed.detail.as_deref(), Some("bad input"));
        assert_eq!(
            record(&report.log, "t3").provenance.verdict.code,
            Some("E-INTERNAL")
        );
        assert_eq!(
            record(&report.log, "t0").provenance.verdict.verdict,
            "completed"
        );
        assert_eq!(record(&report.log, "t0").provenance.attempt, 1);
    }

    #[tokio::test]
    async fn replay_of_recorded_dispatch_order_reproduces_log() {
        let forward: [u64; 6] = [50, 40, 30, 20, 10, 0];
        let original = run_matrix_join(fixture(), config(), permits(), move |t: Task| {
            let ms = forward[index(&t)];
            mixed(t, ms)
        })
        .await
        .expect("original run");
        let reversed: [u64; 6] = [0, 10, 20, 30, 40, 50];
        let replayed = replay_matrix_join(fixture(), &original.dispatch_order, move |t: Task| {
            let ms = reversed[index(&t)];
            mixed(t, ms)
        })
        .await
        .expect("replay");
        assert_eq!(replayed.dispatch_order, original.dispatch_order);
        assert_eq!(replayed.log, original.log);
        assert_eq!(replayed.undispatched, vec!["t5".to_string()]);
    }

    #[tokio::test]
    async fn replay_rejects_orders_that_break_dependencies_or_repeat() {
        let before_dep = vec!["t4".to_string(), "t0".to_string()];
        assert!(
            replay_matrix_join(fixture(), &before_dep, |t: Task| mixed(t, 0))
                .await
                .is_err()
        );
        let repeated = vec!["t0".to_string(), "t0".to_string()];
        assert!(
            replay_matrix_join(fixture(), &repeated, |t: Task| mixed(t, 0))
                .await
                .is_err()
        );
        let unknown = vec!["zz".to_string()];
        assert!(
            replay_matrix_join(fixture(), &unknown, |t: Task| mixed(t, 0))
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn join_rejects_round_barrier_and_producerless_tasks() {
        let mut rounds = config();
        rounds.mode = DispatchMode::Rounds;
        assert!(run_matrix_join(
            batch(0, vec![task("a", &[])]),
            rounds,
            permits(),
            |_t: Task| async { Ok(()) }
        )
        .await
        .is_err());
        let mut producerless = task("a", &[]);
        producerless.subagent_type = None;
        assert!(run_matrix_join(
            batch(0, vec![producerless]),
            config(),
            permits(),
            |_t: Task| async { Ok(()) }
        )
        .await
        .is_err());
    }

    #[test]
    fn log_orders_by_stage_before_depth_creation_and_id() {
        let rec = |stage: u32, depth: usize, creation: usize, id: &str| CompletionRecord {
            key: CompletionKey {
                stage,
                depth,
                creation,
                task_id: id.to_string(),
            },
            outcome: TaskOutcome::Completed,
            provenance: Provenance {
                producer: "w".to_string(),
                attempt: 1,
                verdict: VerdictEnvelope::from_outcome(&TaskOutcome::Completed),
            },
        };
        let log = CompletionLog::from_records(vec![
            rec(1, 0, 0, "a"),
            rec(0, 2, 9, "z"),
            rec(0, 1, 5, "m"),
            rec(0, 1, 5, "b"),
            rec(0, 1, 4, "q"),
        ]);
        assert_eq!(ids(&log), vec!["q", "b", "m", "z", "a"]);
    }
}
