//! Central error bridge for the additive graph family (`pyo3/module.rs`).
//!
//! Boundary contract (single source for every new export):
//! - Rust side: [`BridgeError`] is the only typed error new code returns; it is
//!   `Send + Sync`, owns its strings, and never borrows Python objects.
//! - Python side: [`NativeError`] is the base exception; `NativeInputError`,
//!   `NativeContractError` and `NativeInternalError` are its subclasses. Every
//!   message starts with the stable wire code (`E-INPUT: <op> failed: ...`),
//!   matching the legacy families' convention, so log scrapers keep working.
//! - Error map: invalid/duplicate/unknown-node -> `E-INPUT` / `NativeInputError`;
//!   cycle or unclassified input -> `E-CONTRACT` / `NativeContractError`
//!   (unclassified is never retried); capacity/limit -> `E-CAPACITY` /
//!   `NativeInputError` (message carries `E-CAPACITY`); panic or unknown
//!   Python exception -> `E-INTERNAL` / `NativeInternalError`.
//! - No panic crosses the FFI: every `#[pyfunction]` body runs through
//!   [`guard`], which converts a panic into `BridgeError::Internal`.
//! - Python -> Rust: [`bridge_from_py_type`] maps a Python exception's type
//!   name (and, for `NativeError`, its wire prefix) to a typed `BridgeError`.
//!   An unknown type name is `Unclassified` (no retry), never guessed.

use std::panic::{catch_unwind, AssertUnwindSafe};

use ::gsd_codes::ErrorCode;
use ::pyo3::create_exception;
use ::pyo3::exceptions::PyException;
use ::pyo3::prelude::*;

// Base for every graph-family exception. Catch this to handle all of them.
create_exception!(
    _native,
    NativeError,
    PyException,
    "Base for swarm._native graph-family errors."
);

// Invalid input: wrong type, duplicate node, unknown node, bad id, bad count.
create_exception!(
    _native,
    NativeInputError,
    NativeError,
    "Invalid input (E-INPUT)."
);

// Contract violation: cycle detected, or input could not be classified. No retry.
create_exception!(
    _native,
    NativeContractError,
    NativeError,
    "Contract violation or unclassified input (E-CONTRACT)."
);

// Unexpected internal fault, including a caught Rust panic (E-INTERNAL).
create_exception!(
    _native,
    NativeInternalError,
    NativeError,
    "Internal fault (E-INTERNAL)."
);

