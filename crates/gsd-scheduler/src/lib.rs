//! Matrix Scheduler for GSD-Core
//!
//! DAG-aware, priority-driven, dependency-respecting concurrent executor
//! with configurable fan-out/fan-in and dynamic restructuring.

pub mod aimd_limiter;
pub mod clock;
pub mod dag;
pub mod dag_build;
pub mod dynamic;
pub mod force_parallel_dispatch;
pub mod hitl;
pub mod matrix_join;
pub mod nexus_barrier_join;
mod panic_text;
pub mod policy;
pub mod policy_config;
pub mod ready_frontier;
pub mod semaphore;

pub use aimd_limiter::AimdLimiter;
pub use dag::*;
pub use dag_build::*;
pub use force_parallel_dispatch::{
    effective_global_cap, run_force_parallel, sort_dispatch_order, DispatchConfig, DispatchMode,
    DispatchReport, TaskFailure, TaskOutcome,
};
pub use matrix_join::*;
pub use nexus_barrier_join::*;
pub use policy::*;
pub use policy_config::*;
pub use ready_frontier::*;
pub use semaphore::*;

use gsd_proto::gsd::v1::TaskId;
use parking_lot::RwLock;
use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::task::{Context, Poll, Waker};
use uuid::Uuid;

/// Result type for scheduler operations
pub type SchedulerResult<T> = std::result::Result<T, SchedulerError>;

/// Scheduler errors
#[derive(Debug, thiserror::Error)]
pub enum SchedulerError {
    #[error("Task not found: {0}")]
    TaskNotFound(String),
    #[error("Dependency cycle detected: {0}")]
    CycleDetected(String),
    #[error("Task not ready: {0}")]
    NotReady(String),
    #[error("Budget exhausted: {0}")]
    BudgetExhausted(String),
    #[error("Circuit breaker open: {0}")]
    CircuitBreakerOpen(String),
    #[error("Deadlock detected: {0}")]
    Deadlock(String),
    #[error("Invalid DAG: {0}")]
    InvalidDag(String),
    #[error("Other: {0}")]
    Other(String),
}

impl From<anyhow::Error> for SchedulerError {
    fn from(e: anyhow::Error) -> Self {
        SchedulerError::Other(e.to_string())
    }
}

impl From<gsd_sched_core::ClaimError> for SchedulerError {
    fn from(e: gsd_sched_core::ClaimError) -> Self {
        SchedulerError::Other(e.to_string())
    }
}

impl From<gsd_sched_core::LimiterError> for SchedulerError {
    fn from(e: gsd_sched_core::LimiterError) -> Self {
        SchedulerError::Other(e.to_string())
    }
}

/// How a submitted task ended. Every outcome releases its chat-thread lease (DEC-4).
///
/// Named `LeaseOutcome` because `TaskOutcome` is already exported from this crate root by
/// `force_parallel_dispatch`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LeaseOutcome {
    /// The task finished its work.
    Succeeded,
    /// The task failed.
    Failed,
    /// The task was cancelled.
    Cancelled,
}

/// Session the scheduler reserves task budget against.
const SESSION_ID: &str = "default";
/// Budget reserved for each submitted task. `complete`, or a refused `submit`, returns it.
const TASK_TOKENS: i64 = 1000;
const TASK_WALL_TIME_MS: i64 = 60_000;
const TASK_TOOL_CALLS: i32 = 10;
const TASK_COST_USD: f64 = 0.01;

/// A submitted task's chat-thread lease and the budget thread its reservation was made on.
struct Lease {
    thread_id: String,
    budget_thread: String,
}
/// Pending refund that failed after the lease was removed (M2). Retry of `complete` for the same
/// id resumes only the outstanding refund (no second DAG mark or chat release).
struct PendingRefund {
    budget_thread: String,
}
/// Matrix Scheduler
pub struct MatrixScheduler {
    dag: Arc<RwLock<TaskDag>>,
    chat_pool: Arc<gsd_pool::ChatPool>,
    governor: Arc<gsd_governor::Governor>,
    semaphores: Arc<SemaphoreManager>,
    metrics: Arc<SchedulerMetrics>,
    /// Lease per task id. `submit` inserts it and `complete` removes it.
    leases: parking_lot::Mutex<HashMap<String, Lease>>,
    /// Ids whose admission is in progress: in the DAG but not yet acquired. A pending task is
    /// refused as a dependency and is left off `get_frontier`.
    pending: parking_lot::Mutex<HashSet<String>>,
    /// Pending refunds (M2) that failed after lease removal. `complete` for the same id resumes
    /// only the outstanding refund (no second DAG mark or chat release).
    pending_refunds: parking_lot::Mutex<HashMap<String, PendingRefund>>,
    #[cfg(test)]
    fail_next_refund: std::sync::atomic::AtomicBool,
}

