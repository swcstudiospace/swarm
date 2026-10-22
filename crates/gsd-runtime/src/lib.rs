//! GSD-Core Runtime Primitives
//!
//! This crate provides the core runtime primitives for GSD-Core:
//! - Deterministic clock for replay
//! - Async primitives and synchronization
//! - I/O utilities
//! - Simulation mode for deterministic replay

pub mod clock;
pub mod io;
pub mod sync;

pub use clock::*;
pub use io::*;
pub use sync::*;

/// Result type for runtime operations
pub type RuntimeResult<T> = std::result::Result<T, RuntimeError>;

/// Runtime errors
#[derive(Debug, thiserror::Error)]
pub enum RuntimeError {
    #[error("Timeout: {0}")]
    Timeout(String),
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("Timeout error: {0}")]
    TimeoutError(#[from] tokio::time::error::Elapsed),
    #[error("Join error: {0}")]
    JoinError(#[from] tokio::task::JoinError),
    #[error("Other: {0}")]
    Other(String),
}

impl From<anyhow::Error> for RuntimeError {
    fn from(e: anyhow::Error) -> Self {
        RuntimeError::Other(e.to_string())
    }
}
