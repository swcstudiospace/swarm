//! The `swarm._native` extension module (P6 unit U1).
//!
//! Binds three pieces of the pure Rust engine to Python: the seven shared error codes
//! (`gsd-codes`), the default retry policy (`gsd-failure`) and the read-only `orch_status` wire
//! (`gsd-route`). The logic lives in [`pure`], which has no PyO3 types; the `#[pyfunction]`
//! wrappers at the bottom only convert values and raise exceptions.

use std::time::Duration;

use gsd_codes::ErrorCode;
use gsd_failure::RetryPolicy;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

/// Pure logic with no PyO3 types, so `cargo test` can exercise it without an interpreter.
pub mod pure {
    use super::{Duration, ErrorCode, RetryPolicy};
    use gsd_route::SeamError;

    /// The seven wire codes in `gsd-codes` order.
    pub const CODES: [ErrorCode; 7] = [
        ErrorCode::Input,
        ErrorCode::Timeout,
        ErrorCode::Dep,
        ErrorCode::Capacity,
        ErrorCode::Contract,
        ErrorCode::Policy,
        ErrorCode::Internal,
    ];

    /// Returned when a string is not one of the seven wire codes.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub struct UnknownCode;

    /// The seven wire code strings, in `gsd-codes` order.
    pub fn wire_codes() -> Vec<&'static str> {
        CODES.iter().map(ErrorCode::as_str).collect()
    }

    /// The code whose wire string equals `text`; the match is exact and case-sensitive.
    pub fn code_from_wire(text: &str) -> Result<ErrorCode, UnknownCode> {
        CODES
            .iter()
            .copied()
            .find(|code| code.as_str() == text)
            .ok_or(UnknownCode)
    }

    /// The retry policy in the shape Python sees it: durations as whole milliseconds.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct RetryView {
        pub max_attempts: u32,
        pub base_delay_ms: u64,
        pub max_delay_ms: u64,
        pub retryable_error_codes: Vec<&'static str>,
    }

    /// Converts a policy to its Python view.
    pub fn retry_view(policy: &RetryPolicy) -> RetryView {
        RetryView {
            max_attempts: policy.max_attempts,
            base_delay_ms: millis(policy.base_delay),
            max_delay_ms: millis(policy.max_delay),
            retryable_error_codes: policy
                .retryable_error_codes
                .iter()
                .map(ErrorCode::as_str)
                .collect(),
        }
    }

    /// Whole milliseconds, truncated toward zero; values beyond `u64` saturate.
    pub fn millis(duration: Duration) -> u64 {
        u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
    }

    /// The message for a failed wire call: the wire code, the operation, and the seam text with
    /// its code prefix removed. The seam text names the rule at fault and never echoes input.
    pub fn wire_error_message(operation: &str, error: &SeamError) -> String {
        let code = error.code().as_str();
        let message = error.message();
        let detail = message
            .strip_prefix(code)
            .and_then(|rest| rest.strip_prefix(": "))
            .unwrap_or(message);
        format!("{code}: {operation} failed: {detail}")
    }
}

/// Canonical wire string for one of the seven codes; any other input raises `ValueError`.
#[pyfunction]
fn code_from_wire(code: &str) -> PyResult<&'static str> {
    pure::code_from_wire(code)
        .map(|found| found.as_str())
        .map_err(|_| PyValueError::new_err("unknown error code"))
}

/// The seven wire code strings, in `gsd-codes` order.
#[pyfunction]
fn wire_codes() -> Vec<&'static str> {
    pure::wire_codes()
}

/// The default retry policy as a dict with `max_attempts`, `base_delay_ms`, `max_delay_ms` and
/// `retryable_error_codes`.
#[pyfunction]
fn retry_policy(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let view = pure::retry_view(&RetryPolicy::default());
    let dict = PyDict::new(py);
    dict.set_item("max_attempts", view.max_attempts)?;
    dict.set_item("base_delay_ms", view.base_delay_ms)?;
    dict.set_item("max_delay_ms", view.max_delay_ms)?;
    dict.set_item("retryable_error_codes", view.retryable_error_codes)?;
    Ok(dict)
}

/// Runs the `orch_status` route on UTF-8 JSON request bytes and returns the reply bytes.
///
/// The GIL is released while Rust runs. A failure raises `ValueError` whose message carries the
/// wire code.
#[pyfunction]
fn orch_status_invoke<'py>(py: Python<'py>, request: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
    match py.detach(|| gsd_route::orch_status_invoke(request)) {
        Ok(reply) => Ok(PyBytes::new(py, &reply)),
        Err(error) => Err(PyValueError::new_err(pure::wire_error_message(
            "orch_status_invoke",
            &error,
        ))),
    }
}

/// Decodes the reply wire object into a dict with `exit_code` (int) and `stdout` (str).
///
/// A malformed reply raises `ValueError` whose message carries the wire code.
#[pyfunction]
fn decode_reply_wire<'py>(py: Python<'py>, reply: &[u8]) -> PyResult<Bound<'py, PyDict>> {
    let decoded = gsd_route::decode_reply_wire(reply).map_err(|error| {
        PyValueError::new_err(pure::wire_error_message("decode_reply_wire", &error))
    })?;
    let dict = PyDict::new(py);
    dict.set_item("exit_code", decoded.exit_code)?;
    dict.set_item("stdout", decoded.stdout)?;
    Ok(dict)
}

