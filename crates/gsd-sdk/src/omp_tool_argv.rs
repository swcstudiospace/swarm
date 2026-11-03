//! Argv builders and result helpers mirroring the omp tool layer (`omp/src/tools.ts`,
//! `omp/src/commands.ts`).
//!
//! Every builder returns the tokens for one `swarm` script call, in the order
//! `tests/omp_contract/freeze.md` §1.2 records. Conditional tokens appear only when their input is
//! set. The `--json` flag is appended by the runner, not here. Optional string fields follow the
//! JavaScript truthiness of the source (`if (params.prefix)`): an empty string counts as unset.
//! Fields the source guards with `!== undefined` use `Option::is_some` instead.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;

use gsd_codes::ErrorCode;
use serde::Deserialize;
use serde_json::{Map, Value};

use crate::omp_bridge_pins::{json_depth, BridgeError, MAX_JSON_DEPTH};
use crate::omp_constants_mirror::{is_valid_tool_name, MAX_TOOL_INPUT_BYTES, QA_GATE_TIMEOUT_MS};
use crate::omp_surface_pins::SWARM_SLUGS;

/// Heading that precedes the signed envelope fence in an assignment prompt (`tools.ts:161`).
const ASSIGNMENT_HEADING: &str = "# task.assign (signed envelope)\n```json\n";

/// Error text for an input object that is not a tool call (`tools.ts:495`).
const CALL_SHAPE_MESSAGE: &str = "swarm_tool_call requires a tool name and an input object";

/// Seconds the bridge deadline adds on top of the quality gate's runner `--timeout` for cleanup
/// (`tools.ts:445-446`). Matches the 60 s margin in [`QA_GATE_TIMEOUT_MS`].
const QA_GATE_MARGIN_S: u64 = 60;

/// Frozen vector records replayed by this module (`tests/omp_contract/vectors.json`).
pub const VECTOR_IDS: &[&str] = &[
    "A-ROOT-ARG",
    "A-ORCH-PLAN-01",
    "A-ORCH-PLAN-02",
    "A-ORCH-PLAN-03",
    "A-ORCH-PLAN-04",
    "A-ORCH-PLAN-05",
    "A-ORCH-PLAN-06",
    "A-ORCH-PLAN-07",
    "A-ORCH-PLAN-08",
    "A-ORCH-PLAN-09",
    "A-ORCH-PLAN-10",
    "A-ORCH-PLAN-11",
    "A-ORCH-PLAN-12",
    "A-ORCH-PLAN-13",
    "A-ORCH-STATUS-STATUS-01",
    "A-ORCH-STATUS-STATUS-02",
    "A-ORCH-STATUS-STATUS-03",
    "A-ORCH-STATUS-INGEST-01",
    "A-ORCH-STATUS-INGEST-02",
    "A-ORCH-STATUS-INGEST-03",
    "A-ORCH-STATUS-INGEST-04",
    "A-ORCH-STATUS-INGEST-05",
    "A-ORCH-STATUS-TRANSITION-01",
    "A-ORCH-STATUS-TRANSITION-02",
    "A-ORCH-STATUS-TRANSITION-03",
    "A-ORCH-STATUS-TRANSITION-04",
    "A-ORCH-STATUS-TRANSITION-05",
    "A-ORCH-STATUS-TRANSITION-06",
    "A-ORCH-STATUS-TRANSITION-07",
    "A-ORCH-ASSIGN-01",
    "A-ORCH-ASSIGN-02",
    "A-ORCH-ASSIGN-03",
    "A-ORCH-ASSIGN-04",
    "A-GATE-01",
    "A-GATE-02",
    "A-GATE-03",
    "A-GATE-04",
    "A-GATE-05",
    "A-GATE-06",
    "A-GATE-07",
    "A-CATALOG-01",
    "A-CATALOG-02",
    "A-CATALOG-03",
    "A-CATALOG-04",
    "A-CATALOG-05",
    "A-CATALOG-06",
    "A-TOOL-CALL-01",
    "A-TOOL-CALL-02",
    "A-TOOL-CALL-03",
    "A-TOOL-CALL-04",
    "A-TOOL-CALL-05",
    "A-TOOL-CALL-06",
    "A-TOOL-CALL-07",
    "A-TOOL-CALL-08",
    "X-TOOLRESULT-FAIL-PREFIX-01",
    "X-TOOLRESULT-FAIL-PREFIX-02",
    "X-COMMAND-NONZERO-01",
    "X-COMMAND-NONZERO-02",
    "X-ASSIGNMENT-EXIT-01",
    "X-ASSIGNMENT-EXIT-02",
];

/// The argument to `--root=`. The caller passes the git toplevel, or the cwd when there is none
/// (`tools.ts:136`).
pub fn root_arg(root: &str) -> String {
    format!("--root={root}")
}

/// Parameters of one `orch_plan` call (`tools.ts:144-150`, `commands.ts:265`).
#[derive(Debug, Clone, Copy)]
pub struct PlanParams<'a> {
    /// Brief text, sent as `--brief-text=`.
    pub brief: &'a str,
    /// Pattern, sent as `--pattern=`.
    pub pattern: &'a str,
    /// Risk class, sent as `--risk-class=`.
    pub risk_class: &'a str,
    /// Optional id prefix.
    pub prefix: Option<&'a str>,
    /// Optional correlation id.
    pub correlation_id: Option<&'a str>,
    /// Optional priority.
    pub priority: Option<&'a str>,
    /// One `--acceptance=` token per item.
    pub acceptance: &'a [String],
    /// Slices, sent as one compact `--slices-json=` token when not empty.
    pub slices: &'a [Value],
    /// Sends `--dry-run` when true.
    pub dry_run: bool,
}

