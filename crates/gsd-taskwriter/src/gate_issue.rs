//! The gate-issue tail on the host (phase 14.5, unit U3.6a): `swarm/verdicts.py::issue_gate` and everything it writes.
//!
//! A gate script computes its findings and then calls `issue_gate`. This module is that call, in two parts:
//!
//! * [`derive_gate_issue`] is the pure derivation. From the request ([`GateIssueRequest`]), the Task Store rows it reads
//!   ([`StoreFacts`]), the signing keys ([`KeyMaterial`]) and an id source ([`EnvelopeIds`]) it computes the exact ordered
//!   outcome ([`GatePlan`]): the verdict file, the envelopes returned to the script, the rows and notes to write, the events to
//!   emit, or Python's own refusal. Nothing is read from the environment, the clock or the disk: the time is
//!   [`GateIssueRequest::now`] and the random ids come from the id source.
//! * [`TaskStoreWriter::issue_gate`] is the host applier. It opens ONE `BEGIN IMMEDIATE` transaction, reads the rows the
//!   derivation needs inside it, derives, writes every verdict row and every `notes.feedback` rewrite in that transaction and
//!   commits, or writes nothing. The verdict file and the events are not written here (the route glue does them from the
//!   returned plan); a plan that carries an error writes nothing to the store.
//!
//! # `issue_gate`, branch by branch (the order is Python's)
//!
//! 1. `store = TaskStore(root)` when there is a `--task-id`; `resolve_targets` (before any write): not a gate task (no
//!    `notes.gate`, or an unknown task) is `None`; `notes.gate != gate` is `E-POLICY` `<id> is a <gate> gate task; this script issues
//!    <gate>`; a given `--correlation-id` that is not the task's own is `E-POLICY` `gate task <id> belongs to correlation <own!r>,
//!    not <given!r>; ...`; a gate task with targets that is not `IN_PROGRESS` is `E-POLICY` `<id> is not a running gate task (state
//!    <s>); lease it first`. All three carry `details.task_id`. The targets are `notes.gate_for`.
//! 2. The file envelope's correlation is `--correlation-id`, else the gate task's own. `simulate` (`--dry-run`) adds
//!    `dry_run: true` to `extra`.
//! 3. The task id of the envelope is `--task-id`, else `T-dry` (simulate) or `T-unassigned`. `verdicts/` is created
//!    ([`GatePlan::ensure_verdicts_dir`]) from here on, also when the next step refuses.
//! 4. A signing configuration error ([`KeyMaterial::config_error`]: `SWARM_REQUIRE_KEY=1` without a key, an unloadable
//!    `SWARM_ED25519_KEY`, a key reference in an agent session) is `E-POLICY` `<message>` with `details.task_id`; no file, no row,
//!    no event.
//! 5. Keyless (no real key and no dev-key opt-in): the **advisory** path. An unsigned envelope (`sig: null`,
//!    `payload.advisory = true`, `payload.advisory_reason`) is built; if a per-target finding blocks, its verdict becomes `fail`;
//!    one unsigned envelope per `per_target` key (the target's own correlation when the task exists) is returned; the file is
//!    written; `gate.verdict.unrecorded {task_id, gate, reason}` is emitted (reason: the agent-session text in a session,
//!    else the missing-key text). Nothing is recorded.
//! 6. Signed file envelope (`dev_key` audit event `security.dev_key` when the implicit dev key signs and this is not an agent
//!    session); in a session a blocking per-target finding forces the file verdict `fail`. The file is written.
//! 7. Agent session: `gate.verdict.unrecorded` (session text), return `{}`.
//! 8. No store (no `--task-id`): `gate.verdict.unrecorded {task_id: null, reason: "no --task-id"}`.
//! 9. Simulate: a gate task with targets that A01 did not flag `notes.dry_run` is `gate.verdict.unrecorded` (`dry-run outside a
//!    runner dry-run`); otherwise every target gets `[SIM-1 if "<target>:<gate>" is in SWARM_DRYRUN_FAIL] + findings`.
//! 10. `record_gate_verdicts`: `resolve_targets` again; no targets is `gate.verdict.unrecorded` (`not a gate task` or `empty
//!     gate_for`). Else, in one transaction and per target in order: the target's correlation (an unknown target is `E-INPUT
//!     unknown task <id>` and rolls everything back), a verdict signed with it (`extra + gate_task`), the `verdicts` row
//!     (`ts = now`, `expires_at = now + expires_s`, `findings`/`envelope_json` as `json.dumps`), and for a `fail` verdict one
//!     `notes.feedback` entry `{gate, source: "script", findings}` (`updated_at = now`).
//!
//! # What the plan holds
//!
//! The verdict file is `json.dumps(env, indent=2)` (no trailing newline) at `verdicts/<task>.<gate>.json`. Events come in
//! Python's emission order; Python emits the first `security.dev_key` before it writes the file, and the route glue writes the
//! file first and emits all events after, which is unobservable except in a crash. Python emits the per-target dev-key events
//! before its commit; the glue emits after. The clock is read once ([`GateIssueRequest::now`]): `issued_at`, `ts`, the row `ts`
//! and `updated_at` are all that value, where Python reads `time.time()` at each place. The parity masks are `id`s, `ts`,
//! `issued_at` and `sig`; with a pinned clock and pinned ids the outputs are byte-identical.
//!
//! # What this module refuses to answer (a [`Decline`], `E-DEP` `not_started`, Python answers)
//!
//! * a gate name outside `quality`, `review`, `security`, `release`; a `now` that is not finite or outside `0..=253402300799`;
//! * a finding that is not an object with a `severity` in `info|minor|major|critical|blocker` (Python raises a `KeyError` or
//!   `ValueError`, not a `SwarmError`);
//! * `NaN` or an infinite number anywhere in findings, `runs`, `extra` or per-target findings;
//! * an `extra` key that Python would let override the payload (`gate`, `task_id`, `verdict`, `findings`, `runs`, `expires_s`,
//!   `issued_at`, `advisory`, `advisory_reason`);
//! * a `--task-id` that is not a safe id (Python's `check_task_id` answers `E-INPUT`); a duplicated `per_target` key;
//! * a non-ASCII `repr` in a refusal text (the stored correlation of a gate task, shown with `!r`);
//! * `notes.gate` that is truthy and not text; `notes.gate_for` that is truthy and not a list of text; a `notes.feedback` that is not a
//!   list; a stored value [`crate::Tx::task`] cannot read; a `SWARM_DRYRUN_FAIL` holding U+001C to U+001F (Python's `str.strip` removes
//!   them, Rust's does not);
//! * the signing keys: a key-file reference or an unclassifiable conventional key path is declined by [`KeyMaterial::from_env`]
//!   (the caller maps that to the same refusal before it calls this module);
//! * an id source that cannot draw ([`EnvelopeIds`] returned `None`).
//!
//! Anything a key could leak through is excluded by construction: a [`Decline`] holds a static string, a refusal text is built
//! from the request and the store rows only, and [`KeyMaterial`] has no value-bearing `Debug`.

