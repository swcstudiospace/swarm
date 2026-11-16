//! Pure planner of the `--ingest` route: `ingest()` of `scripts/orch_status.py` with `apply_result` and `reconcile` of `swarm/results.py`
//! (and `apply_result` alone for the headless runner), as one ordered list of store operations.
//!
//! [`plan_ingest`] reads the facts of one call (the validated `task.result` or the validation failure, the rows of the task's correlation, the
//! verdict rows with the host's signature answer, the lease start, the host-computed `result_digest`, `now`, the mode and `--advisory`) and
//! returns what Python does, in Python's order: the auto-claim of a PLANNED or RETRY task (two audited transitions), the transition the result
//! reports, the `notes` merge, the artifact rows and the `outputs` append, the gate-target feedback, the replay key as the LAST write, the
//! events (`task.claimed`, `task.result.rejected`, `task.result.deduplicated`) and, in ingest mode, everything [`plan_reconcile`] plans on the
//! store as those writes leave it (`escalation.request`, `task.approval.skipped`, the reworks and approvals). It opens no database and reads no
//! clock, environment or key. The host applies the whole list in one `BEGIN IMMEDIATE` transaction and replays the [`IngestOp::Emit`]s after the
//! commit.
//!
//! # Why it simulates
//!
//! Python re-reads the store between writes: the auto-claim moves the task before the gate check under the lock reads it, the lease start a gate
//! check reads is the claim the plan has just made, a rework of the reconcile stage sees the `notes` this result wrote (the clone of a gate task
//! keeps its `ingest_keys` and `feedback`). The planner keeps an in-memory copy of the correlation's rows, applies each operation to it as it
//! emits it, and reads every later decision from that copy. Python commits the claim, the result and the reconcile in separate transactions; the
//! host commits the list once. The two differ only in the outcome of a crash between them, and in nothing a reply can show: a refusal that follows
//! a write has no reachable path (see below) and is a [`IngestError::NotStarted`] if it ever happens.
//!
//! # What the list means
//!
//! * [`IngestOp::Transition`] is `store.transition`, the plan of [`decide_transition`] for the row as the earlier operations left it. The
//!   audit `actor` is in the plan: the agent id for a claim and for the reported state, `A01` for the FAILED of a headless rejection.
//! * [`IngestOp::SetNotes`] is `store.set_notes(task, **entries)`: `dict.update` of the pairs in order (an existing key keeps its position, a new
//!   one is appended) and a rewrite of the column as `json.dumps(notes)`.
//! * [`IngestOp::AddArtifact`] is `store.add_artifact`: one `artifacts` row with `ts = now` and the entry
//!   `{kind, uri, version, digest}` appended to the task's `outputs`.
//! * [`IngestOp::AppendFeedback`] is `store.append_feedback(target, entry)` on the TARGET row (`notes.feedback` is created as `[]` when absent).
//! * [`IngestOp::CreateTask`] and [`IngestOp::SetDependsOn`] are the reconcile stage's `store.create` and `store.update(depends_on=...)`.
//! * [`IngestOp::Emit`] is a call of `emit`; the host replays it after the commit, in list order.
//!
//! # The replay key (D-P24)
//!
//! An ingest records `{task_id, attempt, digest, reply}` in `notes.ingest_keys` (newest [`INGEST_KEYS_BOUND`] kept) as the last write of the
//! transaction that holds the transition, the notes, the artifact rows and the feedback. A call whose `(task, attempt, digest)` is already
//! recorded and whose move the state machine accepts (a heartbeat is always accepted) writes nothing and emits `task.result.deduplicated`; its
//! reply is the stored one. Headless results are never keyed. The digest is computed by the host (this crate has no hash).
//!
//! # Refusals
//!
//! A refusal Python raises is [`IngestError::Refused`] with its code, message and `details`, plus the events Python emitted before it raised
//! (`task.result.rejected` in ingest mode, for every `E-CONTRACT`). The refusals of `apply_result` all happen before a write or inside a
//! transaction that rolls back, so a refusal leaves the store unchanged; one that would follow a write is not planned.
//!
//! Everything this planner cannot prove byte-identical is [`IngestError::NotStarted`] before anything is written, and Python answers:
//!
//! * a validation failure that embeds a `repr` Rust does not format ([`ResultError::NotStarted`]);
//! * a `needs` that is not text (Python prints `str(list)`), an `agent_id` that is truthy and not text, an output field that is not text;
//! * a `notes.gate` that is truthy and not text, a `notes.gate_for` that is not a list of text (when the task is a gate task), a verdict
//!   entry that is not an object or whose `verdict` or `findings` has another type, a verdict, gate or target name whose `repr` is not plain
//!   ASCII (printable, no quote, no backslash) when a message embeds it;
//! * a feedback target outside the correlation (Python raises `E-INPUT` inside the transaction), a `notes.feedback` that is not a list, an
//!   `outputs`, `depends_on` or `notes` column Rust cannot read, a task state outside the fourteen;
//! * a verdict envelope outside the provable JSON subset with a valid signature, an `issued_at` that Python's `float()` reads by a rule Rust
//!   does not reproduce;
//! * every refusal of [`decide_transition`] and of [`plan_reconcile`] that is not Python's own answer.
//!
//! # Clock
//!
//! One `now` stands for every `time.time()` Python reads: `updated_at`, every audit `ts`, every artifact `ts` and the lease start a gate check
//! reads for a claim this plan has just made.

use std::fmt;

use gsd_codes::ErrorCode;

use crate::gate_facts::{EnvelopeState, GateTask, Num, VerdictFacts};
use crate::pydumps::to_python_dumps;
use crate::reconcile_plan::{plan_reconcile, EmitEvent, NewTask, ReconcileFacts, ReconcileOp};
use crate::result_parse::ResultError;
use crate::transition_plan::{
    decide_transition, legal_targets, Facts, Request, TransitionError, TransitionPlan, WriteState,
};
use crate::{jstr, obj_get, parse_json, Fields, Json, SeamError};

/// How many replay keys `notes.ingest_keys` keeps (`INGEST_KEYS_BOUND`): the newest ones.
pub const INGEST_KEYS_BOUND: usize = 32;

/// Longest `reason` a transition carries when it comes from a result or a rejection (`[:500]`).
pub const REASON_CAP: usize = 500;

/// The states in which a dependency counts as satisfied (`SATISFIED_STATES`).
pub const SATISFIED_STATES: [&str; 3] = ["IN_REVIEW", "APPROVED", "DONE"];

/// The only agent verdicts that pass a target, after strip and lower-case (`PASS_VERDICTS`).
pub const PASS_VERDICTS: [&str; 2] = ["pass", "approve"];

/// `SEVERITIES` of `swarm/gates.py`, lowest first.
pub const SEVERITIES: [&str; 5] = ["info", "minor", "major", "critical", "blocker"];

/// `BLOCKING_SEVERITY`: a finding at or above this severity fails its target.
pub const BLOCKING_SEVERITY: &str = "major";

/// Which caller applies the result.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// `orch_status.py --ingest`: auto-claim, the BLOCKED and dependency refusals, heartbeats, replay keys, then `reconcile`.
    Ingest,
    /// The headless runner: the session is over, an IN_PROGRESS result and a refusal fail the attempt; no keys, no reconcile.
    Headless,
}

impl Mode {
    /// The `mode` string of the events.
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::Ingest => "ingest",
            Mode::Headless => "headless",
        }
    }
}

/// Everything the host reads before it asks for a plan, from one snapshot taken inside the write transaction.
#[derive(Debug, Clone)]
pub struct IngestFacts {
    /// Which caller applies the result.
    pub mode: Mode,
    /// The task the result is for (the row `apply_result` is given).
    pub task_id: String,
    /// The `agent_id` `apply_result` is given: the audit actor of the claim and of the reported state, and the `producer` of the artifacts.
    /// The ingest route computes it with [`ingest_agent_id`].
    pub agent_id: String,
    /// The validated `task.result` ([`crate::result_parse::validate_result`]), or why it is not one. An invalid result is rejected: ingest emits
    /// `task.result.rejected` and refuses, headless emits it and fails the attempt.
    pub result: Result<Json, ResultError>,
    /// The `meta` object stored beside the result (`{"ingest": str(path)}` for an ingest).
    pub meta: Json,
    /// `result_digest(result)`, the lowercase hex SHA-256 of the canonical result. Required in ingest mode with a valid result.
    pub digest: Option<String>,
    /// `--advisory`: `reconcile(..., approve=False)`. Ignored in headless mode.
    pub advisory: bool,
    /// The `ts` of every CLAIMED transition of the task, in `transitions.id` order (`_lease_start` reads the last).
    pub claims: Vec<f64>,
    /// The correlation as `reconcile` reads it: every `tasks` row (the task's own among them), the verdict rows of the correlation AND of every
    /// gate target the task names (a gate check reads them), the tasks outside the correlation, and the key policy.
    pub reconcile: ReconcileFacts,
}