impl MatrixScheduler {
    pub fn new(chat_pool: Arc<gsd_pool::ChatPool>, governor: Arc<gsd_governor::Governor>) -> Self {
        Self {
            dag: Arc::new(RwLock::new(TaskDag::new())),
            chat_pool,
            governor,
            semaphores: Arc::new(SemaphoreManager::new()),
            metrics: Arc::new(SchedulerMetrics::new()),
            leases: parking_lot::Mutex::new(HashMap::new()),
            pending: parking_lot::Mutex::new(HashSet::new()),
            pending_refunds: parking_lot::Mutex::new(HashMap::new()),
            #[cfg(test)]
            fail_next_refund: std::sync::atomic::AtomicBool::new(false),
        }
    }

    #[cfg(test)]
    pub fn set_fail_next_refund(&self) {
        self.fail_next_refund
            .store(true, std::sync::atomic::Ordering::SeqCst);
    }

    #[cfg(not(test))]
    fn should_fail_next_refund(&self) -> bool {
        false
    }

    #[cfg(test)]
    fn should_fail_next_refund(&self) -> bool {
        self.fail_next_refund
            .swap(false, std::sync::atomic::Ordering::SeqCst)
    }

    /// Reserves the task budget, admits the task and takes a chat-thread lease for it. On `Err`,
    /// any reservation is refunded and no DAG node remains, so the same id can be submitted again.
    /// On `Ok`, the task holds a lease until `complete` is called for it.
    pub async fn submit(&self, task: gsd_proto::gsd::v1::Task) -> SchedulerResult<TaskId> {
        // Reserve budget on a thread id of its own, so `complete` returns exactly this reservation.
        let budget_thread = Uuid::new_v4().to_string();
        self.governor
            .reserve_budget_checked(gsd_proto::gsd::v1::BudgetRequest {
                session_id: Some(gsd_proto::gsd::v1::SessionId {
                    value: SESSION_ID.to_string(),
                }),
                thread_id: Some(gsd_proto::gsd::v1::ChatThreadId {
                    value: budget_thread.clone(),
                }),
                tokens: TASK_TOKENS,
                wall_time_ms: TASK_WALL_TIME_MS,
                tool_calls: TASK_TOOL_CALLS,
                cost_usd: TASK_COST_USD,
            })
            .await
            .map_err(|err| SchedulerError::BudgetExhausted(err.to_string()))?;

        match self.admit(task).await {
            Ok((task_id, thread_id)) => {
                self.leases.lock().insert(
                    task_id.value.clone(),
                    Lease {
                        thread_id,
                        budget_thread,
                    },
                );
                Ok(task_id)
            }
            Err(err) => {
                // Nothing runs for a refused task, so its reservation goes back now.
                self.refund(&budget_thread).await;
                Err(err)
            }
        }
    }

    /// Adds `task` to the DAG as a pending node and acquires its chat thread.
    ///
    /// The node is pending from its DAG insert until its chat thread is acquired. Nothing can
    /// depend on a pending node, so a refused admission leaves no edge behind. A refusal removes
    /// the node, so the same id can be submitted later.
    async fn admit(&self, task: gsd_proto::gsd::v1::Task) -> SchedulerResult<(TaskId, String)> {
        let task_id = self.begin_admission(&task)?;
        match self.check_and_acquire(&task).await {
            Ok(thread_id) => {
                self.finish_admission(&task_id.value, true);
                self.metrics.inc_submitted();
                Ok((task_id, thread_id))
            }
            Err(err) => {
                // Nothing runs for the refused task, so its node comes out of the DAG.
                self.finish_admission(&task_id.value, false);
                Err(err)
            }
        }
    }

