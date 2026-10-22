//! State Store for GSD-Core
//!
//! Versioned, branchable, mergeable session state with conflict resolution.

pub mod backend;
pub mod models;
pub mod checkpoint;
pub mod branch;
pub mod event_store;
pub mod migration;

pub use backend::*;
pub use models::*;
pub use checkpoint::*;
pub use branch::*;
pub use event_store::*;
pub use migration::*;

use std::sync::Arc;
use gsd_proto::gsd::v1::{SessionId, SessionState, Branch, Checkpoint, StateEvent, ThreadState};
use gsd_runtime::{RuntimeResult, RuntimeError};
use gsd_proto::gsd::v1::{SessionId, SessionState, Branch, Checkpoint, StateEvent, ThreadState};
use gsd_serde::{serialize_for_storage, deserialize_from_storage};
use gsd_observability::{TracingLayer, MetricsSink};
use gsd_runtime::{RuntimeResult, RuntimeError};
use std::sync::Arc;
use dashmap::DashMap;
use parking_lot::RwLock;
use uuid::Uuid;
use std::sync::Arc;
use dashmap::DashMap;
use parking_lot::RwLock;
use uuid::Uuid;
use tracing::{trace, debug, info, warn};
use std::sync::Arc;
use parking_lot::RwLock;
use dashmap::DashMap;
use uuid::Uuid;

/// Result type for state store operations
pub type StateResult<T> = std::result::Result<T, StateError>;