/// One step of the plan.
#[derive(Debug, Clone, PartialEq)]
pub enum IngestOp {
    /// `store.transition(task, ...)`: the plan of [`decide_transition`] for the row as the earlier operations left it.
    Transition {
        /// The task.
        task_id: String,
        /// The conditional `UPDATE`, its audit rows and effects.
        plan: TransitionPlan,
    },
    /// `store.set_notes(task, **entries)`.
    SetNotes {
        /// The task.
        task_id: String,
        /// The pairs to merge, in order.
        entries: Fields,
    },
    /// `store.add_artifact(task, ...)`.
    AddArtifact(Artifact),
    /// `store.append_feedback(task, entry)`.
    AppendFeedback {
        /// The target task (not the gate task).
        task_id: String,
        /// The entry appended to `notes.feedback`.
        entry: Json,
    },
    /// `store.create(...)` of a gate rerun (reconcile stage).
    CreateTask(NewTask),
    /// `store.update(task, depends_on=...)` (reconcile stage): the whole new list.
    SetDependsOn {
        /// The task.
        task_id: String,
        /// The new `depends_on`.
        depends_on: Vec<String>,
    },
    /// An `emit` call.
    Emit(EmitEvent),
}

/// The arguments of one `store.add_artifact` call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Artifact {
    /// The task the row belongs to.
    pub task_id: String,
    /// `kind` (`artifact` when the output has none).
    pub kind: String,
    /// `uri` (empty when the output has none).
    pub uri: String,
    /// `version` (`1` when the output has none).
    pub version: String,
    /// `digest` (empty when the output has none).
    pub digest: String,
    /// `producer`: the agent id.
    pub producer: String,
}

/// What the call does and returns.
#[derive(Debug, Clone, PartialEq)]
pub struct IngestPlan {
    /// The operations in Python's order.
    pub ops: Vec<IngestOp>,
    /// The value `apply_result` returns: the task's new state, the stored reply of a replay, or the state a headless rejection left.
    pub reply: String,
    /// The list `reconcile` returns (empty in headless mode).
    pub notes_log: Vec<String>,
    /// The task's state before the call (`task["state"]` of the snapshot): the `from` of the inline transition.
    pub before: String,
    /// The task's state after the call (`store.get(tid)["state"]` once `reconcile` has run): the `to` of the inline transition.
    pub after: String,
}

/// A refusal Python raises: its code, message and `details`, and the events it emitted before raising.
#[derive(Debug, Clone, PartialEq)]
pub struct Refusal {
    /// The `SwarmError`.
    pub error: SeamError,
    /// `task.result.rejected` for an `E-CONTRACT` in ingest mode, in emission order; empty otherwise.
    pub events: Vec<EmitEvent>,
}

/// Why there is no plan.
#[derive(Debug, Clone, PartialEq)]
pub enum IngestError {
    /// Nothing was decided or written: the route reports `write_state = "not_started"` and Python answers.
    NotStarted(String),
    /// Python's own refusal; the store is unchanged.
    Refused(Refusal),
}

impl fmt::Display for IngestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            IngestError::NotStarted(why) => write!(f, "ingest not started: {why}"),
            IngestError::Refused(refusal) => f.write_str(refusal.error.message()),
        }
    }
}

impl std::error::Error for IngestError {}

type Planned<T> = Result<T, IngestError>;

fn not_started<T>(why: impl Into<String>) -> Planned<T> {
    Err(IngestError::NotStarted(why.into()))
}

static NULL: Json = Json::Null;

fn column<'a>(row: &'a Fields, name: &str) -> &'a Json {
    obj_get(row, name).unwrap_or(&NULL)
}

/// Python truthiness of a JSON value.
fn truthy(value: &Json) -> bool {
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

fn set(fields: &mut Fields, key: &str, value: Json) {
    match fields.iter_mut().find(|(name, _)| name == key) {
        Some(entry) => entry.1 = value,
        None => fields.push((key.to_string(), value)),
    }
}

fn text_column(row: &Fields, name: &str) -> Planned<String> {
    match column(row, name) {
        Json::Str(text) => Ok(text.clone()),
        _ => not_started(format!("{name} is not text")),
    }
}

/// `TaskStore._row`'s `notes_json`: the parsed object, `{}` for an absent, empty or non-object value; JSON Rust cannot parse is refused.
fn notes_of(row: &Fields) -> Planned<Fields> {
    match column(row, "notes") {
        Json::Null => Ok(Vec::new()),
        Json::Str(text) if text.is_empty() => Ok(Vec::new()),
        Json::Str(text) => match parse_json(text) {
            Ok(Json::Obj(fields)) => Ok(fields),
            Ok(_) => Ok(Vec::new()),
            Err(_) => not_started("notes is not JSON that Rust can read"),
        },
        _ => not_started("notes is not text"),
    }
}

/// A JSON list column (`outputs`, `depends_on`): `json.loads(value or "[]")`, which must be a list.
fn list_column(row: &Fields, name: &str) -> Planned<Vec<Json>> {
    let parsed = match column(row, name) {
        Json::Null => return Ok(Vec::new()),
        Json::Str(text) if text.is_empty() => return Ok(Vec::new()),
        Json::Str(text) => parse_json(text),
        _ => return not_started(format!("{name} is not text")),
    };
    match parsed {
        Ok(Json::Arr(items)) => Ok(items),
        Ok(_) => not_started(format!("{name} is not a list")),
        Err(_) => not_started(format!("{name} is not JSON that Rust can read")),
    }
}

fn int_column(row: &Fields, name: &str) -> Planned<i64> {
    match column(row, name) {
        Json::Int(number) => Ok(*number),
        _ => not_started(format!("{name} is not an integer")),
    }
}

fn find_row<'a>(rows: &'a [Fields], task_id: &str) -> Planned<&'a Fields> {
    rows.iter()
        .find(|row| matches!(column(row, "task_id"), Json::Str(id) if id == task_id))
        .map_or_else(|| not_started(format!("unknown task {task_id}")), Ok)
}

fn find_row_mut<'a>(rows: &'a mut [Fields], task_id: &str) -> Planned<&'a mut Fields> {
    rows.iter_mut()
        .find(|row| matches!(column(row, "task_id"), Json::Str(id) if id == task_id))
        .map_or_else(|| not_started(format!("unknown task {task_id}")), Ok)
}

/// Python's `repr` of a list of plain strings, `['a', 'b']`; a name whose `repr` needs an escape or another quote is refused.
fn py_repr_list(names: &[String]) -> Planned<String> {
    if !names.iter().all(|name| plain_text(name)) {
        return not_started("a name needs an escaped repr");
    }
    let quoted: Vec<String> = names.iter().map(|name| format!("'{name}'")).collect();
    Ok(format!("[{}]", quoted.join(", ")))
}

/// Printable ASCII without a quote or a backslash: the strings whose `repr` is the text between single quotes.
fn plain_text(text: &str) -> bool {
    text.chars()
        .all(|ch| (' '..='~').contains(&ch) && ch != '\'' && ch != '\\')
}

/// Python's `str.isspace` for the characters `str.strip()` removes.
fn py_space(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\u{1c}'..='\u{1f}')
}

fn json_is(value: &Json, expected: &str) -> bool {
    matches!(value, Json::Str(text) if text == expected)
}

/// `agent_verdict`: `str(entry.get("verdict") or "").strip().lower()`.
fn agent_verdict(entry: &Json) -> Planned<String> {
    let Json::Obj(fields) = entry else {
        return not_started("a verdicts entry is not an object");
    };
    match obj_get(fields, "verdict") {
        None => Ok(String::new()),
        Some(value) if !truthy(value) => Ok(String::new()),
        Some(Json::Str(text)) => Ok(text.trim_matches(py_space).to_lowercase()),
        Some(_) => not_started("a verdict is not text"),
    }
}

/// `agent_failed`: fail closed unless the verdict is one of [`PASS_VERDICTS`].
fn agent_failed(entry: &Json) -> Planned<bool> {
    Ok(!PASS_VERDICTS.contains(&agent_verdict(entry)?.as_str()))
}

/// `_counts_major`: a finding counts as major or worse unless its severity is a known one below major.
fn counts_major(finding: &Json) -> bool {
    let Json::Obj(fields) = finding else {
        return true;
    };
    let Some(Json::Str(severity)) = obj_get(fields, "severity") else {
        return true;
    };
    let lowered = severity.to_lowercase();
    match SEVERITIES.iter().position(|known| *known == lowered) {
        Some(rank) => SEVERITIES
            .iter()
            .position(|known| *known == BLOCKING_SEVERITY)
            .is_some_and(|blocking| rank >= blocking),
        None => true,
    }
}

/// `agent_findings(entry)[0]`: the entry's findings, with one synthesized major finding when the entry fails without one.
fn agent_findings(entry: &Json) -> Planned<Vec<Json>> {
    let Json::Obj(fields) = entry else {
        return not_started("a verdicts entry is not an object");
    };
    let mut items = match obj_get(fields, "findings") {
        None => Vec::new(),
        Some(value) if !truthy(value) => Vec::new(),
        Some(Json::Arr(items)) => items.clone(),
        Some(_) => return not_started("findings is not a list"),
    };
    if !agent_failed(entry)? || items.iter().any(counts_major) {
        return Ok(items);
    }
    let verdict = agent_verdict(entry)?;
    let shown = if verdict.is_empty() {
        "no verdict".to_string()
    } else {
        verdict
    };
    if !plain_text(&shown) {
        return not_started("a verdict needs an escaped repr");
    }
    let why = if items.is_empty() {
        "without findings"
    } else {
        "without a major finding"
    };
    items.push(Json::Obj(vec![
        ("severity".to_string(), jstr("major")),
        ("kind".to_string(), jstr("agent-verdict")),
        (
            "summary".to_string(),
            Json::Str(format!("gate agent reported '{shown}' {why}")),
        ),
    ]));
    Ok(items)
}