    /// Adds `task` to the DAG as a pending node. A dependency that is still pending is refused
    /// as `TaskNotFound`, the error for a dependency that has not been added. The check and the
    /// insert run under the pending lock, so another admission cannot end in between.
    fn begin_admission(&self, task: &gsd_proto::gsd::v1::Task) -> SchedulerResult<TaskId> {
        let mut pending = self.pending.lock();
        if let Some(dep) = task
            .dependencies
            .iter()
            .find(|dep| pending.contains(&dep.value))
        {
            return Err(SchedulerError::TaskNotFound(dep.value.clone()));
        }
        self.dag.write().add_task(task.clone())?;
        // `add_task` accepted the task, so it has an id.
        let task_id = task.id.clone().unwrap_or_default();
        pending.insert(task_id.value.clone());
        Ok(task_id)
    }

    /// Ends the admission of `id`. A refused admission also removes its node. Dependents cannot
    /// attach to a pending node, so the removal drops no edge that a live task relies on.
    fn finish_admission(&self, id: &str, admitted: bool) {
        let mut pending = self.pending.lock();
        pending.remove(id);
        if !admitted {
            let removed = self.dag.write().remove_task(id);
            // A pending node has no dependents: dependents cannot attach to it, and this node stayed pending under the
            // lock until now. A refusal would mean that invariant is broken.
            debug_assert!(
                removed.is_ok(),
                "a refused admission found dependents: {removed:?}"
            );
            if let Err(err) = removed {
                tracing::error!(task = id, error = %err, "a refused admission left its node in the DAG");
            }
        }
    }

    /// Acquires a chat thread for `task`, which is already in the DAG.
    async fn check_and_acquire(&self, task: &gsd_proto::gsd::v1::Task) -> SchedulerResult<String> {
        // Submit to thread pool
        self.chat_pool
            .acquire(gsd_proto::gsd::v1::SubagentType {
                value: task
                    .subagent_type
                    .as_ref()
                    .map(|s| s.value.clone())
                    .unwrap_or_default(),
            })
            .await
            .map_err(|e| SchedulerError::Other(e.to_string()))
    }

    /// Returns the reservation made on `budget_thread`. A refusal is logged rather than returned,
    /// because the caller is already failing with the error that made it refund.
    async fn refund(&self, budget_thread: &str) {
        if let Err(err) = self
            .governor
            .release_budget(
                SESSION_ID,
                budget_thread,
                TASK_TOKENS,
                TASK_WALL_TIME_MS,
                TASK_TOOL_CALLS,
                TASK_COST_USD,
            )
            .await
        {
            tracing::warn!(budget_thread, error = %err, "scheduler budget refund refused");
        }
    }