/// The `orch_plan` argv (`planArgs`, shared by `swarm_plan` and `/swarm`).
pub fn plan_args(root: &str, params: &PlanParams<'_>) -> Vec<String> {
    let mut args = vec![
        root_arg(root),
        format!("--brief-text={}", params.brief),
        format!("--pattern={}", params.pattern),
        format!("--risk-class={}", params.risk_class),
    ];
    push_set(&mut args, "--prefix=", params.prefix);
    push_set(&mut args, "--correlation-id=", params.correlation_id);
    push_set(&mut args, "--priority=", params.priority);
    for item in params.acceptance {
        args.push(format!("--acceptance={item}"));
    }
    if !params.slices.is_empty() {
        args.push(format!(
            "--slices-json={}",
            js_stringify(&Value::Array(params.slices.to_vec()), false)
        ));
    }
    if params.dry_run {
        args.push("--dry-run".to_owned());
    }
    args
}

/// The `orch_status` argv for `swarm_status`: `--correlation-id=` only when set
/// (`tools.ts:212-213`).
pub fn status_args(root: &str, correlation_id: Option<&str>) -> Vec<String> {
    let mut args = vec![root_arg(root)];
    push_set(&mut args, "--correlation-id=", correlation_id);
    args
}

/// The `orch_status` argv for `swarm_ingest`. `ingest_file` is the file written by
/// [`write_input_file`] (`tools.ts:341`).
pub fn ingest_args(root: &str, task_id: &str, ingest_file: &str) -> Vec<String> {
    vec![
        root_arg(root),
        format!("--task-id={task_id}"),
        format!("--ingest={ingest_file}"),
    ]
}

/// The `orch_status` argv for `swarm_transition` (`tools.ts:366-368`). `--transition` takes two
/// space-separated values, so the task id and state are separate tokens.
pub fn transition_args(
    root: &str,
    task_id: &str,
    state: &str,
    reason: &str,
    dry_run: bool,
) -> Vec<String> {
    let mut args = vec![
        root_arg(root),
        "--transition".to_owned(),
        task_id.to_owned(),
        state.to_owned(),
        format!("--reason={reason}"),
    ];
    if dry_run {
        args.push("--dry-run".to_owned());
    }
    args
}

/// The `orch_assign` argv for `swarm_assignment` (`tools.ts:250`).
pub fn assign_args(root: &str, task_id: &str, correlation_id: &str) -> Vec<String> {
    vec![
        root_arg(root),
        format!("--task-id={task_id}"),
        format!("--correlation-id={correlation_id}"),
    ]
}

/// The gate argv for `swarm_gate` (`tools.ts:421-425`). `per_target_findings` is the file from
/// [`write_input_file`], sent only when a findings object was given.
pub fn gate_args(
    root: &str,
    task_id: &str,
    correlation_id: &str,
    per_target_findings: Option<&str>,
    dry_run: bool,
    timeout_s: Option<u64>,
) -> Vec<String> {
    let mut args = vec![
        root_arg(root),
        format!("--task-id={task_id}"),
        format!("--correlation-id={correlation_id}"),
    ];
    if let Some(n) = timeout_s {
        args.push(format!("--timeout={n}"));
    }
    if let Some(file) = per_target_findings {
        args.push(format!("--per-target-findings={file}"));
    }
    if dry_run {
        args.push("--dry-run".to_owned());
    }
    args
}

/// The bridge timeout for a gate. For `quality` it is `QA_GATE_TIMEOUT_MS` by default, or the
/// runner `timeout_s` plus [`QA_GATE_MARGIN_S`] when set. Other gates have no bridge timeout
/// (`tools.ts:426-446`).
pub fn gate_timeout_ms(gate: &str, timeout_s: Option<u64>) -> Option<u64> {
    if gate != "quality" {
        return None;
    }
    Some(match timeout_s {
        None => QA_GATE_TIMEOUT_MS,
        Some(n) => n.saturating_add(QA_GATE_MARGIN_S).saturating_mul(1000),
    })
}

/// The `swarm_tool` argv for `swarm_catalog` (`tools.ts:462-464`). `kind` and `query` are sent
/// when present, including an empty query, matching the `!== undefined` guards in the source.
pub fn catalog_args(
    root: &str,
    caller: &str,
    kind: Option<&str>,
    query: Option<&str>,
) -> Vec<String> {
    let mut args = vec![
        root_arg(root),
        "catalog".to_owned(),
        format!("--caller={caller}"),
    ];
    if let Some(kind) = kind {
        args.push(format!("--kind={kind}"));
    }
    if let Some(query) = query {
        args.push(format!("--query={query}"));
    }
    args
}

/// The `swarm_tool` argv for `swarm_tool_call` (`tools.ts:500-502`). `input_json` is the text from
/// [`check_tool_input`].
pub fn tool_call_args(
    root: &str,
    caller: &str,
    name: &str,
    input_json: &str,
    plan_mode: bool,
) -> Vec<String> {
    let mut args = vec![
        root_arg(root),
        "call".to_owned(),
        format!("--caller={caller}"),
        format!("--name={name}"),
        format!("--input={input_json}"),
    ];
    if plan_mode {
        args.push("--plan-mode".to_owned());
    }
    args
}

