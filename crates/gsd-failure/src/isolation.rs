//! Failure isolation declarations: circuit breaker, retry policy, isolation entry point.

use std::time::Duration;

use crate::taxonomy::ErrorCode;

/// State of a circuit breaker.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CircuitState {
    /// Calls flow normally.
    Closed,
    /// Calls are rejected until the cooldown elapses.
    Open,
    /// A limited number of trial calls are allowed.
    HalfOpen,
}

/// Circuit breaker guarding a single dependency.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CircuitBreaker {
    /// Consecutive failures before the circuit opens.
    pub failure_threshold: u32,
    /// Time the circuit stays open before a trial call is allowed.
    pub cooldown: Duration,
    /// Current circuit state.
    pub state: CircuitState,
}

impl CircuitBreaker {
    /// Creates a closed circuit breaker with the given threshold and cooldown.
    pub fn new(failure_threshold: u32, cooldown: Duration) -> Self {
        Self {
            failure_threshold,
            cooldown,
            state: CircuitState::Closed,
        }
    }
}

/// Retry policy with exponential backoff bounds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetryPolicy {
    /// Maximum number of attempts, including the first.
    pub max_attempts: u32,
    /// Delay before the first retry.
    pub base_delay: Duration,
    /// Upper bound on any single retry delay.
    pub max_delay: Duration,
    /// Error codes eligible for retry.
    ///
    /// The default is the retryable column of the plan's n4 §2 Python-to-Rust
    /// error mapping: `E-TIMEOUT`, `E-DEP`, `E-CAPACITY`, and `E-CONTRACT`.
    /// `E-INPUT`, `E-POLICY`, and `E-INTERNAL` are not retryable.
    pub retryable_error_codes: Vec<crate::taxonomy::ErrorCode>,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_attempts: 3,
            base_delay: Duration::from_millis(100),
            max_delay: Duration::from_secs(5),
            retryable_error_codes: vec![
                ErrorCode::Timeout,
                ErrorCode::Dep,
                ErrorCode::Capacity,
                ErrorCode::Contract,
            ],
        }
    }
}

/// Entry point for failure isolation across dependency domains.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct FailureIsolation;

impl FailureIsolation {
    /// Creates a failure isolation handle.
    pub fn new() -> Self {
        Self
    }
}
