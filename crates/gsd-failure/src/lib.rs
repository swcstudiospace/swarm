//! Failure domain isolation for GSD-Core: error taxonomy, circuit breakers, retry policy.
//!
//! Declarations only in this phase; behavior lands in later phases.

pub mod error_taxonomy_map;
pub mod isolation;
pub mod taxonomy;

pub use error_taxonomy_map::{error_code_from_python, ErrorKind};
pub use isolation::{CircuitBreaker, CircuitState, FailureIsolation, RetryPolicy};
pub use taxonomy::{ErrorCode, FailureError};
