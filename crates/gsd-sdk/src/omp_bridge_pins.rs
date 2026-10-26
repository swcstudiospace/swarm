//! Bridge pins: the omp-side error model, retry table, script-name gate, argv builder, stdout
//! parser and exit-to-verdict mapping of `omp/src/bridge.ts`.
//!
//! `omp/src` is the authority and is never edited from this crate. The frozen records are in
//! `tests/omp_contract/freeze.md` (sections 2 and 3) and `tests/omp_contract/vectors.json`.
//! Spawning, group kill and the stop/abort race live in `omp_bridge_runner`; this module is pure.
//!
//! Decisions where this crate differs from, or extends, the omp source:
//!
//! - No response size cap. omp has none, so none is added. stdout and stderr are used whole,
//!   and only the 400-character tails appear in error text.
//! - Reply depth: a successful reply (exit 0, or exit 1 with `status: "fail"`) whose JSON nests
//!   deeper than [`MAX_JSON_DEPTH`] (32) is rejected with `E-CONTRACT`. omp has no depth check.
//!   Error replies (exit 2 with a wire code) are not depth-checked, because they carry no data.
//! - serde_json stops parsing at its own 128-level recursion limit. Stdout nested deeper than
//!   that is reported as "not a JSON object" (`E-INTERNAL`) before the depth check runs.
//! - The 100 000-byte cap is tool-input only. It lives in `omp_tool_argv`, not here.
//! - Retry backoff is exponential from `RetryPolicy::base_delay`, capped at
//!   `RetryPolicy::max_delay`, with no jitter. The policy declares no jitter.
//! - Exit mapping order follows `bridge.ts:171-186`. `SessionShutdown` is the same as
//!   `StopReason::Abort` at the step where `stopped === "abort"` is checked.

use std::fmt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use gsd_codes::ErrorCode;
use gsd_failure::{error_code_from_python, RetryPolicy};
use serde_json::{Map, Value};

use crate::omp_constants_mirror::{is_valid_script_name, TAIL_CHARS};

/// Vector ids of `tests/omp_contract/vectors.json` that this module replays.
pub const VECTOR_IDS: &[&str] = &[
    "X-ABORT-TIMEOUT-REASON-01",
    "X-ABORT-TIMEOUT-REASON-02",
    "X-ABORT-OTHER",
    "X-TIMEOUT-STOP-01",
    "X-TIMEOUT-STOP-02",
    "X-CANCEL-STOP-01",
    "X-CANCEL-STOP-02",
    "X-EXIT-2-EMPTY-STDOUT-01",
    "X-EXIT-2-EMPTY-STDOUT-02",
    "X-EXIT-NON-JSON-01",
    "X-EXIT-NON-JSON-02",
    "X-EXIT-NON-JSON-03",
    "X-EXIT-0-01",
    "X-EXIT-0-02",
    "X-EXIT-1-FAIL-01",
    "X-EXIT-1-FAIL-02",
    "X-EXIT-2-ERROR-CODE-01",
    "X-EXIT-2-ERROR-CODE-02",
    "X-EXIT-2-ERROR-CODE-03",
    "X-EXIT-2-ERROR-CODE-04",
    "X-EXIT-OTHER",
];

/// Maximum nesting depth of a successful reply, counting the reply object itself as level 1.
pub const MAX_JSON_DEPTH: usize = 32;

/// Why the owning signal of a call fired (`AbortSignal` in omp).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AbortKind {
    /// The call's deadline passed (omp: a `TimeoutError` reason). Maps to `E-TIMEOUT`.
    Deadline,
    /// The caller cancelled the call. Maps to `E-INTERNAL`, with a partial-state warning.
    Cancelled,
    /// The omp session is shutting down and its children are group-killed. Same as
    /// [`StopReason::Abort`].
    SessionShutdown,
}

/// Why the bridge itself stopped a child (`StopReason` in omp).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopReason {
    /// The per-call deadline (`timeout_ms`) fired and the child was killed.
    Timeout,
    /// The child was stopped by an abort or a session shutdown.
    Abort,
}