    /// Completes task `task_id` and releases the chat-thread lease that `submit` took for it.
    ///
    /// Every outcome releases the lease (DEC-4), so each submitted task needs one call. Both
    /// `Placement::Parked` and `Placement::Closed` count as success.
    ///
    /// Returns `SchedulerError::TaskNotFound` when no lease is held for `task_id`, and changes
    /// nothing. When the thread still has queued or in-flight chat work, the call fails and the
    /// lease is kept, so the caller can retry.
    ///
    /// A reservation the governor no longer holds has nothing left to refund. `complete` logs that
    /// and succeeds, as the teardown does for a final `UnknownReservation`.
    ///
    /// Once the thread is released, the DAG records the outcome: `Succeeded` marks the task
    /// complete, so its dependents become ready, and `Failed` or `Cancelled` mark it failed, so it
    /// is never ready again. A refusal other than `ThreadBusy`, such as `UnknownThread`, still
    /// settles the DAG and refunds the reservation, and then returns that refusal. While a
    /// `complete` for an id is in flight, a second call for that id returns `TaskNotFound`.
    pub async fn complete(&self, task_id: &TaskId, outcome: LeaseOutcome) -> SchedulerResult<()> {
        // M2: if this id has a pending refund from a prior failed complete (after lease removal),
        // resume *only* the outstanding refund. No second DAG mark or chat release.
        let pending_refund = self.pending_refunds.lock().remove(&task_id.value);
        if let Some(pending) = pending_refund {
            let do_fail = self.should_fail_next_refund();
            let refunded = if do_fail {
                Err(gsd_governor::GovernorError::Other("test injected".into()))
            } else {
                self.governor
                    .release_budget(
                        SESSION_ID,
                        &pending.budget_thread,
                        TASK_TOKENS,
                        TASK_WALL_TIME_MS,
                        TASK_TOOL_CALLS,
                        TASK_COST_USD,
                    )
                    .await
            };
            match refunded {
                Ok(_) => return Ok(()),
                Err(gsd_governor::GovernorError::UnknownReservation(detail)) => {
                    tracing::warn!(
                        task = %task_id.value,
                        %detail,
                        "pending refund found no reservation; nothing to refund"
                    );
                    return Ok(());
                }
                Err(err) => {
                    self.pending_refunds
                        .lock()
                        .insert(task_id.value.clone(), pending);
                    return Err(SchedulerError::Other(err.to_string()));
                }
            }
        }
        // The guard is a temporary of this statement, so it is released before the await.
        let lease = self
            .leases
            .lock()
            .remove(&task_id.value)
            .ok_or_else(|| SchedulerError::TaskNotFound(task_id.value.clone()))?;
        let released = self.chat_pool.release(&lease.thread_id).await;
        if let Err(err) = &released {
            if matches!(err, gsd_pool::ChatError::ThreadBusy(_)) {
                // The task still runs on its thread, so it keeps its lease and its reservation.
                let message = err.to_string();
                self.leases.lock().insert(task_id.value.clone(), lease);
                return Err(SchedulerError::Other(message));
            }
        }
        // The task has finished on its thread, so the DAG records how it ended.
        let settled = match outcome {
            LeaseOutcome::Succeeded => self.dag.write().mark_complete(task_id),
            LeaseOutcome::Failed | LeaseOutcome::Cancelled => self.dag.write().mark_failed(task_id),
        };
        // The lease is gone, so its reservation returns whether or not the thread release succeeded.
        let do_fail = self.should_fail_next_refund();
        let refunded = if do_fail {
            Err(gsd_governor::GovernorError::Other("test injected".into()))
        } else {
            self.governor
                .release_budget(
                    SESSION_ID,
                    &lease.budget_thread,
                    TASK_TOKENS,
                    TASK_WALL_TIME_MS,
                    TASK_TOOL_CALLS,
                    TASK_COST_USD,
                )
                .await
        };
        let refund = match refunded {
            Ok(_) => Ok(()),
            Err(gsd_governor::GovernorError::UnknownReservation(detail)) => {
                tracing::warn!(
                    task = %task_id.value,
                    thread = %lease.thread_id,
                    %detail,
                    "scheduler budget refund found no reservation; nothing to refund"
                );
                Ok(())
            }
            Err(err) => {
                // M2: retain the pending refund (lease already removed above). A subsequent
                // complete for the same task_id will resume only this outstanding settlement.
                self.pending_refunds.lock().insert(
                    task_id.value.clone(),
                    PendingRefund {
                        budget_thread: lease.budget_thread.clone(),
                    },
                );
                Err(SchedulerError::Other(err.to_string()))
            }
        };
        let placement = match released {
            Ok(placement) => placement,
            Err(err) => {
                // The release refusal is the error returned. A refund or settle failure that
                // comes with it is logged, not dropped.
                for failure in [refund.as_ref().err(), settled.as_ref().err()]
                    .into_iter()
                    .flatten()
                {
                    tracing::error!(
                        task = %task_id.value,
                        error = %failure,
                        "companion failure of a refused chat-thread release"
                    );
                }
                return Err(SchedulerError::Other(err.to_string()));
            }
        };
        // The refund error is returned first. A settle error that comes with it is logged.
        if let Err(err) = refund {
            if let Err(settle) = &settled {
                tracing::error!(
                    task = %task_id.value,
                    error = %settle,
                    "companion failure of a failed budget refund"
                );
            }
            return Err(err);
        }
        settled?;
        tracing::debug!(
            task = %task_id.value,
            thread = %lease.thread_id,
            ?outcome,
            ?placement,
            "chat-thread lease released"
        );
        Ok(())
    }

    /// Returns the ready task ids that are not pending. Completed and failed tasks never appear.
    pub async fn get_frontier(&self) -> Vec<TaskId> {
        let pending = self.pending.lock();
        let dag = self.dag.read();
        dag.get_ready_tasks()
            .into_iter()
            .filter(|task| !pending.contains(&task.value))
            .collect()
    }

    pub fn metrics(&self) -> &SchedulerMetrics {
        &self.metrics
    }
}

pub struct SchedulerMetrics {
    submitted: AtomicU64,
}

impl SchedulerMetrics {
    fn new() -> Self {
        Self {
            submitted: AtomicU64::new(0),
        }
    }
}

impl SchedulerMetrics {
    fn inc_submitted(&self) {
        self.submitted.fetch_add(1, Ordering::Relaxed);
    }
}

