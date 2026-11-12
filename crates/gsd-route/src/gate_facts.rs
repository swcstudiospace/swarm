//! Pure gate facts: the part of the Task Store's approval logic that needs no key.
//!
//! This is `TaskStore._check_row`, `_latest_rows`, `missing_gate_reasons`, `unaccepted_gates`, `latest_verdicts` and `required_gates`
//! (`swarm/taskstore.py`) over plain facts, so the host and the sandbox guest run one function. The oracle is the Python store: the
//! vectors in `tests/vectors/gate_facts.json` are recorded from a real seeded `TaskStore` and replayed here string for string.
//!
//! # The key boundary
//!
//! Python's `_check_row` calls `validate_verdict`, which verifies the envelope signature. That step is the host's: it checks each verdict
//! row's stored envelope text with its own key material and passes the answer as `sig_ok`. Nothing in this module reads a key, a
//! signature byte or the environment, and it never inspects the `sig` field: a missing, empty or malformed `sig` is `sig_ok == false`
//! because the host's verification (`verify_envelope`) says so. Everything else `validate_verdict` checks is pure and is done here
//! ([`parse_envelope`]): the envelope shape of `validate_envelope`, the `gate.verdict` type and the five payload keys.
//!
//! # What this module refuses
//!
//! Rust answers only what it can prove identical to Python. Inputs whose Python meaning this module does not reproduce come back as
//! [`Unsupported`], and the caller must let Python answer: a non-string `gate_task`, a numeric string that Python's `float()` reads
//! through an underscore or a non-ASCII digit, JSON text that Python's `json` parses but the hand-rolled parser does not (`NaN`,
//! `Infinity`, integers beyond `i64`, nesting beyond the parser depth), a `notes.gates` that is not a list of strings, a `risk_class` outside
//! [`gates_for_risk`], and a `notes.verdicts_since` that is not a number. In every one of those cases a row whose signature fails is still
//! answered `bad-sig`, exactly as Python does, because the signature fails before the odd value is read.

use std::cmp::Ordering;

use crate::{obj_get, parse_json, Json, MAX_JSON_DEPTH};

/// Default `expires_s` of `swarm.gates.make_verdict`: a verdict lives 24 hours.
pub const DEFAULT_EXPIRES_S: u32 = 86_400;

/// `expires_s` of the review gate (`scripts/rev_gate.py`), also `_MAX_EXPIRES_S` of the A09 tools: 48 hours.
pub const REVIEW_EXPIRES_S: u32 = 172_800;

/// `ACCEPTED_ISSUER_STATES`: a gate task in one of these states had its result accepted.
pub const ACCEPTED_ISSUER_STATES: [&str; 3] = ["IN_REVIEW", "APPROVED", "DONE"];

/// `GATES_BY_RISK`: the gates a risk class requires unless `notes.gates` overrides them.
pub fn gates_for_risk(risk_class: &str) -> Option<&'static [&'static str]> {
    match risk_class {
        "low" => Some(&["review"]),
        "medium" => Some(&["review", "quality"]),
        "high" => Some(&["review", "quality", "security", "release"]),
        _ => None,
    }
}

/// Envelope fields `validate_envelope` requires (`REQUIRED_FIELDS`).
const ENVELOPE_FIELDS: [&str; 12] = [
    "msg_id",
    "correlation_id",
    "trace_id",
    "source",
    "target",
    "type",
    "schema",
    "ts",
    "ttl_s",
    "priority",
    "risk_class",
    "payload",
];

/// Payload keys `validate_verdict` requires.
const PAYLOAD_FIELDS: [&str; 5] = ["gate", "task_id", "verdict", "findings", "expires_s"];

/// Valid envelope priorities.
const PRIORITIES: [&str; 4] = ["P0", "P1", "P2", "P3"];

/// Valid envelope risk classes.
const RISK_CLASSES: [&str; 3] = ["low", "medium", "high"];

/// The input is outside the subset whose Python behaviour this module reproduces; Python must answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Unsupported(pub &'static str);

impl std::fmt::Display for Unsupported {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "gate facts outside the provable subset: {}", self.0)
    }
}

impl std::error::Error for Unsupported {}

/// What `_check_row` answers for one gate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CheckOutcome {
    /// Verified, current, unexpired, `pass` or `waive`.
    Ok,
    /// Verified and current, with a verdict that is neither `pass` nor `waive`.
    Fail,
    /// Verified and current, but `issued_at + expires_s` is not after the host clock.
    Expired,
    /// No row for the gate.
    Absent,
    /// The row carries no envelope.
    Unsigned,
    /// The envelope is unreadable, malformed, not a `gate.verdict`, or its signature does not verify.
    BadSig,
    /// The signed gate, task, verdict or correlation disagrees with the row or the task, or a signed number is unusable.
    Mismatch,
    /// Signed before the task's last rework.
    Stale,
    /// A signed `dry_run` verdict on a task that was not dispatched in a runner dry run.
    DryRun,
}

impl CheckOutcome {
    /// The exact string Python returns.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Fail => "fail",
            Self::Expired => "expired",
            Self::Absent => "absent",
            Self::Unsigned => "unsigned",
            Self::BadSig => "bad-sig",
            Self::Mismatch => "mismatch",
            Self::Stale => "stale",
            Self::DryRun => "dry-run",
        }
    }

    /// Python's `_VERIFIED`: a verified, current envelope (`ok`, `fail`, `expired`), the only rows `reconcile` acts on.
    pub fn is_verified(self) -> bool {
        matches!(self, Self::Ok | Self::Fail | Self::Expired)
    }
}

/// A verdict row as `SELECT * FROM verdicts` returns it, without the columns the pure part never reads.
#[derive(Debug, Clone, PartialEq)]
pub struct VerdictRow {
    /// `id`: rows are ordered by it, and the highest id of a gate wins.
    pub id: i64,
    /// `task_id` the row is recorded on.
    pub task_id: String,
    /// `gate` column.
    pub gate: String,
    /// `verdict` column (`NULL` is `None`).
    pub verdict: Option<String>,
    /// `envelope_json`: the stored envelope text (`NULL` is `None`; `None` and the empty string both mean unsigned).
    pub envelope_json: Option<String>,
    /// `ts`: when the row was inserted.
    pub ts: f64,
}

