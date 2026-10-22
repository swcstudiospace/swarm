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
pub use clock::*;
pub use dag::*;
pub use dag_build::*;
pub use dynamic::*;
pub use force_parallel_dispatch::{
    effective_global_cap, run_force_parallel, sort_dispatch_order, DispatchConfig, DispatchMode,
    DispatchReport, TaskFailure, TaskOutcome,
};
pub use hitl::*;
pub use matrix_join::*;
pub use nexus_barrier_join::*;
pub use policy::*;
pub use policy_config::*;
pub use ready_frontier::*;
pub use semaphore::*;

use dashmap::DashMap;
use gsd_failure::{CircuitBreaker, RetryPolicy};
use gsd_governor::Governor;
use gsd_observability::{MetricsSink, TracingLayer};
use gsd_pool::{PoolError, PoolResult, ThreadPool};
use gsd_proto::gsd::v1::{ChatThreadId, SubagentType};
use gsd_proto::gsd::v1::{SchedulingPolicy, Task, TaskId, WorkflowDef};
use gsd_runtime::{RuntimeError, RuntimeResult};
use parking_lot::RwLock;
use petgraph::algo::toposort;
use petgraph::graph::{DiGraph, NodeIndex};
use petgraph::visit::EdgeRef;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{Mutex, Notify, Semaphore};
use tracing::{debug, info, trace, warn};
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
    config: SchedulerConfig,
    dag: Arc<RwLock<TaskDag>>,
    thread_pool: Arc<gsd_pool::ThreadPool>,
    governor: Arc<gsd_governor::Governor>,
    observability: Arc<gsd_observability::TracingLayer>,
    failure_isolation: Arc<gsd_failure::FailureIsolation>,
    policy: Arc<RwLock<Box<dyn SchedulingPolicyTrait>>>,
    semaphores: Arc<SemaphoreManager>,
    wait_for_graph: Arc<gsd_runtime::sync::WaitForGraph>,
    metrics: Arc<SchedulerMetrics>,
}

impl MatrixScheduler {
    pub fn new(
        config: SchedulerConfig,
        thread_pool: Arc<gsd_pool::ThreadPool>,
        governor: Arc<gsd_governor::Governor>,
        observability: Arc<gsd_observability::TracingLayer>,
        failure_isolation: Arc<gsd_failure::FailureIsolation>,
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
            config,
            dag: Arc::new(RwLock::new(TaskDag::new())),
            thread_pool,
            governor,
            observability,
            failure_isolation,
            policy: Arc::new(RwLock::new(policy)),
            semaphores: Arc::new(SemaphoreManager::new()),
            wait_for_graph: Arc::new(gsd_runtime::sync::WaitForGraph::new()),
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
        let node_id = self.dag.write().add_task(task.clone())?;

        // Check for cycles
        self.check_cycles()?;

        // Submit to policy for scheduling
        let policy = self.policy.read();
        let priority = policy.priority(&task, &*self.dag.read());
        drop(policy);

        // Submit to thread pool
        let handle = self
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

struct SchedulerMetrics {
    submitted: AtomicU64,
    completed: AtomicU64,
    failed: AtomicU64,
    cancelled: AtomicU64,
    scheduling_overhead_ns: AtomicU64,
}

impl SchedulerMetrics {
    fn new() -> Self {
        Self {
            submitted: AtomicU64::new(0),
            completed: AtomicU64::new(0),
            failed: AtomicU64::new(0),
            cancelled: AtomicU64::new(0),
            scheduling_overhead_ns: AtomicU64::new(0),
        }
    }
}

impl SchedulerMetrics {
    fn inc_submitted(&self) {
        self.submitted.fetch_add(1, Ordering::Relaxed);
    }
    fn inc_completed(&self) {
        self.completed.fetch_add(1, Ordering::Relaxed);
    }
    fn inc_failed(&self) {
        self.failed.fetch_add(1, Ordering::Relaxed);
    }
    fn inc_cancelled(&self) {
        self.cancelled.fetch_add(1, Ordering::Relaxed);
    }
    fn add_scheduling_overhead(&self, ns: u64) {
        self.scheduling_overhead_ns.fetch_add(ns, Ordering::Relaxed);
    }
}
