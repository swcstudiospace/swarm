//! State Store for GSD-Core
//!
//! Versioned, branchable, mergeable session state with conflict resolution.

pub mod session_create;
pub mod session_dispatch;

pub use session_create::{
    bind_thread, create_session, initial_snapshot, CreateError, CreateResult, ThreadBinding,
    MAX_THREADS_PER_SESSION, PHASE_KEY, TASK_KEY,
};
pub use session_dispatch::{
    admit_thread, dispatch_session, handoff_thread, DispatchError, DispatchResult,
};
use std::sync::Arc;

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
    Serialization(#[from] gsd_serde::SerdeError),
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
    async fn get_session(
        &self,
        session_id: &gsd_proto::gsd::v1::SessionId,
    ) -> StateResult<gsd_proto::gsd::v1::SessionState>;
    async fn put_session(&self, session: &gsd_proto::gsd::v1::SessionState) -> StateResult<()>;
    async fn delete_session(&self, session_id: &gsd_proto::gsd::v1::SessionId) -> StateResult<()>;
    async fn list_sessions(&self) -> StateResult<Vec<gsd_proto::gsd::v1::SessionId>>;
    async fn create_branch(
        &self,
        session_id: &gsd_proto::gsd::v1::SessionId,
        branch: &gsd_proto::gsd::v1::Branch,
    ) -> StateResult<()>;
    async fn merge_branch(
        &self,
        session_id: &gsd_proto::gsd::v1::SessionId,
        source: &str,
        target: &str,
    ) -> StateResult<()>;
    async fn create_checkpoint(
        &self,
        session_id: &gsd_proto::gsd::v1::SessionId,
        checkpoint: &gsd_proto::gsd::v1::Checkpoint,
    ) -> StateResult<()>;
    async fn get_checkpoint(
        &self,
        session_id: &gsd_proto::gsd::v1::SessionId,
        checkpoint_id: &str,
    ) -> StateResult<gsd_proto::gsd::v1::Checkpoint>;
    async fn list_checkpoints(
        &self,
        session_id: &gsd_proto::gsd::v1::SessionId,
        limit: u32,
        offset: u32,
    ) -> StateResult<Vec<gsd_proto::gsd::v1::Checkpoint>>;
    async fn append_event(&self, event: &gsd_proto::gsd::v1::StateEvent) -> StateResult<()>;
    async fn watch_events(
        &self,
        session_id: &gsd_proto::gsd::v1::SessionId,
        since: Option<prost_types::Timestamp>,
    ) -> StateResult<Vec<gsd_proto::gsd::v1::StateEvent>>;
}

/// State store
pub struct StateStore {
    config: StateConfig,
    cache:
        Arc<dashmap::DashMap<String, Arc<parking_lot::RwLock<gsd_proto::gsd::v1::SessionState>>>>,
    metrics: Arc<StateMetrics>,
}

impl StateStore {
    pub async fn new(config: StateConfig) -> StateResult<Self> {
        Ok(Self {
            config,
            cache: Arc::new(dashmap::DashMap::new()),
            metrics: Arc::new(StateMetrics::default()),
        })
    }

    pub async fn get_session(
        &self,
        session_id: &gsd_proto::gsd::v1::SessionId,
    ) -> StateResult<gsd_proto::gsd::v1::SessionState> {
        self.cache
            .get(&session_id.value)
            .map(|entry| entry.read().clone())
            .ok_or_else(|| StateError::SessionNotFound(session_id.value.clone()))
    }

    pub async fn put_session(&self, session: &gsd_proto::gsd::v1::SessionState) -> StateResult<()> {
        let key = session
            .session_id
            .as_ref()
            .ok_or_else(|| StateError::Other("SessionState.session_id is missing".to_string()))?
            .value
            .clone();
        self.cache
            .insert(key, Arc::new(parking_lot::RwLock::new(session.clone())));
        Ok(())
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