/// The `swarm._native` module.
#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(code_from_wire, m)?)?;
    m.add_function(wrap_pyfunction!(wire_codes, m)?)?;
    m.add_function(wrap_pyfunction!(retry_policy, m)?)?;
    m.add_function(wrap_pyfunction!(orch_status_invoke, m)?)?;
    m.add_function(wrap_pyfunction!(decode_reply_wire, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::pure;
    use super::{ErrorCode, RetryPolicy};
    use std::time::Duration;

    /// The `orch_status` list request used by the sandbox parity test: one DONE task, one
    /// IN_REVIEW task, one verdict.
    const LIST_REQUEST: &str = r#"{"mode":"list","agent":"A01","script":"orch_status","correlation":null,"db_path":"/x/tasks.db","swarm_dir":"/x","events":3,"tasks":[{"task_id":"T-A","state":"DONE","agent_id":"A05","dag_depth":0,"attempt":0,"rework_loops":0,"risk_class":"low","depends_on":"[]","notes":"{}"},{"task_id":"T-B","state":"IN_REVIEW","agent_id":"A05","dag_depth":1,"attempt":0,"rework_loops":0,"risk_class":"low","depends_on":"[\"T-A\"]","notes":"{}"}],"verdicts":[{"task_id":"T-B","rows":[{"id":1,"task_id":"T-B","gate":"review","verdict":"pass","findings":"[]","ts":1.5,"envelope_json":null}]}],"history":null}"#;

    #[test]
    fn every_wire_code_round_trips_through_code_from_wire() {
        let wires = pure::wire_codes();
        assert_eq!(wires.len(), 7);
        for wire in &wires {
            let code = pure::code_from_wire(wire).expect("a listed wire code is accepted");
            assert_eq!(code.as_str(), *wire);
        }
        let mut unique = wires.clone();
        unique.sort_unstable();
        unique.dedup();
        assert_eq!(unique.len(), 7, "wire codes are distinct");
    }

    #[test]
    fn unknown_or_inexact_codes_are_rejected() {
        for text in ["E-BOGUS", "e-input", "E-INPUT ", "", "INPUT"] {
            assert_eq!(
                pure::code_from_wire(text),
                Err(pure::UnknownCode),
                "{text:?}"
            );
        }
    }

    #[test]
    fn retry_view_matches_the_gsd_failure_default() {
        let default = RetryPolicy::default();
        let view = pure::retry_view(&default);
        assert_eq!(view.max_attempts, default.max_attempts);
        assert_eq!(view.base_delay_ms, pure::millis(default.base_delay));
        assert_eq!(view.max_delay_ms, pure::millis(default.max_delay));
        let expected: Vec<&str> = default
            .retryable_error_codes
            .iter()
            .map(ErrorCode::as_str)
            .collect();
        assert_eq!(view.retryable_error_codes, expected);
        assert!(!view.retryable_error_codes.is_empty());
    }

    #[test]
    fn durations_convert_to_whole_milliseconds() {
        assert_eq!(pure::millis(Duration::from_millis(1500)), 1500);
        assert_eq!(pure::millis(Duration::from_micros(1999)), 1);
        assert_eq!(pure::millis(Duration::from_secs(5)), 5000);
        assert_eq!(pure::millis(Duration::MAX), u64::MAX);
    }

    #[test]
    fn list_request_round_trips_through_the_wire() {
        let reply_bytes = gsd_route::orch_status_invoke(LIST_REQUEST.as_bytes())
            .expect("the list request is answered");
        let reply = gsd_route::decode_reply_wire(&reply_bytes).expect("the reply decodes");
        assert_eq!(reply.exit_code, 0);
        assert!(reply.stdout.contains("\"T-A\""), "{}", reply.stdout);
        assert!(reply.stdout.contains("\"T-B\""), "{}", reply.stdout);
    }

    #[test]
    fn invoke_failure_message_carries_the_code_and_not_the_input() {
        let error = gsd_route::orch_status_invoke(b"{ not json secret-token")
            .expect_err("malformed JSON is rejected");
        let message = pure::wire_error_message("orch_status_invoke", &error);
        assert_eq!(message, "E-INPUT: orch_status_invoke failed: invalid JSON");
        assert!(!message.contains("secret-token"));
    }

    #[test]
    fn malformed_reply_is_a_contract_error() {
        let error = gsd_route::decode_reply_wire(b"{\"exit_code\": 0}")
            .expect_err("a reply without stdout is rejected");
        assert_eq!(error.code(), ErrorCode::Contract);
        assert_eq!(
            pure::wire_error_message("decode_reply_wire", &error),
            "E-CONTRACT: decode_reply_wire failed: reply must hold exactly exit_code and stdout"
        );
    }

    /// Exhaustive on purpose: a new `ErrorCode` variant breaks the build here until listed.
    fn slot_of(code: ErrorCode) -> usize {
        match code {
            ErrorCode::Input => 0,
            ErrorCode::Timeout => 1,
            ErrorCode::Dep => 2,
            ErrorCode::Capacity => 3,
            ErrorCode::Contract => 4,
            ErrorCode::Policy => 5,
            ErrorCode::Internal => 6,
        }
    }

    /// The wire string of each variant, written out independently of `ErrorCode::as_str`.
    fn wire_of(code: ErrorCode) -> &'static str {
        match code {
            ErrorCode::Input => "E-INPUT",
            ErrorCode::Timeout => "E-TIMEOUT",
            ErrorCode::Dep => "E-DEP",
            ErrorCode::Capacity => "E-CAPACITY",
            ErrorCode::Contract => "E-CONTRACT",
            ErrorCode::Policy => "E-POLICY",
            ErrorCode::Internal => "E-INTERNAL",
        }
    }

    #[test]
    fn code_table_lists_every_variant_once_in_wire_order() {
        for (index, code) in pure::CODES.iter().enumerate() {
            assert_eq!(slot_of(*code), index, "CODES[{index}] is out of place");
            assert_eq!(code.as_str(), wire_of(*code));
        }
    }
}