/// Compact JSON text of a tool input, after the size and depth checks (`tools.ts:496-498`).
///
/// The size limit is the UTF-8 byte length of the text, inclusive. The depth limit is
/// `MAX_JSON_DEPTH`.
pub fn check_tool_input(input: &Value) -> Result<String, BridgeError> {
    let text = js_stringify(input, false);
    if text.len() > MAX_TOOL_INPUT_BYTES {
        return Err(BridgeError::new(
            ErrorCode::Input,
            format!("swarm_tool_call input exceeds {MAX_TOOL_INPUT_BYTES} bytes"),
        ));
    }
    if json_depth(input) > MAX_JSON_DEPTH {
        return Err(BridgeError::new(
            ErrorCode::Input,
            format!("swarm_tool_call input nests deeper than {MAX_JSON_DEPTH} levels"),
        ));
    }
    Ok(text)
}

/// Parameters of `swarm_tool_call`. Unknown fields are rejected.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolCallParams {
    /// Registry tool name; must pass `is_valid_tool_name`.
    pub name: String,
    /// The tool input; must be a JSON object.
    pub input: Value,
}

/// Parse and validate `swarm_tool_call` parameters. Every failure is `E-INPUT`
/// (`tools.ts:494-495`).
pub fn parse_tool_call_params(value: &Value) -> Result<ToolCallParams, BridgeError> {
    let shape_error = || BridgeError::new(ErrorCode::Input, CALL_SHAPE_MESSAGE);
    let params = ToolCallParams::deserialize(value).map_err(|_| shape_error())?;
    if !is_valid_tool_name(&params.name) || !params.input.is_object() {
        return Err(shape_error());
    }
    Ok(params)
}

/// Kind of a result file written by [`write_input_file`]; it prefixes the file name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputKind {
    /// An ingest result object (`swarm_ingest`).
    Ingest,
    /// A per-target findings object (`swarm_gate`).
    Findings,
}

impl InputKind {
    /// The file-name prefix for this kind.
    fn as_str(self) -> &'static str {
        match self {
            InputKind::Ingest => "ingest",
            InputKind::Findings => "findings",
        }
    }
}

/// Write `value` to `<swarm_dir>/results/<kind>-<tool_call_id>.json` and return its absolute path.
///
/// Creates `results/` and, only when no `.gitignore` exists yet, writes one containing `*`.
/// An existing one is never overwritten. The tool-call id is sanitised to `[A-Za-z0-9._-]` per
/// UTF-16 unit and cut to 120 units, as the source does. The JSON is pretty-printed with two
/// spaces.
pub fn write_input_file(
    swarm_dir: &Path,
    kind: InputKind,
    tool_call_id: &str,
    value: &Value,
) -> Result<String, BridgeError> {
    let dir = if swarm_dir.is_absolute() {
        swarm_dir.to_path_buf()
    } else {
        std::env::current_dir()
            .map_err(|e| io_error("cannot resolve the working directory", &e))?
            .join(swarm_dir)
    };
    let results = dir.join("results");
    fs::create_dir_all(&results)
        .map_err(|e| io_error(&format!("cannot create {}", results.display()), &e))?;
    if let Ok(mut gitignore) = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(dir.join(".gitignore"))
    {
        // Best effort, as the source: a failed write leaves the directory unignored but usable.
        let _ = gitignore.write_all(b"*");
    }
    let sanitised: String = tool_call_id
        .encode_utf16()
        .take(120)
        .map(|unit| match u8::try_from(unit) {
            Ok(byte) if byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-') => {
                char::from(byte)
            }
            _ => '_',
        })
        .collect();
    let path = results.join(format!("{}-{sanitised}.json", kind.as_str()));
    let text = js_stringify(value, true);
    fs::write(&path, text)
        .map_err(|e| io_error(&format!("cannot write {}", path.display()), &e))?;
    path.into_os_string().into_string().map_err(|_| {
        BridgeError::new(
            ErrorCode::Internal,
            "result path is not valid UTF-8".to_owned(),
        )
    })
}

/// Text returned to the agent: `FAIL: <summary>` for exit 1 only, then each extra line
/// (`tools.ts:157`).
pub fn tool_result_text(exit: i32, summary: &str, extra: &[String]) -> String {
    let mut text = if exit == 1 {
        format!("FAIL: {summary}")
    } else {
        summary.to_owned()
    };
    for line in extra {
        text.push('\n');
        text.push_str(line);
    }
    text
}

/// Whether a bridge call failed: any non-zero exit (`commands.ts:271`).
pub fn command_failed(exit: i32) -> bool {
    exit != 0
}

/// Whether an assignment result is a verified dispatch for this task and correlation
/// (`tools.ts:165-200`).
///
/// The exit must be 0, the status `ok`, and `assignment_verified` true. The task and correlation
/// ids must match the request. The envelope must carry the expected type, schema, source, sig
/// shape and target, and its payload must agree with the request. Exactly one prompt block must
/// carry the envelope verbatim. Both sides are compared as `JSON.stringify` writes them, so key
/// order is significant (`tools.ts:184-187`).
pub fn assignment_is_valid(
    exit: i32,
    result: &Map<String, Value>,
    task_id: &str,
    correlation_id: &str,
) -> bool {
    if exit != 0
        || str_field(result, "status") != Some("ok")
        || result.get("assignment_verified") != Some(&Value::Bool(true))
        || str_field(result, "task_id") != Some(task_id)
        || str_field(result, "correlation_id") != Some(correlation_id)
    {
        return false;
    }
    let Some(slug) = str_field(result, "agent_slug") else {
        return false;
    };
    let Some(prompt) = str_field(result, "prompt") else {
        return false;
    };
    let Some(envelope) = result.get("assignment").and_then(Value::as_object) else {
        return false;
    };
    let Some(payload) = envelope.get("payload").and_then(Value::as_object) else {
        return false;
    };
    let Some(target) = str_field(envelope, "target") else {
        return false;
    };
    if !SWARM_SLUGS.contains(&slug)
        || str_field(envelope, "type") != Some("task.assign")
        || str_field(envelope, "schema") != Some("swarm.v1.task.assign")
        || str_field(envelope, "source") != Some("A01@runner")
        || str_field(envelope, "correlation_id") != Some(correlation_id)
        || !str_field(envelope, "sig").is_some_and(valid_sig)
        || slug.split('-').next().unwrap_or("").to_uppercase() != target
        || str_field(payload, "task_id") != Some(task_id)
        || str_field(payload, "correlation_id") != Some(correlation_id)
        || str_field(payload, "agent_id") != Some(target)
    {
        return false;
    }
    let expected = js_stringify(&Value::Object(envelope.clone()), false);
    let blocks = prompt
        .split(ASSIGNMENT_HEADING)
        .skip(1)
        .filter(|block| {
            let head = block.split("\n```").next().unwrap_or("");
            serde_json::from_str::<Value>(head)
                .is_ok_and(|parsed| js_stringify(&parsed, false) == expected)
        })
        .count();
    blocks == 1
}

