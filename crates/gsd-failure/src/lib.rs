//! Failure domain isolation for GSD-Core: error taxonomy, circuit breakers, retry policy.
//!
//! Declarations only in this phase; behavior lands in later phases.

pub mod isolation;
pub mod taxonomy;

pub use isolation::{CircuitBreaker, CircuitState, FailureIsolation, RetryPolicy};
pub use taxonomy::{ErrorCode, FailureError};