/// A number read the way `float(x)` reads a JSON value.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Num {
    /// `float(x)` succeeds.
    Value(f64),
    /// `float(x)` raises `KeyError`, `TypeError` or `ValueError` (the value is missing, `null`, a list, an object or non-numeric text).
    Invalid,
    /// Python would read it through a rule this module does not reproduce.
    Unsupported,
}

/// The envelope's `payload.gate_task`: the gate task that issued the verdict.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GateTask {
    /// Absent or `null`: written outside a gate task, counts as accepted.
    None,
    /// A task id.
    Task(String),
    /// Neither absent, `null` nor a string: the Task Store lookup of it is not reproduced.
    Unsupported,
}

/// The fields of a structurally valid signed `gate.verdict` envelope that the pure part reads.
#[derive(Debug, Clone, PartialEq)]
pub struct ParsedEnvelope {
    /// `payload.gate`.
    pub gate: Json,
    /// `payload.task_id`.
    pub task_id: Json,
    /// `payload.verdict`.
    pub verdict: Json,
    /// Envelope `correlation_id`.
    pub correlation_id: Json,
    /// `float(payload.issued_at)`.
    pub issued_at: Num,
    /// `float(payload.expires_s)`.
    pub expires_s: Num,
    /// Python truthiness of `payload.dry_run`.
    pub dry_run: bool,
    /// `payload.gate_task`.
    pub gate_task: GateTask,
}

/// What `json.loads` plus `validate_verdict` (minus the signature) make of a stored envelope text.
#[derive(Debug, Clone, PartialEq)]
pub enum EnvelopeState {
    /// Python raises: unparsable, not an object, or a field `validate_envelope` or `validate_verdict` requires is wrong or missing.
    Invalid,
    /// The text is outside what the hand-rolled parser provably reads like Python's `json`.
    Unsupported,
    /// A valid envelope.
    Valid(Box<ParsedEnvelope>),
}

/// One verdict row with the host's answer to "does its stored envelope verify?".
#[derive(Debug, Clone, PartialEq)]
pub struct VerdictFacts {
    /// The row.
    pub row: VerdictRow,
    /// The row's envelope, parsed by [`parse_envelope`].
    pub envelope: EnvelopeState,
    /// The host's `verify_envelope` answer for `row.envelope_json` (`false` for a missing or unreadable envelope).
    pub sig_ok: bool,
}

impl VerdictFacts {
    /// Parses the row's envelope text and attaches the host's signature answer.
    pub fn new(row: VerdictRow, sig_ok: bool) -> Self {
        let envelope = match row.envelope_json.as_deref() {
            None | Some("") => EnvelopeState::Invalid,
            Some(text) => parse_envelope(text),
        };
        Self {
            row,
            envelope,
            sig_ok,
        }
    }
}

/// The facts about the target task that gate decisions read.
#[derive(Debug, Clone, PartialEq)]
pub struct TaskFacts {
    /// `task_id`.
    pub task_id: String,
    /// `correlation_id`.
    pub correlation_id: String,
    /// `_verdicts_since`: `float(notes.verdicts_since or 0)`, the cut-off of the last rework.
    pub verdicts_since: f64,
    /// Python truthiness of `notes.dry_run`.
    pub dry_run: bool,
    /// `_gates_of`: `notes.gates` when present, else `GATES_BY_RISK[risk_class]`, in this order.
    pub required_gates: Vec<String>,
}

/// The state of one task, for the issuer rule of [`unaccepted_gates`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IssuerState {
    /// `task_id`.
    pub task_id: String,
    /// `state`.
    pub state: String,
}

impl TaskFacts {
    /// Builds the facts from the task row columns, reading `notes` the way `TaskStore._row` does (empty, unparsable or non-object
    /// text is an empty object) and `_gates_of` and `_verdicts_since` do.
    pub fn from_row(
        task_id: &str,
        correlation_id: &str,
        risk_class: Option<&str>,
        notes: Option<&str>,
    ) -> Result<Self, Unsupported> {
        let notes: Vec<(String, Json)> = match notes {
            None | Some("") => Vec::new(),
            Some(text) => match parse_json(text) {
                Ok(Json::Obj(fields)) => fields,
                Ok(_) => Vec::new(),
                Err(_) if ambiguous_json_text(text) => {
                    return Err(Unsupported(
                        "notes text that Python's json may read differently",
                    ))
                }
                Err(_) => Vec::new(),
            },
        };
        let required_gates = match obj_get(&notes, "gates") {
            None | Some(Json::Null) => match risk_class.and_then(gates_for_risk) {
                Some(defaults) => defaults.iter().map(|gate| (*gate).to_string()).collect(),
                None => return Err(Unsupported("risk class outside GATES_BY_RISK")),
            },
            Some(Json::Arr(items)) => items
                .iter()
                .map(|item| match item {
                    Json::Str(gate) => Ok(gate.clone()),
                    _ => Err(Unsupported("notes.gates holds a non-string")),
                })
                .collect::<Result<Vec<_>, _>>()?,
            Some(_) => return Err(Unsupported("notes.gates is not a list")),
        };
        Ok(Self {
            task_id: task_id.to_string(),
            correlation_id: correlation_id.to_string(),
            verdicts_since: verdicts_since(obj_get(&notes, "verdicts_since"))?,
            dry_run: obj_get(&notes, "dry_run").is_some_and(py_truthy),
            required_gates,
        })
    }
}