/// The `agent_id` `orch_status.ingest` passes to `apply_result`: `result.get("agent_id") or task["agent_id"] or "agent"`.
///
/// # Errors
///
/// [`IngestError::NotStarted`] when the first truthy candidate is not text (Python would bind it as a number or crash).
pub fn ingest_agent_id(result: &Json, task: &Fields) -> Result<String, IngestError> {
    let named = match result {
        Json::Obj(fields) => obj_get(fields, "agent_id").unwrap_or(&NULL),
        _ => &NULL,
    };
    for candidate in [named, column(task, "agent_id")] {
        match candidate {
            Json::Str(text) if !text.is_empty() => return Ok(text.clone()),
            other if truthy(other) => return not_started("an agent id that is not text"),
            _ => {}
        }
    }
    Ok("agent".to_string())
}

fn event(event_type: &str, payload: Fields) -> EmitEvent {
    EmitEvent {
        event_type: event_type.to_string(),
        payload,
        task_id: None,
    }
}

fn rejected_event(task_id: &str, mode: Mode, reason: &str) -> EmitEvent {
    event(
        "task.result.rejected",
        vec![
            ("task_id".to_string(), jstr(task_id)),
            ("mode".to_string(), jstr(mode.as_str())),
            ("reason".to_string(), jstr(reason)),
        ],
    )
}

/// `e.get("attempt") == attempt` for a stored key entry: an int, a float or a bool compares by value.
fn attempt_matches(entry: &Fields, attempt: i64) -> bool {
    match obj_get(entry, "attempt") {
        Some(Json::Int(number)) => *number == attempt,
        Some(Json::Float(number)) => *number == attempt as f64,
        Some(Json::Bool(flag)) => i64::from(*flag) == attempt,
        _ => false,
    }
}

