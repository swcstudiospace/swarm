//! Shared failure codes, mirroring the seven codes in `swarm/errors.py`.
//!
//! This is a leaf crate with no dependencies so that it builds for every target,
//! including the `wasm32-wasip2` sandboxed guest.

use std::fmt;

/// One of the seven shared failure codes from `swarm/errors.py`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ErrorCode {
    /// Invalid or missing input artifact (`E-INPUT`).
    Input,
    /// Budget, lease, or SLA exceeded (`E-TIMEOUT`).
    Timeout,
    /// Upstream dependency unavailable (`E-DEP`).
    Dep,
    /// Overload or WIP cap hit (`E-CAPACITY`).
    Capacity,
    /// Output rejected by consumer or schema (`E-CONTRACT`).
    Contract,
    /// Blocked by security or compliance policy (`E-POLICY`).
    Policy,
    /// Unexpected internal fault (`E-INTERNAL`).
    Internal,
}

impl ErrorCode {
    /// Returns the wire code string, e.g. `"E-INPUT"`.
    pub fn as_str(&self) -> &'static str {
        match self {
            ErrorCode::Input => "E-INPUT",
            ErrorCode::Timeout => "E-TIMEOUT",
            ErrorCode::Dep => "E-DEP",
            ErrorCode::Capacity => "E-CAPACITY",
            ErrorCode::Contract => "E-CONTRACT",
            ErrorCode::Policy => "E-POLICY",
            ErrorCode::Internal => "E-INTERNAL",
        }
    }
}

impl fmt::Display for ErrorCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}