/// Appends `flag` plus the value when the optional string is set and non-empty
/// (JavaScript truthiness).
fn push_set(args: &mut Vec<String>, flag: &str, value: Option<&str>) {
    if let Some(value) = value.filter(|v| !v.is_empty()) {
        args.push(format!("{flag}{value}"));
    }
}

/// The string at `key`, or `None` when absent or not a string.
fn str_field<'a>(map: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    map.get(key).and_then(Value::as_str)
}

/// Matches `^(?:hmac|ed25519):.+$` without `.` crossing a JavaScript line terminator.
fn valid_sig(sig: &str) -> bool {
    let rest = sig
        .strip_prefix("hmac:")
        .or_else(|| sig.strip_prefix("ed25519:"));
    match rest {
        Some(rest) => !rest.is_empty() && !rest.contains(['\n', '\r', '\u{2028}', '\u{2029}']),
        None => false,
    }
}

/// An I/O failure while writing a result file, as `E-INTERNAL`.
fn io_error(context: &str, error: &std::io::Error) -> BridgeError {
    BridgeError::new(ErrorCode::Internal, format!("{context}: {error}"))
}

/// `JSON.stringify(value)`, or `JSON.stringify(value, null, 2)` when `pretty` is set. Numbers go
/// through [`js_number`], because serde_json writes an integral `f64` as `N.0`.
fn js_stringify(value: &Value, pretty: bool) -> String {
    let mut out = String::new();
    write_js(value, pretty, 0, &mut out);
    out
}

fn write_js(value: &Value, pretty: bool, depth: usize, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => match number.as_f64().filter(|_| number.is_f64()) {
            Some(float) => out.push_str(&js_number(float)),
            None => out.push_str(&number.to_string()),
        },
        Value::String(text) => write_js_string(text, out),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                break_line(pretty, depth + 1, out);
                write_js(item, pretty, depth + 1, out);
            }
            if !items.is_empty() {
                break_line(pretty, depth, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                break_line(pretty, depth + 1, out);
                write_js_string(key, out);
                out.push_str(if pretty { ": " } else { ":" });
                write_js(item, pretty, depth + 1, out);
            }
            if !map.is_empty() {
                break_line(pretty, depth, out);
            }
            out.push('}');
        }
    }
}

/// A new line indented to `depth` in pretty output. Compact output has none.
fn break_line(pretty: bool, depth: usize, out: &mut String) {
    if pretty {
        out.push('\n');
        out.push_str(&"  ".repeat(depth));
    }
}

/// A JSON string literal, escaped as `JSON.stringify` escapes it.
fn write_js_string(text: &str, out: &mut String) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            other if u32::from(other) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", u32::from(other)));
            }
            other => out.push(other),
        }
    }
    out.push('"');
}