/// `float(value or 0)`, as `_verdicts_since` computes it from `notes.verdicts_since`.
fn verdicts_since(value: Option<&Json>) -> Result<f64, Unsupported> {
    let unusable = Unsupported("notes.verdicts_since is not a number");
    match value {
        None | Some(Json::Null | Json::Bool(false) | Json::Int(0)) => Ok(0.0),
        Some(Json::Bool(true)) => Ok(1.0),
        Some(Json::Int(number)) => Ok(*number as f64),
        Some(Json::Float(number)) => Ok(*number),
        Some(Json::Str(text)) if text.is_empty() => Ok(0.0),
        Some(Json::Str(text)) => match py_float_text(text) {
            Num::Value(number) => Ok(number),
            Num::Invalid | Num::Unsupported => Err(unusable),
        },
        Some(Json::Arr(items)) if items.is_empty() => Ok(0.0),
        Some(Json::Obj(fields)) if fields.is_empty() => Ok(0.0),
        Some(Json::Arr(_) | Json::Obj(_)) => Err(unusable),
    }
}

/// Python truthiness of a JSON value.
fn py_truthy(value: &Json) -> bool {
    match value {
        Json::Null => false,
        Json::Bool(flag) => *flag,
        Json::Int(number) => *number != 0,
        Json::Float(number) => *number != 0.0,
        Json::Str(text) => !text.is_empty(),
        Json::Arr(items) => !items.is_empty(),
        Json::Obj(fields) => !fields.is_empty(),
    }
}

/// Index just past the ASCII digits of `bytes` that start at `at`.
fn skip_digits(bytes: &[u8], mut at: usize) -> usize {
    while bytes.get(at).is_some_and(u8::is_ascii_digit) {
        at += 1;
    }
    at
}

/// Whether `body` is `digits[.digits][e[sign]digits]` or `.digits[...]` with at least one mantissa digit and nothing else.
fn is_decimal_literal(body: &str) -> bool {
    let bytes = body.as_bytes();
    let int_end = skip_digits(bytes, 0);
    let mut end = int_end;
    let mut mantissa_digits = int_end;
    if bytes.get(end) == Some(&b'.') {
        let fraction_end = skip_digits(bytes, end + 1);
        mantissa_digits += fraction_end - (end + 1);
        end = fraction_end;
    }
    if mantissa_digits == 0 {
        return false;
    }
    if matches!(bytes.get(end), Some(b'e' | b'E')) {
        let mut at = end + 1;
        if matches!(bytes.get(at), Some(b'+' | b'-')) {
            at += 1;
        }
        let exponent_end = skip_digits(bytes, at);
        if exponent_end == at {
            return false;
        }
        end = exponent_end;
    }
    end == bytes.len()
}

