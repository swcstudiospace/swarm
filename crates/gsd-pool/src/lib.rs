//! Thread Pool Manager for GSD-Core
//!
//! Manages lifecycle, pooling, reuse, and isolation for chat-thread subagents.
//!
//! The public surface is the chat facade in [`chat`], its data types, the thread state machine,
//! and [`PoolError`], [`PoolConfig`] and [`PoolResult`]. The pool machinery (`ThreadPool`, the
//! warm pool, the handle registry and the lifecycle dispatcher) is crate-private (D-1).

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use dashmap::mapref::entry::Entry;
use dashmap::DashMap;
use gsd_codes::ErrorCode;
use gsd_proto::gsd::v1::{ChatThreadId, SubagentType};
use parking_lot::RwLock;
use uuid::Uuid;

use crate::handle_registry::ChatThreadHandle;
use crate::hook_dispatch::{LifecycleDispatcher, LifecycleEvent};
use crate::warm_reuse::WarmPool;

mod cancel;
pub mod chat;
mod chat_runtime;
mod fair_queue;
mod handle_registry;
mod hook_dispatch;
pub mod thread_state_machine;
pub mod thread_state_transition_table;
mod warm_reuse;

pub use cancel::{CancelOutcome, CancelReport};
pub use chat::{
    BudgetPort, BudgetRefusal, BudgetReservation, ChatError, ChatPool, ChatRuntimeConfig, Placement,
};
pub use chat_runtime::{
    ChatMessage, Completed, MessageRef, ShutdownReport, SubmitOutcome, Submitted, Ticket,
};
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
    #[error("Sandbox error: {message}")]
    Sandbox { code: ErrorCode, message: String },
    #[error("Capability mismatch: {0}")]
    CapabilityMismatch(String),
    #[error("Budget exhausted: {0}")]
    BudgetExhausted(String),
    #[error("Other: {0}")]
    Other(String),
}

impl From<anyhow::Error> for PoolError {
    fn from(e: anyhow::Error) -> Self {
        PoolError::Other(format!("{e:#}"))
    }
}

/// Thread pool configuration
#[derive(Debug, Clone)]
pub struct PoolConfig {
    pub max_threads: usize,
    pub warm_pool_size: usize,
    pub eviction_grace_secs: u64,
}

impl Default for PoolConfig {
    fn default() -> Self {
        Self {
            max_threads: 30,
            warm_pool_size: 5,
            eviction_grace_secs: 30,
        }
    }
}

/// Crate-private thread lifecycle machinery behind [`ChatPool`].
///
/// Admission is not checked here: [`handle_registry::HandleRegistry`] owns the live-thread cap.
/// The thread map holds only live threads. A parked thread leaves it in both placements, so the
/// map and the warm pool never hold the same id.
pub(crate) struct ThreadPool {
    /// Live threads and the subagent type each runs. Keyed by `ChatThreadId::value`: the
    /// generated proto id type has no `Eq`/`Hash`.
    threads: DashMap<String, SubagentType>,
    warm_pool: RwLock<WarmPool>,
    lifecycle: LifecycleDispatcher,
    metrics: PoolMetrics,
}

impl ThreadPool {
    /// Creates an empty pool sized by `config.warm_pool_size` and its eviction grace period.
    pub(crate) fn new(config: &PoolConfig) -> Self {
        let warm_pool = WarmPool::new(
            config.warm_pool_size,
            Duration::from_secs(config.eviction_grace_secs),
        );
        Self {
            threads: DashMap::new(),
            warm_pool: RwLock::new(warm_pool),
            lifecycle: LifecycleDispatcher::default(),
            metrics: PoolMetrics::new(),
        }
    }

    /// Creates a live thread with a generated id and returns the id.
    pub(crate) fn create_thread(&self, subagent_type: SubagentType) -> ChatThreadId {
        let id = ChatThreadId {
            value: Uuid::new_v4().to_string(),
        };
        self.threads.insert(id.value.clone(), subagent_type);
        self.announce_created(&id);
        id
    }

    /// Creates a live thread with the caller's `id`.
    ///
    /// Returns `PoolError::AlreadyExists` when `id` is already a live thread. Otherwise any warm
    /// entry under `id` is discarded first, so its termination is reported before the new
    /// thread's creation.
    pub(crate) fn open_with_id(&self, id: &str, subagent_type: SubagentType) -> PoolResult<()> {
        match self.threads.entry(id.to_string()) {
            Entry::Occupied(_) => Err(PoolError::AlreadyExists(id.to_string())),
            Entry::Vacant(slot) => {
                self.discard_warm(id);
                slot.insert(subagent_type);
                self.announce_created(&ChatThreadId {
                    value: id.to_string(),
                });
                Ok(())
            }
        }
    }