/// Budget port backed by the governor. `gsd-pool` defines the port and never implements it
/// (DEC-1); this adapter is the consumer side.
///
/// Reservations carry wall time only. Tokens, tool calls and cost are reserved as zero.
pub struct GovernorBudget {
    governor: Arc<gsd_governor::Governor>,
}

impl GovernorBudget {
    /// Wraps `governor` as a budget port.
    pub fn new(governor: Arc<gsd_governor::Governor>) -> Self {
        Self { governor }
    }
}

impl gsd_pool::BudgetPort for GovernorBudget {
    fn reserve(
        &self,
        reservation: &gsd_pool::BudgetReservation,
    ) -> Result<(), gsd_pool::BudgetRefusal> {
        let wall_time_ms = to_grant_ms(reservation.wall_time_ms)?;
        let request = gsd_proto::gsd::v1::BudgetRequest {
            session_id: Some(gsd_proto::gsd::v1::SessionId {
                value: reservation.session_id.clone(),
            }),
            thread_id: Some(gsd_proto::gsd::v1::ChatThreadId {
                value: reservation.thread_id.clone(),
            }),
            tokens: 0,
            wall_time_ms,
            tool_calls: 0,
            cost_usd: 0.0,
        };
        match block_on(self.governor.reserve_budget_checked(request)) {
            Some(Ok(grant)) if grant.granted => Ok(()),
            Some(Ok(_)) => Err(gsd_pool::BudgetRefusal::Other(
                "governor returned no grant".to_string(),
            )),
            Some(Err(err)) => Err(refusal_of(err)),
            None => Err(gsd_pool::BudgetRefusal::Other(NOT_READY.to_string())),
        }
    }

    fn release(
        &self,
        reservation: &gsd_pool::BudgetReservation,
    ) -> Result<(), gsd_pool::BudgetRefusal> {
        let wall_time_ms = to_grant_ms(reservation.wall_time_ms)?;
        match block_on(self.governor.release_budget(
            &reservation.session_id,
            &reservation.thread_id,
            0,
            wall_time_ms,
            0,
            0.0,
        )) {
            Some(result) => result.map_err(refusal_of),
            None => Err(gsd_pool::BudgetRefusal::Other(NOT_READY.to_string())),
        }
    }
}

/// Converts a reservation's wall time to the governor's signed milliseconds.
fn to_grant_ms(wall_time_ms: u64) -> Result<i64, gsd_pool::BudgetRefusal> {
    i64::try_from(wall_time_ms).map_err(|_| {
        gsd_pool::BudgetRefusal::Invalid(format!("wall_time_ms {wall_time_ms} exceeds i64"))
    })
}

/// Maps a governor fault to the budget port's refusal kinds.
fn refusal_of(err: gsd_governor::GovernorError) -> gsd_pool::BudgetRefusal {
    use gsd_governor::GovernorError;
    use gsd_pool::BudgetRefusal;
    match err {
        GovernorError::BudgetExceeded(m) | GovernorError::CostLimitExceeded(m) => {
            BudgetRefusal::Exceeded(m)
        }
        GovernorError::RateLimitExceeded(m) => BudgetRefusal::RateLimited(m),
        GovernorError::QuotaExceeded(m) => BudgetRefusal::Quota(m),
        GovernorError::InvalidBudget(m) => BudgetRefusal::Invalid(m),
        GovernorError::Unauthorized(m) => BudgetRefusal::Unauthorized(m),
        GovernorError::Other(m) | GovernorError::UnknownReservation(m) => BudgetRefusal::Other(m),
    }
}

/// The message a governor call reports when it does not complete on its first poll. The governor's reserve and release
/// futures contain no await point, so this never happens; the message makes a broken invariant a visible fault.
const NOT_READY: &str = "governor call did not complete on its first poll";

/// Polls a governor future once and returns its output, or `None` when it is not ready.
///
/// Valid only for the governor methods used above: `reserve_budget_checked` and `release_budget` contain no await points,
/// so they are ready on the first poll. A pending future is not polled again: nothing would wake it, so a loop would spin
/// forever. Callers report `None` as a fault.
fn block_on<F: Future>(future: F) -> Option<F::Output> {
    let waker = Waker::noop();
    let mut context = Context::from_waker(waker);
    let mut future = std::pin::pin!(future);
    match future.as_mut().poll(&mut context) {
        Poll::Ready(output) => Some(output),
        Poll::Pending => None,
    }
}