/// Python's `float(text)` for ASCII text: surrounding whitespace, a sign, a decimal literal, `inf`, `infinity` or `nan`.
/// Underscores, non-ASCII text and control characters other than the six whitespace characters are [`Num::Unsupported`]: Python
/// reads some of them (`"1_000"`, Unicode digits, `\x1c` as a space) and rejects others, and this module does not tell them apart.
fn py_float_text(text: &str) -> Num {
    let trimmed = text.trim_matches(|c| matches!(c, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c'));
    if !trimmed.is_ascii() || trimmed.contains('_') || trimmed.chars().any(|c| c.is_ascii_control())
    {
        return Num::Unsupported;
    }
    let (negative, body) = match trimmed.as_bytes().first() {
        Some(b'-') => (true, &trimmed[1..]),
        Some(b'+') => (false, &trimmed[1..]),
        _ => (false, trimmed),
    };
    if body.eq_ignore_ascii_case("inf") || body.eq_ignore_ascii_case("infinity") {
        return Num::Value(if negative {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        });
    }
    if body.eq_ignore_ascii_case("nan") {
        return Num::Value(f64::NAN);
    }
    if !is_decimal_literal(body) {
        return Num::Invalid;
    }
    match trimmed.parse::<f64>() {
        Ok(number) => Num::Value(number),
        Err(_) => Num::Invalid,
    }
}

/// `float(value)` of an optional JSON value, `None` being a missing key.
fn num_of(value: Option<&Json>) -> Num {
    match value {
        None | Some(Json::Null | Json::Arr(_) | Json::Obj(_)) => Num::Invalid,
        Some(Json::Bool(flag)) => Num::Value(f64::from(u8::from(*flag))),
        Some(Json::Int(number)) => Num::Value(*number as f64),
        Some(Json::Float(number)) => Num::Value(*number),
        Some(Json::Str(text)) => py_float_text(text),
    }
}

/// Whether `text` could be JSON that Python's `json.loads` accepts although the hand-rolled parser did not: `NaN` or `Infinity`
/// literals, an integer of 19 or more digits (maybe beyond `i64`), or more nesting than the parser's depth limit.
fn ambiguous_json_text(text: &str) -> bool {
    let mut digits = 0usize;
    let mut longest = 0usize;
    let mut nesting = 0usize;
    for byte in text.bytes() {
        digits = if byte.is_ascii_digit() { digits + 1 } else { 0 };
        longest = longest.max(digits);
        nesting += usize::from(matches!(byte, b'[' | b'{'));
    }
    text.contains("NaN") || text.contains("Infinity") || longest >= 19 || nesting > MAX_JSON_DEPTH
}

/// Reads a stored envelope text the way `validate_verdict` does, except for the signature: `json.loads`, then `validate_envelope`
/// (all twelve fields present, string `type`, `schema`, `priority`, `risk_class`, known priority and risk class,
/// `schema == "swarm.v1." + type`, object `payload`), the `gate.verdict` type and the five payload keys.
///
/// The "type has a dot" rule of `validate_envelope` is implied by the exact type `gate.verdict`. A refusal is [`EnvelopeState::Invalid`]
/// (Python's `bad-sig`), unless the text is one Python may read differently ([`EnvelopeState::Unsupported`]).
pub fn parse_envelope(text: &str) -> EnvelopeState {
    let json = match parse_json(text) {
        Ok(json) => json,
        Err(_) if ambiguous_json_text(text) => return EnvelopeState::Unsupported,
        Err(_) => return EnvelopeState::Invalid,
    };
    let Json::Obj(env) = &json else {
        return EnvelopeState::Invalid;
    };
    if ENVELOPE_FIELDS
        .iter()
        .any(|field| obj_get(env, field).is_none())
    {
        return EnvelopeState::Invalid;
    }
    let text_of = |key: &str| match obj_get(env, key) {
        Some(Json::Str(value)) => Some(value.as_str()),
        _ => None,
    };
    let (Some(msg_type), Some(schema), Some(priority), Some(risk_class)) = (
        text_of("type"),
        text_of("schema"),
        text_of("priority"),
        text_of("risk_class"),
    ) else {
        return EnvelopeState::Invalid;
    };
    if !PRIORITIES.contains(&priority)
        || !RISK_CLASSES.contains(&risk_class)
        || schema != format!("swarm.v1.{msg_type}")
        || msg_type != "gate.verdict"
    {
        return EnvelopeState::Invalid;
    }
    let Some(Json::Obj(payload)) = obj_get(env, "payload") else {
        return EnvelopeState::Invalid;
    };
    if PAYLOAD_FIELDS
        .iter()
        .any(|field| obj_get(payload, field).is_none())
    {
        return EnvelopeState::Invalid;
    }
    let field = |key: &str| obj_get(payload, key).cloned().unwrap_or(Json::Null);
    let gate_task = match obj_get(payload, "gate_task") {
        None | Some(Json::Null) => GateTask::None,
        Some(Json::Str(task)) => GateTask::Task(task.clone()),
        Some(_) => GateTask::Unsupported,
    };
    EnvelopeState::Valid(Box::new(ParsedEnvelope {
        gate: field("gate"),
        task_id: field("task_id"),
        verdict: field("verdict"),
        correlation_id: obj_get(env, "correlation_id")
            .cloned()
            .unwrap_or(Json::Null),
        issued_at: num_of(obj_get(payload, "issued_at")),
        expires_s: num_of(obj_get(payload, "expires_s")),
        dry_run: obj_get(payload, "dry_run").is_some_and(py_truthy),
        gate_task,
    }))
}

/// `_latest_rows`: the highest-id row of each gate among the rows of `task`, inserted at or after its last rework unless
/// `include_stale`. Rows of other tasks are ignored and input order does not matter. The result is in the order Python's dict has:
/// a gate keeps the position of its first row.
pub fn latest_rows<'a>(
    task: &TaskFacts,
    rows: &'a [VerdictFacts],
    include_stale: bool,
) -> Vec<(&'a str, &'a VerdictFacts)> {
    let since = if include_stale {
        0.0
    } else {
        task.verdicts_since
    };
    let mut mine: Vec<&VerdictFacts> = rows
        .iter()
        .filter(|facts| facts.row.task_id == task.task_id && facts.row.ts >= since)
        .collect();
    mine.sort_by_key(|facts| facts.row.id);
    let mut out: Vec<(&str, &VerdictFacts)> = Vec::new();
    for facts in mine {
        match out.iter().position(|(gate, _)| *gate == facts.row.gate) {
            Some(at) => out[at].1 = facts,
            None => out.push((facts.row.gate.as_str(), facts)),
        }
    }
    out
}

/// Whether the JSON value is the string `expected`.
fn json_is(value: &Json, expected: &str) -> bool {
    matches!(value, Json::Str(text) if text == expected)
}

/// `_check_row`: how far the row for `gate` can be trusted for `task`, at the host clock `now`.
///
/// Python's order is reproduced: absent, unsigned, bad-sig, mismatch (gate, task, verdict, then correlation), stale, dry-run, expired,
/// fail, ok.
pub fn check_row(
    task: &TaskFacts,
    gate: &str,
    row: Option<&VerdictFacts>,
    now: f64,
) -> Result<CheckOutcome, Unsupported> {
    let Some(facts) = row else {
        return Ok(CheckOutcome::Absent);
    };
    if matches!(facts.row.envelope_json.as_deref(), None | Some("")) {
        return Ok(CheckOutcome::Unsigned);
    }
    let envelope = match (&facts.envelope, facts.sig_ok) {
        (EnvelopeState::Invalid, _) | (_, false) => return Ok(CheckOutcome::BadSig),
        (EnvelopeState::Unsupported, true) => {
            return Err(Unsupported(
                "envelope text outside the provable JSON subset",
            ))
        }
        (EnvelopeState::Valid(envelope), true) => envelope,
    };
    let row_verdict_matches = match (&envelope.verdict, facts.row.verdict.as_deref()) {
        (Json::Str(signed), Some(stored)) => signed == stored,
        (Json::Null, None) => true,
        _ => false,
    };
    if !json_is(&envelope.gate, gate)
        || !json_is(&envelope.task_id, &task.task_id)
        || !row_verdict_matches
        || !json_is(&envelope.correlation_id, &task.correlation_id)
    {
        return Ok(CheckOutcome::Mismatch);
    }
    let issued_at = match envelope.issued_at {
        Num::Value(issued_at) => issued_at,
        Num::Invalid => return Ok(CheckOutcome::Mismatch),
        Num::Unsupported => return Err(Unsupported("issued_at text outside float() subset")),
    };
    if issued_at < task.verdicts_since {
        return Ok(CheckOutcome::Stale);
    }
    if envelope.dry_run && !task.dry_run {
        return Ok(CheckOutcome::DryRun);
    }
    match envelope.expires_s {
        Num::Value(expires_s) => {
            if (issued_at + expires_s).partial_cmp(&now) != Some(Ordering::Greater) {
                return Ok(CheckOutcome::Expired);
            }
        }
        Num::Invalid => return Ok(CheckOutcome::Mismatch),
        Num::Unsupported => return Err(Unsupported("expires_s text outside float() subset")),
    }
    if json_is(&envelope.verdict, "pass") || json_is(&envelope.verdict, "waive") {
        Ok(CheckOutcome::Ok)
    } else {
        Ok(CheckOutcome::Fail)
    }
}

/// Python's `latest.get(gate)` over the output of [`latest_rows`].
fn row_of<'a>(latest: &[(&str, &'a VerdictFacts)], gate: &str) -> Option<&'a VerdictFacts> {
    latest
        .iter()
        .find(|(name, _)| *name == gate)
        .map(|(_, facts)| *facts)
}