use std::fmt;
use std::io::Read;

use gsd_codes::ErrorCode;
use gsd_envelope::{parse_envelope, sign_envelope, verify_envelope, KeyMaterial, SignError};
use gsd_route::pydumps::to_python_dumps;
use gsd_route::{is_safe_id, jstr, obj_get, to_python_json, Fields, Json, SeamError};

use crate::error::{ErrorKind, WriterError};
use crate::plan::has_non_finite;
use crate::store::{NewVerdict, TextColumn, Tx};
use crate::TaskStoreWriter;

/// The four gates of `swarm/gates.py::GATES`.
const GATES: [&str; 4] = ["quality", "review", "security", "release"];

/// `swarm/gates.py::SEVERITIES`, weakest first; a finding at or above `major` fails its verdict.
const SEVERITIES: [&str; 5] = ["info", "minor", "major", "critical", "blocker"];

/// Payload keys an `extra` entry must not name: Python's `**extra` would override them.
const RESERVED_EXTRA: [&str; 9] = [
    "gate",
    "task_id",
    "verdict",
    "findings",
    "runs",
    "expires_s",
    "issued_at",
    "advisory",
    "advisory_reason",
];

/// Payload members the verdict signs itself (`swarm/gates.py::SIGNED_FIELDS`): an `extra` entry naming one is ignored.
const SIGNED_FIELDS: [&str; 6] = [
    "gate",
    "task_id",
    "verdict",
    "findings",
    "runs",
    "expires_s",
];

/// `swarm/verdicts.py::MISSING_KEY_REASON`.
pub const MISSING_KEY_REASON: &str = "fail-closed: no signing key configured (SWARM_ED25519_KEY and SWARM_SIGNING_KEY are unset; SWARM_ALLOW_INSECURE_DEV_KEY=1 opts into the dev key)";

/// `swarm/verdicts.py::SESSION_UNRECORDED`.
pub const SESSION_UNRECORDED: &str =
    "agent session: the runner records this gate after the session";

/// A request this module will not answer, because it cannot prove its answer byte-identical to Python's. The route glue lets
/// Python answer. The reason is a static string: no value of the request, the store or the environment is in it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Decline(pub &'static str);

impl fmt::Display for Decline {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "gate issue declined: {}", self.0)
    }
}

impl std::error::Error for Decline {}

/// A key-file reference, an unclassifiable conventional key path or a non-UTF-8 signing variable ([`KeyMaterial::from_env`]): refused before
/// any write, so Python answers. The text names the reason, never a value.
impl From<gsd_envelope::Declined> for WriterError {
    fn from(declined: gsd_envelope::Declined) -> Self {
        WriterError::not_started(
            ErrorKind::Unsupported,
            format!("gate issue declined: signing keys: {declined}"),
        )
    }
}

impl From<Decline> for WriterError {
    fn from(decline: Decline) -> Self {
        WriterError::not_started(ErrorKind::Unsupported, decline.to_string())
    }
}

/// What `issue_gate` is called with, and the two environment facts it reads.
#[derive(Debug, Clone, PartialEq)]
pub struct GateIssueRequest {
    /// `gate`: one of the four gate names.
    pub gate: String,
    /// `agent_id`: the envelope's `source`.
    pub agent_id: String,
    /// `findings`: finding objects with a `severity`.
    pub findings: Vec<Json>,
    /// `runs`: the object stored as `payload.runs`.
    pub runs: Fields,
    /// `expires_s`.
    pub expires_s: i64,
    /// `extra`: payload members after `issued_at`, in order.
    pub extra: Fields,
    /// `simulate` (`--dry-run`).
    pub simulate: bool,
    /// `per_target`: findings per target task, in dict order.
    pub per_target: Vec<(String, Vec<Json>)>,
    /// `ctx.task_id` (`--task-id`).
    pub task_id: Option<String>,
    /// `ctx.correlation_id` (`--correlation-id`).
    pub correlation_id: Option<String>,
    /// `SWARM_AGENT_SESSION == "1"`.
    pub session: bool,
    /// The raw `SWARM_DRYRUN_FAIL` value (empty when unset).
    pub dryrun_fail: String,
    /// The one clock reading of the call (`time.time()`), seconds since the epoch.
    pub now: f64,
}

/// The columns of a `tasks` row the derivation reads (`TaskStore.get`).
#[derive(Debug, Clone, PartialEq)]
pub struct TaskFacts {
    /// `correlation_id` (`None` for SQL `NULL`).
    pub correlation_id: Option<String>,
    /// `state`.
    pub state: String,
    /// `notes_json`: the notes object.
    pub notes: Fields,
}

impl TaskFacts {
    /// The facts of a row as [`crate::Tx::task`] returns it.
    pub fn from_row(row: &Fields) -> Result<Self, Decline> {
        let correlation_id = match obj_get(row, "correlation_id") {
            Some(Json::Str(text)) => Some(text.clone()),
            Some(Json::Null) => None,
            _ => return Err(Decline("a task row has a correlation_id that is not text")),
        };
        let Some(Json::Str(state)) = obj_get(row, "state") else {
            return Err(Decline("a task row has a state that is not text"));
        };
        let Some(Json::Obj(notes)) = obj_get(row, "notes_json") else {
            return Err(Decline("a task row has no notes object"));
        };
        Ok(Self {
            correlation_id,
            state: state.clone(),
            notes: notes.clone(),
        })
    }
}

/// The rows the derivation may ask for: the gate task, its `gate_for` targets and the `per_target` keys.
/// `None` means the store has no such task (`TaskStore.get` raises `E-INPUT unknown task`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct StoreFacts {
    /// Task id and its row, in any order.
    pub tasks: Vec<(String, Option<TaskFacts>)>,
}

impl StoreFacts {
    fn get(&self, task_id: &str) -> Result<Option<&TaskFacts>, Decline> {
        self.tasks
            .iter()
            .find(|(id, _)| id == task_id)
            .map(|(_, facts)| facts.as_ref())
            .ok_or(Decline("a task row the derivation needs was not read"))
    }
}

/// Where the random parts of an envelope come from. A `None` declines the request.
pub trait EnvelopeIds {
    /// `_uuid7()`: the `msg_id`, from `now_ms = int(time.time() * 1000)`.
    fn msg_id(&mut self, now_ms: u64) -> Option<String>;
    /// `str(uuid.uuid4())`: the correlation id of an envelope that is given none.
    fn correlation_id(&mut self) -> Option<String>;
    /// `uuid.uuid4().hex`: the `trace_id`.
    fn trace_id(&mut self) -> Option<String>;
}

/// Ids drawn from `/dev/urandom`, in the layouts of `swarm/envelope.py`.
#[derive(Debug)]
pub struct SystemIds {
    source: std::fs::File,
}

