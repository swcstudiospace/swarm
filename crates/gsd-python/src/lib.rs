//! The `swarm._native` extension module (P6 unit U1).
//!
//! Binds five pieces of the pure Rust engine to Python: the seven shared error codes
//! (`gsd-codes`), the default retry policy (`gsd-failure`), the read-only `orch_status` wire
//! (`gsd-route`), the opt-in secret-span scanner (`gsd-redact`, selected by
//! `SWARM_REDACT_ENGINE=rust`) and the scheduler primitives of Phase 14.4 (`gsd-sched-core`:
//! dispatch order, claim patterns and table, the AIMD limiter, retry backoff, the cancel-drain plan).
//! The logic lives in [`pure`] and [`sched`], which have no PyO3 types; the `#[pyfunction]` and
//! `#[pyclass]` wrappers at the bottom only convert values and raise exceptions.

pub mod sched;

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
    use std::panic::{catch_unwind, AssertUnwindSafe};

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

    /// Runs `work`, turning a panic into the `E-INTERNAL` wire message for `operation`.
    ///
    /// The message never carries the panic payload: a panic text can quote the input.
    pub fn guard_panic<T>(operation: &str, work: impl FnOnce() -> T) -> Result<T, String> {
        catch_unwind(AssertUnwindSafe(work)).map_err(|_| {
            format!(
                "{}: {operation} failed: internal panic",
                ErrorCode::Internal.as_str()
            )
        })
    }

    /// `gsd_redact::redact_spans`, with a panic reported as an `E-INTERNAL` message instead of
    /// unwinding into the interpreter, and a text beyond the scanner's length limit reported as
    /// `E-INPUT` (the caller then falls back to the Python scanner; the text is never skipped).
    pub fn redact_spans(text: &str) -> Result<Vec<(usize, usize)>, String> {
        guard_panic("redact_spans", || gsd_redact::redact_spans(text))?.map_err(|error| {
            format!(
                "{}: redact_spans failed: {error}",
                ErrorCode::Input.as_str()
            )
        })
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

/// Secret spans of `text` as `(start, end)` code-point offsets, ascending and disjoint.
///
/// The GIL is released while Rust runs. A scanner panic raises `ValueError` whose message carries
/// `E-INTERNAL` (not PyO3's `PanicException`, which `except Exception` would miss). A string
/// with a lone surrogate cannot be encoded as UTF-8 and raises `UnicodeEncodeError` on the way in.
#[pyfunction]
fn redact_spans(py: Python<'_>, text: &str) -> PyResult<Vec<(usize, usize)>> {
    py.detach(|| pure::redact_spans(text))
        .map_err(PyValueError::new_err)
}

/// The dispatch order of ready rows `(priority, dag_depth, created_at)` as a stable permutation of their indices: priority rank
/// (`P0` first; a missing or unknown priority is `P2`), then `dag_depth`, then `created_at`, both ascending (`None` is 0).
///
/// A priority with a lone surrogate cannot be read as UTF-8 and raises `UnicodeEncodeError` on the way in.
#[pyfunction]
fn sched_order_ready(rows: Vec<sched::OrderRow>) -> PyResult<Vec<usize>> {
    sched::order_ready(&rows).map_err(PyValueError::new_err)
}

/// The canonical form of a claim pattern. `ValueError` (`E-INPUT`) for an empty, absolute, drive-qualified, `~`-prefixed or
/// `..`-containing pattern.
#[pyfunction]
fn sched_normalize_claim(pattern: &str) -> PyResult<String> {
    sched::normalize_claim(pattern).map_err(PyValueError::new_err)
}

/// Whether two claim patterns can match a common path, whatever their modes. `ValueError` (`E-INPUT`) for an invalid pattern.
#[pyfunction]
fn sched_claims_overlap(a: &str, b: &str) -> PyResult<bool> {
    sched::claims_overlap(a, b).map_err(PyValueError::new_err)
}

/// The retry delay in seconds for retry number `attempt`; `unit` is the caller's jitter sample in `[-1, 1]`. `ValueError`
/// (`E-INPUT`) for a jitter outside `[0, 1]` or a non-positive base or cap.
#[pyfunction]
fn sched_backoff_s(attempt: i64, base_s: f64, cap_s: f64, jitter: f64, unit: f64) -> PyResult<f64> {
    sched::backoff_s(attempt, base_s, cap_s, jitter, unit).map_err(PyValueError::new_err)
}

/// The cancel-drain schedule `(grace_s, kill_grace_s)`. `ValueError` (`E-INPUT`) for a NaN argument.
#[pyfunction]
fn sched_drain_plan(
    cancel_grace_s: f64,
    shutdown_budget_s: f64,
    kill_grace_max_s: f64,
    stop_grace_s: f64,
    kill_settle_s: f64,
) -> PyResult<(f64, f64)> {
    sched::drain_plan(
        cancel_grace_s,
        shutdown_budget_s,
        kill_grace_max_s,
        stop_grace_s,
        kill_settle_s,
    )
    .map_err(PyValueError::new_err)
}

/// The claims of every in-flight owner; `acquire` is all-or-nothing and an owner never conflicts with itself.
///
/// Thread-safe: calls serialise on an internal mutex. A claim is `(pattern, mode)` with mode `"exclusive"` or `"shared"`; an invalid
/// pattern or mode raises `ValueError` (`E-INPUT`) before the table changes.
#[pyclass(name = "ClaimTable", frozen, module = "swarm._native")]
struct PyClaimTable {
    inner: sched::SharedClaims,
}

#[pymethods]
impl PyClaimTable {
    #[new]
    fn new() -> Self {
        Self {
            inner: sched::SharedClaims::new(),
        }
    }

    /// Takes every claim for `owner`, or none. Returns `(True, [])`, or `(False, conflicts)` with one
    /// `(holder, held_pattern, requested_pattern)` per collision in the order Python reports them.
    ///
    /// The GIL is released while the glob comparisons run.
    fn acquire(
        &self,
        py: Python<'_>,
        owner: &str,
        claims: Vec<(String, String)>,
    ) -> PyResult<sched::Acquired> {
        py.detach(|| self.inner.acquire(owner, &claims))
            .map_err(PyValueError::new_err)
    }

    /// Drops everything `owner` holds and returns how many claims that was; idempotent.
    fn release(&self, owner: &str) -> PyResult<usize> {
        self.inner.release(owner).map_err(PyValueError::new_err)
    }

    /// A snapshot `{owner: [(pattern, mode), ...]}`, owners in the order they first acquired, claims in acquisition order.
    fn held<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        for (owner, claims) in self.inner.held().map_err(PyValueError::new_err)? {
            dict.set_item(owner, claims)?;
        }
        Ok(dict)
    }
}

