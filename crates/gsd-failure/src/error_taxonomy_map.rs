//! Rust-side error kinds produced by the scheduler and pool, mapped onto the seven shared `ErrorCode`s.
//!
//! This module deliberately depends on no scheduler or pool crate: those crates depend on this one,
//! so the kinds are defined here and the callers convert their own errors into `ErrorKind`.

use crate::taxonomy::ErrorCode;

/// Failure kinds emitted by the host-native scheduler and pool.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ErrorKind {
    /// A pool or budget resource was exhausted.
    Exhausted,
    /// The task graph contains a cycle.
    CycleDetected,
    /// A budget, lease, or SLA deadline passed.
    Timeout,
    /// An upstream dependency is unavailable or a dependency edge failed.
    Dependency,
    /// Output was rejected by a consumer or schema.
    Contract,
    /// Blocked by a security or compliance policy.
    Policy,
    /// Input artifact is invalid or missing.
    Input,
    /// Unexpected internal fault.
    Internal,
    /// Concurrency or WIP cap was hit.
    Capacity,
}

impl ErrorKind {
    /// Every kind, in declaration order.
    pub const ALL: [ErrorKind; 9] = [
        ErrorKind::Exhausted,
        ErrorKind::CycleDetected,
        ErrorKind::Timeout,
        ErrorKind::Dependency,
        ErrorKind::Contract,
        ErrorKind::Policy,
        ErrorKind::Input,
        ErrorKind::Internal,
        ErrorKind::Capacity,
    ];

    /// Returns the shared taxonomy code this kind maps to.
    pub fn error_code(self) -> ErrorCode {
        match self {
            ErrorKind::Exhausted => ErrorCode::Capacity,
            ErrorKind::CycleDetected => ErrorCode::Dep,
            ErrorKind::Timeout => ErrorCode::Timeout,
            ErrorKind::Dependency => ErrorCode::Dep,
            ErrorKind::Contract => ErrorCode::Contract,
            ErrorKind::Policy => ErrorCode::Policy,
            ErrorKind::Input => ErrorCode::Input,
            ErrorKind::Internal => ErrorCode::Internal,
            ErrorKind::Capacity => ErrorCode::Capacity,
        }
    }

    /// Returns the Python-side wire code string, e.g. `"E-CAPACITY"`.
    pub fn as_python_code(self) -> &'static str {
        self.error_code().as_str()
    }
}

/// Parses a Python-side wire code string back into its shared `ErrorCode`.
///
/// Returns `None` for any string that is not one of the seven codes.
pub fn error_code_from_python(code: &str) -> Option<ErrorCode> {
    match code {
        "E-INPUT" => Some(ErrorCode::Input),
        "E-TIMEOUT" => Some(ErrorCode::Timeout),
        "E-DEP" => Some(ErrorCode::Dep),
        "E-CAPACITY" => Some(ErrorCode::Capacity),
        "E-CONTRACT" => Some(ErrorCode::Contract),
        "E-POLICY" => Some(ErrorCode::Policy),
        "E-INTERNAL" => Some(ErrorCode::Internal),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_kind_maps_to_expected_code() {
        let expected = [
            (ErrorKind::Exhausted, ErrorCode::Capacity, "E-CAPACITY"),
            (ErrorKind::CycleDetected, ErrorCode::Dep, "E-DEP"),
            (ErrorKind::Timeout, ErrorCode::Timeout, "E-TIMEOUT"),
            (ErrorKind::Dependency, ErrorCode::Dep, "E-DEP"),
            (ErrorKind::Contract, ErrorCode::Contract, "E-CONTRACT"),
            (ErrorKind::Policy, ErrorCode::Policy, "E-POLICY"),
            (ErrorKind::Input, ErrorCode::Input, "E-INPUT"),
            (ErrorKind::Internal, ErrorCode::Internal, "E-INTERNAL"),
            (ErrorKind::Capacity, ErrorCode::Capacity, "E-CAPACITY"),
        ];
        assert_eq!(expected.len(), ErrorKind::ALL.len());
        for (kind, code, wire) in expected {
            assert_eq!(kind.error_code(), code, "{kind:?}");
            assert_eq!(kind.as_python_code(), wire, "{kind:?}");
        }
    }

    #[test]
    fn python_code_round_trips_for_all_kinds() {
        for kind in ErrorKind::ALL {
            let wire = kind.as_python_code();
            assert_eq!(
                error_code_from_python(wire),
                Some(kind.error_code()),
                "{kind:?} -> {wire}"
            );
        }
    }

    #[test]
    fn all_seven_shared_codes_are_reached() {
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
            assert!(
                ErrorKind::ALL.iter().any(|k| k.error_code() == code),
                "no kind maps to {code}"
            );
            assert_eq!(error_code_from_python(code.as_str()), Some(code));
        }
    }

    #[test]
    fn unknown_wire_code_is_rejected() {
        assert_eq!(error_code_from_python("E-UNKNOWN"), None);
        assert_eq!(error_code_from_python(""), None);
    }
}