impl SystemIds {
    /// Opens the random source; `Decline` when it cannot be opened.
    pub fn open() -> Result<Self, Decline> {
        std::fs::File::open("/dev/urandom")
            .map(|source| Self { source })
            .map_err(|_| Decline("the random source cannot be opened"))
    }

    fn draw(&mut self) -> Option<u128> {
        let mut bytes = [0u8; 16];
        self.source.read_exact(&mut bytes).ok()?;
        Some(u128::from_be_bytes(bytes))
    }
}

/// `str(uuid.UUID(int=value))`.
fn dashed(value: u128) -> String {
    let hex = format!("{value:032x}");
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

impl EnvelopeIds for SystemIds {
    fn msg_id(&mut self, now_ms: u64) -> Option<String> {
        // Python: (ms << 80) | (0x7 << 76) | ((rand >> 2) & 0xfff) << 64 | (0b10 << 62) | (rand & (2**62 - 1)), rand = 74 bits.
        let rand = self.draw()? & ((1u128 << 74) - 1);
        let value = (u128::from(now_ms) << 80)
            | (0x7u128 << 76)
            | (((rand >> 2) & 0xfff) << 64)
            | (0b10u128 << 62)
            | (rand & ((1u128 << 62) - 1));
        Some(dashed(value))
    }

    fn correlation_id(&mut self) -> Option<String> {
        let value = (self.draw()? & !(0xf000u128 << 64) & !(0xc000u128 << 48))
            | (0x4u128 << 76)
            | (0x8u128 << 60);
        Some(dashed(value))
    }

    fn trace_id(&mut self) -> Option<String> {
        let value = (self.draw()? & !(0xf000u128 << 64) & !(0xc000u128 << 48))
            | (0x4u128 << 76)
            | (0x8u128 << 60);
        Some(format!("{value:032x}"))
    }
}

/// Who an event is attributed to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventOrigin {
    /// `ctx.emit`: source `<agent>@<script>`, the context's correlation and task.
    Script,
    /// `runlog.emit` from `swarm.envelope`: source `swarm.envelope`, the envelope's correlation, no task.
    Envelope,
}

/// One event to emit, in Python's order.
#[derive(Debug, Clone, PartialEq)]
pub struct GateEvent {
    /// The event type.
    pub name: &'static str,
    /// The payload, before the run log's redaction.
    pub payload: Json,
    /// The emitting side.
    pub origin: EventOrigin,
    /// `correlation_id` of the record.
    pub correlation_id: Option<String>,
    /// `task_id` of the record.
    pub task_id: Option<String>,
}

/// The verdict file: `<verdicts dir>/<name>`, content `bytes`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateFile {
    /// `<task>.<gate>.json`.
    pub name: String,
    /// `json.dumps(env, indent=2)`.
    pub bytes: String,
}

/// One `verdicts` row (`TaskStore.record_verdict`).
#[derive(Debug, Clone, PartialEq)]
pub struct VerdictRow {
    /// The target task.
    pub task_id: String,
    /// `gate`.
    pub gate: String,
    /// `verdict`.
    pub verdict: String,
    /// `agent_id`.
    pub agent_id: String,
    /// `json.dumps(findings)`.
    pub findings: String,
    /// `now + expires_s`.
    pub expires_at: f64,
    /// `ts`.
    pub ts: f64,
    /// `json.dumps(envelope)`.
    pub envelope_json: String,
    /// The envelope's `sig`.
    pub sig: String,
}

/// One store write, in order.
#[derive(Debug, Clone, PartialEq)]
pub enum GateWrite {
    /// `INSERT INTO verdicts`.
    Verdict(VerdictRow),
    /// `UPDATE tasks SET notes=?, updated_at=?` (`append_feedback`).
    Notes {
        /// The task.
        task_id: String,
        /// `json.dumps(notes)`.
        text: String,
        /// `updated_at`.
        updated_at: f64,
    },
}

/// Everything `issue_gate` does, as data.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct GatePlan {
    /// Whether `swarm_dir/verdicts` is created (it is, from step 3 on, even when step 4 refuses).
    pub ensure_verdicts_dir: bool,
    /// The verdict file; `None` when Python wrote none.
    pub file: Option<GateFile>,
    /// The events, in order.
    pub events: Vec<GateEvent>,
    /// The file envelope Python returns (the first member of its tuple); `None` when Python raised.
    pub envelope: Option<Json>,
    /// The per-target envelopes Python returns (its `recorded` dict), in order.
    pub recorded: Vec<(String, Json)>,
    /// The store writes, all in one transaction. Empty when `error` is set.
    pub writes: Vec<GateWrite>,
    /// Python's own `SwarmError`, when it raises one; the file and events above happened before it.
    pub error: Option<SeamError>,
}

/// `_uuid7`'s millisecond field.
fn now_ms(now: f64) -> u64 {
    (now * 1000.0) as u64
}

/// `time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".000Z"`.
fn iso(now: f64) -> String {
    let seconds = now.floor() as i64;
    let days = seconds.div_euclid(86_400);
    let rest = seconds.rem_euclid(86_400);
    // Days since 1970-01-01 to a civil date (H. Hinnant's algorithm).
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.000Z",
        rest / 3600,
        rest % 3600 / 60,
        rest % 60
    )
}