    /// Takes the most recently parked live handle for `subagent_type`, counting a warm hit.
    /// Expired entries are swept first, and each swept thread is reported as terminated.
    pub(crate) fn pop_warm(
        &self,
        subagent_type: &SubagentType,
        now: Instant,
    ) -> Option<ChatThreadHandle> {
        let mut warm = self.warm_pool.write();
        let expired = warm.evict_expired(now);
        let handle = warm.pop(subagent_type, now);
        drop(warm);
        for id in expired {
            self.report_terminated(id);
        }
        if handle.is_some() {
            self.metrics.inc_acquired_from_warm();
        }
        handle
    }

    /// Makes a handle taken from the warm pool live again under its own id.
    pub(crate) fn reopen_parked(&self, handle: &ChatThreadHandle) {
        self.threads
            .insert(handle.id().value.clone(), handle.subagent_type().clone());
        self.lifecycle
            .dispatch(&LifecycleEvent::OnResume(handle.id().clone()));
    }

    /// Removes any warm entry for thread `id` and reports its termination. Used when a parked id
    /// is opened fresh (DD-13).
    pub(crate) fn discard_warm(&self, id: &str) {
        let removed = self.warm_pool.write().remove_id(id);
        if removed {
            self.report_terminated(id.to_string());
        }
    }

    /// Parks a released handle in the warm pool, aging it from `now`.
    ///
    /// The thread leaves the live set in both outcomes. Each id the push evicted leaves the warm
    /// pool and its termination is reported. When the pool is full the thread is closed: its
    /// termination is reported and the caller gets [`Placement::Closed`]. The handle is never
    /// dropped without a placement.
    pub(crate) fn park_handle(&self, handle: ChatThreadHandle, now: Instant) -> Placement {
        let id = handle.id().value.clone();
        let subagent_type = handle.subagent_type().clone();
        let outcome = self.warm_pool.write().push(&subagent_type, handle, now);
        self.threads.remove(&id);
        self.metrics.inc_released();
        for evicted in outcome.evicted {
            self.report_terminated(evicted);
        }
        if outcome.parked {
            Placement::Parked
        } else {
            self.report_terminated(id);
            Placement::Closed
        }
    }

    /// Removes live thread `id` from the thread map and reports its termination.
    pub(crate) fn forget(&self, id: &str) {
        if self.threads.remove(id).is_some() {
            self.report_terminated(id.to_string());
        }
    }

    /// Reports the termination of a parked thread that acquire discards because it cannot hand
    /// the thread out. `forget` cannot report it, because a parked thread is not in `threads`.
    pub(crate) fn discard_parked(&self, id: &str) {
        self.report_terminated(id.to_string());
    }

    /// Dispatches `OnTerminate` for thread `id`.
    fn report_terminated(&self, id: String) {
        self.lifecycle
            .dispatch(&LifecycleEvent::OnTerminate(ChatThreadId { value: id }));
    }

    /// Reports a newly created thread and counts it.
    fn announce_created(&self, id: &ChatThreadId) {
        self.lifecycle
            .dispatch(&LifecycleEvent::OnCreate(id.clone()));
        self.metrics.inc_created();
    }

    /// Counters for created, warm-reused and released threads.
    #[cfg(test)]
    pub(crate) fn metrics(&self) -> &PoolMetrics {
        &self.metrics
    }

    /// Number of parked handles in the warm pool.
    #[cfg(test)]
    pub(crate) fn warm_len(&self) -> usize {
        self.warm_pool.read().len()
    }
}

/// Counters kept by the pool. Crate-private.
pub(crate) struct PoolMetrics {
    created: AtomicU64,
    acquired_from_warm: AtomicU64,
    released: AtomicU64,
}

impl PoolMetrics {
    fn new() -> Self {
        Self {
            created: AtomicU64::new(0),
            acquired_from_warm: AtomicU64::new(0),
            released: AtomicU64::new(0),
        }
    }

    fn inc_created(&self) {
        self.created.fetch_add(1, Ordering::Relaxed);
    }

    fn inc_acquired_from_warm(&self) {
        self.acquired_from_warm.fetch_add(1, Ordering::Relaxed);
    }