/// `missing_gate_reasons`: the required gates that lack a verified, current, unexpired `pass` or `waive` verdict, with the reason,
/// in required order (a repeated gate appears once, at its first position).
pub fn missing_gate_reasons(
    task: &TaskFacts,
    rows: &[VerdictFacts],
    now: f64,
) -> Result<Vec<(String, CheckOutcome)>, Unsupported> {
    let latest = latest_rows(task, rows, false);
    let mut out: Vec<(String, CheckOutcome)> = Vec::new();
    for gate in &task.required_gates {
        let outcome = check_row(task, gate, row_of(&latest, gate), now)?;
        if outcome != CheckOutcome::Ok && !out.iter().any(|(seen, _)| seen == gate) {
            out.push((gate.clone(), outcome));
        }
    }
    Ok(out)
}

/// `latest_verdicts(verified_only=True)`: [`latest_rows`] without the rows whose [`check_row`] is not verified (`ok`, `fail`, `expired`).
pub fn verified_latest_rows<'a>(
    task: &TaskFacts,
    rows: &'a [VerdictFacts],
    include_stale: bool,
    now: f64,
) -> Result<Vec<(&'a str, &'a VerdictFacts)>, Unsupported> {
    let mut out = Vec::new();
    for (gate, facts) in latest_rows(task, rows, include_stale) {
        if check_row(task, gate, Some(facts), now)?.is_verified() {
            out.push((gate, facts));
        }
    }
    Ok(out)
}