/// Python's `repr` of a string, for ASCII text only.
fn repr_str(text: &str) -> Result<String, Decline> {
    if !text.is_ascii() {
        return Err(Decline(
            "a refusal text would need the repr of non-ASCII text",
        ));
    }
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if u32::from(c) < 0x20 || u32::from(c) == 0x7f => {
                out.push_str(&format!("\\x{:02x}", u32::from(c)));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    Ok(out)
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

/// Index of a finding's severity, `None` when it is not a finding Python's `derive_verdict` can read.
fn severity_rank(finding: &Json) -> Option<usize> {
    let Json::Obj(fields) = finding else {
        return None;
    };
    let Some(Json::Str(severity)) = obj_get(fields, "severity") else {
        return None;
    };
    SEVERITIES.iter().position(|known| known == severity)
}

/// `derive_verdict`: `fail` on any finding at or above `major`. The findings were validated by [`validate_request`].
fn derive_verdict(findings: &[Json]) -> &'static str {
    if findings
        .iter()
        .any(|finding| severity_rank(finding).is_some_and(|rank| rank >= 2))
    {
        "fail"
    } else {
        "pass"
    }
}

/// Sets `key` to `value`, in place when it exists and at the end otherwise (a Python `dict` assignment).
fn upsert(fields: &mut Fields, key: &str, value: Json) {
    match fields.iter_mut().find(|(name, _)| name == key) {
        Some(slot) => slot.1 = value,
        None => fields.push((key.to_string(), value)),
    }
}

/// The canned finding of `swarm/verdicts.py::SIM_FINDING`.
fn sim_finding() -> Json {
    Json::Obj(vec![
        ("id".to_string(), jstr("SIM-1")),
        ("severity".to_string(), jstr("major")),
        ("kind".to_string(), jstr("functional")),
        ("summary".to_string(), jstr("simulated gate failure")),
        ("evidence".to_string(), jstr("")),
        ("ac_ref".to_string(), Json::Null),
        ("owner_suggestion".to_string(), Json::Null),
        ("location".to_string(), Json::Null),
    ])
}

/// The checks that make every later step total. Everything here is a [`Decline`]: Python answers.
pub fn validate_request(request: &GateIssueRequest) -> Result<(), Decline> {
    if !GATES.contains(&request.gate.as_str()) {
        return Err(Decline("the gate is not one of the four gates"));
    }
    if !request.now.is_finite() || !(0.0..253_402_300_800.0).contains(&request.now) {
        return Err(Decline(
            "the clock reading is outside the range Python formats",
        ));
    }
    let lists =
        std::iter::once(&request.findings).chain(request.per_target.iter().map(|(_, items)| items));
    for items in lists {
        if items.iter().any(|item| severity_rank(item).is_none()) {
            return Err(Decline(
                "a finding has no severity Python's derive_verdict reads",
            ));
        }
        if items.iter().any(has_non_finite) {
            return Err(Decline("a finding holds NaN or an infinite number"));
        }
    }
    if has_non_finite(&Json::Obj(request.runs.clone()))
        || has_non_finite(&Json::Obj(request.extra.clone()))
    {
        return Err(Decline("runs or extra hold NaN or an infinite number"));
    }
    if request
        .extra
        .iter()
        .any(|(key, _)| RESERVED_EXTRA.contains(&key.as_str()))
    {
        return Err(Decline(
            "extra names a payload member Python would override",
        ));
    }
    if let Some(task_id) = &request.task_id {
        if !is_safe_id(task_id) {
            return Err(Decline("the task id is not a safe id"));
        }
    }
    for (index, (target, _)) in request.per_target.iter().enumerate() {
        if request.per_target[..index]
            .iter()
            .any(|(earlier, _)| earlier == target)
        {
            return Err(Decline("per_target names a task twice"));
        }
    }
    if request
        .dryrun_fail
        .chars()
        .any(|c| ('\u{1c}'..='\u{1f}').contains(&c))
    {
        return Err(Decline(
            "SWARM_DRYRUN_FAIL holds a character Python strips and Rust does not",
        ));
    }
    Ok(())
}

/// Why a derivation stopped.
enum Stop {
    Decline(Decline),
    Refuse(SeamError),
}

impl From<Decline> for Stop {
    fn from(decline: Decline) -> Self {
        Stop::Decline(decline)
    }
}

/// A signed envelope: the value, its `sig` and whether it verifies under the same keys (`validate_verdict`).
struct Signed {
    json: Json,
    sig: String,
    verified: bool,
}

struct Deriver<'a> {
    request: &'a GateIssueRequest,
    facts: &'a StoreFacts,
    keys: &'a KeyMaterial,
    ids: &'a mut dyn EnvelopeIds,
    plan: GatePlan,
    extra: Fields,
}

/// Derives the whole outcome of `issue_gate`. See the module documentation.
///
/// # Errors
///
/// A [`Decline`] when the request is one this module will not answer. Python's own refusals are in [`GatePlan::error`].
pub fn derive_gate_issue(
    request: &GateIssueRequest,
    facts: &StoreFacts,
    keys: &KeyMaterial,
    ids: &mut dyn EnvelopeIds,
) -> Result<GatePlan, Decline> {
    validate_request(request)?;
    let mut extra = request.extra.clone();
    if request.simulate {
        upsert(&mut extra, "dry_run", Json::Bool(true));
    }
    let mut deriver = Deriver {
        request,
        facts,
        keys,
        ids,
        plan: GatePlan::default(),
        extra,
    };
    match deriver.run() {
        Ok(()) => Ok(deriver.plan),
        Err(Stop::Decline(decline)) => Err(decline),
        Err(Stop::Refuse(error)) => {
            // Python raised: the transaction (if any) rolled back, nothing is returned.
            deriver.plan.writes.clear();
            deriver.plan.recorded.clear();
            deriver.plan.envelope = None;
            deriver.plan.error = Some(error);
            Ok(deriver.plan)
        }
    }
}