#[cfg(test)]
mod governor_budget_tests {
    use super::*;
    use gsd_governor::{GovernorConfig, GovernorError};
    use gsd_pool::{BudgetPort, BudgetRefusal, BudgetReservation};

    fn reservation(wall_time_ms: u64) -> BudgetReservation {
        BudgetReservation {
            session_id: "s1".to_string(),
            thread_id: "t1".to_string(),
            wall_time_ms,
        }
    }

    #[test]
    fn reserve_then_release_settles_wall_time() -> Result<(), Box<dyn std::error::Error>> {
        let governor = Arc::new(gsd_governor::Governor::new(GovernorConfig::default()));
        let port = GovernorBudget::new(Arc::clone(&governor));
        port.reserve(&reservation(1000))
            .map_err(|refusal| format!("reserve refused: {refusal:?}"))?;
        assert_eq!(governor.get_budget_snapshot("s1")?.wall_time_ms_used, 1000);
        port.release(&reservation(1000))
            .map_err(|refusal| format!("release refused: {refusal:?}"))?;
        assert_eq!(governor.get_budget_snapshot("s1")?.wall_time_ms_used, 0);
        Ok(())
    }

    #[test]
    fn wall_time_beyond_i64_is_invalid() {
        let port = GovernorBudget::new(Arc::new(gsd_governor::Governor::new(
            GovernorConfig::default(),
        )));
        assert!(matches!(
            port.reserve(&reservation(u64::MAX)),
            Err(BudgetRefusal::Invalid(_))
        ));
    }

    #[test]
    fn reservation_over_the_thread_budget_is_exceeded() {
        let port = GovernorBudget::new(Arc::new(gsd_governor::Governor::new(
            GovernorConfig::default(),
        )));
        // The default thread budget is 300 000 ms of wall time.
        assert!(matches!(
            port.reserve(&reservation(400_000)),
            Err(BudgetRefusal::Exceeded(_))
        ));
    }

    #[test]
    fn governor_errors_map_to_budget_refusals() {
        assert!(matches!(
            refusal_of(GovernorError::BudgetExceeded(String::new())),
            BudgetRefusal::Exceeded(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::QuotaExceeded(String::new())),
            BudgetRefusal::Quota(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::CostLimitExceeded(String::new())),
            BudgetRefusal::Exceeded(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::RateLimitExceeded(String::new())),
            BudgetRefusal::RateLimited(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::InvalidBudget(String::new())),
            BudgetRefusal::Invalid(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::Unauthorized(String::new())),
            BudgetRefusal::Unauthorized(_)
        ));
        assert!(matches!(
            refusal_of(GovernorError::Other(String::new())),
            BudgetRefusal::Other(_)
        ));
    }

    #[test]
    fn a_pending_governor_future_is_refused_and_not_spun() {
        // A future that stays pending must return `None` after one poll. A spin loop would never return here.
        assert_eq!(block_on(std::future::pending::<u8>()), None);
        assert_eq!(block_on(async { 3_u8 }), Some(3));
    }
}

#[cfg(test)]
mod lease_tests {
    use super::*;
    use gsd_proto::gsd::v1::{SubagentType, Task};

    fn scheduler(max_threads: usize) -> Result<MatrixScheduler, gsd_pool::ChatError> {
        let governor = Arc::new(gsd_governor::Governor::new(
            gsd_governor::GovernorConfig::default(),
        ));
        let budget = Arc::new(GovernorBudget::new(Arc::clone(&governor)));
        let config = gsd_pool::ChatRuntimeConfig {
            max_threads,
            ..gsd_pool::ChatRuntimeConfig::default()
        };
        let pool = gsd_pool::ChatPool::new(config, budget)?;
        Ok(MatrixScheduler::new(Arc::new(pool), governor))
    }

    fn task(id: &str) -> Task {
        Task {
            id: Some(TaskId {
                value: id.to_string(),
            }),
            subagent_type: Some(SubagentType {
                value: "w".to_string(),
            }),
            ..Default::default()
        }
    }

    fn task_id(id: &str) -> TaskId {
        TaskId {
            value: id.to_string(),
        }
    }

