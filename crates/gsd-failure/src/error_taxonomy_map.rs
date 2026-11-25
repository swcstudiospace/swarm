//! Rust-side error kinds produced by the scheduler and pool, mapped onto the seven shared `ErrorCode`s.
//!
//! This module deliberately depends on no scheduler or pool crate: those crates depend on this one,
//! so the kinds are defined here and the callers convert their own errors into `ErrorKind`.

use std::time::Duration;

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
        classify(self)
    }

    /// Returns the Python-side wire code string, e.g. `"E-CAPACITY"`.
    pub fn as_python_code(self) -> &'static str {
        self.error_code().as_str()
    }
}

/// Maps a Rust-side failure kind onto its shared taxonomy code.
///
/// This is the Rust counterpart of Python's `swarm.errors.classify()`: the Python side
/// dispatches on exception classes, while here the caller has already chosen the kind.
pub fn classify(kind: ErrorKind) -> ErrorCode {
    match kind {
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

/// Retry backoff data for one taxonomy code.
///
/// Delays double from `base` up to `cap`; each attempt adds up to `jitter` on top.
/// At most `max_attempts` attempts are made. Behavior lives in the callers; this is data only.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Backoff {
    /// Delay before the first retry.
    pub base: Duration,
    /// Upper bound on the exponential delay.
    pub cap: Duration,
    /// Maximum random jitter added to each delay.
    pub jitter: Duration,
    /// Maximum number of attempts, including the first.
    pub max_attempts: u32,
}

/// Backoff for `E-DEP`: 1 s doubling to 60 s with jitter, max 5 attempts
/// (`plan/rust-wasmer-omp-options.md` §2; `swarm/errors.py` `_exp_backoff_err` jitter of 0.3 s).
pub const DEPENDENCY_BACKOFF: Backoff = Backoff {
    base: Duration::from_secs(1),
    cap: Duration::from_secs(60),
    jitter: Duration::from_millis(300),
    max_attempts: 5,
};

/// Returns the backoff data for a taxonomy code, or `None` when the code has no exponential retry.
pub fn backoff_for(code: ErrorCode) -> Option<Backoff> {
    match code {
        ErrorCode::Dep => Some(DEPENDENCY_BACKOFF),
        _ => None,
    }
}

/// Parses a Python-side wire code string back into its shared `ErrorCode`.
///
/// Returns `None` for any string that is not one of the seven codes.
pub fn error_code_from_python(code: &str) -> Option<ErrorCode> {
    // The wire strings come from `ErrorCode::as_str` in gsd-codes, the one table; this list names the variants only.
    const SHARED_CODES: [ErrorCode; 7] = [
        ErrorCode::Input,
        ErrorCode::Timeout,
        ErrorCode::Dep,
        ErrorCode::Capacity,
        ErrorCode::Contract,
        ErrorCode::Policy,
        ErrorCode::Internal,
    ];
    SHARED_CODES
        .into_iter()
        .find(|known| known.as_str() == code)
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
    fn classify_agrees_with_error_code_for_all_kinds() {
        for kind in ErrorKind::ALL {
            assert_eq!(classify(kind), kind.error_code(), "{kind:?}");
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

    #[test]
    fn dependency_backoff_matches_plan_values() {
        assert_eq!(
            backoff_for(ErrorCode::Dep),
            Some(Backoff {
                base: Duration::from_secs(1),
                cap: Duration::from_secs(60),
                jitter: Duration::from_millis(300),
                max_attempts: 5,
            })
        );
    }

    #[test]
    fn backoff_is_absent_for_codes_without_exponential_retry() {
        for code in [
            ErrorCode::Input,
            ErrorCode::Timeout,
            ErrorCode::Capacity,
            ErrorCode::Contract,
            ErrorCode::Policy,
            ErrorCode::Internal,
        ] {
            assert_eq!(backoff_for(code), None, "{code}");
        }
    }
}