impl Deriver<'_> {
    fn script_event(&mut self, payload_task: Option<&str>, reason: &str) {
        let payload = Json::Obj(vec![
            ("task_id".to_string(), payload_task.map_or(Json::Null, jstr)),
            ("gate".to_string(), jstr(&self.request.gate)),
            ("reason".to_string(), jstr(reason)),
        ]);
        self.plan.events.push(GateEvent {
            name: "gate.verdict.unrecorded",
            payload,
            origin: EventOrigin::Script,
            correlation_id: self.request.correlation_id.clone(),
            task_id: self.request.task_id.clone(),
        });
    }

    /// `resolve_targets`: `Ok(None)` is "not a gate task".
    fn resolve_targets(&self, task_id: &str) -> Result<Option<Vec<String>>, Stop> {
        let request = self.request;
        let Some(task) = self.facts.get(task_id)? else {
            return Ok(None);
        };
        let Some(gate) = obj_get(&task.notes, "gate").filter(|gate| truthy(gate)) else {
            return Ok(None);
        };
        let policy = |message: String| {
            Stop::Refuse(
                SeamError::new(ErrorCode::Policy, message)
                    .with_details(vec![("task_id".to_string(), jstr(task_id))]),
            )
        };
        let Json::Str(task_gate) = gate else {
            return Err(Decline("notes.gate is truthy and not text").into());
        };
        if task_gate != &request.gate {
            return Err(policy(format!(
                "{task_id} is a {task_gate} gate task; this script issues {}",
                request.gate
            )));
        }
        if let Some(given) = request
            .correlation_id
            .as_deref()
            .filter(|given| !given.is_empty())
        {
            if task.correlation_id.as_deref() != Some(given) {
                let own = match &task.correlation_id {
                    Some(own) => repr_str(own)?,
                    None => "None".to_string(),
                };
                let own_plain = task.correlation_id.as_deref().unwrap_or("None");
                return Err(policy(format!(
                    "gate task {task_id} belongs to correlation {own}, not {}; --correlation-id defaults to the SWARM_CORRELATION_ID env var: unset a stale SWARM_CORRELATION_ID export or pass --correlation-id {own_plain}",
                    repr_str(given)?
                )));
            }
        }
        let targets = match obj_get(&task.notes, "gate_for") {
            None => Vec::new(),
            Some(value) if !truthy(value) => Vec::new(),
            Some(Json::Arr(items)) => {
                let mut targets = Vec::with_capacity(items.len());
                for item in items {
                    let Json::Str(target) = item else {
                        return Err(Decline("notes.gate_for holds a value that is not text").into());
                    };
                    targets.push(target.clone());
                }
                targets
            }
            Some(_) => return Err(Decline("notes.gate_for is truthy and not a list").into()),
        };
        if !targets.is_empty() && task.state != "IN_PROGRESS" {
            return Err(policy(format!(
                "{task_id} is not a running gate task (state {}); lease it first",
                task.state
            )));
        }
        Ok(Some(targets))
    }

    /// `build_envelope(source=agent_id, target="A01", msg_type="gate.verdict", priority="P1")`.
    fn build_envelope(
        &mut self,
        payload: Fields,
        correlation: Option<&str>,
    ) -> Result<Fields, Stop> {
        let now = self.request.now;
        let msg_id = self
            .ids
            .msg_id(now_ms(now))
            .ok_or(Decline("the id source cannot draw"))?;
        let correlation = match correlation.filter(|value| !value.is_empty()) {
            Some(value) => value.to_string(),
            None => self
                .ids
                .correlation_id()
                .ok_or(Decline("the id source cannot draw"))?,
        };
        let trace = self
            .ids
            .trace_id()
            .ok_or(Decline("the id source cannot draw"))?;
        Ok(vec![
            ("msg_id".to_string(), Json::Str(msg_id)),
            ("correlation_id".to_string(), Json::Str(correlation)),
            ("trace_id".to_string(), Json::Str(trace)),
            ("causation_id".to_string(), Json::Null),
            ("source".to_string(), jstr(&self.request.agent_id)),
            ("target".to_string(), jstr("A01")),
            ("type".to_string(), jstr("gate.verdict")),
            ("schema".to_string(), jstr("swarm.v1.gate.verdict")),
            ("ts".to_string(), Json::Str(iso(now))),
            ("ttl_s".to_string(), Json::Int(300)),
            ("priority".to_string(), jstr("P1")),
            ("risk_class".to_string(), jstr("low")),
            ("payload".to_string(), Json::Obj(payload)),
            ("sig".to_string(), Json::Null),
        ])
    }

    /// The payload of `make_verdict` (`advisory = None`) or `advisory_envelope` (`Some(reason)`).
    fn payload(
        &self,
        task: &str,
        verdict: &str,
        findings: &[Json],
        extra: &Fields,
        advisory: Option<&str>,
    ) -> Fields {
        let request = self.request;
        let mut payload = vec![
            ("gate".to_string(), jstr(&request.gate)),
            ("task_id".to_string(), jstr(task)),
            ("verdict".to_string(), jstr(verdict)),
            ("findings".to_string(), Json::Arr(findings.to_vec())),
            ("runs".to_string(), Json::Obj(request.runs.clone())),
            ("expires_s".to_string(), Json::Int(request.expires_s)),
            ("issued_at".to_string(), Json::Float(request.now)),
        ];
        if let Some(reason) = advisory {
            payload.push(("advisory".to_string(), Json::Bool(true)));
            payload.push(("advisory_reason".to_string(), jstr(reason)));
        }
        // Python's `{signed fields, **extra}`: an `extra` entry naming a signed field is ignored; any other entry is set the
        // way a dict assignment would (in place when it exists, at the end otherwise).
        for (key, value) in extra {
            if !SIGNED_FIELDS.contains(&key.as_str()) {
                upsert(&mut payload, key, value.clone());
            }
        }
        payload
    }

    /// `advisory_envelope`: unsigned, `sig: null`.
    fn advisory(
        &mut self,
        task: &str,
        findings: &[Json],
        correlation: Option<&str>,
        reason: &str,
    ) -> Result<Fields, Stop> {
        let extra = self.extra.clone();
        let payload = self.payload(
            task,
            derive_verdict(findings),
            findings,
            &extra,
            Some(reason),
        );
        self.build_envelope(payload, correlation)
    }

    /// `make_verdict` + `sign_envelope`; the dev-key audit event is pushed when `audit`.
    fn signed(
        &mut self,
        task: &str,
        findings: &[Json],
        forced_verdict: Option<&str>,
        correlation: Option<&str>,
        extra: &Fields,
        audit: bool,
    ) -> Result<Signed, Stop> {
        let verdict = forced_verdict.unwrap_or_else(|| derive_verdict(findings));
        let payload = self.payload(task, verdict, findings, extra, None);
        let fields = self.build_envelope(payload, correlation)?;
        let text = to_python_dumps(&Json::Obj(fields));
        let mut parsed = parse_envelope(&text)
            .map_err(|_| Decline("the envelope text cannot be parsed back exactly"))?;
        let outcome = match sign_envelope(&mut parsed, self.keys) {
            Ok(outcome) => outcome,
            Err(SignError::Policy(error)) => return Err(Stop::Refuse(error)),
            Err(SignError::Declined(_)) => {
                return Err(Decline("the envelope signer declined").into())
            }
        };
        let json = parsed.as_json().clone();
        let Json::Obj(members) = &json else {
            return Err(Decline("the signed envelope is not an object").into());
        };
        let Some(Json::Str(sig)) = obj_get(members, "sig") else {
            return Err(Decline("the signed envelope has no signature").into());
        };
        let sig = sig.clone();
        let correlation = match obj_get(members, "correlation_id") {
            Some(Json::Str(value)) => value.clone(),
            _ => return Err(Decline("the signed envelope has no correlation").into()),
        };
        if audit && outcome.dev_key_audit {
            self.plan.events.push(GateEvent {
                name: "security.dev_key",
                payload: Json::Obj(vec![
                    ("msg_type".to_string(), jstr("gate.verdict")),
                    ("source".to_string(), jstr(&self.request.agent_id)),
                ]),
                origin: EventOrigin::Envelope,
                correlation_id: Some(correlation),
                task_id: None,
            });
        }
        let verified = verify_envelope(&parsed, self.keys);
        Ok(Signed {
            json,
            sig,
            verified,
        })
    }

    fn per_target_blocks(&self) -> bool {
        self.request
            .per_target
            .iter()
            .any(|(_, items)| derive_verdict(items) == "fail")
    }

    fn write_file(&mut self, task: &str, envelope: &Json) {
        self.plan.file = Some(GateFile {
            name: format!("{task}.{}.json", self.request.gate),
            bytes: to_python_json(envelope),
        });
    }

    fn run(&mut self) -> Result<(), Stop> {
        let request = self.request;
        let facts = self.facts;
        let targets = match &request.task_id {
            Some(task_id) => self.resolve_targets(task_id)?,
            None => None,
        };
        let gate_task = match (&request.task_id, &targets) {
            (Some(task_id), Some(_)) => facts.get(task_id)?,
            _ => None,
        };
        let correlation: Option<String> = request
            .correlation_id
            .clone()
            .filter(|value| !value.is_empty())
            .or_else(|| gate_task.and_then(|task| task.correlation_id.clone()));
        let placeholder = if request.simulate {
            "T-dry"
        } else {
            "T-unassigned"
        };
        let task: String = request
            .task_id
            .clone()
            .unwrap_or_else(|| placeholder.to_string());
        self.plan.ensure_verdicts_dir = true;
        if let Some(message) = self.keys.config_error() {
            return Err(Stop::Refuse(
                SeamError::new(ErrorCode::Policy, message)
                    .with_details(vec![("task_id".to_string(), jstr(&task))]),
            ));
        }
        let session = request.session;
        if !self.keys.real_key_configured() && !self.keys.dev_key_active() {
            return self.run_advisory(&task, correlation.as_deref(), session);
        }
        let forced = (session && self.per_target_blocks()).then_some("fail");
        let extra = self.extra.clone();
        let file = self.signed(
            &task,
            &request.findings,
            forced,
            correlation.as_deref(),
            &extra,
            !session,
        )?;
        self.write_file(&task, &file.json);
        self.plan.envelope = Some(file.json);
        if session {
            self.script_event(request.task_id.as_deref(), SESSION_UNRECORDED);
            return Ok(());
        }
        let Some(store_task) = &request.task_id else {
            self.script_event(None, "no --task-id");
            return Ok(());
        };
        let mut per_target = request.per_target.clone();
        if request.simulate {
            if targets.as_ref().is_some_and(|list| !list.is_empty())
                && !gate_task
                    .and_then(|task| obj_get(&task.notes, "dry_run"))
                    .is_some_and(truthy)
            {
                self.script_event(Some(store_task), "dry-run outside a runner dry-run");
                return Ok(());
            }
            per_target = self.simulated(targets.as_deref().unwrap_or(&[]));
        }
        self.record(store_task, targets, &per_target)
    }

    /// Step 5: the keyless advisory path.
    fn run_advisory(
        &mut self,
        task: &str,
        correlation: Option<&str>,
        session: bool,
    ) -> Result<(), Stop> {
        let request = self.request;
        let facts = self.facts;
        let reason = if session {
            SESSION_UNRECORDED
        } else {
            MISSING_KEY_REASON
        };
        let mut fields = self.advisory(task, &request.findings, correlation, reason)?;
        if self.per_target_blocks() {
            if let Some((_, Json::Obj(payload))) =
                fields.iter_mut().find(|(key, _)| key == "payload")
            {
                upsert(payload, "verdict", jstr("fail"));
            }
        }
        let mut recorded: Vec<(String, Json)> = Vec::new();
        for (target, items) in &request.per_target {
            let mut target_correlation = correlation.map(str::to_string);
            if request.task_id.is_some() {
                if let Some(row) = facts.get(target)? {
                    target_correlation = row.correlation_id.clone();
                }
            }
            let fields = self.advisory(target, items, target_correlation.as_deref(), reason)?;
            upsert(&mut recorded, target, Json::Obj(fields));
        }
        let envelope = Json::Obj(fields);
        self.write_file(task, &envelope);
        self.script_event(request.task_id.as_deref(), reason);
        self.plan.envelope = Some(envelope);
        self.plan.recorded = recorded;
        Ok(())
    }

    /// `simulated_per_target` + the caller's findings, per target in order.
    fn simulated(&self, targets: &[String]) -> Vec<(String, Vec<Json>)> {
        let request = self.request;
        let failing: Vec<&str> = request
            .dryrun_fail
            .split(',')
            .map(str::trim)
            .filter(|entry| !entry.is_empty())
            .collect();
        let mut out: Vec<(String, Vec<Json>)> = Vec::new();
        for target in targets {
            let mut items = Vec::new();
            if failing.contains(&format!("{target}:{}", request.gate).as_str()) {
                items.push(sim_finding());
            }
            items.extend(request.findings.iter().cloned());
            match out.iter_mut().find(|(name, _)| name == target) {
                Some(slot) => slot.1 = items,
                None => out.push((target.clone(), items)),
            }
        }
        out
    }

    /// `record_gate_verdicts`.
    fn record(
        &mut self,
        gate_task_id: &str,
        targets: Option<Vec<String>>,
        per_target: &[(String, Vec<Json>)],
    ) -> Result<(), Stop> {
        let request = self.request;
        let facts = self.facts;
        let targets = match targets {
            Some(list) if !list.is_empty() => list,
            other => {
                let reason = if other.is_none() {
                    "not a gate task"
                } else {
                    "empty gate_for"
                };
                self.script_event(Some(gate_task_id), reason);
                return Ok(());
            }
        };
        let mut extra = self.extra.clone();
        upsert(&mut extra, "gate_task", jstr(gate_task_id));
        let mut notes_cache: Vec<(String, Fields)> = Vec::new();
        let mut recorded: Vec<(String, Json)> = Vec::new();
        for target in &targets {
            let tf: &[Json] = per_target
                .iter()
                .find(|(name, _)| name == target)
                .map_or(&request.findings[..], |(_, items)| &items[..]);
            let Some(row) = facts.get(target)? else {
                return Err(Stop::Refuse(SeamError::new(
                    ErrorCode::Input,
                    format!("unknown task {target}"),
                )));
            };
            let signed = self.signed(
                target,
                tf,
                None,
                row.correlation_id.as_deref(),
                &extra,
                true,
            )?;
            if !signed.verified {
                return Err(Stop::Refuse(SeamError::new(
                    ErrorCode::Policy,
                    "verdict signature invalid",
                )));
            }
            let verdict = derive_verdict(tf);
            self.plan.writes.push(GateWrite::Verdict(VerdictRow {
                task_id: target.clone(),
                gate: request.gate.clone(),
                verdict: verdict.to_string(),
                agent_id: request.agent_id.clone(),
                findings: to_python_dumps(&Json::Arr(tf.to_vec())),
                expires_at: request.now + request.expires_s as f64,
                ts: request.now,
                envelope_json: to_python_dumps(&signed.json),
                sig: signed.sig,
            }));
            if verdict == "fail" {
                let slot = match notes_cache.iter().position(|(id, _)| id == target) {
                    Some(index) => index,
                    None => {
                        notes_cache.push((target.clone(), row.notes.clone()));
                        notes_cache.len() - 1
                    }
                };
                let notes = &mut notes_cache[slot].1;
                let entry = Json::Obj(vec![
                    ("gate".to_string(), jstr(&request.gate)),
                    ("source".to_string(), jstr("script")),
                    ("findings".to_string(), Json::Arr(tf.to_vec())),
                ]);
                match notes.iter_mut().find(|(key, _)| key == "feedback") {
                    Some((_, Json::Arr(items))) => items.push(entry),
                    Some(_) => return Err(Decline("notes.feedback is not a list").into()),
                    None => notes.push(("feedback".to_string(), Json::Arr(vec![entry]))),
                }
                self.plan.writes.push(GateWrite::Notes {
                    task_id: target.clone(),
                    text: to_python_dumps(&Json::Obj(notes.clone())),
                    updated_at: request.now,
                });
            }
            match recorded.iter_mut().find(|(name, _)| name == target) {
                Some(slot) => slot.1 = signed.json,
                None => recorded.push((target.clone(), signed.json)),
            }
        }
        self.plan.recorded = recorded;
        Ok(())
    }
}

