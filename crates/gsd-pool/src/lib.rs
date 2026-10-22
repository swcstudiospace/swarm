//! Thread Pool Manager for GSD-Core
//!
//! Manages lifecycle, pooling, reuse, and isolation for chat-thread subagents.

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Instant;

use dashmap::DashMap;
use gsd_proto::gsd::v1::{ChatThreadId, SubagentType, ThreadStateKind};
use parking_lot::RwLock;
use uuid::Uuid;

pub mod handle_registry;
pub mod thread_state_machine;
pub mod thread_state_transition_table;

pub use handle_registry::{ChatThreadHandle, HandleRegistry};
pub use thread_state_machine::transition;
pub use thread_state_transition_table::{
    allowed_successors, is_legal_transition, ThreadPhase, BLOCK_REASON_KEY,
};

/// Result type for pool operations
pub type PoolResult<T> = std::result::Result<T, PoolError>;

/// Pool errors
#[derive(Debug, thiserror::Error)]
pub enum PoolError {
    #[error("Thread not found: {0}")]
    NotFound(String),
    #[error("Thread already exists: {0}")]
    AlreadyExists(String),
    #[error("Pool exhausted: {0}")]
    Exhausted(String),
    #[error("Invalid state transition: {0}")]
    InvalidState(String),
    #[error("Sandbox error: {0}")]
    Sandbox(String),
    #[error("Capability mismatch: {0}")]
    CapabilityMismatch(String),
    #[error("Budget exhausted: {0}")]
    BudgetExhausted(String),
    #[error("Other: {0}")]
    Other(String),
}

impl From<anyhow::Error> for PoolError {
    fn from(e: anyhow::Error) -> Self {
        PoolError::Other(e.to_string())
    }
}

/// Thread pool configuration
#[derive(Debug, Clone)]
pub struct PoolConfig {
    pub max_threads: usize,
    pub warm_pool_size: usize,
    pub eviction_grace_secs: u64,
    pub checkpoint_interval_secs: u64,
    pub enable_sandbox: bool,
}

impl Default for PoolConfig {
    fn default() -> Self {
        Self {
            max_threads: 30,
            warm_pool_size: 5,
            eviction_grace_secs: 30,
            checkpoint_interval_secs: 60,
            enable_sandbox: true,
        }
    }
}

/// Thread pool manager
pub struct ThreadPool {
    config: PoolConfig,
    /// Keyed by `ChatThreadId::value`: the generated proto id type has no `Eq`/`Hash`.
    threads: Arc<DashMap<String, Arc<parking_lot::Mutex<ThreadEntry>>>>,
    warm_pool: Arc<RwLock<WarmPool>>,
    lifecycle_hooks: Arc<RwLock<LifecycleHooks>>,
    metrics: Arc<PoolMetrics>,
}

impl ThreadPool {
    pub fn new(config: PoolConfig) -> Self {
        Self {
            config,
            threads: Arc::new(DashMap::new()),
            warm_pool: Arc::new(RwLock::new(WarmPool::new())),
            lifecycle_hooks: Arc::new(RwLock::new(LifecycleHooks::new())),
            metrics: Arc::new(PoolMetrics::new()),
        }
    }

    pub async fn acquire(&self, subagent_type: SubagentType) -> PoolResult<ChatThreadHandle> {
        // Try to get from warm pool first
        if let Some(handle) = self.warm_pool.write().pop(&subagent_type) {
            self.metrics.inc_acquired_from_warm();
            return Ok(handle);
        }

        // Create new thread
        let handle = self.create_thread(subagent_type).await?;
        self.metrics.inc_created();
        Ok(handle)
    }

    pub async fn release(&self, handle: ChatThreadHandle) -> PoolResult<()> {
        let thread_id = handle.id().clone();

        if let Some(entry) = self.threads.get(thread_id.value.as_str()) {
            let mut entry = entry.lock();
            entry.state = ThreadStateKind::Idle;

            // Return to warm pool if there's space
            if self.warm_pool.read().len() < self.config.warm_pool_size {
                self.warm_pool
                    .write()
                    .push(entry.subagent_type.clone(), handle.clone());
            }
        }

        self.metrics.inc_released();
        Ok(())
    }

    pub async fn scale(&self, target: usize) -> PoolResult<()> {
        if target > self.config.max_threads {
            return Err(PoolError::Exhausted(
                "Target exceeds max_threads".to_string(),
            ));
        }
        // Implementation for dynamic scaling
        Ok(())
    }