/// State store errors
#[derive(Debug, thiserror::Error)]
pub enum StateError {
    #[error("Session not found: {0}")]
    SessionNotFound(String),
    #[error("Branch not found: {0}")]
    BranchNotFound(String),
    #[error("Checkpoint not found: {0}")]
    CheckpointNotFound(String),
    #[error("Conflict: {0}")]
    Conflict(String),
    #[error("Storage error: {0}")]
    Storage(String),
    #[error("Serialization error: {0}")]
    Serialization(#[from] crate::serde::SerdeError),
    #[error("Backend error: {0}")]
    Backend(String),
    #[error("Conflict during merge: {0}")]
    MergeConflict(String),
    #[error("Other: {0}")]
    Other(String),
}

impl From<anyhow::Error> for StateError {
    fn from(e: anyhow::Error) -> Self {
        StateError::Other(e.to_string())
    }
}

/// State store configuration
#[derive(Debug, Clone)]
pub struct StateConfig {
    pub backend: BackendType,
    pub postgres_url: Option<String>,
    pub sqlite_path: Option<String>,
    pub s3_bucket: Option<String>,
    pub redis_url: Option<String>,
    pub checkpoint_interval_secs: u64,
    pub max_checkpoints: usize,
    pub enable_branching: bool,
}

impl Default for StateConfig {
    fn default() -> Self {
        Self {
            backend: BackendType::Sqlite,
            postgres_url: None,
            sqlite_path: Some("./gsd_state.db".to_string()),
            s3_bucket: None,
            redis_url: None,
            checkpoint_interval_secs: 60,
            max_checkpoints: 100,
            enable_branching: true,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendType {
    Postgres,
    Sqlite,
    S3,
    Redis,
    Memory,
}

/// State store trait for pluggable backends
#[async_trait::async_trait]
pub trait StateBackend: Send + Sync {
    async fn get_session(&self, session_id: &gsd_proto::gsd::v1::SessionId) -> StateResult<gsd_proto::gsd::v1::SessionState>;
    async fn put_session(&self, session: &gsd_proto::gsd::v1::SessionState) -> StateResult<()>;
    async fn delete_session(&self, session_id: &gsd_proto::gsd::v1::SessionId) -> StateResult<()>;
    async fn list_sessions(&self) -> StateResult<Vec<gsd_proto::gsd::v1::SessionId>>;
    async fn create_branch(&self, session_id: &gsd_proto::gsd::v1::SessionId, branch: &gsd_proto::gsd::v1::Branch) -> StateResult<()>;
    async fn merge_branch(&self, session_id: &gsd_proto::gsd::v1::SessionId, source: &str, target: &str) -> StateResult<()>;
    async fn create_checkpoint(&self, session_id: &gsd_proto::gsd::v1::SessionId, checkpoint: &gsd_proto::gsd::v1::Checkpoint) -> StateResult<()>;
    async fn get_checkpoint(&self, session_id: &gsd_proto::gsd::v1::SessionId, checkpoint_id: &str) -> StateResult<gsd_proto::gsd::v1::Checkpoint>;
    async fn list_checkpoints(&self, session_id: &gsd_proto::gsd::v1::SessionId, limit: u32, offset: u32) -> StateResult<Vec<gsd_proto::gsd::v1::Checkpoint>>;
    async fn append_event(&self, event: &gsd_proto::gsd::v1::StateEvent) -> StateResult<()>;
    async fn watch_events(&self, session_id: &gsd_proto::gsd::v1::SessionId, since: Option<google.protobuf.Timestamp>) -> StateResult<Vec<gsd_proto::gsd::v1::StateEvent>>;
}

/// State store
pub struct StateStore {
    config: StateConfig,
    backend: Arc<dyn StateBackend>,
    cache: Arc<dashmap::DashMap<String, Arc<parking_lot::RwLock<gsd_proto::gsd::v1::SessionState>>>>,
    event_store: Arc<EventStore>,
    metrics: Arc<StateMetrics>,
}

impl StateStore {
    pub async fn new(config: StateConfig) -> StateResult<Self> {
        let backend: Arc<dyn StateBackend> = match config.backend {
            BackendType::Postgres => {
                #[cfg(feature = "postgres")]
                {
                    Arc::new(backend::PostgresBackend::new(&config.postgres_url.unwrap_or_default()).await?)
                }
                #[cfg(not(feature = "postgres"))]
                {
                    return Err(StateError::Backend("Postgres backend not compiled".to_string()));
                }
            }
            BackendType::Sqlite => {
                Arc::new(backend::SqliteBackend::new(&config.sqlite_path.unwrap_or_default()).await?)
            }
            BackendType::Memory => {
                Arc::new(backend::MemoryBackend::new())
            }
            _ => return Err(StateError::Backend("Backend not implemented".to_string())),
        };

        Ok(Self {
            config,
            backend,
            cache: Arc::new(dashmap::DashMap::new()),
            event_store: Arc::new(EventStore::new()),
            metrics: Arc::new(StateMetrics::new()),
        })
    }

    pub async fn get_session(&self, session_id: &gsd_proto::gsd::v1::SessionId) -> StateResult<gsd_proto::gsd::v1::SessionState> {
        if let Some(cached) = self.cache.get(&session_id.value) {
            return Ok(cached.read().clone());
        }

        let session = self.backend.get_session(&gsd_proto::gsd::v1::SessionId { value: session_id.value.clone() }).await?;
        self.cache.insert(session_id.value.clone(), Arc::new(parking_lot::RwLock::new(session.clone())));
        Ok(session)
    }

    pub async fn put_session(&self, session: &gsd_proto::gsd::v1::SessionState) -> StateResult<()> {
        self.backend.put_session(session).await?;
        self.cache.insert(session.session_id.value.clone(), Arc::new(parking_lot::RwLock::new(session.clone())));
        Ok(())
    }

    pub async fn create_checkpoint(&self, session_id: &gsd_proto::gsd::v1::SessionId, description: &str) -> StateResult<gsd_proto::gsd::v1::Checkpoint> {
        let session = self.get_session(session_id).await?;
        let checkpoint = gsd_proto::gsd::v1::Checkpoint {
            id: uuid::Uuid::new_v4().to_string(),
            session_id: session_id.value.clone(),
            description: description.to_string(),
            created_at: Some(google.protobuf.Timestamp {
                seconds: gsd_runtime::clock::now_system().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs() as i64,
                nanos: 0,
            }),
            state_snapshot: gsd_serde::serialize_for_storage(&gsd_proto::gsd::v1::SessionState::default())?,
            metadata: std::collections::HashMap::new(),
        };
        
        self.backend.create_checkpoint(session_id, &checkpoint).await?;
        Ok(checkpoint)
    }

    pub async fn branch(&self, session_id: &gsd_proto::gsd::v1::SessionId, branch_name: &str) -> StateResult<gsd_proto::gsd::v1::Branch> {
        let checkpoint = self.create_checkpoint(session_id, "Branch point").await?;
        let branch = gsd_proto::gsd::v1::Branch {
            name: branch_name.to_string(),
            parent_branch: "main".to_string(),
            base_checkpoint_id: checkpoint.id.clone(),
            created_at: Some(google.protobuf.Timestamp {
                seconds: gsd_runtime::clock::now_system().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs() as i64,
                nanos: 0,
            }),
            metadata: std::collections::HashMap::new(),
        };
        
        self.backend.create_branch(
            &gsd_proto::gsd::v1::SessionId { value: session_id.value.clone() },
            &branch
        ).await?;
        
        Ok(branch)
    }

    pub async fn merge(&self, session_id: &gsd_proto::gsd::v1::SessionId, source: &str, target: &str) -> StateResult<()> {
        self.backend.merge_branch(
            &gsd_proto::gsd::v1::SessionId { value: session_id.value.clone() },
            source,
            target
        ).await
    }

    pub async fn append_event(&self, event: gsd_proto::gsd::v1::StateEvent) -> StateResult<()> {
        self.event_store.append(event).await
    }

    pub fn metrics(&self) -> &StateMetrics {
        &self.metrics
    }
}

struct StateMetrics {
    gets: std::sync::atomic::AtomicU64,
    puts: std::sync::atomic::AtomicU64,
    checkpoints: std::sync::atomic::AtomicU64,
    branches: std::sync::atomic::AtomicU64,
    merges: std::sync::atomic::AtomicU64,
    cache_hits: std::sync::atomic::AtomicU64,
    cache_misses: std::sync::atomic::AtomicU64,
}

impl Default for StateMetrics {
    fn default() -> Self {
        Self {
            gets: std::sync::atomic::AtomicU64::new(0),
            puts: std::sync::atomic::AtomicU64::new(0),
            checkpoints: std::sync::atomic::AtomicU64::new(0),
            branches: std::sync::atomic::AtomicU64::new(0),
            merges: std::sync::atomic::AtomicU64::new(0),
            cache_hits: std::sync::atomic::AtomicU64::new(0),
            cache_misses: std::sync::atomic::AtomicU64::new(0),
        }
    }
}