/// A bridge failure: a wire code, a message, and optional structured details.
#[derive(Debug, Clone, PartialEq)]
pub struct BridgeError {
    /// The wire code. It is kept even for `E-POLICY`, whose message omits the token.
    pub code: ErrorCode,
    /// The human-readable message, already carrying the `CODE: ` prefix except for `E-POLICY`.
    pub message: String,
    /// The whole reply object when the script returned one, else `None`.
    pub details: Option<Value>,
}

impl BridgeError {
    /// Builds an error with the `SwarmToolError` message rule.
    ///
    /// A detail that already starts with `CODE:` is not prefixed again. For `E-POLICY` the
    /// leading `E-POLICY:` and every `E-POLICY` token are removed, the rest is trimmed, and an
    /// empty remainder becomes `refused`. The code is kept in [`BridgeError::code`].
    pub fn new(code: ErrorCode, detail: impl Into<String>) -> Self {
        let detail = detail.into();
        let prefix = format!("{}:", code.as_str());
        let prefixed = if detail.starts_with(&prefix) {
            detail
        } else {
            format!("{prefix} {detail}")
        };
        let message = if code == ErrorCode::Policy {
            let body = prefixed
                .strip_prefix(prefix.as_str())
                .unwrap_or(prefixed.as_str());
            let body = body.trim_start().replace("E-POLICY", "");
            let body = body.trim();
            if body.is_empty() {
                "refused".to_owned()
            } else {
                body.to_owned()
            }
        } else {
            prefixed
        };
        Self {
            code,
            message,
            details: None,
        }
    }
}

impl fmt::Display for BridgeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for BridgeError {}

/// A successful reply: exit 0, or exit 1 with `status: "fail"`.
#[derive(Debug, Clone, PartialEq)]
pub struct BridgeReply {
    /// The child's exit code, 0 or 1.
    pub exit_code: i32,
    /// The stdout object, unchanged.
    pub json: Map<String, Value>,
    /// The swarm directory the child ran against.
    pub swarm_dir: PathBuf,
}

/// Whether a failure code is retryable under the default policy.
pub fn is_retryable(code: ErrorCode) -> bool {
    RetryPolicy::default().retryable_error_codes.contains(&code)
}

/// The delay before retrying, or `None` when the call must not be retried.
///
/// `attempt` is the 1-based number of the attempt that just failed. A retry is allowed only
/// for an idempotent call, a code in the policy's retry table, and `attempt < max_attempts`
/// (`max_attempts` counts the first attempt). The delay is `base_delay` doubled per prior
/// attempt, capped at `max_delay`.
pub fn retry_delay(
    code: ErrorCode,
    idempotent: bool,
    attempt: u32,
    policy: &RetryPolicy,
) -> Option<Duration> {
    if !idempotent
        || !policy.retryable_error_codes.contains(&code)
        || attempt >= policy.max_attempts
    {
        return None;
    }
    Some(backoff(policy, attempt))
}

fn backoff(policy: &RetryPolicy, attempt: u32) -> Duration {
    let exponent = attempt.saturating_sub(1);
    let scaled = 1u32
        .checked_shl(exponent)
        .and_then(|factor| policy.base_delay.checked_mul(factor));
    scaled.unwrap_or(policy.max_delay).min(policy.max_delay)
}

/// Accepts only script stems that `omp_constants_mirror::is_valid_script_name` accepts.
///
/// Otherwise returns `E-INPUT` with the JSON-quoted name.
pub fn check_script_name(script: &str) -> Result<(), BridgeError> {
    if is_valid_script_name(script) {
        return Ok(());
    }
    let quoted = serde_json::to_string(script).unwrap_or_else(|_| format!("\"{script}\""));
    Err(BridgeError::new(
        ErrorCode::Input,
        format!("invalid script name {quoted}"),
    ))
}

/// The canonical path `<root>/scripts/<script>.py`, after [`check_script_name`].
pub fn script_path(root: &Path, script: &str) -> Result<PathBuf, BridgeError> {
    check_script_name(script)?;
    Ok(root.join("scripts").join(format!("{script}.py")))
}