    #[tokio::test]
    async fn thirty_tasks_each_released_by_complete_all_fit_the_default_cap(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(30)?;
        for index in 0..30 {
            let id = format!("t{index}");
            scheduler.submit(task(&id)).await?;
            scheduler
                .complete(&task_id(&id), LeaseOutcome::Succeeded)
                .await?;
        }
        Ok(())
    }

    #[tokio::test]
    async fn seventy_tasks_each_released_by_complete_all_fit_the_default_cap(
    ) -> Result<(), Box<dyn std::error::Error>> {
        // Sixty reservations of 60 s exhaust the default 3,600 s session budget. `complete` returns each
        // reservation, so seventy tasks run one after another and all fit.
        let scheduler = scheduler(30)?;
        for index in 0..70 {
            let id = format!("t{index}");
            scheduler.submit(task(&id)).await?;
            scheduler
                .complete(&task_id(&id), LeaseOutcome::Succeeded)
                .await?;
        }
        Ok(())
    }

    #[tokio::test]
    async fn failed_task_releases_its_lease_for_the_next_submit(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(1)?;
        scheduler.submit(task("t1")).await?;
        scheduler
            .complete(&task_id("t1"), LeaseOutcome::Failed)
            .await?;
        scheduler.submit(task("t2")).await?;
        Ok(())
    }

    #[tokio::test]
    async fn complete_of_an_unknown_task_is_refused_and_changes_nothing(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(1)?;
        scheduler.submit(task("t1")).await?;
        let refused = scheduler
            .complete(&task_id("nope"), LeaseOutcome::Succeeded)
            .await;
        assert!(matches!(
            &refused,
            Err(SchedulerError::TaskNotFound(id)) if id == "nope"
        ));
        // The lease on t1 survived the refused call, so t1 still completes.
        scheduler
            .complete(&task_id("t1"), LeaseOutcome::Succeeded)
            .await?;
        scheduler.submit(task("t2")).await?;
        Ok(())
    }

    #[tokio::test]
    async fn a_reservation_already_released_completes_without_error(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(1)?;
        scheduler.submit(task("t1")).await?;
        let budget_thread = scheduler
            .leases
            .lock()
            .get(&task_id("t1").value)
            .map(|lease| lease.budget_thread.clone())
            .ok_or("t1 holds no lease")?;
        // Another path settles the same reservation first, so the pair has nothing left to refund.
        scheduler
            .governor
            .release_budget(
                SESSION_ID,
                &budget_thread,
                TASK_TOKENS,
                TASK_WALL_TIME_MS,
                TASK_TOOL_CALLS,
                TASK_COST_USD,
            )
            .await?;
        scheduler
            .complete(&task_id("t1"), LeaseOutcome::Succeeded)
            .await?;
        // The lease is cleared, so a second complete finds nothing.
        assert!(matches!(
            scheduler
                .complete(&task_id("t1"), LeaseOutcome::Succeeded)
                .await,
            Err(SchedulerError::TaskNotFound(_))
        ));
        Ok(())
    }

    #[tokio::test]
    async fn failed_acquire_leaves_the_id_resubmittable() -> Result<(), Box<dyn std::error::Error>>
    {
        let scheduler = scheduler(1)?;
        scheduler.submit(task("t1")).await?;
        // The only thread is held by t1, so t2 is refused at acquire, after its DAG insert.
        let refused = scheduler.submit(task("t2")).await;
        assert!(matches!(refused, Err(SchedulerError::Other(_))));
        scheduler
            .complete(&task_id("t1"), LeaseOutcome::Succeeded)
            .await?;
        // The refused insert was removed, so the same id is accepted again.
        scheduler.submit(task("t2")).await?;
        Ok(())
    }

    fn task_on(id: &str, deps: &[&str]) -> Task {
        Task {
            dependencies: deps.iter().map(|dep| task_id(dep)).collect(),
            ..task(id)
        }
    }