/// `notes.ingest_keys` as `_stored_keys` reads it: the object entries of a list, nothing for another type.
fn stored_keys(notes: &Fields) -> Vec<&Fields> {
    match obj_get(notes, "ingest_keys") {
        Some(Json::Arr(items)) => items
            .iter()
            .filter_map(|item| match item {
                Json::Obj(fields) => Some(fields),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

/// The in-memory copy of the correlation plus the plan built so far.
struct Sim<'a> {
    facts: &'a IngestFacts,
    now: f64,
    rows: Vec<Fields>,
    claims: Vec<f64>,
    ops: Vec<IngestOp>,
    wrote: bool,
}

impl<'a> Sim<'a> {
    fn new(facts: &'a IngestFacts, now: f64) -> Planned<Self> {
        find_row(&facts.reconcile.tasks, &facts.task_id)?;
        Ok(Self {
            facts,
            now,
            rows: facts.reconcile.tasks.clone(),
            claims: facts.claims.clone(),
            ops: Vec::new(),
            wrote: false,
        })
    }

    fn tid(&self) -> &'a str {
        self.facts.task_id.as_str()
    }

    fn row(&self) -> Planned<&Fields> {
        find_row(&self.rows, self.tid())
    }

    fn state(&self) -> Planned<String> {
        text_column(self.row()?, "state")
    }

    fn notes(&self) -> Planned<Fields> {
        notes_of(self.row()?)
    }

    /// The SwarmError `E-CONTRACT` with `task_id` (and the extra details) as the refusals of `results.py` raise it.
    fn contract(&self, message: &str, extra: Fields) -> SeamError {
        let mut details = vec![("task_id".to_string(), jstr(self.tid()))];
        details.extend(extra);
        SeamError::new(ErrorCode::Contract, message).with_details(details)
    }

    /// Python's raise: ingest records the rejection of an `E-CONTRACT` first (`ingest()` calls `reject`).
    fn refuse(&self, error: SeamError) -> IngestError {
        if self.wrote {
            return IngestError::NotStarted("a refusal that follows a write".to_string());
        }
        let events = if self.facts.mode == Mode::Ingest && error.code() == ErrorCode::Contract {
            vec![rejected_event(self.tid(), self.facts.mode, error.message())]
        } else {
            Vec::new()
        };
        IngestError::Refused(Refusal { error, events })
    }

    fn transition_error(&self, error: TransitionError) -> IngestError {
        match error.write_state {
            WriteState::NotStarted => IngestError::NotStarted(error.message),
            WriteState::Final => self.refuse(
                SeamError::unprefixed(error.code, error.message).with_details(error.details),
            ),
        }
    }

    /// Records an operation: applies it to the copy of the rows and appends it to the list.
    fn push(&mut self, op: IngestOp) -> Planned<()> {
        let now = self.now;
        match &op {
            IngestOp::Transition { task_id, plan } => {
                let row = find_row_mut(&mut self.rows, task_id)?;
                set(row, "state", jstr(&plan.to));
                for (name, value) in &plan.columns {
                    match (name.as_str(), value) {
                        ("state", _) => {}
                        (_, Json::Obj(_)) => set(row, name, Json::Str(to_python_dumps(value))),
                        _ => set(row, name, value.clone()),
                    }
                }
                set(row, "updated_at", Json::Float(now));
                if plan.audit.iter().any(|step| step.to == "CLAIMED") {
                    self.claims.push(now);
                }
            }
            IngestOp::SetNotes { task_id, entries } => {
                let row = find_row_mut(&mut self.rows, task_id)?;
                let mut notes = notes_of(row)?;
                for (key, value) in entries {
                    set(&mut notes, key, value.clone());
                }
                set(row, "notes", Json::Str(to_python_dumps(&Json::Obj(notes))));
                set(row, "updated_at", Json::Float(now));
            }
            IngestOp::AddArtifact(artifact) => {
                let row = find_row_mut(&mut self.rows, &artifact.task_id)?;
                let mut outputs = list_column(row, "outputs")?;
                outputs.push(Json::Obj(vec![
                    ("kind".to_string(), jstr(&artifact.kind)),
                    ("uri".to_string(), jstr(&artifact.uri)),
                    ("version".to_string(), jstr(&artifact.version)),
                    ("digest".to_string(), jstr(&artifact.digest)),
                ]));
                set(
                    row,
                    "outputs",
                    Json::Str(to_python_dumps(&Json::Arr(outputs))),
                );
                set(row, "updated_at", Json::Float(now));
            }
            IngestOp::AppendFeedback { task_id, entry } => {
                let row = find_row_mut(&mut self.rows, task_id)
                    .or_else(|_| not_started("a feedback target outside the correlation"))?;
                let mut notes = notes_of(row)?;
                match notes.iter_mut().find(|(key, _)| key == "feedback") {
                    Some((_, Json::Arr(items))) => items.push(entry.clone()),
                    Some(_) => return not_started("notes.feedback is not a list"),
                    None => notes.push(("feedback".to_string(), Json::Arr(vec![entry.clone()]))),
                }
                set(row, "notes", Json::Str(to_python_dumps(&Json::Obj(notes))));
                set(row, "updated_at", Json::Float(now));
            }
            IngestOp::CreateTask(_) | IngestOp::SetDependsOn { .. } | IngestOp::Emit(_) => {}
        }
        self.wrote = true;
        self.ops.push(op);
        Ok(())
    }

    /// `store.transition(tid, to, actor=actor, reason=reason)` on the row as the earlier operations left it.
    fn transition(&mut self, to: &str, actor: &str, reason: &str) -> Planned<()> {
        let facts = Facts {
            task: Some(self.row()?.clone()),
            approval: None,
        };
        let request = Request {
            task_id: self.tid().to_string(),
            to: to.to_string(),
            actor: actor.to_string(),
            reason: reason.to_string(),
            manual: false,
        };
        let plan = decide_transition(&facts, &request, self.now)
            .map_err(|error| self.transition_error(error))?;
        self.push(IngestOp::Transition {
            task_id: self.tid().to_string(),
            plan,
        })
    }

    /// `reject` of a headless call: the event, then FAILED while the row is still IN_PROGRESS on the attempt that produced the result.
    fn reject_headless(&mut self, reason: &str, attempt: &Json) -> Planned<String> {
        let rejected = rejected_event(self.tid(), self.facts.mode, reason);
        self.push(IngestOp::Emit(rejected))?;
        let state = self.state()?;
        if state == "IN_PROGRESS" && column(self.row()?, "attempt") == attempt {
            let cut: String = reason.chars().take(REASON_CAP).collect();
            self.transition("FAILED", "A01", &cut)?;
            return Ok("FAILED".to_string());
        }
        Ok(state)
    }

    /// `_refuse_gate`: headless rejects the attempt, ingest raises.
    fn refuse_gate(&mut self, error: SeamError, attempt: &Json) -> Planned<String> {
        match self.facts.mode {
            Mode::Headless => self.reject_headless(error.message(), attempt),
            Mode::Ingest => Err(self.refuse(error)),
        }
    }

    /// An invalid or missing result: ingest refuses after recording the rejection, headless rejects the attempt.
    fn invalid_result(&mut self, error: &SeamError) -> Planned<String> {
        match self.facts.mode {
            Mode::Headless => {
                let attempt = column(self.row()?, "attempt").clone();
                self.reject_headless(error.message(), &attempt)
            }
            Mode::Ingest => {
                let with_task = SeamError::unprefixed(error.code(), error.message())
                    .with_details(vec![("task_id".to_string(), jstr(self.tid()))]);
                Err(self.refuse(with_task))
            }
        }
    }

    /// `notes.gate` when it is truthy (the task is a gate task); a truthy value that is not text is refused.
    fn gate_of_task(&self) -> Planned<Option<String>> {
        let notes = self.notes()?;
        match obj_get(&notes, "gate") {
            None => Ok(None),
            Some(value) if !truthy(value) => Ok(None),
            Some(Json::Str(gate)) => Ok(Some(gate.clone())),
            Some(_) => not_started("notes.gate is not text"),
        }
    }

    /// `notes.gate_for`: absent is empty; anything but a list of text is refused.
    fn gate_for(&self) -> Planned<Vec<String>> {
        let notes = self.notes()?;
        match obj_get(&notes, "gate_for") {
            None => Ok(Vec::new()),
            Some(Json::Arr(items)) => items
                .iter()
                .map(|item| match item {
                    Json::Str(target) => Ok(target.clone()),
                    _ => not_started("notes.gate_for holds a non-string"),
                })
                .collect(),
            Some(_) => not_started("notes.gate_for is not a list"),
        }
    }

    /// `gate_verdict_value_since`: the signed verdict of the newest row of `gate` on `target` that `gate_task` issued at or after `since`.
    /// A `null` verdict is Python's `None`, which `gate_verdict_since` reads as "no row".
    fn verdict_since(&self, target: &str, gate: &str, since: f64) -> Planned<Option<Json>> {
        let mut rows: Vec<&VerdictFacts> = self
            .facts
            .reconcile
            .verdicts
            .iter()
            .filter(|facts| facts.row.task_id == target && facts.row.gate == gate)
            .collect();
        rows.sort_by_key(|facts| std::cmp::Reverse(facts.row.id));
        for facts in rows {
            let envelope = match (&facts.envelope, facts.sig_ok) {
                (_, false) | (EnvelopeState::Invalid, _) => continue,
                (EnvelopeState::Unsupported, true) => {
                    return not_started("an envelope outside the provable JSON subset")
                }
                (EnvelopeState::Valid(envelope), true) => envelope,
            };
            let issued_by_this_task =
                matches!(&envelope.gate_task, GateTask::Task(issuer) if issuer == self.tid());
            if !(json_is(&envelope.gate, gate)
                && json_is(&envelope.task_id, target)
                && issued_by_this_task)
            {
                continue;
            }
            match envelope.issued_at {
                Num::Value(issued) if issued >= since => {
                    return Ok(match &envelope.verdict {
                        Json::Null => None,
                        other => Some(other.clone()),
                    });
                }
                Num::Value(_) | Num::Invalid => {}
                Num::Unsupported => return not_started("issued_at outside the float() subset"),
            }
        }
        Ok(None)
    }

    /// `_gate_script_missing`: the targets without a verifying row that the gate script issued during the current lease.
    fn script_missing(&self, gate: &str, targets: &[String]) -> Planned<Vec<String>> {
        let state = self.state()?;
        let since = match (state.as_str(), self.claims.last()) {
            ("PLANNED" | "RETRY", _) | (_, None) => return Ok(targets.to_vec()),
            (_, Some(since)) => *since,
        };
        let mut missing = Vec::new();
        for target in targets {
            if self.verdict_since(target, gate, since)?.is_none() {
                missing.push(target.clone());
            }
        }
        Ok(missing)
    }

    /// `_review_verdict_mismatch`: the targets whose agent verdict fails while the review row of this lease is not `fail`.
    fn review_mismatch(
        &self,
        gate: &str,
        targets: &[String],
        result: &Fields,
    ) -> Planned<Vec<String>> {
        let mut failing: Vec<String> = Vec::new();
        if let Some(Json::Obj(verdicts)) = obj_get(result, "verdicts") {
            for (key, entry) in verdicts {
                if agent_failed(entry)? {
                    failing.push(key.clone());
                }
            }
        }
        failing.sort();
        failing.dedup();
        let Some(since) = self.claims.last().copied() else {
            return Ok(Vec::new());
        };
        if failing.is_empty() {
            return Ok(Vec::new());
        }
        let accused = if failing.iter().any(|key| !targets.contains(key)) {
            targets.to_vec()
        } else {
            failing
        };
        let mut out = Vec::new();
        for target in accused {
            if self
                .verdict_since(&target, gate, since)?
                .is_some_and(|recorded| !json_is(&recorded, "fail"))
            {
                out.push(target);
            }
        }
        out.sort();
        Ok(out)
    }

    /// `_gate_result_error`: why this gate task's IN_REVIEW result must be refused, measured on the row as it stands now.
    fn gate_result_error(&self, result: &Fields) -> Planned<Option<SeamError>> {
        let Some(gate) = self.gate_of_task()? else {
            return Ok(None);
        };
        let targets = self.gate_for()?;
        let missing = self.script_missing(&gate, &targets)?;
        if !missing.is_empty() {
            let shown = py_repr_list(&missing)?;
            return Ok(Some(self.contract(
                &format!("gate script not run for {shown}"),
                Vec::new(),
            )));
        }
        if gate != "review" {
            return Ok(None);
        }
        let mismatch = self.review_mismatch(&gate, &targets, result)?;
        if mismatch.is_empty() {
            return Ok(None);
        }
        let shown = py_repr_list(&mismatch)?;
        let listed = Json::Arr(mismatch.iter().map(|target| jstr(target)).collect());
        Ok(Some(self.contract(
            &format!(
                "review verdict mismatch: agent verdicts fail {shown} but the review rows recorded this \
                 lease do not; re-run swarm_gate with a major finding for each of {shown}"
            ),
            vec![("targets".to_string(), listed)],
        )))
    }

    /// `store.deps_satisfied(task)`: every dependency of the task is IN_REVIEW, APPROVED or DONE in its correlation.
    fn deps_satisfied(&self) -> Planned<bool> {
        for dependency in list_column(self.row()?, "depends_on")? {
            match dependency {
                Json::Str(id) => {
                    let satisfied = self.rows.iter().any(|row| {
                        matches!(column(row, "task_id"), Json::Str(known) if *known == id)
                            && matches!(column(row, "state"), Json::Str(state) if SATISFIED_STATES.contains(&state.as_str()))
                    });
                    if !satisfied {
                        return Ok(false);
                    }
                }
                Json::Arr(_) | Json::Obj(_) => return not_started("an unhashable dependency"),
                _ => return Ok(false),
            }
        }
        Ok(true)
    }

    /// `_replay_reply`: the reply the first application of this `(task, attempt, digest)` returned, when it was applied.
    fn replay_reply(&self, attempt: i64, digest: &str) -> Planned<Option<String>> {
        let notes = self.notes()?;
        let first = stored_keys(&notes).into_iter().find(|entry| {
            json_is(column(entry, "task_id"), self.tid())
                && attempt_matches(entry, attempt)
                && json_is(column(entry, "digest"), digest)
        });
        Ok(first.and_then(|entry| match column(entry, "reply") {
            Json::Str(reply) => Some(reply.clone()),
            _ => None,
        }))
    }

    /// `_record_key`: appends the key and keeps the newest [`INGEST_KEYS_BOUND`].
    fn record_key(&mut self, attempt: i64, digest: &str, reply: &str) -> Planned<()> {
        let notes = self.notes()?;
        let mut kept: Vec<Json> = stored_keys(&notes)
            .into_iter()
            .filter(|entry| {
                !(attempt_matches(entry, attempt) && json_is(column(entry, "digest"), digest))
            })
            .map(|entry| Json::Obj(entry.clone()))
            .collect();
        kept.push(Json::Obj(vec![
            ("task_id".to_string(), jstr(self.tid())),
            ("attempt".to_string(), Json::Int(attempt)),
            ("digest".to_string(), jstr(digest)),
            ("reply".to_string(), jstr(reply)),
        ]));
        let first = kept.len().saturating_sub(INGEST_KEYS_BOUND);
        let newest = kept.split_off(first);
        self.push(IngestOp::SetNotes {
            task_id: self.tid().to_string(),
            entries: vec![("ingest_keys".to_string(), Json::Arr(newest))],
        })
    }

    /// The artifact rows of `result.get("outputs", []) or []`.
    fn add_artifacts(&mut self, result: &Fields) -> Planned<()> {
        let outputs = match obj_get(result, "outputs") {
            None => return Ok(()),
            Some(value) if !truthy(value) => return Ok(()),
            Some(Json::Arr(items)) => items,
            Some(_) => return not_started("outputs is not a list"),
        };
        let text = |output: &Fields, key: &str, default: &str| match obj_get(output, key) {
            None => Ok(default.to_string()),
            Some(Json::Str(value)) => Ok(value.clone()),
            Some(_) => not_started(format!("an output {key} is not text")),
        };
        for output in outputs {
            let Json::Obj(fields) = output else {
                return not_started("an output is not an object");
            };
            self.push(IngestOp::AddArtifact(Artifact {
                task_id: self.tid().to_string(),
                kind: text(fields, "kind", "artifact")?,
                uri: text(fields, "uri", "")?,
                version: text(fields, "version", "1")?,
                digest: text(fields, "digest", "")?,
                producer: self.facts.agent_id.clone(),
            }))?;
        }
        Ok(())
    }

    /// `_agent_verdict_feedback`: failing entries of `verdicts{}` become feedback on the gate task's own targets.
    fn agent_feedback(&mut self, result: &Fields) -> Planned<()> {
        let Some(gate) = self.gate_of_task()? else {
            return Ok(());
        };
        let targets = self.gate_for()?;
        let Some(Json::Obj(verdicts)) = obj_get(result, "verdicts") else {
            return Ok(());
        };
        for (target, entry) in verdicts {
            if !targets.contains(target) || !agent_failed(entry)? {
                continue;
            }
            let findings = agent_findings(entry)?;
            self.push(IngestOp::AppendFeedback {
                task_id: target.clone(),
                entry: Json::Obj(vec![
                    ("gate".to_string(), jstr(&gate)),
                    ("source".to_string(), jstr("agent")),
                    ("findings".to_string(), Json::Arr(findings)),
                ]),
            })?;
        }
        Ok(())
    }

    /// `apply_result`: returns the task's new state.
    fn apply_result(&mut self) -> Planned<String> {
        let facts = self.facts;
        let tid = self.tid();
        let result = match &facts.result {
            Ok(result) => result,
            Err(ResultError::NotStarted(why)) => {
                return not_started(format!("the result is not reproducible: {}", why.as_str()))
            }
            Err(ResultError::Contract(error)) => return self.invalid_result(error),
        };
        let Json::Obj(fields) = result else {
            return not_started("the result is not an object");
        };
        let reported = match obj_get(fields, "state") {
            Some(Json::Str(state)) if crate::result_parse::STATES.contains(&state.as_str()) => {
                state.as_str()
            }
            _ => return not_started("the result names no state of task.result.v1"),
        };
        let ingest = facts.mode == Mode::Ingest;
        let snapshot_state = self.state()?;
        let snapshot_attempt = column(self.row()?, "attempt").clone();
        if !ingest && reported == "IN_PROGRESS" {
            return self.reject_headless(
                "E-CONTRACT: session ended in IN_PROGRESS",
                &snapshot_attempt,
            );
        }
        if ingest && snapshot_state == "BLOCKED" {
            let error = self.contract(
                &format!("{tid} is BLOCKED; A01 releases it with orch_status --transition after approval"),
                Vec::new(),
            );
            return Err(self.refuse(error));
        }
        if reported == "IN_REVIEW" {
            if let Some(error) = self.gate_result_error(fields)? {
                return self.refuse_gate(error, &snapshot_attempt);
            }
        }
        if ingest && matches!(snapshot_state.as_str(), "PLANNED" | "RETRY") {
            if !self.deps_satisfied()? {
                let error = self.contract(&format!("{tid} has unmet dependencies"), Vec::new());
                return Err(self.refuse(error));
            }
            self.transition("CLAIMED", &facts.agent_id, "claimed via task.result ingest")?;
            self.transition("IN_PROGRESS", &facts.agent_id, "lease started")?;
            self.push(IngestOp::Emit(event(
                "task.claimed",
                vec![
                    ("task_id".to_string(), jstr(tid)),
                    ("agent".to_string(), jstr(&facts.agent_id)),
                    ("mode".to_string(), jstr(facts.mode.as_str())),
                ],
            )))?;
        }
        let current = self.state()?;
        let (target, reason) = match reported {
            "BLOCKED" => (Some("BLOCKED"), blocked_reason(fields)?),
            "FAILED" => (Some("FAILED"), failed_reason(fields)),
            "IN_REVIEW" => (Some("IN_REVIEW"), "agent reported IN_REVIEW".to_string()),
            _ => {
                if current != "IN_PROGRESS" {
                    let error = self.contract(
                        &format!("illegal transition {current} → IN_PROGRESS for {tid}"),
                        Vec::new(),
                    );
                    return Err(self.refuse(error));
                }
                (None, String::new())
            }
        };
        self.write_result(result, fields, target, &reason, &snapshot_attempt)
    }

    /// The second half of `apply_result`: the replay check, then the transition and its provenance in one transaction.
    fn write_result(
        &mut self,
        result: &Json,
        fields: &Fields,
        target: Option<&str>,
        reason: &str,
        snapshot_attempt: &Json,
    ) -> Planned<String> {
        let facts = self.facts;
        let ingest = facts.mode == Mode::Ingest;
        let attempt = int_column(self.row()?, "attempt")?;
        let digest = match (ingest, facts.digest.as_deref()) {
            (true, Some(digest)) => Some(digest),
            (true, None) => return not_started("an ingest without a result digest"),
            (false, _) => None,
        };
        if let Some(digest) = digest {
            if let Some(reply) = self.replay_reply(attempt, digest)? {
                let accepted = match target {
                    Some(target) => legal_targets(&self.state()?).map_or_else(
                        || not_started("a state outside the fourteen"),
                        |legal| Ok(legal.contains(&target)),
                    )?,
                    None => true,
                };
                if accepted {
                    self.push(IngestOp::Emit(event(
                        "task.result.deduplicated",
                        vec![
                            ("task_id".to_string(), jstr(self.tid())),
                            ("mode".to_string(), jstr(facts.mode.as_str())),
                            ("attempt".to_string(), Json::Int(attempt)),
                            ("digest".to_string(), jstr(digest)),
                            ("reply".to_string(), jstr(&reply)),
                        ],
                    )))?;
                    return Ok(reply);
                }
            }
        }
        if target == Some("IN_REVIEW") {
            if let Some(error) = self.gate_result_error(fields)? {
                return self.refuse_gate(error, snapshot_attempt);
            }
        }
        if let Some(target) = target {
            self.transition(target, &facts.agent_id, reason)?;
        }
        self.push(IngestOp::SetNotes {
            task_id: self.tid().to_string(),
            entries: vec![
                ("result".to_string(), result.clone()),
                ("meta".to_string(), facts.meta.clone()),
            ],
        })?;
        self.add_artifacts(fields)?;
        if target == Some("IN_REVIEW") {
            self.agent_feedback(fields)?;
        }
        let new_state = self.state()?;
        if let Some(digest) = digest {
            self.record_key(attempt, digest, &new_state)?;
        }
        Ok(new_state)
    }
}

/// `str(result.get("needs", "blocked"))[:500]`: only text is reproduced (Python prints the `repr` of a list or an object).
fn blocked_reason(result: &Fields) -> Planned<String> {
    match obj_get(result, "needs") {
        None => Ok("blocked".to_string()),
        Some(Json::Str(needs)) => Ok(needs.chars().take(REASON_CAP).collect()),
        Some(_) => not_started("needs is not text"),
    }
}

/// `json.dumps(result.get("error", {}))[:500]`.
fn failed_reason(result: &Fields) -> String {
    let error = obj_get(result, "error")
        .cloned()
        .unwrap_or_else(|| Json::Obj(Vec::new()));
    to_python_dumps(&error).chars().take(REASON_CAP).collect()
}

/// Plans `orch_status.ingest` (ingest mode) or `apply_result` (headless) for one task.
///
/// # Errors
///
/// [`IngestError::Refused`] is Python's own refusal (the store is unchanged); [`IngestError::NotStarted`] means Python must answer: see the
/// module documentation for the list.
pub fn plan_ingest(facts: &IngestFacts, now: f64) -> Result<IngestPlan, IngestError> {
    let mut sim = Sim::new(facts, now)?;
    let before = sim.state()?;
    let reply = sim.apply_result()?;
    let mut notes_log = Vec::new();
    let mut after = sim.state()?;
    if facts.mode == Mode::Ingest {
        let stage = ReconcileFacts {
            correlation_id: facts.reconcile.correlation_id.clone(),
            tasks: sim.rows.clone(),
            verdicts: facts.reconcile.verdicts.clone(),
            other_tasks: facts.reconcile.other_tasks.clone(),
            key_policy: facts.reconcile.key_policy.clone(),
        };
        let plan = plan_reconcile(&stage, !facts.advisory, now)
            .map_err(|refusal| IngestError::NotStarted(refusal.0))?;
        for op in plan.ops {
            let op = match op {
                ReconcileOp::Transition { task_id, plan } => {
                    if task_id == facts.task_id {
                        after = plan.to.clone();
                    }
                    IngestOp::Transition { task_id, plan }
                }
                ReconcileOp::CreateTask(task) => IngestOp::CreateTask(task),
                ReconcileOp::SetDependsOn {
                    task_id,
                    depends_on,
                } => IngestOp::SetDependsOn {
                    task_id,
                    depends_on,
                },
                ReconcileOp::SetNotes {
                    task_id,
                    key,
                    value,
                } => IngestOp::SetNotes {
                    task_id,
                    entries: vec![(key, value)],
                },
                ReconcileOp::Emit(emitted) => IngestOp::Emit(emitted),
            };
            sim.ops.push(op);
        }
        notes_log = plan.notes_log;
    }
    Ok(IngestPlan {
        ops: sim.ops,
        reply,
        notes_log,
        before,
        after,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gate_facts::{IssuerState, VerdictRow};
    use crate::reconcile_plan::KeyPolicy;

    const VECTORS: &str = include_str!("../tests/vectors/ingest.json");

    fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
        match value {
            Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
            other => panic!("not an object looking for {key}: {other:?}"),
        }
    }

    fn has(value: &Json, key: &str) -> bool {
        matches!(value, Json::Obj(fields) if obj_get(fields, key).is_some())
    }

    fn text(value: &Json) -> &str {
        match value {
            Json::Str(text) => text,
            other => panic!("not text: {other:?}"),
        }
    }

    fn flag(value: &Json) -> bool {
        match value {
            Json::Bool(flag) => *flag,
            other => panic!("not a bool: {other:?}"),
        }
    }

    fn items(value: &Json) -> &[Json] {
        match value {
            Json::Arr(items) => items,
            other => panic!("not a list: {other:?}"),
        }
    }

    fn fields(value: &Json) -> Fields {
        match value {
            Json::Obj(fields) => fields.clone(),
            other => panic!("not an object: {other:?}"),
        }
    }

    fn number(value: &Json) -> f64 {
        match value {
            Json::Int(number) => *number as f64,
            Json::Float(number) => *number,
            other => panic!("not a number: {other:?}"),
        }
    }

    fn integer(value: &Json) -> i64 {
        match value {
            Json::Int(number) => *number,
            other => panic!("not an integer: {other:?}"),
        }
    }

    fn row_of<'a>(row: &'a Fields, key: &str) -> &'a Json {
        obj_get(row, key).unwrap_or_else(|| panic!("no column {key}"))
    }

    /// What Python did with one recorded call.
    enum Outcome {
        /// The call returned.
        Returned,
        /// Python raised its own `SwarmError`.
        Refused,
        /// Python crashed, or committed before raising.
        Crashed,
    }

    /// One recorded case: the facts Rust is given and the answer Python gave.
    struct Case {
        name: String,
        group: String,
        rust_refuses: bool,
        outcome: Outcome,
        facts: IngestFacts,
        now: f64,
        agent_id: Option<String>,
        before: Vec<Fields>,
        transitions_max: i64,
        artifacts_max: i64,
        expect: Json,
    }

    impl Case {
        fn parse(value: &Json) -> Self {
            let policy = member(value, "key_policy");
            let correlation = text(member(value, "correlation_id")).to_string();
            let before: Vec<Fields> = items(member(value, "tasks_before"))
                .iter()
                .map(fields)
                .collect();
            let in_correlation = |row: &Fields| text(row_of(row, "correlation_id")) == correlation;
            let stored = |row: &Json, key: &str| match member(row, key) {
                Json::Null => None,
                other => Some(text(other).to_string()),
            };
            let result = match member(value, "result") {
                contract if has(contract, "contract") => Err(ResultError::Contract(
                    SeamError::unprefixed(ErrorCode::Contract, text(member(contract, "contract"))),
                )),
                valid => Ok(member(valid, "value").clone()),
            };
            let mode = match text(member(value, "mode")) {
                "ingest" => Mode::Ingest,
                "headless" => Mode::Headless,
                other => panic!("unknown mode {other}"),
            };
            let agent_id = match member(value, "agent_id") {
                Json::Str(agent) => Some(agent.clone()),
                _ => None,
            };
            let expect = member(value, "expect").clone();
            let outcome = if has(&expect, "python_error") {
                Outcome::Crashed
            } else if has(&expect, "error") {
                Outcome::Refused
            } else {
                Outcome::Returned
            };
            let facts = IngestFacts {
                mode,
                task_id: text(member(value, "task_id")).to_string(),
                agent_id: agent_id.clone().unwrap_or_default(),
                result,
                meta: match member(value, "meta") {
                    Json::Null => Json::Obj(Vec::new()),
                    other => other.clone(),
                },
                digest: match member(value, "digest") {
                    Json::Null => None,
                    other => Some(text(other).to_string()),
                },
                advisory: flag(member(value, "advisory")),
                claims: items(member(value, "claims")).iter().map(number).collect(),
                reconcile: ReconcileFacts {
                    correlation_id: correlation.clone(),
                    tasks: before
                        .iter()
                        .filter(|row| in_correlation(row))
                        .cloned()
                        .collect(),
                    verdicts: items(member(value, "verdicts"))
                        .iter()
                        .map(|row| {
                            VerdictFacts::new(
                                VerdictRow {
                                    id: integer(member(row, "id")),
                                    task_id: text(member(row, "task_id")).to_string(),
                                    gate: text(member(row, "gate")).to_string(),
                                    verdict: stored(row, "verdict"),
                                    envelope_json: stored(row, "envelope_json"),
                                    ts: number(member(row, "ts")),
                                },
                                flag(member(row, "sig_ok")),
                            )
                        })
                        .collect(),
                    other_tasks: before
                        .iter()
                        .filter(|row| !in_correlation(row))
                        .map(|row| IssuerState {
                            task_id: text(row_of(row, "task_id")).to_string(),
                            state: text(row_of(row, "state")).to_string(),
                        })
                        .collect(),
                    key_policy: Some(KeyPolicy {
                        require_key: flag(member(policy, "require_key")),
                        real_key: flag(member(policy, "real_key")),
                        dev_key: flag(member(policy, "dev_key")),
                    }),
                },
            };
            Self {
                name: text(member(value, "name")).to_string(),
                group: text(member(value, "group")).to_string(),
                rust_refuses: text(member(value, "rust")) == "not_started",
                outcome,
                facts,
                now: number(member(value, "now")),
                agent_id,
                before,
                transitions_max: integer(member(value, "transitions_before_max")),
                artifacts_max: integer(member(value, "artifacts_before_max")),
                expect,
            }
        }
    }

    fn cases() -> Vec<Case> {
        let doc = parse_json(VECTORS).expect("the vector file parses");
        items(member(&doc, "cases"))
            .iter()
            .map(Case::parse)
            .collect()
    }

    fn case_named(name: &str) -> Case {
        cases()
            .into_iter()
            .find(|case| case.name == name)
            .unwrap_or_else(|| panic!("no case {name}"))
    }

    /// The database as the host leaves it: the rows SQLite holds, text for JSON columns.
    struct Store {
        tasks: Vec<Fields>,
        transitions: Vec<Fields>,
        artifacts: Vec<Fields>,
        next_transition: i64,
        next_artifact: i64,
        events: Vec<EmitEvent>,
        now: f64,
    }

    impl Store {
        fn row(&mut self, task_id: &str) -> &mut Fields {
            self.tasks
                .iter_mut()
                .find(|row| text(row_of(row, "task_id")) == task_id)
                .unwrap_or_else(|| panic!("the plan names {task_id}, which does not exist"))
        }

        fn audit(
            &mut self,
            task_id: &str,
            from: Option<&str>,
            to: &str,
            actor: &str,
            reason: &str,
        ) {
            let row = vec![
                ("id".to_string(), Json::Int(self.next_transition)),
                ("task_id".to_string(), jstr(task_id)),
                ("from_state".to_string(), from.map_or(Json::Null, jstr)),
                ("to_state".to_string(), jstr(to)),
                ("actor".to_string(), jstr(actor)),
                ("reason".to_string(), jstr(reason)),
                ("ts".to_string(), Json::Float(self.now)),
            ];
            self.next_transition += 1;
            self.transitions.push(row);
        }

        fn notes(row: &Fields) -> Fields {
            let stored = match row_of(row, "notes") {
                Json::Null => "",
                other => text(other),
            };
            match parse_json(stored) {
                Ok(Json::Obj(notes)) => notes,
                _ => Vec::new(),
            }
        }

        /// The write each operation makes, with the guards the host's conditional `UPDATE` gives.
        fn apply(&mut self, op: &IngestOp) {
            let now = Json::Float(self.now);
            match op {
                IngestOp::Transition { task_id, plan } => {
                    let row = self.row(task_id);
                    assert_eq!(
                        text(row_of(row, "state")),
                        plan.from,
                        "UPDATE ... WHERE state = from matched no row for {task_id}"
                    );
                    set(row, "state", jstr(&plan.to));
                    for (name, value) in &plan.columns {
                        match value {
                            Json::Obj(_) => set(row, name, Json::Str(to_python_dumps(value))),
                            _ if name != "state" => set(row, name, value.clone()),
                            _ => {}
                        }
                    }
                    set(row, "updated_at", now);
                    let mut chain = plan.from.clone();
                    for step in &plan.audit {
                        assert_eq!(step.from, chain, "the audit rows of {task_id} do not chain");
                        chain = step.to.clone();
                        self.audit(
                            task_id,
                            Some(&step.from),
                            &step.to,
                            &step.actor,
                            &step.reason,
                        );
                    }
                    assert_eq!(chain, plan.to);
                }
                IngestOp::SetNotes { task_id, entries } => {
                    let row = self.row(task_id);
                    let mut notes = Self::notes(row);
                    for (key, value) in entries {
                        set(&mut notes, key, value.clone());
                    }
                    set(row, "notes", Json::Str(to_python_dumps(&Json::Obj(notes))));
                    set(row, "updated_at", now);
                }
                IngestOp::AddArtifact(artifact) => {
                    let id = self.next_artifact;
                    self.next_artifact += 1;
                    self.artifacts.push(vec![
                        ("id".to_string(), Json::Int(id)),
                        ("task_id".to_string(), jstr(&artifact.task_id)),
                        ("kind".to_string(), jstr(&artifact.kind)),
                        ("uri".to_string(), jstr(&artifact.uri)),
                        ("version".to_string(), jstr(&artifact.version)),
                        ("digest".to_string(), jstr(&artifact.digest)),
                        ("producer".to_string(), jstr(&artifact.producer)),
                        ("ts".to_string(), now.clone()),
                    ]);
                    let row = self.row(&artifact.task_id);
                    let mut outputs = match parse_json(text(row_of(row, "outputs"))) {
                        Ok(Json::Arr(outputs)) => outputs,
                        other => panic!("outputs is {other:?}"),
                    };
                    outputs.push(Json::Obj(vec![
                        ("kind".to_string(), jstr(&artifact.kind)),
                        ("uri".to_string(), jstr(&artifact.uri)),
                        ("version".to_string(), jstr(&artifact.version)),
                        ("digest".to_string(), jstr(&artifact.digest)),
                    ]));
                    set(
                        row,
                        "outputs",
                        Json::Str(to_python_dumps(&Json::Arr(outputs))),
                    );
                    set(row, "updated_at", now);
                }
                IngestOp::AppendFeedback { task_id, entry } => {
                    let row = self.row(task_id);
                    let mut notes = Self::notes(row);
                    match notes.iter_mut().find(|(key, _)| key == "feedback") {
                        Some((_, Json::Arr(list))) => list.push(entry.clone()),
                        Some(other) => panic!("feedback is {other:?}"),
                        None => {
                            notes.push(("feedback".to_string(), Json::Arr(vec![entry.clone()])))
                        }
                    }
                    set(row, "notes", Json::Str(to_python_dumps(&Json::Obj(notes))));
                    set(row, "updated_at", now);
                }
                IngestOp::CreateTask(new) => {
                    assert!(
                        !self
                            .tasks
                            .iter()
                            .any(|row| text(row_of(row, "task_id")) == new.task_id),
                        "{} exists already",
                        new.task_id
                    );
                    let depends = Json::Arr(new.depends_on.iter().map(|id| jstr(id)).collect());
                    self.tasks.push(vec![
                        ("task_id".to_string(), jstr(&new.task_id)),
                        ("correlation_id".to_string(), jstr(&new.correlation_id)),
                        ("parent_id".to_string(), Json::Null),
                        (
                            "dag_depth".to_string(),
                            new.dag_depth.map_or(Json::Null, Json::Int),
                        ),
                        ("capability".to_string(), new.capability.clone()),
                        ("agent_id".to_string(), new.agent_id.clone()),
                        ("title".to_string(), jstr(&new.title)),
                        ("inputs".to_string(), jstr("[]")),
                        ("outputs".to_string(), jstr("[]")),
                        (
                            "acceptance".to_string(),
                            Json::Str(to_python_dumps(&new.acceptance)),
                        ),
                        (
                            "budget".to_string(),
                            Json::Str(to_python_dumps(&new.budget)),
                        ),
                        ("risk_class".to_string(), new.risk_class.clone()),
                        ("priority".to_string(), new.priority.clone()),
                        ("state".to_string(), jstr("CREATED")),
                        ("attempt".to_string(), Json::Int(0)),
                        ("max_attempts".to_string(), Json::Int(3)),
                        ("rework_loops".to_string(), Json::Int(0)),
                        (
                            "depends_on".to_string(),
                            Json::Str(to_python_dumps(&depends)),
                        ),
                        ("assigned_to".to_string(), Json::Null),
                        (
                            "notes".to_string(),
                            Json::Str(to_python_dumps(&Json::Obj(new.notes.clone()))),
                        ),
                        ("created_at".to_string(), now.clone()),
                        ("updated_at".to_string(), now),
                    ]);
                    self.audit(&new.task_id, None, "CREATED", "system", "created");
                }
                IngestOp::SetDependsOn {
                    task_id,
                    depends_on,
                } => {
                    let list = Json::Arr(depends_on.iter().map(|id| jstr(id)).collect());
                    let row = self.row(task_id);
                    set(row, "depends_on", Json::Str(to_python_dumps(&list)));
                    set(row, "updated_at", now);
                }
                IngestOp::Emit(event) => self.events.push(event.clone()),
            }
        }
    }

    fn set(row: &mut Fields, key: &str, value: Json) {
        super::set(row, key, value);
    }

    fn same_row(left: &Fields, right: &Fields) -> bool {
        left.len() == right.len()
            && left
                .iter()
                .all(|(name, value)| obj_get(right, name) == Some(value))
    }

    fn assert_rows(name: &str, what: &str, got: &[Fields], want: &[Json]) {
        assert_eq!(got.len(), want.len(), "{name}: number of {what} rows");
        for (at, (got, want)) in got.iter().zip(want).enumerate() {
            assert!(
                same_row(got, &fields(want)),
                "{name}: {what} row {at} differs\n  rust:   {got:?}\n  python: {want:?}"
            );
        }
    }

    fn assert_events(name: &str, got: &[EmitEvent], want: &[Json]) {
        assert_eq!(got.len(), want.len(), "{name}: number of events");
        for (got, want) in got.iter().zip(want) {
            assert_eq!(
                got.event_type,
                text(member(want, "type")),
                "{name}: event type"
            );
            assert_eq!(
                Json::Obj(got.payload.clone()),
                *member(want, "payload"),
                "{name}: payload of {}",
                got.event_type
            );
            let task_id = match member(want, "task_id") {
                Json::Null => None,
                other => Some(text(other).to_string()),
            };
            assert_eq!(
                got.task_id, task_id,
                "{name}: task_id keyword of {}",
                got.event_type
            );
        }
    }

    /// Replays one recorded case: plan, apply the operations to a copy of the facts, and compare everything Python left behind.
    fn replay(case: &Case) {
        let name = case.name.as_str();
        let mut facts = case.facts.clone();
        let agent = match (facts.mode, &facts.result) {
            (Mode::Ingest, Ok(result)) => {
                let row = case
                    .before
                    .iter()
                    .find(|row| text(row_of(row, "task_id")) == facts.task_id)
                    .expect("the task row");
                ingest_agent_id(result, row)
            }
            _ => Ok(facts.agent_id.clone()),
        };
        let outcome = agent.and_then(|agent| {
            facts.agent_id = agent;
            plan_ingest(&facts, case.now)
        });
        if case.rust_refuses || matches!(case.outcome, Outcome::Crashed) {
            assert!(
                matches!(outcome, Err(IngestError::NotStarted(_))),
                "{name}: Rust must answer not_started, planned {outcome:?}"
            );
            return;
        }
        if matches!(case.outcome, Outcome::Refused) {
            let Err(IngestError::Refused(refusal)) = outcome else {
                panic!("{name}: Rust must refuse like Python, got {outcome:?}");
            };
            let want = member(&case.expect, "error");
            assert_eq!(
                refusal.error.code().as_str(),
                text(member(want, "code")),
                "{name}: code"
            );
            assert_eq!(
                refusal.error.message(),
                text(member(want, "message")),
                "{name}: message"
            );
            assert_eq!(
                Json::Obj(refusal.error.details().to_vec()),
                *member(want, "details"),
                "{name}: details"
            );
            assert_events(name, &refusal.events, items(member(&case.expect, "events")));
            assert_rows(
                name,
                "tasks (unchanged)",
                &case.before,
                items(member(&case.expect, "tasks")),
            );
            return;
        }
        let plan = outcome.unwrap_or_else(|refusal| panic!("{name}: {refusal}"));
        let mut store = Store {
            tasks: case.before.clone(),
            transitions: Vec::new(),
            artifacts: Vec::new(),
            next_transition: case.transitions_max + 1,
            next_artifact: case.artifacts_max + 1,
            events: Vec::new(),
            now: case.now,
        };
        for op in &plan.ops {
            store.apply(op);
        }
        assert_rows(
            name,
            "tasks",
            &store.tasks,
            items(member(&case.expect, "tasks")),
        );
        assert_rows(
            name,
            "transitions",
            &store.transitions,
            items(member(&case.expect, "transitions")),
        );
        assert_rows(
            name,
            "artifacts",
            &store.artifacts,
            items(member(&case.expect, "artifacts")),
        );
        assert_events(name, &store.events, items(member(&case.expect, "events")));
        assert_eq!(
            plan.reply,
            text(member(&case.expect, "reply")),
            "{name}: reply"
        );
        assert_eq!(
            plan.before,
            text(member(&case.expect, "before")),
            "{name}: state before"
        );
        assert_eq!(
            plan.after,
            text(member(&case.expect, "after")),
            "{name}: state after"
        );
        let log: Vec<&str> = items(member(&case.expect, "notes_log"))
            .iter()
            .map(text)
            .collect();
        assert_eq!(plan.notes_log, log, "{name}: notes_log");
        if let Some(agent) = &case.agent_id {
            assert_eq!(
                facts.agent_id, *agent,
                "{name}: the agent id the route passes"
            );
        }
    }

    #[test]
    fn every_recorded_case_replays_to_the_state_python_left() {
        let all = cases();
        assert!(
            all.len() >= 400,
            "the vector file has only {} cases",
            all.len()
        );
        for case in &all {
            replay(case);
        }
    }

    #[test]
    fn the_cases_cover_every_behaviour_the_planner_has() {
        let all = cases();
        let seen = |pick: &dyn Fn(&Case) -> bool| all.iter().any(pick);
        let events = |case: &Case, kind: &str| {
            has(&case.expect, "events")
                && items(member(&case.expect, "events"))
                    .iter()
                    .any(|event| text(member(event, "type")) == kind)
        };
        let error_text = |case: &Case, needle: &str| {
            has(&case.expect, "error")
                && text(member(member(&case.expect, "error"), "message")).contains(needle)
        };
        let checks: [(&str, bool); 18] = [
            ("auto-claim", seen(&|c| events(c, "task.claimed"))),
            (
                "replay skip",
                seen(&|c| events(c, "task.result.deduplicated")),
            ),
            (
                "rejected event",
                seen(&|c| events(c, "task.result.rejected")),
            ),
            (
                "approval skipped",
                seen(&|c| events(c, "task.approval.skipped")),
            ),
            (
                "escalation request",
                seen(&|c| events(c, "escalation.request")),
            ),
            ("BLOCKED refusal", seen(&|c| error_text(c, "is BLOCKED"))),
            (
                "unmet dependencies",
                seen(&|c| error_text(c, "unmet dependencies")),
            ),
            (
                "gate script missing",
                seen(&|c| error_text(c, "gate script not run")),
            ),
            (
                "review mismatch",
                seen(&|c| error_text(c, "review verdict mismatch")),
            ),
            (
                "illegal transition",
                seen(&|c| error_text(c, "illegal transition")),
            ),
            (
                "max attempts",
                seen(&|c| error_text(c, "max_attempts exceeded")),
            ),
            (
                "invalid result",
                seen(&|c| error_text(c, "task.result invalid")),
            ),
            (
                "headless reject to FAILED",
                seen(&|c| {
                    c.group == "invalid"
                        && c.facts.mode == Mode::Headless
                        && has(&c.expect, "reply")
                        && text(member(&c.expect, "reply")) == "FAILED"
                }),
            ),
            (
                "feedback written",
                seen(&|c| {
                    has(&c.expect, "tasks")
                        && items(member(&c.expect, "tasks"))
                            .iter()
                            .any(|row| text(member(row, "notes")).contains("\"feedback\""))
                }),
            ),
            (
                "synthesized finding",
                seen(&|c| {
                    has(&c.expect, "tasks")
                        && items(member(&c.expect, "tasks"))
                            .iter()
                            .any(|row| text(member(row, "notes")).contains("agent-verdict"))
                }),
            ),
            (
                "artifact rows",
                seen(&|c| {
                    has(&c.expect, "artifacts") && !items(member(&c.expect, "artifacts")).is_empty()
                }),
            ),
            (
                "a refusal Rust declines",
                seen(&|c| c.rust_refuses && matches!(c.outcome, Outcome::Refused)),
            ),
            (
                "a crash Rust declines",
                seen(&|c| matches!(c.outcome, Outcome::Crashed)),
            ),
        ];
        for (what, covered) in checks {
            assert!(covered, "no recorded case covers: {what}");
        }
        assert!(
            all.iter().any(|c| c.facts.mode == Mode::Headless)
                && all.iter().any(|c| c.facts.advisory)
        );
    }

    #[test]
    fn the_constants_match_python() {
        let doc = parse_json(VECTORS).expect("the vector file parses");
        let constants = member(&doc, "constants");
        assert_eq!(
            integer(member(constants, "ingest_keys_bound")),
            INGEST_KEYS_BOUND as i64
        );
        let names = |key: &str| -> Vec<String> {
            items(member(constants, key))
                .iter()
                .map(|v| text(v).to_string())
                .collect()
        };
        let mut satisfied: Vec<String> =
            SATISFIED_STATES.iter().map(|s| (*s).to_string()).collect();
        satisfied.sort();
        assert_eq!(names("satisfied_states"), satisfied);
        let mut pass: Vec<String> = PASS_VERDICTS.iter().map(|s| (*s).to_string()).collect();
        pass.sort();
        assert_eq!(names("pass_verdicts"), pass);
        let severities: Vec<String> = SEVERITIES.iter().map(|s| (*s).to_string()).collect();
        assert_eq!(names("severities"), severities);
        assert_eq!(
            text(member(constants, "blocking_severity")),
            BLOCKING_SEVERITY
        );
        assert_eq!(
            names("states"),
            crate::transition_plan::STATES.map(String::from).to_vec()
        );
    }

    #[test]
    fn a_heartbeat_writes_notes_then_artifacts_then_the_key_last() {
        let case = case_named("matrix-ingest-IN_PROGRESS-IN_PROGRESS");
        let plan = plan_ingest(&case.facts, case.now).expect("a plan");
        let kinds: Vec<String> = plan
            .ops
            .iter()
            .map(|op| match op {
                IngestOp::SetNotes { entries, .. } => format!(
                    "notes:{}",
                    entries
                        .iter()
                        .map(|(key, _)| key.as_str())
                        .collect::<Vec<_>>()
                        .join("+")
                ),
                IngestOp::AddArtifact(_) => "artifact".to_string(),
                other => format!("{other:?}"),
            })
            .collect();
        assert_eq!(
            kinds,
            ["notes:result+meta", "artifact", "notes:ingest_keys"]
        );
    }

    #[test]
    fn an_in_review_result_orders_claim_transition_notes_artifacts_feedback_key() {
        let case = case_named("feedback-major-finding");
        let plan = plan_ingest(&case.facts, case.now).expect("a plan");
        let kinds: Vec<&str> = plan
            .ops
            .iter()
            .map(|op| match op {
                IngestOp::Transition { plan, .. } => plan.to.as_str(),
                IngestOp::SetNotes { entries, .. } if entries[0].0 == "result" => "notes",
                IngestOp::SetNotes { entries, .. } if entries[0].0 == "ingest_keys" => "key",
                IngestOp::AddArtifact(_) => "artifact",
                IngestOp::AppendFeedback { .. } => "feedback",
                _ => "other",
            })
            .collect();
        assert_eq!(
            &kinds[..5],
            ["IN_REVIEW", "notes", "artifact", "feedback", "key"]
        );
    }

    #[test]
    fn an_auto_claim_is_two_audited_transitions_and_one_event_before_the_result() {
        let case = case_named("claim-PLANNED-dependency-DONE-IN_REVIEW");
        let plan = plan_ingest(&case.facts, case.now).expect("a plan");
        let head: Vec<String> = plan
            .ops
            .iter()
            .take(4)
            .map(|op| match op {
                IngestOp::Transition { plan, .. } => format!("{}>{}", plan.from, plan.to),
                IngestOp::Emit(event) => event.event_type.clone(),
                _ => "other".to_string(),
            })
            .collect();
        assert_eq!(
            head,
            [
                "PLANNED>CLAIMED",
                "CLAIMED>IN_PROGRESS",
                "task.claimed",
                "IN_PROGRESS>IN_REVIEW"
            ]
        );
    }

    #[test]
    fn the_agent_id_is_the_first_truthy_of_result_task_and_agent() {
        let row = |agent: Json| -> Fields { vec![("agent_id".to_string(), agent)] };
        let result = |agent: Option<Json>| {
            Json::Obj(
                agent
                    .map(|a| vec![("agent_id".to_string(), a)])
                    .unwrap_or_default(),
            )
        };
        let table: [(Option<Json>, Json, Option<&str>); 9] = [
            (Some(jstr("R")), jstr("T"), Some("R")),
            (None, jstr("T"), Some("T")),
            (Some(jstr("")), jstr("T"), Some("T")),
            (Some(Json::Null), jstr("T"), Some("T")),
            (Some(Json::Int(0)), jstr("T"), Some("T")),
            (None, Json::Null, Some("agent")),
            (Some(jstr("")), jstr(""), Some("agent")),
            (Some(Json::Int(5)), jstr("T"), None),
            (None, Json::Int(7), None),
        ];
        for (given, task, want) in table {
            let got = ingest_agent_id(&result(given.clone()), &row(task.clone()));
            match want {
                Some(want) => assert_eq!(got.as_deref().ok(), Some(want), "{given:?} / {task:?}"),
                None => assert!(
                    matches!(got, Err(IngestError::NotStarted(_))),
                    "{given:?} / {task:?}"
                ),
            }
        }
    }

    #[test]
    fn a_result_without_a_digest_cannot_be_keyed() {
        let mut case = case_named("matrix-ingest-IN_PROGRESS-IN_PROGRESS");
        case.facts.digest = None;
        assert!(matches!(
            plan_ingest(&case.facts, case.now),
            Err(IngestError::NotStarted(_))
        ));
    }

    #[test]
    fn a_task_row_missing_from_the_correlation_is_not_started() {
        let mut case = case_named("matrix-ingest-IN_PROGRESS-IN_PROGRESS");
        case.facts.task_id = "T-nowhere".to_string();
        assert!(matches!(
            plan_ingest(&case.facts, case.now),
            Err(IngestError::NotStarted(_))
        ));
    }
}
