//! Thread Pool Manager for GSD-Core
//!
//! Manages lifecycle, pooling, reuse, and isolation for chat-thread subagents.
//!
//! The public surface is the chat facade in [`chat`], its data types, the thread state machine,
//! and [`PoolError`], [`PoolConfig`] and [`PoolResult`]. The pool machinery (`ThreadPool`, the warm
//! pool and the handle registry) is crate-private.

use std::time::{Duration, Instant};

use dashmap::mapref::entry::Entry;
use dashmap::DashMap;
use gsd_codes::ErrorCode;
use gsd_proto::gsd::v1::{ChatThreadId, SubagentType};
use parking_lot::RwLock;
use uuid::Uuid;

use crate::handle_registry::ChatThreadHandle;
use crate::warm_reuse::WarmPool;

mod cancel;
pub mod chat;
mod chat_runtime;
mod fair_queue;
mod handle_registry;
pub mod thread_state_machine;
pub mod thread_state_transition_table;
mod warm_reuse;

pub use cancel::{CancelOutcome, CancelReport};
pub use chat::{
    BudgetPort, BudgetRefusal, BudgetReservation, ChatError, ChatPool, ChatRuntimeConfig,
    Placement, MAX_ID_BYTES, MAX_PAYLOAD_BYTES,
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
        }
    }

    /// Creates a live thread with a generated id and returns the id.
    pub(crate) fn create_thread(&self, subagent_type: SubagentType) -> ChatThreadId {
        let id = ChatThreadId {
            value: Uuid::new_v4().to_string(),
        };
        self.threads.insert(id.value.clone(), subagent_type);
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
                Ok(())
            }
        }
    }

    /// Takes the most recently parked live handle for `subagent_type`, counting a warm hit.
    /// Expired entries are swept first.
    pub(crate) fn pop_warm(
        &self,
        subagent_type: &SubagentType,
        now: Instant,
    ) -> Option<ChatThreadHandle> {
        let mut warm = self.warm_pool.write();
        warm.evict_expired(now);
        warm.pop(subagent_type, now)
    }

    /// Makes a handle taken from the warm pool live again under its own id.
    pub(crate) fn reopen_parked(&self, handle: &ChatThreadHandle) {
        self.threads
            .insert(handle.id().value.clone(), handle.subagent_type().clone());
    }

    /// Removes any warm entry for thread `id`. Used when a parked id is opened fresh (DD-13).
    pub(crate) fn discard_warm(&self, id: &str) {
        self.warm_pool.write().remove_id(id);
    }

    /// Parks a released handle in the warm pool, aging it from `now`.
    ///
    /// The thread leaves the live set in both outcomes. When the pool is full the thread is closed
    /// and the caller gets [`Placement::Closed`]. The handle is never dropped without a placement.
    pub(crate) fn park_handle(&self, handle: ChatThreadHandle, now: Instant) -> Placement {
        let id = handle.id().value.clone();
        let subagent_type = handle.subagent_type().clone();
        let outcome = self.warm_pool.write().push(&subagent_type, handle, now);
        self.threads.remove(&id);
        if outcome.parked {
            Placement::Parked
        } else {
            Placement::Closed
        }
    }

    /// Removes live thread `id` from the thread map.
    pub(crate) fn forget(&self, id: &str) {
        self.threads.remove(id);
    }

    /// Number of parked handles in the warm pool.
    #[cfg(test)]
    pub(crate) fn warm_len(&self) -> usize {
        self.warm_pool.read().len()
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
    use super::*;

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
    fn a_full_warm_pool_evicts_its_expired_entries_for_a_new_park() {
        let config = PoolConfig {
            warm_pool_size: 2,
            eviction_grace_secs: 10,
            ..PoolConfig::default()
        };
        let pool = ThreadPool::new(&config);
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
    }

    #[test]
    fn discard_warm_removes_the_parked_thread() {
        let pool = ThreadPool::new(&PoolConfig::default());
        let t0 = Instant::now();
        assert!(matches!(
            pool.park_handle(handle("t1"), t0),
            Placement::Parked
        ));
        assert_eq!(pool.warm_len(), 1);

        pool.discard_warm("t1");
        assert_eq!(pool.warm_len(), 0);
    }
}