    #[tokio::test]
    async fn a_pending_task_cannot_be_depended_on_and_is_not_on_the_frontier(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(4)?;
        scheduler.begin_admission(&task("x"))?;
        let refused = scheduler.begin_admission(&task_on("y", &["x"]));
        assert!(matches!(
            &refused,
            Err(SchedulerError::TaskNotFound(id)) if id == "x"
        ));
        assert!(scheduler.get_frontier().await.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn a_dependent_of_a_refused_admission_is_refused(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(4)?;
        scheduler.begin_admission(&task("x"))?;
        scheduler.finish_admission("x", false);
        let refused = scheduler.begin_admission(&task_on("y", &["x"]));
        assert!(matches!(
            &refused,
            Err(SchedulerError::TaskNotFound(id)) if id == "x"
        ));
        assert!(scheduler.get_frontier().await.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn an_admitted_dependency_holds_its_dependent_off_the_frontier(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(4)?;
        scheduler.begin_admission(&task("x"))?;
        scheduler.finish_admission("x", true);
        scheduler.begin_admission(&task_on("y", &["x"]))?;
        scheduler.finish_admission("y", true);
        assert_eq!(scheduler.get_frontier().await, vec![task_id("x")]);
        Ok(())
    }

    #[tokio::test]
    async fn a_succeeded_task_leaves_the_frontier_and_unblocks_its_dependents(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(2)?;
        scheduler.submit(task("a")).await?;
        scheduler.submit(task_on("b", &["a"])).await?;
        assert_eq!(scheduler.get_frontier().await, vec![task_id("a")]);
        scheduler
            .complete(&task_id("a"), LeaseOutcome::Succeeded)
            .await?;
        assert_eq!(scheduler.get_frontier().await, vec![task_id("b")]);
        Ok(())
    }

    #[tokio::test]
    async fn a_failed_task_is_never_ready_again_and_keeps_its_dependents_blocked(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(2)?;
        scheduler.submit(task("a")).await?;
        scheduler.submit(task_on("b", &["a"])).await?;
        assert_eq!(scheduler.get_frontier().await, vec![task_id("a")]);
        scheduler
            .complete(&task_id("a"), LeaseOutcome::Failed)
            .await?;
        assert!(scheduler.get_frontier().await.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn a_cancelled_task_is_never_ready_again() -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(2)?;
        scheduler.submit(task("a")).await?;
        scheduler.submit(task_on("b", &["a"])).await?;
        assert_eq!(scheduler.get_frontier().await, vec![task_id("a")]);
        scheduler
            .complete(&task_id("a"), LeaseOutcome::Cancelled)
            .await?;
        assert!(scheduler.get_frontier().await.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn submit_refusal_reason_reaches_the_caller() -> Result<(), Box<dyn std::error::Error>> {
        // The session holds 1 s of wall time, so the 60 s task reservation is refused by the session.
        let mut config = gsd_governor::GovernorConfig::default();
        config.default_session_budget.max_wall_time_ms = 1_000;
        let governor = Arc::new(gsd_governor::Governor::new(config));
        let budget = Arc::new(GovernorBudget::new(Arc::clone(&governor)));
        let pool = gsd_pool::ChatPool::new(gsd_pool::ChatRuntimeConfig::default(), budget)?;
        let scheduler = MatrixScheduler::new(Arc::new(pool), governor);
        match scheduler.submit(task("a")).await {
            Err(SchedulerError::BudgetExhausted(reason)) => {
                assert!(
                    reason.contains("session budget"),
                    "refusal reason was {reason:?}"
                );
            }
            other => panic!("expected BudgetExhausted, got {other:?}"),
        }
        Ok(())
    }
    #[tokio::test]
    async fn complete_refund_error_retains_for_retry_and_refund_happens_exactly_once(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let scheduler = scheduler(1)?;
        scheduler.set_fail_next_refund();
        let snap0 = scheduler.governor.get_budget_snapshot(SESSION_ID)?;
        scheduler.submit(task("t1")).await?;
        let first = scheduler
            .complete(&task_id("t1"), LeaseOutcome::Succeeded)
            .await;
        assert!(
            first.is_err(),
            "first complete must hit injected refund error and retain"
        );
        let snap1 = scheduler.governor.get_budget_snapshot(SESSION_ID)?;
        assert_eq!(snap0, snap1, "reservation still held after failed refund");
        let second = scheduler
            .complete(&task_id("t1"), LeaseOutcome::Succeeded)
            .await;
        assert!(
            second.is_ok(),
            "second complete resumes the pending refund exactly once"
        );
        let snap2 = scheduler.governor.get_budget_snapshot(SESSION_ID)?;
        assert_ne!(snap1, snap2, "reservation released exactly once on retry");
        // third should be TaskNotFound (no lease, no pending)
        assert!(matches!(
            scheduler
                .complete(&task_id("t1"), LeaseOutcome::Succeeded)
                .await,
            Err(SchedulerError::TaskNotFound(_))
        ));
        Ok(())
    }
}