/// The rows the derivation needs, read inside the transaction: the gate task, then its `gate_for` targets and the `per_target` keys.
async fn read_facts(tx: &mut Tx, request: &GateIssueRequest) -> Result<StoreFacts, WriterError> {
    let mut facts = StoreFacts::default();
    let Some(task_id) = &request.task_id else {
        return Ok(facts);
    };
    let mut wanted: Vec<String> = Vec::new();
    let row = tx.task(task_id).await?;
    let gate_task = match row {
        Some(row) => Some(TaskFacts::from_row(&row)?),
        None => None,
    };
    if let Some(task) = &gate_task {
        if let Some(Json::Arr(items)) = obj_get(&task.notes, "gate_for") {
            wanted.extend(items.iter().filter_map(|item| match item {
                Json::Str(target) => Some(target.clone()),
                _ => None,
            }));
        }
    }
    wanted.extend(request.per_target.iter().map(|(target, _)| target.clone()));
    facts.tasks.push((task_id.clone(), gate_task));
    for id in wanted {
        if facts.tasks.iter().any(|(known, _)| known == &id) {
            continue;
        }
        let task = match tx.task(&id).await? {
            Some(row) => Some(TaskFacts::from_row(&row)?),
            None => None,
        };
        facts.tasks.push((id, task));
    }
    Ok(facts)
}