/// `unaccepted_gates`: the required gates not in `reasons` whose current row was issued by a gate task whose result was never
/// accepted. A row without `gate_task` is accepted; an unreadable or unverified envelope, an unknown issuer and a state outside
/// [`ACCEPTED_ISSUER_STATES`] are not. `reasons` is the output of [`missing_gate_reasons`] (a gate in it is already reported missing);
/// `issuers` holds the state of every task an envelope may name.
pub fn unaccepted_gates(
    task: &TaskFacts,
    rows: &[VerdictFacts],
    reasons: &[(String, CheckOutcome)],
    issuers: &[IssuerState],
) -> Result<Vec<String>, Unsupported> {
    let latest = latest_rows(task, rows, false);
    let mut out: Vec<String> = Vec::new();
    for gate in &task.required_gates {
        if reasons.iter().any(|(reported, _)| reported == gate) || out.contains(gate) {
            continue;
        }
        let Some(facts) = row_of(&latest, gate) else {
            continue;
        };
        let accepted = match (&facts.envelope, facts.sig_ok) {
            (EnvelopeState::Invalid, _) | (_, false) => false,
            (EnvelopeState::Unsupported, true) => {
                return Err(Unsupported(
                    "envelope text outside the provable JSON subset",
                ))
            }
            (EnvelopeState::Valid(envelope), true) => match &envelope.gate_task {
                GateTask::None => true,
                GateTask::Task(issuer) => issuers
                    .iter()
                    .find(|known| known.task_id == *issuer)
                    .is_some_and(|known| ACCEPTED_ISSUER_STATES.contains(&known.state.as_str())),
                GateTask::Unsupported => {
                    return Err(Unsupported("gate_task is neither null nor a string"))
                }
            },
        };
        if !accepted {
            out.push(gate.clone());
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    const VECTORS: &str = include_str!("../tests/vectors/gate_facts.json");
    const GATES: [&str; 4] = ["review", "quality", "security", "release"];

    fn parsed(text: &str) -> Json {
        parse_json(text).expect("the vector file parses")
    }

    fn field<'a>(value: &'a Json, key: &str) -> &'a Json {
        match value {
            Json::Obj(fields) => {
                obj_get(fields, key).unwrap_or_else(|| panic!("missing key {key}"))
            }
            other => panic!("expected an object for {key}, got {other:?}"),
        }
    }

    fn text(value: &Json) -> &str {
        match value {
            Json::Str(text) => text,
            other => panic!("expected a string, got {other:?}"),
        }
    }

    fn number(value: &Json) -> f64 {
        match value {
            Json::Int(number) => *number as f64,
            Json::Float(number) => *number,
            other => panic!("expected a number, got {other:?}"),
        }
    }

    fn items(value: &Json) -> &[Json] {
        match value {
            Json::Arr(items) => items,
            other => panic!("expected an array, got {other:?}"),
        }
    }

    fn opt_text(value: &Json) -> Option<String> {
        match value {
            Json::Null => None,
            other => Some(text(other).to_string()),
        }
    }

    fn pairs(value: &Json) -> Vec<(String, String)> {
        items(value)
            .iter()
            .map(|pair| {
                let pair = items(pair);
                let second = match &pair[1] {
                    Json::Int(id) => id.to_string(),
                    other => text(other).to_string(),
                };
                (text(&pair[0]).to_string(), second)
            })
            .collect()
    }

    fn task_of(case: &Json) -> TaskFacts {
        let task = field(case, "task");
        TaskFacts::from_row(
            text(field(task, "task_id")),
            text(field(task, "correlation_id")),
            opt_text(field(task, "risk_class")).as_deref(),
            opt_text(field(task, "notes")).as_deref(),
        )
        .expect("every vector task is inside the provable subset")
    }

    fn rows_of(case: &Json) -> Vec<VerdictFacts> {
        items(field(case, "rows"))
            .iter()
            .map(|row| {
                let id = match field(row, "id") {
                    Json::Int(id) => *id,
                    other => panic!("row id {other:?}"),
                };
                let row_facts = VerdictRow {
                    id,
                    task_id: text(field(row, "task_id")).to_string(),
                    gate: text(field(row, "gate")).to_string(),
                    verdict: opt_text(field(row, "verdict")),
                    envelope_json: opt_text(field(row, "envelope_json")),
                    ts: number(field(row, "ts")),
                };
                let sig_ok = matches!(field(row, "sig_ok"), Json::Bool(true));
                VerdictFacts::new(row_facts, sig_ok)
            })
            .collect()
    }

    fn issuers_of(case: &Json) -> Vec<IssuerState> {
        items(field(case, "task_states"))
            .iter()
            .map(|state| IssuerState {
                task_id: text(field(state, "task_id")).to_string(),
                state: text(field(state, "state")).to_string(),
            })
            .collect()
    }

    /// `[gate, id]` pairs of a `latest_rows`-shaped answer.
    fn id_pairs(latest: &[(&str, &VerdictFacts)]) -> Vec<(String, String)> {
        latest
            .iter()
            .map(|(gate, facts)| ((*gate).to_string(), facts.row.id.to_string()))
            .collect()
    }

    /// Replays every query of one case over `rows`, comparing each Python answer exactly.
    fn replay(case: &Json, rows: &[VerdictFacts], label: &str) {
        let name = text(field(case, "name"));
        let task = task_of(case);
        let issuers = issuers_of(case);
        let required: Vec<String> = items(field(case, "required_gates"))
            .iter()
            .map(|gate| text(gate).to_string())
            .collect();
        assert_eq!(
            task.required_gates, required,
            "{name} {label}: required gates"
        );
        for query in items(field(case, "queries")) {
            let now = number(field(query, "now"));
            let at = format!("{name} {label} now={now}");
            let latest = latest_rows(&task, rows, false);
            let checks: Vec<(String, String)> = GATES
                .iter()
                .map(|gate| {
                    let outcome =
                        check_row(&task, gate, row_of(&latest, gate), now).expect("supported");
                    ((*gate).to_string(), outcome.as_str().to_string())
                })
                .collect();
            assert_eq!(
                checks,
                pairs(field(query, "checks")),
                "{at}: _check_row of every gate"
            );
            let reasons = missing_gate_reasons(&task, rows, now).expect("supported");
            let reason_pairs: Vec<(String, String)> = reasons
                .iter()
                .map(|(gate, outcome)| (gate.clone(), outcome.as_str().to_string()))
                .collect();
            assert_eq!(
                reason_pairs,
                pairs(field(query, "reasons")),
                "{at}: missing_gate_reasons"
            );
            let unaccepted = unaccepted_gates(&task, rows, &reasons, &issuers).expect("supported");
            let expected: Vec<String> = items(field(query, "unaccepted"))
                .iter()
                .map(|gate| text(gate).to_string())
                .collect();
            assert_eq!(unaccepted, expected, "{at}: unaccepted_gates");
            let expected_latest = field(query, "latest");
            assert_eq!(
                id_pairs(&latest_rows(&task, rows, false)),
                pairs(field(expected_latest, "current")),
                "{at}: latest_verdicts"
            );
            assert_eq!(
                id_pairs(&latest_rows(&task, rows, true)),
                pairs(field(expected_latest, "all")),
                "{at}: latest_verdicts include_stale"
            );
            for (include_stale, key) in [(false, "verified"), (true, "verified_all")] {
                let verified =
                    verified_latest_rows(&task, rows, include_stale, now).expect("supported");
                assert_eq!(
                    id_pairs(&verified),
                    pairs(field(expected_latest, key)),
                    "{at}: latest_verdicts verified_only, include_stale={include_stale}"
                );
            }
        }
    }

    #[test]
    fn every_python_case_replays_exactly_in_either_row_order() {
        let doc = parsed(VECTORS);
        let cases = items(field(&doc, "cases"));
        assert!(
            cases.len() > 200,
            "the vector file lost cases: {}",
            cases.len()
        );
        for case in cases {
            let rows = rows_of(case);
            replay(case, &rows, "rows in id order");
            let mut reversed = rows;
            reversed.reverse();
            replay(case, &reversed, "rows reversed");
        }
    }

    #[test]
    fn vectors_exercise_every_outcome_and_both_expiry_constants() {
        let doc = parsed(VECTORS);
        let mut seen_checks: Vec<String> = Vec::new();
        let mut seen_reasons: Vec<String> = Vec::new();
        let mut unaccepted_cases = 0;
        let mut distinct_lengths = std::collections::BTreeSet::new();
        for case in items(field(&doc, "cases")) {
            for query in items(field(case, "queries")) {
                seen_checks.extend(
                    pairs(field(query, "checks"))
                        .into_iter()
                        .map(|(_, outcome)| outcome),
                );
                seen_reasons.extend(
                    pairs(field(query, "reasons"))
                        .into_iter()
                        .map(|(_, outcome)| outcome),
                );
                unaccepted_cases += usize::from(!items(field(query, "unaccepted")).is_empty());
                distinct_lengths.insert(items(field(query, "reasons")).len());
            }
        }
        let outcomes = [
            CheckOutcome::Ok,
            CheckOutcome::Fail,
            CheckOutcome::Expired,
            CheckOutcome::Absent,
            CheckOutcome::Unsigned,
            CheckOutcome::BadSig,
            CheckOutcome::Mismatch,
            CheckOutcome::Stale,
            CheckOutcome::DryRun,
        ];
        for outcome in outcomes {
            assert!(
                seen_checks.iter().any(|seen| seen == outcome.as_str()),
                "no vector checks {outcome:?}"
            );
            if outcome != CheckOutcome::Ok {
                assert!(
                    seen_reasons.iter().any(|seen| seen == outcome.as_str()),
                    "no vector reports {outcome:?}"
                );
            }
        }
        assert!(
            unaccepted_cases >= 10,
            "too few unaccepted vectors: {unaccepted_cases}"
        );
        assert!(
            distinct_lengths.len() >= 4,
            "several-gates vectors are missing: {distinct_lengths:?}"
        );
    }

    #[test]
    fn tables_and_constants_match_python() {
        let doc = parsed(VECTORS);
        let constants = field(&doc, "constants");
        assert_eq!(
            number(field(constants, "default_expires_s")),
            f64::from(DEFAULT_EXPIRES_S)
        );
        assert_eq!(
            number(field(constants, "review_expires_s")),
            f64::from(REVIEW_EXPIRES_S)
        );
        let accepted: Vec<&str> = items(field(constants, "accepted_issuer_states"))
            .iter()
            .map(text)
            .collect();
        let mut ours = ACCEPTED_ISSUER_STATES.to_vec();
        ours.sort_unstable();
        assert_eq!(ours, accepted);
        let by_risk = field(constants, "gates_by_risk");
        for risk in ["low", "medium", "high"] {
            let expected: Vec<&str> = items(field(by_risk, risk)).iter().map(text).collect();
            assert_eq!(
                gates_for_risk(risk),
                Some(expected.as_slice()),
                "GATES_BY_RISK[{risk}]"
            );
        }
        let Json::Obj(risks) = by_risk else {
            panic!("gates_by_risk is not an object")
        };
        assert_eq!(risks.len(), 3, "Python gained a risk class");
        assert_eq!(gates_for_risk("extreme"), None);
        let outcomes: Vec<&str> = items(field(constants, "outcomes"))
            .iter()
            .map(text)
            .collect();
        let ours: Vec<&str> = [
            CheckOutcome::Ok,
            CheckOutcome::Fail,
            CheckOutcome::Expired,
            CheckOutcome::Absent,
            CheckOutcome::Unsigned,
            CheckOutcome::BadSig,
            CheckOutcome::Mismatch,
            CheckOutcome::Stale,
            CheckOutcome::DryRun,
        ]
        .iter()
        .map(|outcome| outcome.as_str())
        .collect();
        assert_eq!(ours, outcomes);
        let verified: Vec<&str> = items(field(constants, "verified_outcomes"))
            .iter()
            .map(text)
            .collect();
        let mut ours_verified: Vec<&str> =
            [CheckOutcome::Ok, CheckOutcome::Fail, CheckOutcome::Expired]
                .iter()
                .filter(|outcome| outcome.is_verified())
                .map(|outcome| outcome.as_str())
                .collect();
        ours_verified.sort_unstable();
        assert_eq!(ours_verified, verified);
        for outcome in [
            CheckOutcome::Absent,
            CheckOutcome::Unsigned,
            CheckOutcome::BadSig,
            CheckOutcome::Mismatch,
            CheckOutcome::Stale,
            CheckOutcome::DryRun,
        ] {
            assert!(
                !outcome.is_verified(),
                "{outcome:?} must not count as verified"
            );
        }
    }

    fn envelope_text(issued_at: &str, expires_s: &str, extra: &str) -> String {
        format!(
            r#"{{"msg_id":"m","correlation_id":"c","trace_id":"t","source":"s","target":"A01","type":"gate.verdict","schema":"swarm.v1.gate.verdict","ts":"x","ttl_s":300,"priority":"P1","risk_class":"low","sig":"hmac:AA==","payload":{{"gate":"review","task_id":"T","verdict":"pass","findings":[],"expires_s":{expires_s},"issued_at":{issued_at}{extra}}}}}"#
        )
    }

    fn task() -> TaskFacts {
        TaskFacts::from_row("T", "c", Some("low"), None).expect("plain task")
    }

    fn facts(envelope: &str, sig_ok: bool) -> VerdictFacts {
        let row = VerdictRow {
            id: 1,
            task_id: "T".to_string(),
            gate: "review".to_string(),
            verdict: Some("pass".to_string()),
            envelope_json: Some(envelope.to_string()),
            ts: 10.0,
        };
        VerdictFacts::new(row, sig_ok)
    }

    #[test]
    fn a_value_python_reads_but_this_module_does_not_is_refused_only_when_the_signature_holds() {
        let nan_literal = envelope_text("1000.0", "NaN", "");
        let deep = format!("{}{}", "[".repeat(200), "]".repeat(200));
        let long_int = envelope_text("1000.0", "123456789012345678901234567890", "");
        let cases: Vec<(&str, String, &str)> = vec![
            (
                "issued_at with an underscore",
                envelope_text("\"1_000\"", "86400", ""),
                "float() subset",
            ),
            (
                "issued_at with a Unicode digit",
                envelope_text("\"\u{663}\"", "86400", ""),
                "float() subset",
            ),
            (
                "issued_at with a control character",
                envelope_text("\"\\u001c5\"", "86400", ""),
                "float() subset",
            ),
            (
                "expires_s with an underscore",
                envelope_text("1000.0", "\"8_6\"", ""),
                "float() subset",
            ),
            ("NaN literal", nan_literal, "JSON subset"),
            ("integer beyond i64", long_int, "JSON subset"),
            (
                "deep nesting",
                envelope_text("1000.0", "86400", &format!(",\"deep\":{deep}")),
                "JSON subset",
            ),
        ];
        for (label, envelope, _) in cases {
            let row = facts(&envelope, true);
            let refusal = missing_gate_reasons(&task(), std::slice::from_ref(&row), 1001.0);
            assert!(refusal.is_err(), "{label}: a verified row must be refused");
            let unverified = facts(&envelope, false);
            let answered = missing_gate_reasons(&task(), std::slice::from_ref(&unverified), 1001.0);
            assert_eq!(
                answered,
                Ok(vec![("review".to_string(), CheckOutcome::BadSig)]),
                "{label}: a row whose signature fails is bad-sig whatever it holds"
            );
        }
    }

    #[test]
    fn an_unreadable_expiry_is_only_reached_after_the_earlier_checks() {
        let envelope = envelope_text("\"abc\"", "\"8_6\"", "");
        let row = facts(&envelope, true);
        assert_eq!(
            check_row(&task(), "review", Some(&row), 1001.0),
            Ok(CheckOutcome::Mismatch),
            "issued_at is checked first and is not a number"
        );
        let stale = envelope_text("5.0", "\"8_6\"", "");
        let row = facts(&stale, true);
        let mut late = task();
        late.verdicts_since = 6.0;
        assert_eq!(
            check_row(&late, "review", Some(&row), 1001.0),
            Ok(CheckOutcome::Stale)
        );
        let mut dry = task();
        dry.dry_run = false;
        let flagged = envelope_text("1000.0", "\"8_6\"", ",\"dry_run\":true");
        let row = facts(&flagged, true);
        assert_eq!(
            check_row(&dry, "review", Some(&row), 1001.0),
            Ok(CheckOutcome::DryRun)
        );
    }

    #[test]
    fn a_gate_task_that_is_not_a_string_is_refused_only_for_a_passing_row() {
        let rows = [facts(
            &envelope_text("1000.0", "86400", ",\"gate_task\":5"),
            true,
        )];
        assert_eq!(
            unaccepted_gates(&task(), &rows, &[], &[]),
            Err(Unsupported("gate_task is neither null nor a string"))
        );
        let reported = [("review".to_string(), CheckOutcome::Fail)];
        assert_eq!(
            unaccepted_gates(&task(), &rows, &reported, &[]),
            Ok(Vec::new())
        );
        let unverified = [facts(
            &envelope_text("1000.0", "86400", ",\"gate_task\":5"),
            false,
        )];
        assert_eq!(
            unaccepted_gates(&task(), &unverified, &[], &[]),
            Ok(vec!["review".to_string()])
        );
    }

    #[test]
    fn the_reasons_argument_decides_which_gates_the_issuer_rule_skips() {
        let rows = [facts(
            &envelope_text("1000.0", "86400", ",\"gate_task\":\"G\""),
            true,
        )];
        let failed = [IssuerState {
            task_id: "G".to_string(),
            state: "FAILED".to_string(),
        }];
        let done = [IssuerState {
            task_id: "G".to_string(),
            state: "DONE".to_string(),
        }];
        assert_eq!(
            unaccepted_gates(&task(), &rows, &[], &failed),
            Ok(vec!["review".to_string()])
        );
        assert_eq!(unaccepted_gates(&task(), &rows, &[], &done), Ok(Vec::new()));
        assert_eq!(
            unaccepted_gates(&task(), &rows, &[], &[]),
            Ok(vec!["review".to_string()])
        );
        let reported = [("review".to_string(), CheckOutcome::Expired)];
        assert_eq!(
            unaccepted_gates(&task(), &rows, &reported, &failed),
            Ok(Vec::new())
        );
    }

    #[test]
    fn task_facts_refuse_what_python_would_crash_on_or_read_in_an_unproven_way() {
        let refused: [(&str, Option<&str>, Option<&str>); 9] = [
            (
                "verdicts_since text",
                Some("low"),
                Some(r#"{"verdicts_since":"abc"}"#),
            ),
            (
                "verdicts_since list",
                Some("low"),
                Some(r#"{"verdicts_since":[1]}"#),
            ),
            ("gates string", Some("low"), Some(r#"{"gates":"review"}"#)),
            ("gates number", Some("low"), Some(r#"{"gates":5}"#)),
            (
                "gates with a number",
                Some("low"),
                Some(r#"{"gates":["review",1]}"#),
            ),
            ("unknown risk class", Some("extreme"), None),
            ("risk class null", None, Some("{}")),
            ("notes with NaN", Some("low"), Some(r#"{"a":NaN}"#)),
            (
                "notes with a huge integer",
                Some("low"),
                Some(r#"{"a":12345678901234567890}"#),
            ),
        ];
        for (label, risk, notes) in refused {
            assert!(
                TaskFacts::from_row("T", "c", risk, notes).is_err(),
                "{label}"
            );
        }
        let overridden =
            TaskFacts::from_row("T", "c", Some("extreme"), Some(r#"{"gates":["quality"]}"#));
        assert_eq!(
            overridden.map(|task| task.required_gates),
            Ok(vec!["quality".to_string()])
        );
        let numeric =
            TaskFacts::from_row("T", "c", Some("low"), Some(r#"{"verdicts_since":"12.5"}"#));
        assert_eq!(numeric.map(|task| task.verdicts_since), Ok(12.5));
    }

    #[test]
    fn python_float_text_grammar() {
        let values: [(&str, f64); 10] = [
            ("1000.5", 1000.5),
            (" 1000.5\n", 1000.5),
            ("1e3", 1000.0),
            ("+.5e3", 500.0),
            ("1000.", 1000.0),
            ("-1000.5", -1000.5),
            ("inf", f64::INFINITY),
            ("-Infinity", f64::NEG_INFINITY),
            ("1e999", f64::INFINITY),
            ("\t7\u{b}", 7.0),
        ];
        for (input, expected) in values {
            assert_eq!(py_float_text(input), Num::Value(expected), "{input:?}");
        }
        assert!(matches!(py_float_text("NaN"), Num::Value(number) if number.is_nan()));
        for input in [
            "", "   ", "abc", "0x10", "1e", "1.5.5", ".", "+", "--1", "1 2", "e5", "1e+",
            "infinit", "nann",
        ] {
            assert_eq!(py_float_text(input), Num::Invalid, "{input:?}");
        }
        for input in ["1_000", "\u{663}", "\u{1c}5", "1\u{85}", "5\x00"] {
            assert_eq!(py_float_text(input), Num::Unsupported, "{input:?}");
        }
    }

    #[test]
    fn envelope_text_that_is_not_json_is_invalid_unless_python_might_read_it() {
        for garbage in [
            "{not json",
            "[1,",
            "",
            "nul",
            "{\"a\":1} trailing",
            "01",
            "'single'",
        ] {
            assert_eq!(
                parse_envelope(garbage),
                EnvelopeState::Invalid,
                "{garbage:?}"
            );
        }
        for ambiguous in [
            "{\"a\":NaN}",
            "{\"a\":-Infinity}",
            "[123456789012345678901]",
        ] {
            assert_eq!(
                parse_envelope(ambiguous),
                EnvelopeState::Unsupported,
                "{ambiguous:?}"
            );
        }
        assert!(matches!(
            parse_envelope(&envelope_text("1.0", "5", "")),
            EnvelopeState::Valid(_)
        ));
    }
}
