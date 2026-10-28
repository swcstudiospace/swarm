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

use gsd_proto::gsd::v1::{SchedulingPolicy, TaskId};
use parking_lot::RwLock;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
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

/// Scheduler configuration
#[derive(Debug, Clone)]
pub struct SchedulerConfig {
    pub max_concurrent_tasks: usize,
    pub default_policy: SchedulingPolicy,
    pub enable_deadlock_detection: bool,
    pub deadlock_check_interval_ms: u64,
    pub preemption_timeout_ms: u64,
}

impl Default for SchedulerConfig {
    fn default() -> Self {
        Self {
            max_concurrent_tasks: 30,
            default_policy: gsd_proto::gsd::v1::SchedulingPolicy::CriticalPathThroughputHybrid,
            enable_deadlock_detection: true,
            deadlock_check_interval_ms: 1000,
            preemption_timeout_ms: 30000,
        }
    }
}

/// Matrix Scheduler
pub struct MatrixScheduler {
    dag: Arc<RwLock<TaskDag>>,
    thread_pool: Arc<gsd_pool::ThreadPool>,
    governor: Arc<gsd_governor::Governor>,
    policy: Arc<RwLock<Box<dyn SchedulingPolicyTrait>>>,
    semaphores: Arc<SemaphoreManager>,
    metrics: Arc<SchedulerMetrics>,
}

impl MatrixScheduler {
    pub fn new(
        thread_pool: Arc<gsd_pool::ThreadPool>,
        governor: Arc<gsd_governor::Governor>,
    ) -> Self {
        let policy = match SchedulingPolicy::CriticalPathThroughputHybrid {
            gsd_proto::gsd::v1::SchedulingPolicy::CriticalPathThroughputHybrid => {
                Box::new(CriticalPathThroughputHybridPolicy::new())
                    as Box<dyn SchedulingPolicyTrait>
            }
            _ => Box::new(CriticalPathThroughputHybridPolicy::new())
                as Box<dyn SchedulingPolicyTrait>,
        };

        Self {
            dag: Arc::new(RwLock::new(TaskDag::new())),
            thread_pool,
            governor,
            policy: Arc::new(RwLock::new(policy)),
            semaphores: Arc::new(SemaphoreManager::new()),
            metrics: Arc::new(SchedulerMetrics::new()),
        }
    }

    pub async fn submit(&self, task: gsd_proto::gsd::v1::Task) -> SchedulerResult<TaskId> {
        // Reserve budget
        let budget_request = gsd_proto::gsd::v1::BudgetRequest {
            session_id: Some(gsd_proto::gsd::v1::SessionId {
                value: "default".to_string(),
            }),
            thread_id: Some(gsd_proto::gsd::v1::ChatThreadId {
                value: Uuid::new_v4().to_string(),
            }),
            tokens: 1000,
            wall_time_ms: 60000,
            tool_calls: 10,
            cost_usd: 0.01,
        };

        let grant = self.governor.reserve_budget(budget_request).await;
        if !grant.granted {
            return Err(SchedulerError::BudgetExhausted(
                "Budget not granted".to_string(),
            ));
        }

        // Add to DAG
        self.dag.write().add_task(task.clone())?;

        // Check for cycles
        self.check_cycles()?;

        // Submit to policy for scheduling
        let _priority = {
            let policy = self.policy.read();
            policy.priority(&task, &self.dag.read())
        };

        // Submit to thread pool
        let _handle = self
            .thread_pool
            .acquire(gsd_proto::gsd::v1::SubagentType {
                value: task
                    .subagent_type
                    .as_ref()
                    .map(|s| s.value.clone())
                    .unwrap_or_default(),
            })
            .await
            .map_err(|e| SchedulerError::Other(e.to_string()))?;

        // Update metrics
        self.metrics.inc_submitted();

        Ok(task.id.unwrap_or_else(|| gsd_proto::gsd::v1::TaskId {
            value: Uuid::new_v4().to_string(),
        }))
    }

    fn check_cycles(&self) -> SchedulerResult<()> {
        let dag = self.dag.read();
        if let Some(cycle) = dag.detect_cycles() {
            return Err(SchedulerError::CycleDetected(format!(
                "Cycle detected: {:?}",
                cycle
            )));
        }
        Ok(())
    }

    pub async fn get_frontier(&self) -> Vec<TaskId> {
        let dag = self.dag.read();
        dag.get_ready_tasks()
    }

    pub fn set_policy(&self, policy: SchedulingPolicy) {
        let policy_impl = match policy {
            gsd_proto::gsd::v1::SchedulingPolicy::CriticalPathThroughputHybrid => {
                Box::new(CriticalPathThroughputHybridPolicy::new())
                    as Box<dyn SchedulingPolicyTrait>
            }
            _ => Box::new(CriticalPathThroughputHybridPolicy::new())
                as Box<dyn SchedulingPolicyTrait>,
        };
        *self.policy.write() = policy_impl;
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