/// The AIMD limiter of the scheduler with its once-per-burst gate and failure-streak circuit, driven by the caller's clock.
///
/// `now` and `submitted_at` are monotonic seconds, finite and in `[0, 1e9]`. `ValueError` (`E-INPUT`) unless
/// `1 <= min_n <= max_n`, `cooldown_s` is finite and `>= 0` and `retry_cap_s` finite and `> 0`; a `streak_threshold` of 0 or less
/// turns the circuit off. Thread-safe: calls serialise on an internal mutex.
#[pyclass(name = "AimdLimiter", frozen, module = "swarm._native")]
struct PyAimdLimiter {
    inner: sched::Limiter,
}

#[pymethods]
impl PyAimdLimiter {
    #[new]
    #[pyo3(signature = (max_n, min_n, cooldown_s, retry_cap_s, streak_threshold))]
    fn new(
        max_n: i64,
        min_n: i64,
        cooldown_s: f64,
        retry_cap_s: f64,
        streak_threshold: i64,
    ) -> PyResult<Self> {
        let inner = sched::Limiter::new(max_n, min_n, cooldown_s, retry_cap_s, streak_threshold)
            .map_err(PyValueError::new_err)?;
        Ok(Self { inner })
    }

    /// The cap on concurrent runs right now.
    fn current(&self) -> PyResult<usize> {
        self.inner.current().map_err(PyValueError::new_err)
    }