    fn inc_released(&self) {
        self.released.fetch_add(1, Ordering::Relaxed);
    }

    /// Threads created since the pool started.
    #[cfg(test)]
    pub(crate) fn created(&self) -> u64 {
        self.created.load(Ordering::Relaxed)
    }

    /// Threads taken from the warm pool.
    #[cfg(test)]
    pub(crate) fn acquired_from_warm(&self) -> u64 {
        self.acquired_from_warm.load(Ordering::Relaxed)
    }

    /// Threads released or parked, including those closed because the warm pool was full.
    #[cfg(test)]
    pub(crate) fn released(&self) -> u64 {
        self.released.load(Ordering::Relaxed)
    }
}

#[cfg(test)]
mod sandbox_error_tests {
    use super::*;

    #[test]
    fn sandbox_variant_carries_each_error_code() {
        let codes = [
            ErrorCode::Input,
            ErrorCode::Timeout,
            ErrorCode::Dep,
            ErrorCode::Capacity,
            ErrorCode::Contract,
            ErrorCode::Policy,
            ErrorCode::Internal,
        ];
        for code in codes {
            let err = PoolError::Sandbox {
                code,
                message: "sandbox failure".to_string(),
            };
            match &err {
                PoolError::Sandbox { code: got, .. } => assert_eq!(*got, code),
                other => panic!("expected Sandbox variant, got {other:?}"),
            }
            assert!(err.to_string().starts_with("Sandbox error: "));
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use parking_lot::Mutex;

    use super::*;
    use crate::hook_dispatch::LifecycleObserver;

    /// Records the thread id of every `OnTerminate` event, in dispatch order.
    struct TerminateRecorder {
        terminated: Arc<Mutex<Vec<String>>>,
    }

    impl LifecycleObserver for TerminateRecorder {
        fn on_event(&self, event: &LifecycleEvent) {
            if let LifecycleEvent::OnTerminate(id) = event {
                self.terminated.lock().push(id.value.clone());
            }
        }
    }

    fn handle(thread: &str) -> ChatThreadHandle {
        ChatThreadHandle::new(
            ChatThreadId {
                value: thread.to_string(),
            },
            SubagentType {
                value: "coder".to_string(),
            },
        )
    }

    /// Builds a pool with a terminate recorder registered before any event is dispatched.
    fn pool_with_recorder(config: &PoolConfig) -> (ThreadPool, Arc<Mutex<Vec<String>>>) {
        let pool = ThreadPool::new(config);
        let terminated = Arc::new(Mutex::new(Vec::new()));
        pool.lifecycle.register(Arc::new(TerminateRecorder {
            terminated: Arc::clone(&terminated),
        }));
        (pool, terminated)
    }

    #[test]
    fn from_anyhow_keeps_root_cause() {
        let err = anyhow::anyhow!("root cause").context("outer context");
        let converted = PoolError::from(err);
        assert!(
            converted.to_string().contains("root cause"),
            "converted error lost the root cause: {converted}"
        );
    }

    #[test]
    fn evicted_warm_threads_are_terminated_once() {
        let config = PoolConfig {
            warm_pool_size: 2,
            eviction_grace_secs: 10,
            ..PoolConfig::default()
        };
        let (pool, terminated) = pool_with_recorder(&config);
        let t0 = Instant::now();
        assert!(matches!(
            pool.park_handle(handle("t1"), t0),
            Placement::Parked
        ));
        assert!(matches!(
            pool.park_handle(handle("t2"), t0),
            Placement::Parked
        ));
        // The pool is full and both entries are past the grace period, so this park evicts both.
        let later = t0 + Duration::from_secs(11);
        assert!(matches!(
            pool.park_handle(handle("t3"), later),
            Placement::Parked
        ));
        assert_eq!(pool.warm_len(), 1);

        let mut got = terminated.lock().clone();
        got.sort();
        assert_eq!(got, vec!["t1".to_string(), "t2".to_string()]);
    }

    #[test]
    fn discarded_warm_thread_is_terminated_once() {
        let (pool, terminated) = pool_with_recorder(&PoolConfig::default());
        let t0 = Instant::now();
        assert!(matches!(
            pool.park_handle(handle("t1"), t0),
            Placement::Parked
        ));
        assert_eq!(pool.warm_len(), 1);

        pool.discard_warm("t1");
        assert_eq!(pool.warm_len(), 0);
        assert_eq!(*terminated.lock(), vec!["t1".to_string()]);
    }
}