/// The argv after the interpreter: the script path, the caller's args in order, then `--json`.
/// Caller args that already end in `--json` get no second one, so the child sees it once.
///
/// The interpreter (`python3`) is the runner's concern.
pub fn script_argv(script_path: &Path, args: &[String]) -> Vec<String> {
    let mut argv = Vec::with_capacity(args.len() + 2);
    argv.push(script_path.to_string_lossy().into_owned());
    argv.extend(args.iter().cloned());
    if args.last().map(String::as_str) != Some("--json") {
        argv.push("--json".to_owned());
    }
    argv
}

/// Parses stdout as one JSON object. Arrays, scalars and invalid JSON give `None`.
pub fn parse_object(stdout: &str) -> Option<Map<String, Value>> {
    match serde_json::from_str::<Value>(stdout).ok()? {
        Value::Object(map) => Some(map),
        _ => None,
    }
}

/// Nesting depth of a JSON value. Scalars are 0. Each object or array adds one level, and an
/// empty container counts as one level.
pub fn json_depth(value: &Value) -> usize {
    match value {
        Value::Object(map) => map_depth(map),
        Value::Array(items) => 1 + items.iter().map(json_depth).max().unwrap_or(0),
        _ => 0,
    }
}

fn map_depth(map: &Map<String, Value>) -> usize {
    1 + map.values().map(json_depth).max().unwrap_or(0)
}

/// Maps one finished child to a reply or a `BridgeError`, in the order of `bridge.ts:171-186`.
///
/// Order: (1) the call's signal, (2) a timeout stop, (3) an abort stop or session shutdown,
/// (4) a usage error (exit 2 with empty stdout), (5) non-object stdout, (6) exit 0, (7) exit 1
/// with `status: "fail"`, (8) exit 2 with a wire error code, (9) everything else.
///
/// `stderr` and `stdout` are cut to their last [`TAIL_CHARS`] characters in error text.
pub fn classify_exit(
    script: &str,
    timeout_ms: u64,
    signal_abort: Option<AbortKind>,
    stopped: Option<StopReason>,
    exit_code: Option<i32>,
    stdout: &str,
    stderr: &str,
    swarm_dir: &Path,
) -> Result<BridgeReply, BridgeError> {
    match signal_abort {
        Some(AbortKind::Deadline) => {
            return Err(BridgeError::new(
                ErrorCode::Timeout,
                format!("{script} deadline exceeded"),
            ));
        }
        Some(AbortKind::Cancelled) => return Err(cancelled_partial_state(script)),
        Some(AbortKind::SessionShutdown) | None => {}
    }
    if stopped == Some(StopReason::Timeout) {
        return Err(BridgeError::new(
            ErrorCode::Timeout,
            format!("{script} exceeded {timeout_ms} ms and was killed"),
        ));
    }
    let shutdown = signal_abort == Some(AbortKind::SessionShutdown);
    if shutdown || stopped == Some(StopReason::Abort) {
        return Err(BridgeError::new(
            ErrorCode::Internal,
            format!("{script} cancelled (omp session shutdown); re-check with swarm_status"),
        ));
    }
    if exit_code == Some(2) && stdout.trim().is_empty() {
        return Err(BridgeError::new(
            ErrorCode::Input,
            format!("{script} usage error: {}", tail(stderr.trim())),
        ));
    }
    let Some(json) = parse_object(stdout) else {
        let text = if stdout.trim().is_empty() {
            stderr.trim()
        } else {
            stdout.trim()
        };
        return Err(BridgeError::new(
            ErrorCode::Internal,
            format!(
                "{script} exit {}, stdout is not a JSON object: {}",
                exit_label(exit_code),
                tail(text)
            ),
        ));
    };
    match exit_code {
        Some(0) => return reply(script, 0, json, swarm_dir),
        Some(1) if json.get("status").and_then(Value::as_str) == Some("fail") => {
            return reply(script, 1, json, swarm_dir);
        }
        _ => {}
    }
    if exit_code == Some(2) {
        if let Some((code, message)) = wire_error(&json) {
            let mut err = BridgeError::new(code, message);
            err.details = Some(Value::Object(json));
            return Err(err);
        }
    }
    let text = if stderr.trim().is_empty() {
        stdout.trim()
    } else {
        stderr.trim()
    };
    let mut err = BridgeError::new(
        ErrorCode::Internal,
        format!(
            "{script} unexpected exit {}: {}",
            exit_label(exit_code),
            tail(text)
        ),
    );
    err.details = Some(Value::Object(json));
    Err(err)
}