/// `Number.prototype.toString` for a finite number, as `JSON.stringify` writes it. The digits are
/// the shortest round-trip digits. Magnitudes from 1e-6 up to 1e21 (exclusive) print in decimal
/// form; others print in exponent form with a signed exponent, such as `1e+21` or `1e-7`.
fn js_number(value: f64) -> String {
    if !value.is_finite() {
        return "null".to_owned();
    }
    if value == 0.0 {
        return "0".to_owned();
    }
    let scientific = format!("{:e}", value.abs());
    let (mantissa, exponent) = scientific
        .split_once('e')
        .unwrap_or((scientific.as_str(), "0"));
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let k = digits.len() as i64;
    let e: i64 = exponent.parse().unwrap_or(0);
    let n = e + 1;
    let body = if (k..=21).contains(&n) {
        format!("{digits}{}", "0".repeat((n - k) as usize))
    } else if (1..=21).contains(&n) {
        let (whole, fraction) = digits.split_at(n as usize);
        format!("{whole}.{fraction}")
    } else if (-5..=0).contains(&n) {
        format!("0.{}{digits}", "0".repeat((-n) as usize))
    } else {
        let sign = if e < 0 { '-' } else { '+' };
        let head = match digits.split_at(1) {
            (first, "") => first.to_owned(),
            (first, rest) => format!("{first}.{rest}"),
        };
        format!("{head}e{sign}{}", e.abs())
    };
    if value < 0.0 {
        format!("-{body}")
    } else {
        body
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract_vectors::all;
    use serde_json::json;
    use std::path::PathBuf;

    const ROOT: &str = "/repo";

    fn s(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|p| (*p).to_owned()).collect()
    }

    fn plan<'a>(acceptance: &'a [String], slices: &'a [Value]) -> PlanParams<'a> {
        PlanParams {
            brief: "b",
            pattern: "feature",
            risk_class: "low",
            prefix: None,
            correlation_id: None,
            priority: None,
            acceptance,
            slices,
            dry_run: false,
        }
    }

    /// A unique scratch directory per test under the system temp dir.
    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("gsd-sdk-argv-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// Asserts each id names one of the frozen records. This checks existence only: the `value`
    /// templates are not rendered, so the byte-level check is each test's own argv assertion.
    fn records_exist(ids: &[&str]) {
        let present: Vec<String> = all()
            .iter()
            .filter_map(|r| r["id"].as_str().map(str::to_owned))
            .collect();
        for id in ids {
            assert!(present.iter().any(|p| p == id), "{id} missing");
        }
    }

    fn assignment_fixture() -> Map<String, Value> {
        let envelope = json!({
            "type": "task.assign",
            "schema": "swarm.v1.task.assign",
            "source": "A01@runner",
            "target": "A05",
            "correlation_id": "corr-1",
            "sig": "hmac:abc",
            "payload": {"task_id": "T-1", "correlation_id": "corr-1", "agent_id": "A05"},
        });
        let prompt = format!(
            "{ASSIGNMENT_HEADING}{}\n```\nrest",
            serde_json::to_string(&envelope).unwrap()
        );
        json!({
            "status": "ok",
            "assignment_verified": true,
            "task_id": "T-1",
            "correlation_id": "corr-1",
            "agent_slug": "a05-backend",
            "prompt": prompt,
            "assignment": envelope,
        })
        .as_object()
        .unwrap()
        .clone()
    }

    #[test]
    fn root_arg_renders_the_resolved_root() {
        assert_eq!(root_arg("/repo"), "--root=/repo");
        records_exist(&["A-ROOT-ARG"]);
    }

    #[test]
    fn plan_minimal_has_only_the_required_tokens_in_order() {
        let args = plan_args(ROOT, &plan(&[], &[]));
        assert_eq!(
            args,
            s(&[
                "--root=/repo",
                "--brief-text=b",
                "--pattern=feature",
                "--risk-class=low"
            ])
        );
        records_exist(&[
            "A-ORCH-PLAN-01",
            "A-ORCH-PLAN-02",
            "A-ORCH-PLAN-03",
            "A-ORCH-PLAN-04",
        ]);
    }

    #[test]
    fn plan_with_every_optional_field_yields_the_full_order() {
        let acceptance = s(&["first", "second"]);
        let slices = [json!({"id": "s1"})];
        let params = PlanParams {
            prefix: Some("pre"),
            correlation_id: Some("corr"),
            priority: Some("P1"),
            dry_run: true,
            ..plan(&acceptance, &slices)
        };
        assert_eq!(
            plan_args(ROOT, &params),
            s(&[
                "--root=/repo",
                "--brief-text=b",
                "--pattern=feature",
                "--risk-class=low",
                "--prefix=pre",
                "--correlation-id=corr",
                "--priority=P1",
                "--acceptance=first",
                "--acceptance=second",
                r#"--slices-json=[{"id":"s1"}]"#,
                "--dry-run",
            ])
        );
        records_exist(&[
            "A-ORCH-PLAN-05",
            "A-ORCH-PLAN-06",
            "A-ORCH-PLAN-07",
            "A-ORCH-PLAN-08",
            "A-ORCH-PLAN-09",
            "A-ORCH-PLAN-10",
        ]);
    }

    #[test]
    fn plan_treats_empty_optional_strings_as_unset() {
        let params = PlanParams {
            prefix: Some(""),
            correlation_id: Some(""),
            priority: Some(""),
            ..plan(&[], &[])
        };
        assert_eq!(plan_args(ROOT, &params).len(), 4);
    }

    #[test]
    fn plan_omits_slices_when_the_list_is_empty() {
        let args = plan_args(ROOT, &plan(&[], &[]));
        assert!(!args.iter().any(|a| a.starts_with("--slices-json=")));
    }

    #[test]
    fn status_adds_correlation_only_when_set() {
        assert_eq!(status_args(ROOT, None), s(&["--root=/repo"]));
        assert_eq!(
            status_args(ROOT, Some("corr")),
            s(&["--root=/repo", "--correlation-id=corr"])
        );
        records_exist(&["A-ORCH-STATUS-STATUS-01", "A-ORCH-STATUS-STATUS-02"]);
    }

    #[test]
    fn ingest_carries_task_id_and_file() {
        assert_eq!(
            ingest_args(ROOT, "T-1", "/x/results/ingest-c.json"),
            s(&[
                "--root=/repo",
                "--task-id=T-1",
                "--ingest=/x/results/ingest-c.json"
            ])
        );
        records_exist(&[
            "A-ORCH-STATUS-INGEST-02",
            "A-ORCH-STATUS-INGEST-03",
            "A-ORCH-STATUS-INGEST-04",
        ]);
    }

    #[test]
    fn transition_splits_task_and_state_and_adds_dry_run_last() {
        assert_eq!(
            transition_args(ROOT, "T-1", "DONE", "why", false),
            s(&[
                "--root=/repo",
                "--transition",
                "T-1",
                "DONE",
                "--reason=why"
            ])
        );
        assert_eq!(
            transition_args(ROOT, "T-1", "DONE", "why", true)
                .last()
                .map(String::as_str),
            Some("--dry-run")
        );
        records_exist(&[
            "A-ORCH-STATUS-TRANSITION-01",
            "A-ORCH-STATUS-TRANSITION-02",
            "A-ORCH-STATUS-TRANSITION-03",
            "A-ORCH-STATUS-TRANSITION-04",
            "A-ORCH-STATUS-TRANSITION-05",
            "A-ORCH-STATUS-TRANSITION-06",
        ]);
    }

    #[test]
    fn assign_has_task_and_correlation() {
        assert_eq!(
            assign_args(ROOT, "T-1", "corr"),
            s(&["--root=/repo", "--task-id=T-1", "--correlation-id=corr"])
        );
        records_exist(&[
            "A-ORCH-ASSIGN-01",
            "A-ORCH-ASSIGN-02",
            "A-ORCH-ASSIGN-03",
            "A-ORCH-ASSIGN-04",
        ]);
    }

    #[test]
    fn gate_orders_findings_before_dry_run() {
        assert_eq!(
            gate_args(ROOT, "T-1", "corr", None, false, None),
            s(&["--root=/repo", "--task-id=T-1", "--correlation-id=corr"])
        );
        assert_eq!(
            gate_args(ROOT, "T-1", "corr", Some("/f.json"), true, None),
            s(&[
                "--root=/repo",
                "--task-id=T-1",
                "--correlation-id=corr",
                "--per-target-findings=/f.json",
                "--dry-run",
            ])
        );
        records_exist(&[
            "A-GATE-02",
            "A-GATE-03",
            "A-GATE-04",
            "A-GATE-05",
            "A-GATE-06",
        ]);
    }

    #[test]
    fn gate_timeout_is_the_qa_timeout_for_quality_only() {
        assert_eq!(gate_timeout_ms("quality", None), Some(1_860_000));
        assert_eq!(gate_timeout_ms("review", None), None);
        assert_eq!(gate_timeout_ms("security", None), None);
        assert_eq!(gate_timeout_ms("release", None), None);
        records_exist(&["A-GATE-07"]);
    }

    /// SDK-02: `timeout_s` (the quality gate's runner budget, `tools.ts:440`) becomes `--timeout=<n>` after `--correlation-id`, and the
    /// bridge deadline grows to `(timeout_s + 60)` seconds (`tools.ts:445-446`). `gate_args` takes `timeout_s` as its last parameter.
    #[test]
    fn gate_args_quality_timeout_s_emits_flag_and_extends_deadline() {
        assert_eq!(
            gate_args(ROOT, "T-1", "corr", None, false, Some(3600)),
            s(&[
                "--root=/repo",
                "--task-id=T-1",
                "--correlation-id=corr",
                "--timeout=3600",
            ])
        );
        assert_eq!(
            gate_args(ROOT, "T-1", "corr", Some("/f.json"), true, Some(3600)),
            s(&[
                "--root=/repo",
                "--task-id=T-1",
                "--correlation-id=corr",
                "--timeout=3600",
                "--per-target-findings=/f.json",
                "--dry-run",
            ])
        );
        assert_eq!(gate_timeout_ms("quality", Some(3600)), Some(3_660_000));
    }

    #[test]
    fn catalog_sends_kind_and_query_when_present_even_if_empty() {
        assert_eq!(
            catalog_args(ROOT, "agent", None, None),
            s(&["--root=/repo", "catalog", "--caller=agent"])
        );
        assert_eq!(
            catalog_args(ROOT, "agent", Some("tools"), Some("")),
            s(&[
                "--root=/repo",
                "catalog",
                "--caller=agent",
                "--kind=tools",
                "--query=",
            ])
        );
        records_exist(&[
            "A-CATALOG-01",
            "A-CATALOG-02",
            "A-CATALOG-03",
            "A-CATALOG-04",
            "A-CATALOG-05",
        ]);
    }

    #[test]
    fn tool_call_adds_plan_mode_last_only_in_plan_mode() {
        assert_eq!(
            tool_call_args(ROOT, "agent", "be_route_norm", "{}", false),
            s(&[
                "--root=/repo",
                "call",
                "--caller=agent",
                "--name=be_route_norm",
                "--input={}",
            ])
        );
        let plan_mode = tool_call_args(ROOT, "agent", "be_route_norm", "{}", true);
        assert_eq!(plan_mode.last().map(String::as_str), Some("--plan-mode"));
        records_exist(&[
            "A-TOOL-CALL-01",
            "A-TOOL-CALL-02",
            "A-TOOL-CALL-03",
            "A-TOOL-CALL-04",
            "A-TOOL-CALL-05",
            "A-TOOL-CALL-06",
        ]);
    }

    #[test]
    fn input_of_exactly_the_cap_is_accepted() {
        // A JSON string adds two quote bytes to its content.
        let input = Value::String("a".repeat(MAX_TOOL_INPUT_BYTES - 2));
        let text = check_tool_input(&input).unwrap();
        assert_eq!(text.len(), MAX_TOOL_INPUT_BYTES);
    }

    #[test]
    fn input_one_byte_over_the_cap_is_rejected_as_e_input() {
        let input = Value::String("a".repeat(MAX_TOOL_INPUT_BYTES - 1));
        let err = check_tool_input(&input).unwrap_err();
        assert_eq!(err.code, ErrorCode::Input);
        assert!(err
            .message
            .contains("swarm_tool_call input exceeds 100000 bytes"));
    }

    #[test]
    fn input_cap_counts_utf8_bytes_not_characters() {
        // 33 333 euro signs are 99 999 bytes plus 2 quotes = 100 001 bytes, though they are only
        // 33 333 characters.
        let over = Value::String("€".repeat(33_333));
        assert_eq!(check_tool_input(&over).unwrap_err().code, ErrorCode::Input);
        let at = Value::String("€".repeat(33_332));
        assert_eq!(check_tool_input(&at).unwrap().len(), 99_998);
    }

    #[test]
    fn input_nesting_deeper_than_the_limit_is_rejected() {
        let nested =
            |levels: usize| (0..levels).fold(Value::Null, |inner, _| Value::Array(vec![inner]));
        // Each container is one json_depth level, so MAX_JSON_DEPTH is the last accepted depth.
        assert!(check_tool_input(&nested(MAX_JSON_DEPTH)).is_ok());
        let err = check_tool_input(&nested(MAX_JSON_DEPTH + 1)).unwrap_err();
        assert_eq!(err.code, ErrorCode::Input);
    }

    #[test]
    fn parse_accepts_a_valid_call() {
        let params =
            parse_tool_call_params(&json!({"name": "be_route_norm", "input": {"a": 1}})).unwrap();
        assert_eq!(params.name, "be_route_norm");
        assert_eq!(params.input, json!({"a": 1}));
    }

    #[test]
    fn parse_rejects_unknown_fields() {
        let err = parse_tool_call_params(&json!({"name": "ok_tool", "input": {}, "extra": 1}))
            .unwrap_err();
        assert_eq!(err.code, ErrorCode::Input);
    }

    #[test]
    fn parse_rejects_an_invalid_tool_name() {
        for name in ["Bad", "9lead", "a-b", ""] {
            let err = parse_tool_call_params(&json!({"name": name, "input": {}})).unwrap_err();
            assert_eq!(err.code, ErrorCode::Input, "{name}");
        }
    }

    #[test]
    fn parse_rejects_a_non_object_input_or_missing_fields() {
        assert!(parse_tool_call_params(&json!({"name": "ok_tool", "input": []})).is_err());
        assert!(parse_tool_call_params(&json!({"name": "ok_tool"})).is_err());
        assert!(parse_tool_call_params(&json!({"input": {}})).is_err());
        assert!(parse_tool_call_params(&json!("ok_tool")).is_err());
    }

    #[test]
    fn write_input_file_sanitises_the_name_and_round_trips_json() {
        let dir = scratch("sanitise");
        let value = json!({"severity": "major", "list": [1, 2]});
        let path = write_input_file(&dir, InputKind::Findings, "call/1 x:é", &value).unwrap();
        assert!(Path::new(&path).is_absolute());
        assert!(path.ends_with("results/findings-call_1_x__.json"), "{path}");
        let text = fs::read_to_string(&path).unwrap();
        assert_eq!(serde_json::from_str::<Value>(&text).unwrap(), value);
        assert!(text.contains("\n  \""), "pretty printed with two spaces");
        records_exist(&["A-GATE-05", "A-ORCH-STATUS-INGEST-01"]);
    }

    #[test]
    fn write_input_file_cuts_the_name_to_120_utf16_units() {
        let dir = scratch("cut");
        let id = "a".repeat(200);
        let path = write_input_file(&dir, InputKind::Ingest, &id, &json!({})).unwrap();
        let file = Path::new(&path)
            .file_name()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        assert_eq!(file, format!("ingest-{}.json", "a".repeat(120)));
    }

    #[test]
    fn write_input_file_writes_gitignore_once_and_never_overwrites() {
        let dir = scratch("gitignore");
        write_input_file(&dir, InputKind::Ingest, "c1", &json!({})).unwrap();
        let gitignore = dir.join(".gitignore");
        assert_eq!(fs::read_to_string(&gitignore).unwrap(), "*");
        fs::write(&gitignore, "keep").unwrap();
        write_input_file(&dir, InputKind::Ingest, "c2", &json!({})).unwrap();
        assert_eq!(fs::read_to_string(&gitignore).unwrap(), "keep");
    }

    #[test]
    fn tool_result_prefixes_fail_only_for_exit_one() {
        let extra = s(&["db: x", "more"]);
        assert_eq!(
            tool_result_text(1, "gate red", &extra),
            "FAIL: gate red\ndb: x\nmore"
        );
        assert_eq!(tool_result_text(0, "ok", &[]), "ok");
        assert_eq!(tool_result_text(2, "usage", &[]), "usage");
        records_exist(&["X-TOOLRESULT-FAIL-PREFIX-01", "X-TOOLRESULT-FAIL-PREFIX-02"]);
    }

    #[test]
    fn command_failed_is_any_non_zero_exit() {
        assert!(!command_failed(0));
        assert!(command_failed(1));
        assert!(command_failed(2));
        records_exist(&["X-COMMAND-NONZERO-01", "X-COMMAND-NONZERO-02"]);
    }

    #[test]
    fn assignment_accepts_a_verified_dispatch() {
        let result = assignment_fixture();
        assert!(assignment_is_valid(0, &result, "T-1", "corr-1"));
        records_exist(&["X-ASSIGNMENT-EXIT-01", "X-ASSIGNMENT-EXIT-02"]);
    }

    #[test]
    fn assignment_rejects_a_non_zero_exit() {
        assert!(!assignment_is_valid(
            1,
            &assignment_fixture(),
            "T-1",
            "corr-1"
        ));
    }

    #[test]
    fn assignment_rejects_a_non_ok_status() {
        let mut result = assignment_fixture();
        result.insert("status".into(), json!("fail"));
        assert!(!assignment_is_valid(0, &result, "T-1", "corr-1"));
    }

    #[test]
    fn assignment_rejects_a_mismatched_request_or_unverified_result() {
        let base = assignment_fixture();
        assert!(!assignment_is_valid(0, &base, "T-2", "corr-1"));
        assert!(!assignment_is_valid(0, &base, "T-1", "corr-2"));
        let mut unverified = base.clone();
        unverified.insert("assignment_verified".into(), json!(false));
        assert!(!assignment_is_valid(0, &unverified, "T-1", "corr-1"));
    }

    #[test]
    fn assignment_rejects_a_target_that_does_not_match_the_slug() {
        let mut result = assignment_fixture();
        result.insert("agent_slug".into(), json!("a06-frontend"));
        assert!(!assignment_is_valid(0, &result, "T-1", "corr-1"));
    }

    /// The prompt block for `envelope`, in the shape the assignment check parses.
    fn envelope_block(envelope: &Value) -> String {
        format!(
            "{ASSIGNMENT_HEADING}{}\n```\n",
            serde_json::to_string(envelope).unwrap()
        )
    }

    #[test]
    fn assignment_rejects_an_unsigned_or_malformed_sig() {
        for sig in ["hmac:", "md5:abc", "hmac:a\nb", "ed25519"] {
            let mut result = assignment_fixture();
            let mut envelope = result["assignment"].clone();
            envelope["sig"] = json!(sig);
            result.insert("prompt".into(), json!(envelope_block(&envelope)));
            result.insert("assignment".into(), envelope);
            assert!(!assignment_is_valid(0, &result, "T-1", "corr-1"), "{sig}");
        }
        // Control: the same rebuilt result with a well-formed sig is accepted.
        let mut control = assignment_fixture();
        let mut envelope = control["assignment"].clone();
        envelope["sig"] = json!("ed25519:xyz");
        control.insert("prompt".into(), json!(envelope_block(&envelope)));
        control.insert("assignment".into(), envelope);
        assert!(assignment_is_valid(0, &control, "T-1", "corr-1"));
    }

    #[test]
    fn assignment_requires_exactly_one_matching_prompt_block() {
        let base = assignment_fixture();
        let envelope = base["assignment"].clone();
        let block = format!(
            "{ASSIGNMENT_HEADING}{}\n```\n",
            serde_json::to_string(&envelope).unwrap()
        );
        let mut twice = base.clone();
        twice.insert("prompt".into(), json!(format!("{block}{block}")));
        assert!(!assignment_is_valid(0, &twice, "T-1", "corr-1"));

        let mut none = base.clone();
        none.insert("prompt".into(), json!("no envelope here"));
        assert!(!assignment_is_valid(0, &none, "T-1", "corr-1"));
    }

    #[test]
    fn assignment_rejects_a_prompt_block_whose_envelope_differs() {
        let mut result = assignment_fixture();
        let mut changed = result["assignment"].clone();
        changed["sig"] = json!("hmac:zzz");
        result.insert("prompt".into(), json!(envelope_block(&changed)));
        assert!(!assignment_is_valid(0, &result, "T-1", "corr-1"));
    }

    #[test]
    fn assignment_rejects_a_prompt_block_with_reordered_keys() {
        let base = assignment_fixture();
        let Value::Object(fields) = base["assignment"].clone() else {
            panic!("the envelope is a JSON object");
        };
        let mut pairs: Vec<(String, Value)> = fields.into_iter().collect();
        pairs.reverse();
        let reordered: Map<String, Value> = pairs.into_iter().collect();
        let mut result = base.clone();
        result.insert(
            "prompt".into(),
            json!(envelope_block(&Value::Object(reordered))),
        );
        assert!(!assignment_is_valid(0, &result, "T-1", "corr-1"));
    }

    /// Call-site records with no token of their own here: the caller picks the argument.
    #[test]
    fn call_site_records_exist_in_the_frozen_contract() {
        records_exist(&[
            "A-ROOT-ARG",
            "A-ORCH-PLAN-11",
            "A-ORCH-PLAN-12",
            "A-ORCH-PLAN-13",
            "A-ORCH-STATUS-STATUS-03",
            "A-ORCH-STATUS-INGEST-05",
            "A-ORCH-STATUS-TRANSITION-07",
            "A-ORCH-ASSIGN-01",
            "A-GATE-01",
            "A-CATALOG-06",
            "A-TOOL-CALL-07",
            "A-TOOL-CALL-08",
        ]);
    }

    #[test]
    fn json_text_writes_numbers_the_way_json_stringify_does() {
        assert_eq!(js_stringify(&json!({"n": 2.0}), false), r#"{"n":2}"#);
        let parsed: Value = serde_json::from_str(r#"{"n":1e3}"#).unwrap();
        assert_eq!(js_stringify(&parsed, false), r#"{"n":1000}"#);
        assert_eq!(js_stringify(&json!({"n": 0.5}), false), r#"{"n":0.5}"#);
        assert_eq!(
            js_stringify(&json!({"n": 1e20}), false),
            r#"{"n":100000000000000000000}"#
        );
        assert_eq!(js_stringify(&json!({"n": 1e21}), false), r#"{"n":1e+21}"#);
        assert_eq!(js_stringify(&json!({"n": 1e-7}), false), r#"{"n":1e-7}"#);
        assert_eq!(js_stringify(&json!({"n": -2.5}), false), r#"{"n":-2.5}"#);
    }

    #[test]
    fn tool_input_and_slices_write_integral_numbers_without_a_fraction() {
        assert_eq!(check_tool_input(&json!({"n": 2.0})).unwrap(), r#"{"n":2}"#);
        let slices = [json!({"n": 2.0})];
        let args = plan_args(ROOT, &plan(&[], &slices));
        assert!(args.contains(&r#"--slices-json=[{"n":2}]"#.to_owned()));
    }

    #[test]
    fn written_input_is_pretty_printed_with_javascript_numbers() {
        let dir = scratch("pretty-numbers");
        let value = json!({"a": [2.0], "b": {}});
        let path = write_input_file(&dir, InputKind::Ingest, "p1", &value).unwrap();
        let text = fs::read_to_string(&path).unwrap();
        assert_eq!(text, "{\n  \"a\": [\n    2\n  ],\n  \"b\": {}\n}");
    }
}