    async fn create_thread(&self, subagent_type: SubagentType) -> PoolResult<ChatThreadHandle> {
        let thread_id = ChatThreadId {
            value: Uuid::new_v4().to_string(),
        };
        let handle = ChatThreadHandle::new(thread_id.clone(), subagent_type.clone());

        let entry = ThreadEntry {
            id: thread_id.clone(),
            subagent_type,
            state: ThreadStateKind::Initializing,
            handle: handle.clone(),
            created_at: gsd_runtime::clock::now(),
            last_checkpoint: None,
        };

        self.threads.insert(
            thread_id.value.clone(),
            Arc::new(parking_lot::Mutex::new(entry)),
        );

        // Run on_create hooks
        self.run_hooks(LifecycleEvent::OnCreate, &thread_id).await?;

        // Initialize thread
        {
            let entry = self
                .threads
                .get(thread_id.value.as_str())
                .ok_or_else(|| PoolError::NotFound(thread_id.value.clone()))?;
            entry.lock().state = ThreadStateKind::Running;
        }

        // Run on_initialize hooks
        self.run_hooks(LifecycleEvent::OnInitialize, &thread_id)
            .await?;

        Ok(handle)
    }

    async fn run_hooks(&self, event: LifecycleEvent, thread_id: &ChatThreadId) -> PoolResult<()> {
        let hooks = self.lifecycle_hooks.read();
        hooks.run(event, thread_id)
    }

    pub fn metrics(&self) -> &PoolMetrics {
        &self.metrics
    }
}

struct ThreadEntry {
    id: ChatThreadId,
    subagent_type: SubagentType,
    state: ThreadStateKind,
    handle: ChatThreadHandle,
    created_at: Instant,
    last_checkpoint: Option<Instant>,
}

struct WarmPool {
    /// Keyed by `SubagentType::value`.
    pool: DashMap<String, Vec<ChatThreadHandle>>,
}

impl WarmPool {
    fn new() -> Self {
        Self {
            pool: DashMap::new(),
        }
    }

    fn pop(&self, subagent_type: &SubagentType) -> Option<ChatThreadHandle> {
        self.pool
            .get_mut(subagent_type.value.as_str())
            .and_then(|mut v| v.pop())
    }

    fn push(&self, subagent_type: SubagentType, handle: ChatThreadHandle) {
        self.pool
            .entry(subagent_type.value)
            .or_default()
            .push(handle);
    }

    fn len(&self) -> usize {
        self.pool.iter().map(|v| v.len()).sum()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum LifecycleEvent {
    OnCreate,
    OnInitialize,
    OnAssign,
    OnSuspend,
    OnResume,
    OnCheckpoint,
    OnComplete,
    OnFailure,
    OnTerminate,
}

struct LifecycleHooks {
    hooks: DashMap<LifecycleEvent, Vec<Box<dyn Fn(&ChatThreadId) -> PoolResult<()> + Send + Sync>>>,
}

impl LifecycleHooks {
    fn new() -> Self {
        let hooks = DashMap::new();
        for event in [
            LifecycleEvent::OnCreate,
            LifecycleEvent::OnInitialize,
            LifecycleEvent::OnAssign,
            LifecycleEvent::OnSuspend,
            LifecycleEvent::OnResume,
            LifecycleEvent::OnCheckpoint,
            LifecycleEvent::OnComplete,
            LifecycleEvent::OnFailure,
            LifecycleEvent::OnTerminate,
        ] {
            hooks.insert(event, Vec::new());
        }
        Self { hooks }
    }

    /// Runs every hook registered for `event`, stopping at the first error.
    fn run(&self, event: LifecycleEvent, thread_id: &ChatThreadId) -> PoolResult<()> {
        if let Some(hooks) = self.hooks.get(&event) {
            for hook in hooks.value() {
                hook(thread_id)?;
            }
        }
        Ok(())
    }
}

struct PoolMetrics {
    created: std::sync::atomic::AtomicU64,
    acquired_from_warm: std::sync::atomic::AtomicU64,
    released: std::sync::atomic::AtomicU64,
    evicted: std::sync::atomic::AtomicU64,
    failed: std::sync::atomic::AtomicU64,
}

impl PoolMetrics {
    fn new() -> Self {
        Self {
            created: std::sync::atomic::AtomicU64::new(0),
            acquired_from_warm: std::sync::atomic::AtomicU64::new(0),
            released: std::sync::atomic::AtomicU64::new(0),
            evicted: std::sync::atomic::AtomicU64::new(0),
            failed: std::sync::atomic::AtomicU64::new(0),
        }
    }
}

impl PoolMetrics {
    fn inc_created(&self) {
        self.created.fetch_add(1, Ordering::Relaxed);
    }
    fn inc_acquired_from_warm(&self) {
        self.acquired_from_warm.fetch_add(1, Ordering::Relaxed);
    }
    fn inc_released(&self) {
        self.released.fetch_add(1, Ordering::Relaxed);
    }
    fn inc_evicted(&self) {
        self.evicted.fetch_add(1, Ordering::Relaxed);
    }
    fn inc_failed(&self) {
        self.failed.fetch_add(1, Ordering::Relaxed);
    }
}