fn cancelled_partial_state(script: &str) -> BridgeError {
    BridgeError::new(
        ErrorCode::Internal,
        format!(
            "{script} cancelled; Task Store state may be partially applied \
             — re-check with swarm_status before retrying"
        ),
    )
}

fn reply(
    script: &str,
    exit_code: i32,
    json: Map<String, Value>,
    swarm_dir: &Path,
) -> Result<BridgeReply, BridgeError> {
    if map_depth(&json) > MAX_JSON_DEPTH {
        return Err(BridgeError::new(
            ErrorCode::Contract,
            format!("{script} reply JSON nests deeper than {MAX_JSON_DEPTH} levels"),
        ));
    }
    Ok(BridgeReply {
        exit_code,
        json,
        swarm_dir: swarm_dir.to_path_buf(),
    })
}

/// The `error` object's wire code (one of the seven) and message. A non-string message becomes
/// `unknown error`.
fn wire_error(json: &Map<String, Value>) -> Option<(ErrorCode, String)> {
    let error = json.get("error")?.as_object()?;
    let code = error_code_from_python(error.get("code")?.as_str()?)?;
    let message = error
        .get("message")
        .and_then(Value::as_str)
        .unwrap_or("unknown error");
    Some((code, message.to_owned()))
}

fn exit_label(exit_code: Option<i32>) -> String {
    exit_code.map_or_else(|| "null".to_owned(), |code| code.to_string())
}

