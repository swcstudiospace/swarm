//! Shared error taxonomy, mirroring the seven codes in `swarm/errors.py`.

use std::fmt;

pub use gsd_codes::ErrorCode;

/// An error carrying one taxonomy code and a human-readable message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailureError {
    code: ErrorCode,
    message: String,
}

impl FailureError {
    /// Creates a new error with the given code and message.
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    /// Returns the taxonomy code of this error.
    pub fn code(&self) -> ErrorCode {
        self.code
    }

    /// Returns the message of this error.
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for FailureError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code.as_str(), self.message)
    }
}

impl std::error::Error for FailureError {}