/// The typed Rust error every new export returns internally.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BridgeError {
    /// Wrong type/value, duplicate, unknown node, malformed id.
    InvalidInput { op: &'static str, detail: String },
    /// A cycle was detected; the graph cannot be scheduled.
    Cycle { op: &'static str },
    /// A bounded cap was exceeded. Message carries `E-CAPACITY`.
    Capacity { op: &'static str, detail: String },
    /// Input could not be classified. Never retry.
    Unclassified { op: &'static str, detail: String },
    /// Caught panic or any other unexpected fault.
    Internal { op: &'static str },
}

impl BridgeError {
    /// Stable wire code for this error.
    pub fn code(&self) -> ErrorCode {
        match self {
            BridgeError::InvalidInput { .. } => ErrorCode::Input,
            BridgeError::Cycle { .. } | BridgeError::Unclassified { .. } => ErrorCode::Contract,
            BridgeError::Capacity { .. } => ErrorCode::Capacity,
            BridgeError::Internal { .. } => ErrorCode::Internal,
        }
    }

    /// Whether a caller may retry this error. Only `Internal` is retryable in
    /// principle; the bridge itself never retries, and `Unclassified` never is.
    pub fn retryable(&self) -> bool {
        matches!(self, BridgeError::Internal { .. })
    }

    /// The wire message: `<code>: <op> failed: <detail>`.
    pub fn wire(&self) -> String {
        let code = self.code().as_str();
        match self {
            BridgeError::InvalidInput { op, detail }
            | BridgeError::Capacity { op, detail }
            | BridgeError::Unclassified { op, detail } => {
                format!("{code}: {op} failed: {detail}")
            }
            BridgeError::Cycle { op } => format!("{code}: {op} failed: cycle detected"),
            BridgeError::Internal { op } => {
                format!("{code}: {op} failed: internal panic")
            }
        }
    }

    /// Convert to the Python exception for this error's class.
    pub fn to_pyerr(&self) -> PyErr {
        let message = self.wire();
        match self {
            BridgeError::InvalidInput { .. } | BridgeError::Capacity { .. } => {
                NativeInputError::new_err(message)
            }
            BridgeError::Cycle { .. } | BridgeError::Unclassified { .. } => {
                NativeContractError::new_err(message)
            }
            BridgeError::Internal { .. } => NativeInternalError::new_err(message),
        }
    }
}

impl std::fmt::Display for BridgeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.wire())
    }
}

impl std::error::Error for BridgeError {}

/// Run `work`, converting a panic into `BridgeError::Internal` for `op`.
/// The panic payload is dropped, never echoed: it can quote the input.
pub fn guard<T>(
    op: &'static str,
    work: impl FnOnce() -> Result<T, BridgeError>,
) -> Result<T, BridgeError> {
    catch_unwind(AssertUnwindSafe(work)).unwrap_or_else(|_| Err(BridgeError::Internal { op }))
}

/// Map a Python exception's type name to a typed `BridgeError`.
///
/// `message` is `str(exc)`. For `NativeError` and subclasses the wire prefix
/// (`E-INPUT:` etc.) already in `message` is preserved via `InvalidInput`
/// only when it is `E-INPUT`; other prefixes are re-derived from the type
/// name so a forged prefix cannot change the class. Unknown type names are
/// `Unclassified` (no retry).
pub fn bridge_from_py_type(op: &'static str, type_name: &str, message: &str) -> BridgeError {
    // Genuine NativeError hierarchy: preserve an E-INPUT prefix verbatim.
    if type_name.starts_with("Native") && message.starts_with("E-INPUT:") {
        return BridgeError::InvalidInput {
            op,
            detail: message.to_owned(),
        };
    }
    match type_name {
        "ValueError" | "TypeError" | "AttributeError" | "NativeInputError" => {
            BridgeError::InvalidInput {
                op,
                detail: message.to_owned(),
            }
        }
        "NativeContractError" => BridgeError::Unclassified {
            op,
            detail: message.to_owned(),
        },
        "TimeoutError" => BridgeError::InvalidInput {
            op,
            detail: format!("timeout: {message}"),
        },
        "MemoryError" => BridgeError::Capacity {
            op,
            detail: message.to_owned(),
        },
        "NativeInternalError" | "PanicException" => BridgeError::Internal { op },
        _ => BridgeError::Unclassified {
            op,
            detail: format!("unclassified exception {type_name}: {message}"),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::{bridge_from_py_type, BridgeError};

    #[test]
    fn wire_format_carries_code_and_operation() {
        let error = BridgeError::InvalidInput {
            op: "graph_validate",
            detail: "duplicate node".to_owned(),
        };
        assert_eq!(
            error.wire(),
            "E-INPUT: graph_validate failed: duplicate node"
        );
        assert_eq!(error.code().as_str(), "E-INPUT");
    }

    #[test]
    fn cycle_is_contract_and_unclassified_is_never_retryable() {
        assert_eq!(
            BridgeError::Cycle {
                op: "graph_schedule"
            }
            .code()
            .as_str(),
            "E-CONTRACT"
        );
        let unclassified = BridgeError::Unclassified {
            op: "error_from_py",
            detail: "x".to_owned(),
        };
        assert!(!unclassified.retryable());
        assert!(!BridgeError::Cycle { op: "x" }.retryable());
    }

    #[test]
    fn py_type_names_map_to_typed_errors() {
        assert!(matches!(
            bridge_from_py_type("error_from_py", "ValueError", "bad"),
            BridgeError::InvalidInput { .. }
        ));
        assert!(matches!(
            bridge_from_py_type("error_from_py", "SomethingNew", "bad"),
            BridgeError::Unclassified { .. }
        ));
        assert!(matches!(
            bridge_from_py_type("error_from_py", "MemoryError", "bad"),
            BridgeError::Capacity { .. }
        ));
    }

    #[test]
    fn guard_converts_panic_without_echoing_payload() {
        let result = super::guard("graph_validate", || -> Result<(), BridgeError> {
            panic!("secret payload");
        });
        assert_eq!(
            result,
            Err(BridgeError::Internal {
                op: "graph_validate"
            })
        );
        assert!(!result.unwrap_err().wire().contains("secret"));
    }
}