/// The last `TAIL_CHARS` characters of `text`. Never splits a character.
fn tail(text: &str) -> &str {
    let count = text.chars().count();
    if count <= TAIL_CHARS {
        return text;
    }
    match text.char_indices().nth(count - TAIL_CHARS) {
        Some((index, _)) => &text[index..],
        None => text,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract_vectors::value;

    fn classify(
        signal: Option<AbortKind>,
        stopped: Option<StopReason>,
        code: Option<i32>,
        stdout: &str,
        stderr: &str,
    ) -> Result<BridgeReply, BridgeError> {
        classify_exit(
            "orch_status",
            1_000,
            signal,
            stopped,
            code,
            stdout,
            stderr,
            Path::new("/swarm"),
        )
    }

    fn exit(code: Option<i32>, stdout: &str, stderr: &str) -> Result<BridgeReply, BridgeError> {
        classify(None, None, code, stdout, stderr)
    }

    fn wire_code(id: &str) -> ErrorCode {
        value(id)
            .as_str()
            .and_then(error_code_from_python)
            .unwrap_or_else(|| panic!("{id} is not a frozen wire code"))
    }

    fn nested(depth: usize) -> Value {
        let mut node = Value::Null;
        for _ in 0..depth {
            node = Value::Array(vec![node]);
        }
        node
    }

    fn reply_nested_to(depth: usize) -> String {
        let mut map = Map::new();
        map.insert("x".to_owned(), nested(depth - 1));
        serde_json::to_string(&Value::Object(map)).expect("object serialises")
    }

    #[test]
    fn exit_0_object_is_reply_with_exit_code_zero() {
        let reply = exit(Some(0), r#"{"status":"ok","n":1}"#, "").expect("exit 0 object is ok");
        assert_eq!(reply.exit_code, 0);
        assert_eq!(reply.json.get("n"), Some(&Value::from(1)));
        assert_eq!(reply.swarm_dir, PathBuf::from("/swarm"));
    }

    #[test]
    fn exit_0_non_object_is_internal() {
        let err = exit(Some(0), "[1]", "").expect_err("array is not an object");
        assert_eq!(err.code, ErrorCode::Internal);
        assert_eq!(
            err.message,
            "E-INTERNAL: orch_status exit 0, stdout is not a JSON object: [1]"
        );
        assert_eq!(err.details, None);
    }

    #[test]
    fn exit_1_fail_status_is_reply_with_exit_code_one() {
        let reply = exit(Some(1), r#"{"status":"fail"}"#, "").expect("fail status is data");
        assert_eq!(reply.exit_code, 1);
        assert_eq!(reply.json.get("status"), Some(&Value::from("fail")));
    }

    #[test]
    fn exit_1_other_status_is_internal_with_details() {
        let err = exit(Some(1), r#"{"status":"ok"}"#, "").expect_err("exit 1 needs fail");
        assert_eq!(err.code, ErrorCode::Internal);
        assert_eq!(
            err.message,
            "E-INTERNAL: orch_status unexpected exit 1: {\"status\":\"ok\"}"
        );
        assert_eq!(
            err.details,
            Some(serde_json::json!({"status": "ok"})),
            "the whole object travels in details"
        );
    }

    #[test]
    fn exit_2_empty_stdout_is_input_with_stderr_tail() {
        let err = exit(Some(2), " \n", "usage: bad flag\n").expect_err("usage error");
        assert_eq!(err.code, ErrorCode::Input);
        assert_eq!(
            err.message,
            "E-INPUT: orch_status usage error: usage: bad flag"
        );
    }

    #[test]
    fn exit_2_each_wire_code_is_carried() {
        let cases = [
            ("E-INPUT", ErrorCode::Input),
            ("E-TIMEOUT", ErrorCode::Timeout),
            ("E-DEP", ErrorCode::Dep),
            ("E-CAPACITY", ErrorCode::Capacity),
            ("E-CONTRACT", ErrorCode::Contract),
            ("E-POLICY", ErrorCode::Policy),
            ("E-INTERNAL", ErrorCode::Internal),
        ];
        for (wire, expected) in cases {
            let stdout = format!(r#"{{"error":{{"code":"{wire}","message":"boom"}}}}"#);
            let err = exit(Some(2), &stdout, "").expect_err("exit 2 with a wire code fails");
            assert_eq!(err.code, expected, "code {wire}");
            assert_eq!(
                err.details,
                Some(serde_json::from_str::<Value>(&stdout).expect("valid fixture")),
                "details keep the whole object for {wire}"
            );
        }
    }

    #[test]
    fn exit_2_unknown_or_non_string_code_is_internal() {
        let unknown = exit(Some(2), r#"{"error":{"code":"E-BOGUS"}}"#, "")
            .expect_err("unknown code is rejected");
        assert_eq!(unknown.code, ErrorCode::Internal);
        let numeric =
            exit(Some(2), r#"{"error":{"code":7}}"#, "").expect_err("non-string code is rejected");
        assert_eq!(numeric.code, ErrorCode::Internal);
        assert!(unknown
            .message
            .starts_with("E-INTERNAL: orch_status unexpected exit 2: "));
    }

    #[test]
    fn exit_2_without_message_reports_unknown_error() {
        let err = exit(Some(2), r#"{"error":{"code":"E-DEP","message":3}}"#, "")
            .expect_err("exit 2 with a code fails");
        assert_eq!(err.code, ErrorCode::Dep);
        assert_eq!(err.message, "E-DEP: unknown error");
    }

    #[test]
    fn exit_3_is_internal() {
        let err = exit(Some(3), "{}", "").expect_err("exit 3 is not in the contract");
        assert_eq!(err.code, ErrorCode::Internal);
        assert_eq!(err.message, "E-INTERNAL: orch_status unexpected exit 3: {}");
    }

    #[test]
    fn missing_exit_code_renders_null() {
        let err = exit(None, r#"{"status":"ok"}"#, "").expect_err("killed by signal");
        assert!(err
            .message
            .starts_with("E-INTERNAL: orch_status unexpected exit null: "));
        let err = exit(None, "oops", "crash").expect_err("non-object output");
        assert_eq!(
            err.message,
            "E-INTERNAL: orch_status exit null, stdout is not a JSON object: oops"
        );
        let err = exit(None, "", "crash").expect_err("empty stdout falls back to stderr");
        assert_eq!(
            err.message,
            "E-INTERNAL: orch_status exit null, stdout is not a JSON object: crash"
        );
    }

    #[test]
    fn timeout_stop_beats_exit_code() {
        let err = classify(
            None,
            Some(StopReason::Timeout),
            Some(0),
            r#"{"status":"ok"}"#,
            "",
        )
        .expect_err("a killed child never succeeds");
        assert_eq!(err.code, ErrorCode::Timeout);
        assert_eq!(
            err.message,
            "E-TIMEOUT: orch_status exceeded 1000 ms and was killed"
        );
    }

    #[test]
    fn deadline_signal_beats_abort_stop() {
        let err = classify(
            Some(AbortKind::Deadline),
            Some(StopReason::Abort),
            None,
            "",
            "",
        )
        .expect_err("deadline wins");
        assert_eq!(err.code, ErrorCode::Timeout);
        assert_eq!(err.message, "E-TIMEOUT: orch_status deadline exceeded");
    }

    #[test]
    fn cancelled_signal_reports_partial_state() {
        let err = classify(Some(AbortKind::Cancelled), None, Some(0), "{}", "")
            .expect_err("cancel never succeeds");
        assert_eq!(err.code, ErrorCode::Internal);
        assert_eq!(
            err.message,
            "E-INTERNAL: orch_status cancelled; Task Store state may be partially applied \
             — re-check with swarm_status before retrying"
        );
    }

    #[test]
    fn session_shutdown_matches_abort_stop() {
        let shutdown = classify(Some(AbortKind::SessionShutdown), None, Some(0), "{}", "")
            .expect_err("shutdown never succeeds");
        let aborted = classify(None, Some(StopReason::Abort), Some(0), "{}", "")
            .expect_err("abort never succeeds");
        assert_eq!(shutdown, aborted);
        assert_eq!(
            shutdown.message,
            "E-INTERNAL: orch_status cancelled (omp session shutdown); re-check with swarm_status"
        );
    }

    #[test]
    fn policy_message_drops_code_token() {
        let prefixed = BridgeError::new(ErrorCode::Policy, "E-POLICY: plan mode refuses writes");
        assert_eq!(prefixed.message, "plan mode refuses writes");
        assert_eq!(prefixed.code, ErrorCode::Policy);

        let bare = BridgeError::new(ErrorCode::Policy, "gate owner only");
        assert_eq!(bare.message, "gate owner only");

        let inner = BridgeError::new(ErrorCode::Policy, "denied E-POLICY by owner");
        assert!(!inner.message.contains("E-POLICY"));
        assert_eq!(inner.message, "denied  by owner");
    }

    #[test]
    fn policy_empty_remainder_is_refused() {
        assert_eq!(
            BridgeError::new(ErrorCode::Policy, "E-POLICY").message,
            "refused"
        );
        assert_eq!(
            BridgeError::new(ErrorCode::Policy, "E-POLICY:   ").message,
            "refused"
        );
    }

    #[test]
    fn known_code_prefix_is_not_doubled() {
        let given = BridgeError::new(ErrorCode::Input, "E-INPUT: x");
        assert_eq!(given.message, "E-INPUT: x");
        let bare = BridgeError::new(ErrorCode::Input, "x");
        assert_eq!(bare.message, "E-INPUT: x");
    }

    #[test]
    fn stderr_tail_keeps_last_400_chars() {
        let stderr = format!("{}{}", "x".repeat(10), "€".repeat(450));
        let err = exit(Some(2), "", &stderr).expect_err("usage error");
        assert_eq!(
            err.message,
            format!("E-INPUT: orch_status usage error: {}", "€".repeat(400))
        );
    }

    #[test]
    fn is_retryable_follows_default_table() {
        for code in [
            ErrorCode::Timeout,
            ErrorCode::Dep,
            ErrorCode::Capacity,
            ErrorCode::Contract,
        ] {
            assert!(is_retryable(code), "{code:?} is retryable");
        }
        for code in [ErrorCode::Input, ErrorCode::Policy, ErrorCode::Internal] {
            assert!(!is_retryable(code), "{code:?} is not retryable");
        }
    }

    #[test]
    fn retry_delay_requires_idempotent_call() {
        let policy = RetryPolicy::default();
        assert_eq!(retry_delay(ErrorCode::Dep, false, 1, &policy), None);
    }

    #[test]
    fn retry_delay_rejects_non_retryable_codes() {
        let policy = RetryPolicy::default();
        for code in [ErrorCode::Input, ErrorCode::Policy, ErrorCode::Internal] {
            assert_eq!(retry_delay(code, true, 1, &policy), None, "{code:?}");
        }
    }

    #[test]
    fn retry_delay_stops_at_max_attempts() {
        let policy = RetryPolicy::default();
        assert!(retry_delay(ErrorCode::Dep, true, policy.max_attempts - 1, &policy).is_some());
        assert_eq!(
            retry_delay(ErrorCode::Dep, true, policy.max_attempts, &policy),
            None
        );
    }

    #[test]
    fn retry_delay_backoff_doubles_and_caps() {
        let policy = RetryPolicy::default();
        assert_eq!(
            retry_delay(ErrorCode::Timeout, true, 1, &policy),
            Some(Duration::from_millis(100))
        );
        assert_eq!(
            retry_delay(ErrorCode::Timeout, true, 2, &policy),
            Some(Duration::from_millis(200))
        );

        let capped = RetryPolicy {
            max_attempts: 5,
            base_delay: Duration::from_millis(100),
            max_delay: Duration::from_millis(150),
            retryable_error_codes: RetryPolicy::default().retryable_error_codes,
        };
        assert_eq!(
            retry_delay(ErrorCode::Timeout, true, 2, &capped),
            Some(Duration::from_millis(150))
        );
    }

    #[test]
    fn retry_delay_huge_attempt_does_not_overflow() {
        let policy = RetryPolicy {
            max_attempts: u32::MAX,
            ..RetryPolicy::default()
        };
        assert_eq!(
            retry_delay(ErrorCode::Capacity, true, 40, &policy),
            Some(policy.max_delay)
        );
    }

    #[test]
    fn retry_delay_uses_the_policy_table() {
        let policy = RetryPolicy {
            retryable_error_codes: Vec::new(),
            ..RetryPolicy::default()
        };
        assert_eq!(retry_delay(ErrorCode::Timeout, true, 1, &policy), None);
    }

    #[test]
    fn script_path_rejects_bad_names() {
        for name in ["../x", "Orch", "a-b", ""] {
            let err = script_path(Path::new("/repo"), name).expect_err("invalid stem");
            assert_eq!(err.code, ErrorCode::Input, "{name:?}");
            assert!(
                err.message.starts_with("E-INPUT: invalid script name "),
                "{name:?}: {}",
                err.message
            );
        }
    }

    #[test]
    fn script_path_accepts_valid_stem() {
        assert_eq!(
            script_path(Path::new("/repo"), "orch_status"),
            Ok(PathBuf::from("/repo/scripts/orch_status.py"))
        );
    }

    #[test]
    fn parse_object_accepts_only_objects() {
        assert!(parse_object("[1]").is_none());
        assert!(parse_object("1").is_none());
        assert!(parse_object("\"text\"").is_none());
        assert!(parse_object("null").is_none());
        assert!(parse_object("{").is_none());
        let map = parse_object(r#"{"status":"ok"}"#).expect("object parses");
        assert_eq!(map.get("status"), Some(&Value::from("ok")));
    }

    #[test]
    fn json_depth_counts_containers() {
        assert_eq!(json_depth(&Value::Bool(true)), 0);
        assert_eq!(json_depth(&Value::Object(Map::new())), 1);
        assert_eq!(json_depth(&Value::Array(Vec::new())), 1);
        assert_eq!(json_depth(&Value::Array(vec![Value::Null])), 1);
        assert_eq!(json_depth(&serde_json::json!({"a": [1]})), 2);
        assert_eq!(json_depth(&nested(32)), 32);
    }

    #[test]
    fn reply_depth_32_accepted_and_33_rejected() {
        let reply = exit(Some(0), &reply_nested_to(32), "").expect("depth 32 is accepted");
        assert_eq!(json_depth(&Value::Object(reply.json)), 32);

        let err = exit(Some(0), &reply_nested_to(33), "").expect_err("depth 33 is rejected");
        assert_eq!(err.code, ErrorCode::Contract);
        assert_eq!(
            err.message,
            "E-CONTRACT: orch_status reply JSON nests deeper than 32 levels"
        );
    }

    #[test]
    fn script_argv_appends_json_last_and_keeps_order() {
        let argv = script_argv(
            Path::new("/r/scripts/orch_status.py"),
            &["--root=/r".to_owned(), "--task-id=T1".to_owned()],
        );
        assert_eq!(
            argv,
            vec![
                "/r/scripts/orch_status.py",
                "--root=/r",
                "--task-id=T1",
                "--json"
            ]
        );
        assert_eq!(
            script_argv(Path::new("/r/scripts/x.py"), &[]),
            vec!["/r/scripts/x.py", "--json"]
        );
    }

    #[test]
    fn script_argv_carries_json_exactly_once() {
        let path = Path::new("/r/scripts/orch_status.py");
        let bare = script_argv(path, &["--task-id=T1".to_owned()]);
        assert_eq!(bare.iter().filter(|a| a.as_str() == "--json").count(), 1);
        let given = script_argv(path, &["--task-id=T1".to_owned(), "--json".to_owned()]);
        assert_eq!(given.iter().filter(|a| a.as_str() == "--json").count(), 1);
        assert_eq!(given.last().map(String::as_str), Some("--json"));
    }

    #[test]
    fn replay_stop_records() {
        let timeout = classify(None, Some(StopReason::Timeout), None, "", "")
            .expect_err("stopped by timeout");
        assert_eq!(timeout.code, wire_code("X-TIMEOUT-STOP-02"));
        let abort =
            classify(None, Some(StopReason::Abort), None, "", "").expect_err("stopped by abort");
        assert_eq!(abort.code, wire_code("X-CANCEL-STOP-02"));
    }

    #[test]
    fn replay_abort_records() {
        let deadline = classify(Some(AbortKind::Deadline), None, None, "", "")
            .expect_err("TimeoutError reason");
        assert_eq!(deadline.code, wire_code("X-ABORT-TIMEOUT-REASON-02"));
        let other =
            classify(Some(AbortKind::Cancelled), None, None, "", "").expect_err("other reason");
        assert_eq!(other.code, wire_code("X-ABORT-OTHER"));
    }

    #[test]
    fn replay_empty_stdout_and_non_json_records() {
        let usage = exit(Some(2), "", "usage").expect_err("exit 2 empty stdout");
        assert_eq!(usage.code, wire_code("X-EXIT-2-EMPTY-STDOUT-02"));
        assert!(parse_object("[1]").is_none(), "X-EXIT-NON-JSON-01");
        let non_json = exit(Some(0), "[1]", "").expect_err("parseObject is undefined");
        assert_eq!(non_json.code, wire_code("X-EXIT-NON-JSON-03"));
    }

    #[test]
    fn replay_exit_0_and_exit_1_records() {
        let object = serde_json::json!({"status": "ok"});
        let zero = exit(Some(0), &object.to_string(), "").expect("exit 0 is a reply");
        assert_eq!(zero.exit_code, 0);
        assert_eq!(Value::Object(zero.json), object);

        let fail = serde_json::json!({"status": "fail"});
        let one = exit(Some(1), &fail.to_string(), "").expect("fail status is a reply");
        assert_eq!(one.exit_code, 1);
        assert_eq!(Value::Object(one.json), fail);
    }

    #[test]
    fn replay_exit_2_error_code_records() {
        let gated = exit(Some(1), r#"{"error":{"code":"E-DEP","message":"m"}}"#, "")
            .expect_err("error mapping applies only at exit 2");
        assert_eq!(gated.code, ErrorCode::Internal);

        let non_string = exit(Some(2), r#"{"error":{"code":7}}"#, "").expect_err("non-string");
        assert_eq!(non_string.code, ErrorCode::Internal);

        let unknown = exit(Some(2), r#"{"error":{"code":"E-BOGUS"}}"#, "").expect_err("unknown");
        assert_eq!(unknown.code, ErrorCode::Internal);

        let carried = exit(Some(2), r#"{"error":{"code":"E-DEP","message":"m"}}"#, "")
            .expect_err("wire code is carried");
        assert_eq!(carried.code, ErrorCode::Dep);
        assert!(carried.details.is_some());
    }

    #[test]
    fn replay_other_exit_record() {
        let err = exit(Some(3), "{}", "").expect_err("other exit");
        assert_eq!(err.code, wire_code("X-EXIT-OTHER"));
    }
}