// ---- the wire form ------------------------------------------------------------------------------------------------------------

/// Members of the wire form of a [`GateIssueRequest`].
const REQUEST_KEYS: [&str; 13] = [
    "gate",
    "agent_id",
    "findings",
    "runs",
    "expires_s",
    "extra",
    "simulate",
    "per_target",
    "task_id",
    "correlation_id",
    "session",
    "dryrun_fail",
    "now",
];

impl GateIssueRequest {
    /// Reads the wire form `{"gate", "agent_id", "findings", "runs", "expires_s", "extra", "simulate", "per_target", "task_id",
    /// "correlation_id", "session", "dryrun_fail", "now"}`. `gate`, `agent_id` and `now` are required; the others default to
    /// Python's defaults (`[]`, `{}`, 86400, `None`, `False`, `""`). `extra`, `per_target`, `task_id` and `correlation_id` may be `null`.
    ///
    /// # Errors
    ///
    /// A [`Decline`] for an unknown member or a value of the wrong type; the text never quotes a value.
    pub fn from_json(value: &Json) -> Result<Self, Decline> {
        let Json::Obj(fields) = value else {
            return Err(Decline("the request is not an object"));
        };
        if fields
            .iter()
            .any(|(key, _)| !REQUEST_KEYS.contains(&key.as_str()))
        {
            return Err(Decline("the request has an unknown member"));
        }
        let text = |key: &str| -> Result<Option<String>, Decline> {
            match obj_get(fields, key) {
                None | Some(Json::Null) => Ok(None),
                Some(Json::Str(text)) => Ok(Some(text.clone())),
                Some(_) => Err(Decline("a request member is not text")),
            }
        };
        let object = |key: &str| -> Result<Fields, Decline> {
            match obj_get(fields, key) {
                None | Some(Json::Null) => Ok(Vec::new()),
                Some(Json::Obj(members)) => Ok(members.clone()),
                Some(_) => Err(Decline("a request member is not an object")),
            }
        };
        let flag = |key: &str| -> Result<bool, Decline> {
            match obj_get(fields, key) {
                None => Ok(false),
                Some(Json::Bool(flag)) => Ok(*flag),
                Some(_) => Err(Decline("a request member is not a boolean")),
            }
        };
        let list = |items: &Json| -> Result<Vec<Json>, Decline> {
            match items {
                Json::Arr(items) => Ok(items.clone()),
                _ => Err(Decline("a request member is not a list")),
            }
        };
        let findings = match obj_get(fields, "findings") {
            None => Vec::new(),
            Some(items) => list(items)?,
        };
        let mut per_target = Vec::new();
        for (target, items) in object("per_target")? {
            per_target.push((target, list(&items)?));
        }
        let expires_s = match obj_get(fields, "expires_s") {
            None => 86_400,
            Some(Json::Int(number)) => *number,
            Some(_) => return Err(Decline("expires_s is not an integer")),
        };
        let now = match obj_get(fields, "now") {
            Some(Json::Float(number)) => *number,
            Some(Json::Int(number)) => *number as f64,
            _ => return Err(Decline("the request has no clock reading")),
        };
        Ok(Self {
            gate: text("gate")?.ok_or(Decline("the request has no gate"))?,
            agent_id: text("agent_id")?.ok_or(Decline("the request has no agent_id"))?,
            findings,
            runs: object("runs")?,
            expires_s,
            extra: object("extra")?,
            simulate: flag("simulate")?,
            per_target,
            task_id: text("task_id")?,
            correlation_id: text("correlation_id")?,
            session: flag("session")?,
            dryrun_fail: text("dryrun_fail")?.unwrap_or_default(),
            now,
        })
    }
}

impl GatePlan {
    /// The plan as the oracle vectors record Python's outcome: `ensure_verdicts_dir`, `file` `{name, text}` or `null`, `events`
    /// `[{name, payload, origin, correlation_id, task_id}]`, `envelope`, `recorded` `[[target, envelope]]`, `error`
    /// `{code, message, details}` or `null`, and the number of store writes.
    pub fn to_json(&self) -> Json {
        let events = self
            .events
            .iter()
            .map(|event| {
                Json::Obj(vec![
                    ("name".to_string(), jstr(event.name)),
                    ("payload".to_string(), event.payload.clone()),
                    (
                        "origin".to_string(),
                        jstr(match event.origin {
                            EventOrigin::Script => "script",
                            EventOrigin::Envelope => "envelope",
                        }),
                    ),
                    (
                        "correlation_id".to_string(),
                        event.correlation_id.as_deref().map_or(Json::Null, jstr),
                    ),
                    (
                        "task_id".to_string(),
                        event.task_id.as_deref().map_or(Json::Null, jstr),
                    ),
                ])
            })
            .collect();
        Json::Obj(vec![
            (
                "ensure_verdicts_dir".to_string(),
                Json::Bool(self.ensure_verdicts_dir),
            ),
            (
                "file".to_string(),
                self.file.as_ref().map_or(Json::Null, |file| {
                    Json::Obj(vec![
                        ("name".to_string(), jstr(&file.name)),
                        ("text".to_string(), jstr(&file.bytes)),
                    ])
                }),
            ),
            ("events".to_string(), Json::Arr(events)),
            (
                "envelope".to_string(),
                self.envelope.clone().unwrap_or(Json::Null),
            ),
            (
                "recorded".to_string(),
                Json::Arr(
                    self.recorded
                        .iter()
                        .map(|(target, envelope)| Json::Arr(vec![jstr(target), envelope.clone()]))
                        .collect(),
                ),
            ),
            (
                "error".to_string(),
                self.error.as_ref().map_or(Json::Null, |error| {
                    Json::Obj(vec![
                        ("code".to_string(), jstr(error.code().as_str())),
                        ("message".to_string(), jstr(error.message())),
                        ("details".to_string(), Json::Obj(error.details().to_vec())),
                    ])
                }),
            ),
            (
                "writes".to_string(),
                Json::Int(i64::try_from(self.writes.len()).unwrap_or(i64::MAX)),
            ),
        ])
    }
}