    /// A success at `now`; True when the limit grew (it never does during a cooldown).
    fn on_success(&self, now: f64) -> PyResult<bool> {
        self.inner.on_success(now).map_err(PyValueError::new_err)
    }

    /// A bare rate-limit signal: halves on every call, with no burst dedupe. True when the limit dropped.
    #[pyo3(signature = (now, retry_after_s=None))]
    fn on_rate_limit(&self, now: f64, retry_after_s: Option<f64>) -> PyResult<bool> {
        self.inner
            .on_rate_limit(now, retry_after_s)
            .map_err(PyValueError::new_err)
    }

    /// A failure of `task_id` that is not a rate limit; True when it was the failure that tripped the streak circuit and halved
    /// the limit.
    fn on_failure(&self, now: f64, task_id: &str) -> PyResult<bool> {
        self.inner
            .on_failure(now, task_id)
            .map_err(PyValueError::new_err)
    }

    /// A rate-limit hit from a session submitted at `submitted_at`, through the once-per-burst gate. True when the limit dropped;
    /// False for a hit of the same burst (`submitted_at` before the last decrease) and for a hit at the floor.
    #[pyo3(signature = (submitted_at, now, retry_after_s=None))]
    fn on_burst_hit(
        &self,
        submitted_at: f64,
        now: f64,
        retry_after_s: Option<f64>,
    ) -> PyResult<bool> {
        self.inner
            .on_burst_hit(submitted_at, now, retry_after_s)
            .map_err(PyValueError::new_err)
    }
}

/// The `swarm._native` module.
#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(code_from_wire, m)?)?;
    m.add_function(wrap_pyfunction!(wire_codes, m)?)?;
    m.add_function(wrap_pyfunction!(retry_policy, m)?)?;
    m.add_function(wrap_pyfunction!(orch_status_invoke, m)?)?;
    m.add_function(wrap_pyfunction!(decode_reply_wire, m)?)?;
    m.add_function(wrap_pyfunction!(redact_spans, m)?)?;
    m.add_function(wrap_pyfunction!(sched_order_ready, m)?)?;
    m.add_function(wrap_pyfunction!(sched_normalize_claim, m)?)?;
    m.add_function(wrap_pyfunction!(sched_claims_overlap, m)?)?;
    m.add_function(wrap_pyfunction!(sched_backoff_s, m)?)?;
    m.add_function(wrap_pyfunction!(sched_drain_plan, m)?)?;
    m.add_class::<PyClaimTable>()?;
    m.add_class::<PyAimdLimiter>()?;
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
    fn redact_spans_returns_the_oracle_spans_in_code_points() {
        for (text, expected) in [
            ("", vec![]),
            ("short", vec![]),
            ("password=hunter2hunter2 ok", vec![(9, 23)]),
            ("caf\u{e9} token=abcdef \u{1f600}", vec![(11, 17)]),
        ] {
            assert_eq!(pure::redact_spans(text), Ok(expected), "{text:?}");
        }
    }

    #[test]
    fn a_panic_becomes_the_coded_internal_error_without_the_payload() {
        let secret = "hunter2-do-not-echo";
        let result: Result<u8, String> = pure::guard_panic("redact_spans", || {
            panic!("index out of range near {secret}")
        });
        assert_eq!(
            result,
            Err("E-INTERNAL: redact_spans failed: internal panic".to_owned())
        );
    }

    #[test]
    fn guard_panic_passes_a_normal_result_through() {
        assert_eq!(pure::guard_panic("redact_spans", || 7), Ok(7));
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