impl TaskStoreWriter {
    /// `issue_gate`'s tail on the host: derive the outcome inside one `BEGIN IMMEDIATE` transaction and write the verdict rows and
    /// the `notes.feedback` rewrites of a `fail`, all in that transaction, or nothing.
    ///
    /// Without a task id the store is not touched. When the plan carries an [`GatePlan::error`] or no write, the transaction is
    /// rolled back unchanged. The verdict file and the events are the caller's, from the returned plan.
    ///
    /// # Errors
    ///
    /// A not-started [`WriterError`] for a [`Decline`] or a store that cannot be opened, read or locked; a final one when a
    /// write fails (the transaction is rolled back) or the commit fails.
    pub async fn issue_gate(
        &self,
        request: &GateIssueRequest,
        keys: &KeyMaterial,
        ids: &mut dyn EnvelopeIds,
    ) -> Result<GatePlan, WriterError> {
        validate_request(request)?;
        if request.task_id.is_none() {
            return Ok(derive_gate_issue(
                request,
                &StoreFacts::default(),
                keys,
                ids,
            )?);
        }
        let mut tx = self.store().begin().await?;
        let planned = match read_facts(&mut tx, request).await {
            Ok(facts) => derive_gate_issue(request, &facts, keys, ids).map_err(WriterError::from),
            Err(error) => Err(error),
        };
        let plan = match planned {
            Ok(plan) => plan,
            Err(error) => {
                tx.rollback().await.ok();
                return Err(error);
            }
        };
        if plan.error.is_some() || plan.writes.is_empty() {
            tx.rollback().await.ok();
            return Ok(plan);
        }
        for write in &plan.writes {
            let written = match write {
                GateWrite::Verdict(row) => {
                    tx.insert_verdict(&NewVerdict {
                        task_id: &row.task_id,
                        gate: &row.gate,
                        verdict: &row.verdict,
                        agent_id: &row.agent_id,
                        findings: &row.findings,
                        expires_at: row.expires_at,
                        ts: row.ts,
                        envelope_json: &row.envelope_json,
                        sig: &row.sig,
                    })
                    .await
                }
                GateWrite::Notes {
                    task_id,
                    text,
                    updated_at,
                } => {
                    tx.update_text(task_id, TextColumn::Notes, text, *updated_at)
                        .await
                }
            };
            if let Err(error) = written {
                tx.rollback().await.ok();
                return Err(error);
            }
        }
        tx.commit().await?;
        Ok(plan)
    }
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;

    use gsd_envelope::{KeyFilePresence, KeyMaterial};
    use gsd_route::{jstr, obj_get, Fields, Json};

    use super::{Deriver, EnvelopeIds, GateIssueRequest, GatePlan, StoreFacts};

    /// Ids that are never drawn: `payload` takes none.
    struct NoIds;

    impl EnvelopeIds for NoIds {
        fn msg_id(&mut self, _now_ms: u64) -> Option<String> {
            None
        }
        fn correlation_id(&mut self) -> Option<String> {
            None
        }
        fn trace_id(&mut self) -> Option<String> {
            None
        }
    }

    fn request(extra: Fields) -> GateIssueRequest {
        GateIssueRequest {
            gate: "review".to_string(),
            agent_id: "A09".to_string(),
            findings: Vec::new(),
            runs: Vec::new(),
            expires_s: 86_400,
            extra,
            simulate: false,
            per_target: Vec::new(),
            task_id: Some("T-1".to_string()),
            correlation_id: None,
            session: false,
            dryrun_fail: String::new(),
            now: 1_700_000_000.5,
        }
    }

    /// `payload(task, verdict, findings, extra, advisory)` of a deriver over `request`, with no store and no keys.
    fn payload_of(request: &GateIssueRequest) -> Fields {
        let facts = StoreFacts::default();
        let keys = KeyMaterial::from_lookup(|_| None::<OsString>, |_| KeyFilePresence::Absent)
            .expect("keyless key material");
        let mut ids = NoIds;
        let deriver = Deriver {
            request,
            facts: &facts,
            keys: &keys,
            ids: &mut ids,
            plan: GatePlan::default(),
            extra: request.extra.clone(),
        };
        deriver.payload("T-1", "fail", &[], &request.extra, None)
    }

    /// P1-06: `swarm/gates.py::make_verdict` ignores an `extra` entry that names a signed field; this is the Rust mirror.
    #[test]
    fn extra_cannot_override_the_signed_payload_fields() {
        let extra: Fields = vec![
            ("gate".to_string(), jstr("release")),
            ("task_id".to_string(), jstr("T-OTHER")),
            ("verdict".to_string(), jstr("pass")),
            ("findings".to_string(), Json::Arr(Vec::new())),
            (
                "runs".to_string(),
                Json::Obj(vec![("x".to_string(), Json::Int(1))]),
            ),
            ("expires_s".to_string(), Json::Int(999_999)),
            ("waived_by".to_string(), jstr("alice")),
        ];
        let payload = payload_of(&request(extra));

        let names: Vec<&str> = payload.iter().map(|(key, _)| key.as_str()).collect();
        for key in [
            "gate",
            "task_id",
            "verdict",
            "findings",
            "runs",
            "expires_s",
        ] {
            assert_eq!(
                names.iter().filter(|name| **name == key).count(),
                1,
                "signed field {key} must appear exactly once"
            );
        }
        assert!(matches!(obj_get(&payload, "gate"), Some(Json::Str(v)) if v == "review"));
        assert!(matches!(obj_get(&payload, "task_id"), Some(Json::Str(v)) if v == "T-1"));
        assert!(matches!(obj_get(&payload, "verdict"), Some(Json::Str(v)) if v == "fail"));
        assert!(matches!(
            obj_get(&payload, "expires_s"),
            Some(Json::Int(86_400))
        ));
        assert!(
            matches!(obj_get(&payload, "runs"), Some(Json::Obj(members)) if members.is_empty())
        );
        assert!(
            matches!(obj_get(&payload, "findings"), Some(Json::Arr(items)) if items.is_empty())
        );
        assert!(matches!(obj_get(&payload, "waived_by"), Some(Json::Str(v)) if v == "alice"));
    }

    /// `issued_at` stays injectable: the signed store passes its own clock reading through `extra`.
    #[test]
    fn issued_at_in_extra_still_replaces_the_clock_reading() {
        let extra: Fields = vec![("issued_at".to_string(), Json::Float(5.0))];
        let payload = payload_of(&request(extra));
        assert!(matches!(obj_get(&payload, "issued_at"), Some(Json::Float(v)) if *v == 5.0));
        assert_eq!(
            payload.iter().filter(|(key, _)| key == "issued_at").count(),
            1
        );
    }
}
