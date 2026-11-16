//! Pure derivation of `scripts/orch_plan.py`: the task DAG of a brief, as the rows the host creates and the bytes it writes.
//!
//! [`derive_plan`] reads the facts of one call (the arguments, the roster of agents, the tasks the store already holds under the candidate
//! prefixes, the signing state, the host's fresh correlation id and `now`) and returns what `run()` of `orch_plan.py` does, in its order: the
//! reply, the audited store operations, the plan file, the effects to replay after the commit, and the controller kickoff to sign. It opens no
//! database, file, process or clock, draws no random number and reads no environment or key. The host reads and applies; this module decides.
//!
//! # Inventory of `scripts/orch_plan.py` (507 lines) and `swarm/blast_radius.py` (335 lines)
//!
//! ## Inputs
//!
//! * The arguments (`add_args` plus `AgentScript.parser`): `--brief FILE`, `--brief-text`, `--pattern` (`feature`, `hotfix`, `dependency`,
//!   `cloud-native`, `cluster`, `web3`, `custom`, `parallel`), `--slices FILE`, `--slices-json`, `--plan FILE`, `--risk-class`
//!   (`low`, `medium`, `high`), `--priority` (`P0` to `P3`), `--prefix`, `--acceptance` (repeatable), `--graph-id`, `--correlation-id` (or
//!   `SWARM_CORRELATION_ID`), `--dry-run`, `--root` (alias `--repo`), `--json`, `--task-id`. [`PlanRequest`] carries every one the derivation
//!   reads; `--root`, `--json` and `--task-id` stay with the host.
//! * Files, read by the host and handed over as text ([`FileRead`]): the brief when `--brief` names an existing path
//!   (`Path(args.brief).exists()`, then `read_text(encoding="utf-8")`), the custom plan (`Path(args.plan).read_text()`, locale encoding) and
//!   the slice file (`read_text(encoding="utf-8")`). A path that exists but cannot be read is [`FileRead::Unreadable`]; Python then raises an
//!   exception its generic handler prints with a traceback, so the derivation refuses at the point where Python would have read it.
//! * The roster `swarm.manifest` (`by_capability`, `get_agent`): the `id`, `code`, `slug` and `capabilities` of every agent, in roster order
//!   ([`Agent`]). It is read for custom plans and for blast-radius slices only.
//! * The Task Store, in one `BEGIN IMMEDIATE` snapshot: every `tasks` row whose `task_id` starts with `<p>-` for each candidate prefix `p`
//!   ([`candidate_prefixes`]; `_existing` reads them one by one with `store.get`), and every `tasks` row of the correlations those rows belong
//!   to and of the correlation being planned, in `ORDER BY dag_depth, created_at` order (`store.list(correlation_id=...)`, which
//!   `_tasks_from_store` calls for the plan file and for a reuse). Rows are `SELECT *` dicts, JSON columns still text ([`PlanFacts`]).
//! * Per correlation of those rows, the text of `<swarm_dir>/plans/<corr>.json` or `None` when it cannot be opened (`_read_plan_tasks`,
//!   `_execution_result`), and `str(swarm_dir/plans)` for the `plan_file` text of the reply.
//! * The environment: `SWARM_AGENT_SESSION` or `SWARM_CHILD` equal to `1` (a child session never signs a kickoff), `signing_config_error()`
//!   and `real_key_configured()` ([`Signing`]); `SUBSTRATE_URL` and `SUBSTRATE_GRAPH_ID` are read by `_bind_graph` only, which the host
//!   replays.
//! * The clock: `store.create` reads `time.time()` for `created_at` and `updated_at`, `_log` for every audit `ts`, `store.transition` for
//!   `updated_at`. The derivation uses the one `now` it is given; see "Clock".
//!
//! ## Outputs
//!
//! * The reply dict of `run()` ([`PlanDerived::reply`]; the host adds `agent` and `script` with [`crate::finish`], as `AgentScript.main` does):
//!   the dry-run object, the reuse object or the creation object.
//! * Store operations ([`PlanOp`]): for each task of the plan one `store.create` (with its `transitions` row `NULL -> CREATED`, actor `system`,
//!   reason `created`), then `transition(VALIDATED, reason="brief validated by A01")` and `transition(PLANNED, reason="pattern=<p>")`, both
//!   with the default actor `A01`, all inside the one `store.transaction()`. [`NewTask::insert_values`] is the `INSERT` of `store.create`.
//! * The plan file `plans/<corr>.json` (`json.dumps(plan, indent=2)`, published by rename) and `latest_correlation` (the bare correlation id)
//!   ([`PlanFiles`]).
//! * Effects, replayed in order by the host after the commit ([`PlanEffect`]): `_bind_graph` (described, never run: it speaks to the
//!   substrate and is fail-open) and `emit("plan.updated", {"pattern", "task_count"})`.
//! * The controller kickoff ([`Kickoff`]): `_execution_result` builds and signs a `task.assign` envelope when a real signing key is configured.
//!   The payload is derived here; the envelope (ids, timestamp, signature) is the host's.
//! * The correlation id `ctx.correlation_id` holds when the call returns ([`PlanDerived::ctx_correlation_id`]): the run-log records of
//!   `AgentScript.main` carry it.
//!
//! ## Effects of the Python code that this module only describes
//!
//! `TaskStore(root=...)` (opens the file, creates the schema), `store.transaction()` (`BEGIN IMMEDIATE`), `swarm_dir(create=True)` and the
//! `.gitignore` it writes, `plans.mkdir`, the atomic plan file, `latest_correlation`, `_bind_graph`, `ctx.emit`, the `live.json` refresh of
//! every transition, `sign_envelope`.
//!
//! ## Nondeterminism
//!
//! * `str(uuid.uuid4())` when no correlation id is given: the host draws it ([`PlanFacts::new_correlation_id`]); the derivation only uses it
//!   when [`PlanRequest::correlation_id`] is `None`.
//! * `time.time()` ([`derive_plan`]'s `now`).
//! * The Graph ID `_bind_graph` mints (`mint_graph_id`) and the winner `bind_graph` returns: not derivable, and they never enter the notes,
//!   the brief hash or the plan, so the effect carries only what the Python call is given.
//! * The envelope of the kickoff: `msg_id` (uuid7), `trace_id`, `ts`, `sig`.
//! * The plan file's tie order, see "Clock".
//!
//! # Clock
//!
//! Python reads the clock for every row and every audit entry. The plan stamps the `i`-th new task with `created_at = now + i * 1e-6`
//! ([`CREATED_AT_STEP`]): strictly increasing, so `ORDER BY dag_depth, created_at` (the order of `store.list`, of the plan file and of the
//! reuse reply) is deterministic in SQLite and equals the creation order, which is what the increasing wall clock gives Python. The host sets
//! `updated_at` and every audit `ts` to `now`. The parity masks are therefore `updated_at`, the audit `ts` and `created_at`.
//!
//! # Host contract
//!
//! 1. Parse the argv with the grammar of `orch_plan.py` (the host's own mirror of argparse) and read the files that `--brief` (when the path
//!    exists), `--plan` and `--slices` name. An empty path flag is no flag. Draw `str(uuid.uuid4())` and read `now` once.
//! 2. Check `--task-id` and `--correlation-id` with [`crate::check_safe_id`] as `AgentScript.main` does (that is not part of `run()`).
//! 3. Unless the call is a dry run, open the store and `BEGIN IMMEDIATE`; read [`PlanFacts`]: the rows under [`candidate_prefixes`], the rows of
//!    every correlation involved, and the plan file of each. Call [`derive_plan`] inside the transaction (a dry run needs no store: it reads
//!    no rows, so empty row lists give the same answer).
//! 4. [`PlanError::NotStarted`]: roll back, print an `E-DEP` reply with `error.details.write_state = "not_started"`, and let Python answer.
//!    [`PlanError::Refused`]: roll back and print Python's error (`SeamError::to_json`), exit 2.
//! 5. [`PlanDerived::ops`] in order: `INSERT` with [`NewTask::insert_values`], and for a [`Step`] the conditional `UPDATE tasks SET state = ?,
//!    updated_at = now WHERE task_id = ? AND state = ?` with its audit row (`ts = now`). Commit. Then write [`PlanFiles`] (the plan by
//!    rename, `latest_correlation` plainly), then replay [`PlanDerived::effects`] in order (`BindGraph` only when the substrate is enabled),
//!    then sign the [`Kickoff`] if there is one and append `execution_assignment` and `execution_assignment_verified: true` to the reply.
//! 6. Print [`PlanDerived::reply`] with [`crate::finish`] (`agent` `A01`, `script` `orch_plan`) and record the run-log entry with
//!    [`PlanDerived::ctx_correlation_id`].
//!
//! A failure after the commit (the files, the signature) is never retried by Python: the host prints a typed error that says the outcome is
//! unknown.
//!
//! # The declines (every case Python answers)
//!
//! Anything Rust cannot prove byte-identical is [`PlanError::NotStarted`], decided before the first write, so Python answers. Each message
//! starts with its class (`<class>: ...`), and the tests pin every class to cases of the vectors:
//!
//! * `python-trace`: Python raises an exception that is not a `SwarmError`, so its generic handler prints a `trace`: a custom plan that is not
//!   `{"tasks": [..]}` of objects, a task without a text `id` or without `capability`, a `depends_on` element that is not a task id or names a
//!   task defined later or not at all (a cycle included), a `risk_class` outside `low`, `medium`, `high` at creation, a gate task without `gate`
//!   or `for`, `--pattern custom` without `--plan`.
//! * `value-shape`: a custom plan value Python iterates, coerces or fails on after the commit, which Rust does not reproduce: a `depends_on`
//!   that is not a list, `gates` that is neither a list nor an object, a gate task whose `for` is not a list of ids, a `title` that is not
//!   text or `null` (SQLite binds or coerces it).
//! * `unreadable-file`: a brief, plan or slice file that exists but cannot be read; the decline happens where Python would read it, so an
//!   earlier refusal of Python (a bad `--graph-id`, `--plan` with slices) is still Python's own answer.
//! * `json-not-reproduced`: a blast-radii block, `--slices-json`, slice file, plan file or stored column that is not JSON, or JSON [`Json`]
//!   cannot hold (an integer outside `i64`, `NaN`, a lone surrogate). Python's `JSONDecodeError.msg` texts are not reproduced.
//! * `non-ascii-repr`: a message that embeds the `repr` of a value (`ends in .rN`, `no agent matches`, `invalid --graph-id`, `got ...`,
//!   `repeated`) when the value is not ASCII: whether a character is printable is Unicode data Rust has no copy of. An agent reference or a
//!   roster key that is not ASCII is declined too: Python lower-cases it by Unicode rules (the Kelvin sign lower-cases to `k`).
//! * `unicode-digit`: a custom task id that ends in a non-ASCII number, where `\d` of `re` is Unicode-aware.
//! * `after-write`: Python commits the plan and then raises (an empty plan with a signing key, a number the kickoff cannot sign): the
//!   derivation refuses before the write and Python does the whole call.
//! * `kickoff-input`: a value the controller kickoff does not reproduce (an acceptance item that is not text, a budget number that is not an
//!   integer, a task with a risk class outside the three).
//! * `stored-row`: a stored row or fact the derivation cannot read the way Python does (a column of an unexpected type, `notes.gates` that is
//!   not a list, a risk class outside the three when the gates are derived from it, a plan file fact the host did not supply).
//!
//! Blast-radius slices (`--slices`, `--slices-json`, the ```` ```blast-radii ```` block) and `--pattern parallel` are derived here: the
//! corpus covers the fence, the lane naming, the path normalisation, the disjointness analysis and the agent assignment.

use std::collections::HashMap;
use std::fmt;

use gsd_codes::ErrorCode;

use crate::pydumps::{parse_python_json, to_python_dumps, to_python_dumps_sorted};
use crate::{check_safe_id, jstr, obj_get, py_float, to_python_json, Fields, Json, SeamError};

/// How far apart `created_at` of two tasks of one plan is: the `i`-th new task gets `now + i * CREATED_AT_STEP`.
pub const CREATED_AT_STEP: f64 = 1e-6;

/// How much of the brief a task's `notes.brief_excerpt` keeps (`brief[:2000]`, in code points).
pub const BRIEF_EXCERPT_CHARS: usize = 2000;

/// The most blast radii one plan accepts (`_MAX_SLICES`).
pub const MAX_SLICES: usize = 32;

/// The text `run()` puts in place of a missing brief in a dry run.
const DRY_RUN_BRIEF: &str = "(dry-run placeholder brief)";

/// The task states in which `deps_satisfied` counts a dependency as done (`SATISFIED_STATES`).
const SATISFIED_STATES: [&str; 3] = ["IN_REVIEW", "APPROVED", "DONE"];

/// The states a kickoff counts as schedulable (`_execution_result`).
const READY_STATES: [&str; 4] = ["CREATED", "VALIDATED", "PLANNED", "RETRY"];

/// Risk classes from lowest to highest, the order `_execution_result` takes the maximum in.
const RISK_ORDER: [&str; 3] = ["low", "medium", "high"];

/// The largest integer a JavaScript number holds exactly (`_JS_SAFE_INT`).
const JS_SAFE_INT: i64 = (1 << 53) - 1;

/// A file the host was asked to read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FileRead {
    /// The text of the file as Python's `read_text` returns it: decoded, with universal newlines (`\r\n` and a lone `\r` read as `\n`).
    Text(String),
    /// The path exists (or was named) and Python's read raises, so Python's generic handler answers.
    Unreadable,
}

/// One agent of the roster, as `swarm.manifest` returns it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Agent {
    /// `id`, for example `A05`.
    pub id: String,
    /// `code`.
    pub code: String,
    /// `slug`.
    pub slug: String,
    /// `capabilities`, in roster order.
    pub capabilities: Vec<String>,
}

/// What `_execution_result` reads from the environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Signing {
    /// `SWARM_AGENT_SESSION` or `SWARM_CHILD` is `1`: no signing check, no kickoff.
    pub agent_session: bool,
    /// `signing_config_error()`.
    pub config_error: Option<String>,
    /// `real_key_configured()`.
    pub real_key: bool,
}

/// The plan file of one correlation, as the host found it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredPlanFile {
    /// The correlation the file `plans/<correlation_id>.json` belongs to.
    pub correlation_id: String,
    /// Its text, or `None` when it cannot be opened (`OSError`).
    pub text: Option<String>,
}

/// Everything the host reads before it asks for a derivation, from one snapshot taken inside the write transaction.
#[derive(Debug, Clone)]
pub struct PlanFacts {
    /// `str(uuid.uuid4())`, drawn by the host; used only when the request names no correlation id.
    pub new_correlation_id: String,
    /// The roster, in roster order.
    pub agents: Vec<Agent>,
    /// `str(swarm_dir(root, create=True) / "plans")`.
    pub plans_dir: String,
    /// The signing state of the environment.
    pub signing: Signing,
    /// Every `tasks` row whose id starts with `<p>-` for a prefix `p` of [`candidate_prefixes`] (`dict(sqlite3.Row)`, JSON text).
    pub prefix_tasks: Vec<Fields>,
    /// Every `tasks` row of the correlation being planned and of every correlation a row of `prefix_tasks` belongs to, ordered by
    /// `dag_depth, created_at` (`store.list`).
    pub correlation_tasks: Vec<Fields>,
    /// The plan file of every correlation that has a row in `correlation_tasks` or is being planned.
    pub plan_files: Vec<StoredPlanFile>,
}

/// The arguments of one `orch_plan.py` call that the derivation reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanRequest {
    /// `--pattern`, as argparse accepted it.
    pub pattern: String,
    /// `--risk-class`.
    pub risk_class: String,
    /// `--priority`.
    pub priority: String,
    /// `--prefix`; `None` when absent. Python tests `is None` for the derived prefix and truthiness for the hint and the dry run.
    pub prefix: Option<String>,
    /// `--acceptance`, in order.
    pub acceptance: Vec<String>,
    /// `--graph-id`.
    pub graph_id: Option<String>,
    /// `--brief` when it names an existing path: that file; `None` when the flag is absent, empty or names nothing.
    pub brief_file: Option<FileRead>,
    /// `--brief-text`.
    pub brief_text: Option<String>,
    /// `--plan` when it is a non-empty path: that file.
    pub plan_file: Option<FileRead>,
    /// `--slices-json`.
    pub slices_json: Option<String>,
    /// `--slices` when it is a non-empty path: that file.
    pub slices_file: Option<FileRead>,
    /// The correlation id of `--correlation-id` or `SWARM_CORRELATION_ID`, already checked by `AgentScript.main`.
    pub correlation_id: Option<String>,
    /// `--dry-run`.
    pub dry_run: bool,
}

/// One `store.create` call, with its defaults applied.
#[derive(Debug, Clone, PartialEq)]
pub struct NewTask {
    /// `<prefix>-<suffix>`.
    pub task_id: String,
    /// The correlation.
    pub correlation_id: String,
    /// `capability`.
    pub capability: String,
    /// `title`: text or `null`.
    pub title: Json,
    /// `agent_id`.
    pub agent_id: String,
    /// `dag_depth`: one more than the deepest dependency, `0` for none.
    pub dag_depth: i64,
    /// `depends_on`, prefixed.
    pub depends_on: Vec<String>,
    /// `acceptance`: the row's own or `--acceptance`.
    pub acceptance: Json,
    /// `budget` of the risk class.
    pub budget: Json,
    /// `risk_class`.
    pub risk_class: String,
    /// `priority`.
    pub priority: String,
    /// `notes`, in insertion order.
    pub notes: Fields,
    /// `created_at`: `now + index * CREATED_AT_STEP`.
    pub created_at: f64,
}

impl NewTask {
    /// The values of the `INSERT INTO tasks (...)` of `store.create`, in the order of its column list; JSON columns are the text
    /// `json.dumps` writes. `state` is `CREATED` (the two transitions follow) and `updated_at` is `created_at`.
    pub fn insert_values(&self) -> Fields {
        let column = |name: &str, value: Json| (name.to_string(), value);
        vec![
            column("task_id", jstr(&self.task_id)),
            column("correlation_id", jstr(&self.correlation_id)),
            column("parent_id", Json::Null),
            column("dag_depth", Json::Int(self.dag_depth)),
            column("capability", jstr(&self.capability)),
            column("agent_id", jstr(&self.agent_id)),
            column("title", self.title.clone()),
            column("inputs", jstr("[]")),
            column("acceptance", jstr(&to_python_dumps(&self.acceptance))),
            column("budget", jstr(&to_python_dumps(&self.budget))),
            column("risk_class", jstr(&self.risk_class)),
            column("priority", jstr(&self.priority)),
            column("state", jstr("CREATED")),
            column(
                "depends_on",
                jstr(&to_python_dumps(&Json::Arr(
                    self.depends_on.iter().map(|id| jstr(id)).collect(),
                ))),
            ),
            column("max_attempts", Json::Int(3)),
            column(
                "notes",
                jstr(&to_python_dumps(&Json::Obj(self.notes.clone()))),
            ),
            column("created_at", Json::Float(self.created_at)),
            column("updated_at", Json::Float(self.created_at)),
        ]
    }
}

/// One audited `store.transition` call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Step {
    /// The task.
    pub task_id: String,
    /// The state the row is in (the `state = ?` guard).
    pub from: &'static str,
    /// The state it moves to.
    pub to: &'static str,
    /// The audit `actor` (`A01`, the default of `store.transition`).
    pub actor: &'static str,
    /// The audit `reason`.
    pub reason: String,
}

/// One step of the creation transaction, in Python's order.
#[derive(Debug, Clone, PartialEq)]
pub enum PlanOp {
    /// `store.create(...)`: the row and its audit entry `NULL -> CREATED` (actor `system`, reason `created`).
    Create(Box<NewTask>),
    /// `store.transition(...)`.
    Transition(Step),
}

/// The files a creation writes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanFiles {
    /// `<corr>.json`, the name inside `plans/`; written to a temporary file and renamed.
    pub plan_name: String,
    /// `json.dumps(plan, indent=2)`.
    pub plan_text: String,
    /// The text of `latest_correlation`: the bare correlation id, no newline.
    pub latest_correlation: String,
}

/// What `_bind_graph(args, ctx, corr, brief, tasks)` is given.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GraphBinding {
    /// The correlation.
    pub correlation_id: String,
    /// `--graph-id`.
    pub explicit_graph_id: Option<String>,
    /// The brief text (`resolve_graph_id` looks for a Graph ID in it).
    pub brief: String,
    /// `t["task_id"]` of every task of the plan file, in order: the `nodes` of `graph_register`.
    pub task_ids: Vec<String>,
}

/// An effect the host replays after the commit.
#[derive(Debug, Clone, PartialEq)]
pub enum PlanEffect {
    /// `_bind_graph(...)`: fail-open and a no-op without `SUBSTRATE_URL`, so the host replays it only when the substrate is enabled.
    BindGraph(GraphBinding),
    /// `ctx.emit(event_type, payload)` with `ctx.correlation_id` set to the plan's correlation.
    Emit {
        /// The event type (`plan.updated`).
        event_type: String,
        /// The payload.
        payload: Fields,
    },
}

/// The controller kickoff `_execution_result` signs.
#[derive(Debug, Clone, PartialEq)]
pub struct Kickoff {
    /// `correlation_id` of `build_envelope`.
    pub correlation_id: String,
    /// `risk_class` of `build_envelope`: the highest risk class of the correlation's tasks.
    pub risk_class: String,
    /// `payload` of `build_envelope`, after `_json_safe`.
    pub payload: Json,
}

/// What kind of answer the derivation produced.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    /// `--dry-run`: nothing is written.
    DryRun,
    /// The prefix already holds this exact plan: nothing is written.
    Reused,
    /// A new plan: operations, files and effects follow.
    Created,
}

/// What the call does and returns.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanDerived {
    /// Which answer this is.
    pub outcome: Outcome,
    /// The dict `run()` returns, without `agent` and `script`. With a [`Kickoff`] the host appends `execution_assignment` and
    /// `execution_assignment_verified: true` after signing.
    pub reply: Fields,
    /// `ctx.correlation_id` when `run()` returns: `None` for a dry run that was given no correlation id, else the plan's.
    pub ctx_correlation_id: Option<String>,
    /// The creation transaction; empty unless [`Outcome::Created`].
    pub ops: Vec<PlanOp>,
    /// The files to write after the transaction commits; `None` unless [`Outcome::Created`].
    pub files: Option<PlanFiles>,
    /// The effects to replay after the files, in order.
    pub effects: Vec<PlanEffect>,
    /// The kickoff to sign, when a real key is configured and the call is not a dry run or a child session.
    pub kickoff: Option<Kickoff>,
}

/// Why there is no derivation.
#[derive(Debug, Clone, PartialEq)]
pub enum PlanError {
    /// Nothing was decided or written: the route reports `write_state = "not_started"` and Python answers.
    NotStarted(String),
    /// Python's own `SwarmError`; nothing was written.
    Refused(SeamError),
}

impl fmt::Display for PlanError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PlanError::NotStarted(why) => write!(f, "plan not started: {why}"),
            PlanError::Refused(error) => f.write_str(error.message()),
        }
    }
}

impl std::error::Error for PlanError {}

type Planned<T> = Result<T, PlanError>;

fn not_started<T>(why: impl Into<String>) -> Planned<T> {
    Err(PlanError::NotStarted(why.into()))
}

fn refused<T>(code: ErrorCode, message: impl AsRef<str>) -> Planned<T> {
    Err(PlanError::Refused(SeamError::new(code, message)))
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

/// `dict` assignment: an existing key keeps its position, a new one is appended.
fn set(fields: &mut Fields, key: &str, value: Json) {
    match fields.iter_mut().find(|(name, _)| name == key) {
        Some(entry) => entry.1 = value,
        None => fields.push((key.to_string(), value)),
    }
}

/// A value of the three numeric Python types, for `==`.
#[derive(Clone, Copy)]
enum Number {
    Int(i64),
    Float(f64),
}

fn number_of(value: &Json) -> Option<Number> {
    match value {
        Json::Bool(flag) => Some(Number::Int(i64::from(*flag))),
        Json::Int(number) => Some(Number::Int(*number)),
        Json::Float(number) => Some(Number::Float(*number)),
        _ => None,
    }
}

/// `int == float` as Python compares them: exactly, not through a lossy conversion.
fn same_number(left: Number, right: Number) -> bool {
    match (left, right) {
        (Number::Int(a), Number::Int(b)) => a == b,
        (Number::Float(a), Number::Float(b)) => a == b,
        (Number::Int(int), Number::Float(float)) | (Number::Float(float), Number::Int(int)) => {
            float.fract() == 0.0
                && (i64::MIN as f64..i64::MAX as f64).contains(&float)
                && float as i64 == int
        }
    }
}

/// Python `==` of two JSON values: `True == 1`, `1 == 1.0`, objects compare without regard to key order.
fn py_eq(left: &Json, right: &Json) -> bool {
    if let (Some(a), Some(b)) = (number_of(left), number_of(right)) {
        return same_number(a, b);
    }
    match (left, right) {
        (Json::Null, Json::Null) => true,
        (Json::Str(a), Json::Str(b)) => a == b,
        (Json::Arr(a), Json::Arr(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(x, y)| py_eq(x, y))
        }
        (Json::Obj(a), Json::Obj(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(key, value)| obj_get(b, key).is_some_and(|other| py_eq(value, other)))
        }
        _ => false,
    }
}

/// Python's `repr` of a string, for printable ASCII and the ASCII control characters; `None` when the text has a non-ASCII character
/// (whether it is printable is Unicode data).
fn repr_str(text: &str) -> Option<String> {
    if !text.is_ascii() {
        return None;
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
    Some(out)
}

/// Python's `repr` of a JSON value, `None` when a string inside it needs Unicode data.
fn repr_json(value: &Json) -> Option<String> {
    Some(match value {
        Json::Null => "None".to_string(),
        Json::Bool(true) => "True".to_string(),
        Json::Bool(false) => "False".to_string(),
        Json::Int(number) => number.to_string(),
        Json::Float(number) if number.is_nan() => "nan".to_string(),
        Json::Float(number) if number.is_infinite() => {
            if *number > 0.0 { "inf" } else { "-inf" }.to_string()
        }
        Json::Float(number) => py_float(*number),
        Json::Str(text) => repr_str(text)?,
        Json::Arr(items) => {
            let parts: Option<Vec<String>> = items.iter().map(repr_json).collect();
            format!("[{}]", parts?.join(", "))
        }
        Json::Obj(fields) => {
            let mut parts = Vec::with_capacity(fields.len());
            for (key, item) in fields {
                parts.push(format!("{}: {}", repr_str(key)?, repr_json(item)?));
            }
            format!("{{{}}}", parts.join(", "))
        }
    })
}

/// `repr(value)` as message text, or the decline when Rust cannot format it.
fn repr_text(value: &Json) -> Planned<String> {
    repr_json(value).map_or_else(
        || not_started("non-ascii-repr: a message embeds the repr of a non-ASCII value"),
        Ok,
    )
}

/// `str(value)`: a string is itself, everything else prints as its `repr`.
fn str_text(value: &Json) -> Planned<String> {
    match value {
        Json::Str(text) => Ok(text.clone()),
        other => repr_text(other),
    }
}

/// Python's `str.isspace` for one character (what `strip()` removes): the Unicode `White_Space` set plus the four separators U+001C to U+001F.
fn py_isspace(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\u{1c}'..='\u{1f}')
}

/// Python's `str.strip()` without arguments.
fn py_strip(text: &str) -> &str {
    text.trim_matches(py_isspace)
}

/// `SHA-256` of `data` as lowercase hex (`hashlib.sha256(data).hexdigest()`).
///
/// `gsd-route` has no hash dependency (and the guest must not gain one), so the digest is computed here.
pub fn sha256_hex(data: &[u8]) -> String {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];
    let mut state: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let mut message = data.to_vec();
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&((data.len() as u64) * 8).to_be_bytes());
    for block in message.as_chunks::<64>().0 {
        let mut schedule = [0u32; 64];
        for (slot, word) in schedule.iter_mut().zip(block.as_chunks::<4>().0) {
            *slot = u32::from_be_bytes(*word);
        }
        for at in 16..64 {
            let low = schedule[at - 15].rotate_right(7)
                ^ schedule[at - 15].rotate_right(18)
                ^ (schedule[at - 15] >> 3);
            let high = schedule[at - 2].rotate_right(17)
                ^ schedule[at - 2].rotate_right(19)
                ^ (schedule[at - 2] >> 10);
            schedule[at] = schedule[at - 16]
                .wrapping_add(low)
                .wrapping_add(schedule[at - 7])
                .wrapping_add(high);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = state;
        for at in 0..64 {
            let sum1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let choose = (e & f) ^ (!e & g);
            let first = h
                .wrapping_add(sum1)
                .wrapping_add(choose)
                .wrapping_add(K[at])
                .wrapping_add(schedule[at]);
            let sum0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let majority = (a & b) ^ (a & c) ^ (b & c);
            let second = sum0.wrapping_add(majority);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(first);
            d = c;
            c = b;
            b = a;
            a = first.wrapping_add(second);
        }
        for (slot, add) in state.iter_mut().zip([a, b, c, d, e, f, g, h]) {
            *slot = slot.wrapping_add(add);
        }
    }
    state.iter().map(|word| format!("{word:08x}")).collect()
}

/// `substrate_tee.is_graph_id`: `ut-[0-9a-z]+-[0-9a-f]{8}` matched against the whole text.
fn is_graph_id(text: &str) -> bool {
    let Some(rest) = text.strip_prefix("ut-") else {
        return false;
    };
    let Some((base36, tail)) = rest.rsplit_once('-') else {
        return false;
    };
    !base36.is_empty()
        && base36
            .bytes()
            .all(|b| b.is_ascii_digit() || b.is_ascii_lowercase())
        && tail.len() == 8
        && tail
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Whether Python's `re.search(r"\.r\d+$", id)` matches. `$` also matches before one trailing newline; `\d` is Unicode-aware, so an id that
/// ends in a non-ASCII number is declined.
fn ends_in_rework_suffix(id: &str) -> Planned<bool> {
    for end in std::iter::once(id).chain(id.strip_suffix('\n')) {
        let digits = end.chars().rev().take_while(char::is_ascii_digit).count();
        let head = &end[..end.len() - digits];
        if head
            .chars()
            .next_back()
            .is_some_and(|ch| !ch.is_ascii() && ch.is_numeric())
        {
            return not_started("unicode-digit: the task id ends in a non-ASCII number");
        }
        if digits > 0 && head.ends_with(".r") {
            return Ok(true);
        }
    }
    Ok(false)
}

/// `"T" + corr.replace("-", "")[:n]`.
pub fn derived_prefix(correlation_id: &str, n: usize) -> String {
    let hex: String = correlation_id
        .chars()
        .filter(|&ch| ch != '-')
        .take(n)
        .collect();
    format!("T{hex}")
}

/// The prefixes whose existing tasks [`PlanFacts::prefix_tasks`] must hold: the explicit prefix when there is a non-`None` one, else
/// the 4 and the 6 character derived prefixes of the correlation id the call will use (`request.correlation_id` or the host's fresh one).
///
/// The host reads every `tasks` row whose `task_id` starts with `<prefix>-` for each of them (compare with `substr`, not `LIKE`).
pub fn candidate_prefixes(correlation_id: &str, explicit: Option<&str>) -> Vec<String> {
    match explicit {
        Some(prefix) => vec![prefix.to_string()],
        None => vec![
            derived_prefix(correlation_id, 4),
            derived_prefix(correlation_id, 6),
        ],
    }
}

/// How a pattern row decides its gates (`gate spec` of `PATTERNS`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Gates {
    /// `None`: the risk class decides.
    FromRisk,
    /// `[]`: none.
    Empty,
    /// `{"gate": name, "for": targets}`: this IS a gate task.
    GateTask(&'static str, &'static [&'static str]),
}

/// One row of `PATTERNS`.
struct PatternRow {
    suffix: &'static str,
    capability: &'static str,
    agent: &'static str,
    title: &'static str,
    deps: &'static [&'static str],
    gates: Gates,
    risk: Option<&'static str>,
}

macro_rules! row {
    ($suffix:expr, $cap:expr, $agent:expr, $title:expr, $deps:expr, $gates:expr) => {
        PatternRow {
            suffix: $suffix,
            capability: $cap,
            agent: $agent,
            title: $title,
            deps: $deps,
            gates: $gates,
            risk: None,
        }
    };
    ($suffix:expr, $cap:expr, $agent:expr, $title:expr, $deps:expr, $gates:expr, $risk:expr) => {
        PatternRow {
            suffix: $suffix,
            capability: $cap,
            agent: $agent,
            title: $title,
            deps: $deps,
            gates: $gates,
            risk: Some($risk),
        }
    };
}

use Gates::{Empty, FromRisk, GateTask};

#[rustfmt::skip]
const FEATURE: &[PatternRow] = &[
    row!("req", "req.spec", "A02", "Requirements spec + acceptance criteria", &[], Empty),
    row!("arch", "design.blueprint", "A03", "Architecture blueprint, API contract, ADRs", &["req"], Empty),
    row!("ux", "ux.spec", "A04", "Design tokens + UX spec + a11y requirements", &["req"], Empty),
    row!("data", "data.migration", "A07", "Data model, migrations, data contract", &["arch"], FromRisk),
    row!("be", "code.backend", "A05", "Backend implementation against contracts", &["arch", "data"], FromRisk),
    row!("fe", "code.frontend", "A06", "Frontend implementation against tokens/contracts", &["arch", "ux"], FromRisk),
    row!("qa", "gate.quality", "A08", "Quality gate: tests + verdict", &["be", "fe", "data"], GateTask("quality", &["be", "fe", "data"])),
    row!("rev", "gate.review", "A09", "Review gate: code review verdict", &["be", "fe", "data"], GateTask("review", &["be", "fe", "data"])),
    row!("sec", "gate.security", "A10", "Security gate: SAST/secrets/deps verdict", &["be", "fe", "data"], GateTask("security", &["be", "fe", "data"])),
    row!("build", "build.artifact", "A11", "CI validation + build artifact record", &["qa", "rev", "sec"], Empty),
    row!("rel", "release.plan", "A12", "Release gate + canary plan", &["build"], GateTask("release", &["be", "fe", "data"])),
    row!("obs", "obs.slo", "A13", "SLOs, alerts, deploy telemetry guardrails", &["rel"], Empty),
    row!("docs", "docs.bundle", "A15", "Docs bundle, API reference, runbook, changelog", &["rel"], Empty),
];

#[rustfmt::skip]
const HOTFIX: &[PatternRow] = &[
    row!("rca", "maint.rca", "A14", "Root-cause analysis + minimal patch task", &[], Empty),
    row!("patch", "code.patch", "A05", "Minimal-diff hotfix", &["rca"], FromRisk),
    row!("qa", "gate.quality", "A08", "Quality gate (priority)", &["patch"], GateTask("quality", &["patch"])),
    row!("rev", "gate.review", "A09", "Review gate (priority)", &["patch"], GateTask("review", &["patch"])),
    row!("sec", "gate.security", "A10", "Security gate (priority)", &["patch"], GateTask("security", &["patch"])),
    row!("rel", "release.promote", "A12", "Guarded promote + unfreeze", &["qa", "rev", "sec"], GateTask("release", &["patch"])),
    row!("obs", "obs.incident", "A13", "Confirm stabilization, close incident", &["rel"], Empty),
    row!("retro", "maint.debt", "A14", "Retro → memory + debt register", &["obs"], Empty),
];

#[rustfmt::skip]
const DEPENDENCY: &[PatternRow] = &[
    row!("scan", "sec.deps", "A10", "Dependency/CVE scan", &[], Empty),
    row!("patch", "maint.deps", "A14", "Patch tasks (24h deadline for KEV)", &["scan"], Empty),
    row!("bump", "code.patch", "A05", "Dependency bumps", &["patch"], FromRisk),
    row!("qa", "gate.quality", "A08", "Quality gate", &["bump"], GateTask("quality", &["bump"])),
    row!("rev", "gate.review", "A09", "Review gate", &["bump"], GateTask("review", &["bump"])),
    row!("sec", "gate.security", "A10", "Security gate", &["bump"], GateTask("security", &["bump"])),
    row!("rel", "release.plan", "A12", "Scheduled canary promotion", &["qa", "rev", "sec"], GateTask("release", &["bump"])),
    row!("obs", "obs.slo", "A13", "Regression watch", &["rel"], Empty),
];

#[rustfmt::skip]
const CLOUD_NATIVE: &[PatternRow] = &[
    row!("req", "req.spec", "A02", "Requirements spec + acceptance criteria", &[], Empty),
    row!("arch", "design.blueprint", "A03", "Architecture blueprint, API contract, ADRs", &["req"], Empty),
    row!("cloud", "cloud.architecture", "A16", "Cloud-native architecture: landing zone, resilience, residency", &["arch"], Empty),
    row!("plat", "platform.golden_path", "A17", "Golden path, catalog entry and platform claims", &["cloud"], Empty),
    row!("events", "event.topology", "A19", "Event topology, schemas and serverless config", &["arch", "cloud", "plat"], FromRisk),
    row!("be", "code.backend", "A05", "Backend implementation against contracts and the cloud architecture", &["arch", "cloud", "events", "plat"], FromRisk),
    row!("cost", "finops.model", "A20", "Cost model, budget and carbon estimate", &["cloud"], Empty),
    row!("img", "supply.image", "A18", "Pre-build image policy: Dockerfile lint and base-image pins", &["be"], Empty),
    row!("qa", "gate.quality", "A08", "Quality gate: tests + verdict", &["be", "events"], GateTask("quality", &["be", "events"])),
    row!("rev", "gate.review", "A09", "Review gate: code review verdict", &["be", "events"], GateTask("review", &["be", "events"])),
    row!("sec", "gate.security", "A10", "Security gate: SAST/secrets/deps verdict", &["be", "events", "img"], GateTask("security", &["be", "events"])),
    row!("build", "build.artifact", "A11", "CI validation + build artifact record", &["qa", "rev", "sec"], Empty),
    row!("prov", "supply.image", "A18", "Post-build evidence: SBOM, provenance and signature for the build artifact", &["build"], Empty),
    row!("rel", "release.plan", "A12", "Release gate + canary plan", &["build", "prov", "plat", "cost"], GateTask("release", &["be", "events"])),
    row!("obs", "obs.slo", "A13", "SLOs, alerts, deploy telemetry guardrails", &["rel"], Empty),
    row!("docs", "docs.bundle", "A15", "Docs bundle, API reference, runbook, changelog", &["rel"], Empty),
];

#[rustfmt::skip]
const CLUSTER: &[PatternRow] = &[
    row!("cap", "cluster.capacity", "A21", "Cluster capacity, version-skew and deprecated-API review", &[], Empty),
    row!("net", "mesh.traffic", "A23", "Traffic, ingress and NetworkPolicy design", &["cap"], Empty),
    row!("pol", "kpol.admission", "A24", "Pod Security, RBAC and admission policy set", &["cap"], Empty),
    row!("gitops", "gitops.manifest", "A22", "Manifests, overlays and sync policy for the workload", &["cap", "net", "pol"], FromRisk, "high"),
    row!("netchk", "mesh.netpol", "A23", "NetworkPolicy and mesh coverage scan of the rendered manifests", &["gitops"], Empty),
    row!("polchk", "kpol.admission", "A24", "Pod Security, RBAC and admission scan of the rendered manifests", &["gitops"], Empty),
    row!("res", "resilience.backup", "A25", "PDB, spread, probes, backup and DR evidence", &["gitops"], Empty),
    row!("qa", "gate.quality", "A08", "Quality gate: tests + verdict", &["gitops"], GateTask("quality", &["gitops"])),
    row!("rev", "gate.review", "A09", "Review gate: manifest and policy review", &["gitops"], GateTask("review", &["gitops"])),
    row!("sec", "gate.security", "A10", "Security gate: manifest, policy and supply-chain verdict", &["gitops", "polchk"], GateTask("security", &["gitops"])),
    row!("roll", "gitops.rollout", "A22", "Progressive-delivery and promotion plan", &["qa", "rev", "sec", "res", "netchk"], Empty),
    row!("rel", "release.plan", "A12", "Release gate + canary plan", &["roll"], GateTask("release", &["gitops"])),
    row!("obs", "obs.slo", "A13", "SLOs, alerts, deploy telemetry guardrails", &["rel"], Empty),
    row!("docs", "docs.runbook", "A15", "Runbook and changelog", &["rel"], Empty),
];

#[rustfmt::skip]
const WEB3: &[PatternRow] = &[
    row!("req", "req.spec", "A02", "Requirements spec + acceptance criteria", &[], Empty),
    row!("arch", "design.blueprint", "A03", "Protocol architecture, interfaces, ADRs", &["req"], Empty),
    row!("cust", "custody.policy", "A30", "Signer, multisig, timelock and key-ceremony policy draft, before the audit", &["arch"], Empty),
    row!("sol", "web3.contract", "A26", "Smart contracts, tests, gas report and deployment plan", &["arch", "cust"], FromRisk, "high"),
    row!("chain", "chain.node", "A28", "Network guard, RPC policy and indexer plan", &["sol", "cust"], Empty),
    row!("dapp", "dapp.wallet", "A29", "Wallet and chain integration against the ABI", &["arch", "sol", "chain"], FromRisk, "high"),
    row!("aud", "audit.static", "A27", "Contract audit findings and invariant suite", &["sol"], Empty),
    row!("comp", "custody.compliance", "A30", "Post-audit custody evidence: privileged roles, timelock band, compliance pack", &["cust", "aud"], Empty),
    row!("qa", "gate.quality", "A08", "Quality gate: tests + verdict", &["sol", "dapp", "aud"], GateTask("quality", &["sol", "dapp"])),
    row!("rev", "gate.review", "A09", "Review gate: code review verdict", &["sol", "dapp"], GateTask("review", &["sol", "dapp"])),
    row!("sec", "gate.security", "A10", "Security gate: includes the audit findings", &["sol", "dapp", "aud"], GateTask("security", &["sol", "dapp"])),
    row!("build", "build.artifact", "A11", "CI validation + build artifact record", &["qa", "rev", "sec"], Empty),
    row!("rel", "release.plan", "A12", "Release gate; mainnet deployment stays human-approved", &["build", "chain", "comp"], GateTask("release", &["sol", "dapp"])),
    row!("obs", "obs.slo", "A13", "SLOs, alerts, chain and indexer monitoring guardrails", &["rel"], Empty),
    row!("docs", "docs.bundle", "A15", "Docs bundle, API reference, runbook, changelog", &["rel"], Empty),
];

/// The names of `PATTERNS`, in Python's order.
pub const PATTERN_NAMES: [&str; 6] = [
    "feature",
    "hotfix",
    "dependency",
    "cloud-native",
    "cluster",
    "web3",
];

/// `PATTERNS[name]`, or `None` for `custom`, `parallel` and any other name.
fn pattern_table(name: &str) -> Option<&'static [PatternRow]> {
    Some(match name {
        "feature" => FEATURE,
        "hotfix" => HOTFIX,
        "dependency" => DEPENDENCY,
        "cloud-native" => CLOUD_NATIVE,
        "cluster" => CLUSTER,
        "web3" => WEB3,
        _ => return None,
    })
}

/// `_budget(risk)`: the budget of a risk class, `None` for any other value.
fn budget_of(risk: &str) -> Option<Json> {
    let (tokens, wall, cost) = match risk {
        "low" => (150_000, 900, 3),
        "medium" => (300_000, 1800, 8),
        "high" => (600_000, 3600, 20),
        _ => return None,
    };
    Some(Json::Obj(vec![
        ("max_tokens".to_string(), Json::Int(tokens)),
        ("max_wall_s".to_string(), Json::Int(wall)),
        ("max_cost_usd".to_string(), Json::Int(cost)),
    ]))
}

/// `GATES_BY_RISK[risk]`, `None` for an unknown class.
fn gates_by_risk(risk: &str) -> Option<&'static [&'static str]> {
    Some(match risk {
        "low" => &["review"],
        "medium" => &["review", "quality"],
        "high" => &["review", "quality", "security", "release"],
        _ => return None,
    })
}

/// One plan row, padded to nine fields (`_as_row`). The Json fields keep whatever a custom plan carried until creation validates them.
#[derive(Debug, Clone)]
struct PlanRow {
    suffix: String,
    capability: String,
    agent: String,
    title: Json,
    deps: Json,
    gates: Json,
    risk: Json,
    acceptance: Json,
    /// The blast-radius lane (`row[8]`): a non-empty object, or `None`.
    lane: Option<Fields>,
}

/// A JSON list of strings.
fn strings<S: AsRef<str>>(items: &[S]) -> Json {
    Json::Arr(items.iter().map(|item| jstr(item.as_ref())).collect())
}

fn pattern_rows(table: &[PatternRow]) -> Vec<PlanRow> {
    table
        .iter()
        .map(|row| PlanRow {
            suffix: row.suffix.to_string(),
            capability: row.capability.to_string(),
            agent: row.agent.to_string(),
            title: jstr(row.title),
            deps: strings(row.deps),
            gates: match row.gates {
                Gates::FromRisk => Json::Null,
                Gates::Empty => Json::Arr(Vec::new()),
                Gates::GateTask(gate, targets) => Json::Obj(vec![
                    ("gate".to_string(), jstr(gate)),
                    ("for".to_string(), strings(targets)),
                ]),
            },
            risk: row.risk.map_or(Json::Null, jstr),
            acceptance: Json::Arr(Vec::new()),
            lane: None,
        })
        .collect()
}

impl PlanRow {
    /// The row as `_plan_sha_rows` hashes it: eight fields, plus the lane when the row has one.
    fn hashed(&self) -> Json {
        let mut fields = vec![
            jstr(&self.suffix),
            jstr(&self.capability),
            jstr(&self.agent),
            self.title.clone(),
            self.deps.clone(),
            self.gates.clone(),
            self.risk.clone(),
            self.acceptance.clone(),
        ];
        if let Some(lane) = &self.lane {
            fields.push(Json::Obj(lane.clone()));
        }
        Json::Arr(fields)
    }

    fn is_slice(&self) -> bool {
        self.lane
            .as_ref()
            .is_some_and(|lane| obj_get(lane, "role") == Some(&jstr("slice")))
    }
}

/// The first agent, in roster order, with `ident` as its id, code or slug (`get_agent`); `Err` is the decline.
fn find_agent<'a>(agents: &'a [Agent], ident: &str) -> Planned<Option<&'a Agent>> {
    let ascii =
        |agent: &Agent| agent.id.is_ascii() && agent.code.is_ascii() && agent.slug.is_ascii();
    if !ident.is_ascii() || !agents.iter().all(ascii) {
        return not_started("non-ascii-repr: an agent reference or roster key is not ASCII, and `str.lower` is Unicode-aware");
    }
    let wanted = ident.to_ascii_lowercase();
    Ok(agents.iter().find(|agent| {
        [&agent.id, &agent.code, &agent.slug]
            .iter()
            .any(|key| key.to_ascii_lowercase() == wanted)
    }))
}

/// `KeyError.args[0]` of `get_agent(ident)`.
fn unknown_agent_message(ident: &str) -> Planned<String> {
    Ok(format!("no agent matches {}", repr_text(&jstr(ident))?))
}

/// `by_capability(capability)`: the agents that offer it, in roster order. A capability that is not text matches nothing.
fn offering<'a>(agents: &'a [Agent], capability: &Json) -> Vec<&'a Agent> {
    match capability {
        Json::Str(name) => agents
            .iter()
            .filter(|agent| agent.capabilities.iter().any(|offered| offered == name))
            .collect(),
        _ => Vec::new(),
    }
}

fn parse_text(text: &str, what: &str) -> Planned<Json> {
    parse_python_json(text).map_or_else(
        |_| {
            not_started(format!(
                "json-not-reproduced: {what} is not JSON that Rust reads like Python"
            ))
        },
        Ok,
    )
}

fn read_text<'a>(file: &'a FileRead, what: &str) -> Planned<&'a str> {
    match file {
        FileRead::Text(text) => Ok(text),
        FileRead::Unreadable => not_started(format!("unreadable-file: {what} cannot be read")),
    }
}

/// `load_custom`: the rows of a custom plan file, with Python's checks in Python's order.
fn load_custom(file: &FileRead, agents: &[Agent]) -> Planned<Vec<PlanRow>> {
    let data = parse_text(read_text(file, "the plan file")?, "the plan file")?;
    let Json::Obj(document) = &data else {
        return not_started(
            "python-trace: the plan file is not an object, `data[\"tasks\"]` raises",
        );
    };
    let Some(Json::Arr(tasks)) = obj_get(document, "tasks") else {
        return not_started("python-trace: the plan file has no list `tasks`");
    };
    let mut rows = Vec::with_capacity(tasks.len());
    for task in tasks {
        let Json::Obj(task) = task else {
            return not_started("python-trace: a custom task is not an object");
        };
        let Some(Json::Str(id)) = obj_get(task, "id") else {
            return not_started("python-trace: a custom task has no text `id`");
        };
        if ends_in_rework_suffix(id)? {
            return refused(
                ErrorCode::Input,
                format!(
                    "task id {} ends in .rN, which rework reruns use; rename the task",
                    repr_text(&jstr(id))?
                ),
            );
        }
        let capability = obj_get(task, "capability");
        let agent = obj_get(task, "agent").unwrap_or(&NULL);
        let (capability, agent_id) = if truthy(agent) {
            let ident = str_text(agent)?;
            let Some(owner) = find_agent(agents, &ident)? else {
                return refused(
                    ErrorCode::Input,
                    format!("task {id}: {}", unknown_agent_message(&ident)?),
                );
            };
            let Some(capability) = capability else {
                return not_started("python-trace: a custom task has no `capability`");
            };
            let offered = matches!(capability, Json::Str(name) if owner.capabilities.iter().any(|c| c == name));
            if !offered {
                return refused(
                    ErrorCode::Input,
                    format!(
                        "task {id}: {} does not offer {}",
                        owner.id,
                        str_text(capability)?
                    ),
                );
            }
            (capability, owner.id.clone())
        } else {
            let Some(capability) = capability else {
                return not_started("python-trace: a custom task has no `capability`");
            };
            let Some(first) = offering(agents, capability)
                .first()
                .map(|owner| owner.id.clone())
            else {
                return refused(
                    ErrorCode::Input,
                    format!("no agent offers capability {}", str_text(capability)?),
                );
            };
            (capability, first)
        };
        let Json::Str(capability) = capability else {
            return not_started("python-trace: a capability that is not text was accepted");
        };
        rows.push(PlanRow {
            suffix: id.clone(),
            capability: capability.clone(),
            agent: agent_id,
            title: obj_get(task, "title").cloned().unwrap_or_else(|| jstr(id)),
            deps: obj_get(task, "depends_on")
                .cloned()
                .unwrap_or_else(|| Json::Arr(Vec::new())),
            gates: obj_get(task, "gates").cloned().unwrap_or(Json::Null),
            risk: obj_get(task, "risk_class").cloned().unwrap_or(Json::Null),
            acceptance: obj_get(task, "acceptance")
                .cloned()
                .unwrap_or_else(|| Json::Arr(Vec::new())),
            lane: None,
        });
    }
    Ok(rows)
}

/// `_DEFAULT_CAPABILITY[id]`: the capability of a lane agent, `None` for an agent that is not one (`_LANE_AGENTS` is its key set).
fn default_lane_capability(id: &str) -> Option<&'static str> {
    Some(match id {
        "A02" => "req.spec",
        "A03" => "design.blueprint",
        "A04" => "ux.spec",
        "A05" => "code.backend",
        "A06" => "code.frontend",
        "A07" => "data.migration",
        "A11" => "ci.pipeline",
        "A13" => "obs.slo",
        "A14" => "maint.patch",
        "A15" => "docs.bundle",
        "A16" => "cloud.architecture",
        "A17" => "platform.golden_path",
        "A18" => "supply.image",
        "A19" => "event.topology",
        "A20" => "finops.model",
        "A21" => "cluster.capacity",
        "A22" => "gitops.manifest",
        "A23" => "mesh.traffic",
        "A24" => "kpol.admission",
        "A25" => "resilience.backup",
        "A26" => "web3.contract",
        "A27" => "audit.static",
        "A28" => "chain.node",
        "A29" => "dapp.wallet",
        "A30" => "custody.policy",
        _ => return None,
    })
}

/// A validated blast radius (`normalize_slices`).
#[derive(Debug, Clone, PartialEq, Eq)]
struct Slice {
    id: String,
    paths: Vec<String>,
    agent: String,
    capability: String,
    title: String,
    acceptance: Vec<String>,
}

/// `_path`: one blast-radius path, normalised.
fn slice_path(value: &Json, slice_id: &str) -> Planned<String> {
    let Json::Str(text) = value else {
        return refused(
            ErrorCode::Input,
            format!("slice {slice_id}: each blast-radius path must be a non-empty string"),
        );
    };
    let stripped = py_strip(text);
    if stripped.is_empty() {
        return refused(
            ErrorCode::Input,
            format!("slice {slice_id}: each blast-radius path must be a non-empty string"),
        );
    }
    let raw = stripped.replace('\\', "/");
    if raw.starts_with('/') || raw.starts_with('~') {
        return refused(
            ErrorCode::Input,
            format!(
                "slice {slice_id}: path must be repo-relative, got {}",
                repr_text(&jstr(text))?
            ),
        );
    }
    let parts: Vec<&str> = raw
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect();
    if parts.is_empty() || parts.contains(&"..") {
        return refused(
            ErrorCode::Input,
            format!(
                "slice {slice_id}: path must stay inside the repo, got {}",
                repr_text(&jstr(text))?
            ),
        );
    }
    Ok(parts.join("/"))
}

/// `_overlaps`: equal paths, or one a directory prefix of the other.
fn paths_overlap(left: &str, right: &str) -> bool {
    if left == right {
        return true;
    }
    let (short, long) = if left.chars().count() <= right.chars().count() {
        (left, right)
    } else {
        (right, left)
    };
    long.strip_prefix(short)
        .is_some_and(|rest| rest.starts_with('/'))
}

/// `_SLICE_ID.fullmatch`: `[a-z][a-z0-9-]{0,31}`.
fn is_slice_id(id: &str) -> bool {
    let mut chars = id.chars();
    chars.next().is_some_and(|first| first.is_ascii_lowercase())
        && id.len() <= 32
        && chars.all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-')
}

/// `normalize_slices`: the validated slices of a JSON array of blast radii.
fn normalize_slices(raw: &Json, agents: &[Agent]) -> Planned<Vec<Slice>> {
    let Json::Arr(items) = raw else {
        return refused(ErrorCode::Input, "blast radii must be a JSON array");
    };
    if items.len() < 2 {
        return refused(
            ErrorCode::Input,
            "a parallel plan needs at least two disjoint blast radii",
        );
    }
    if items.len() > MAX_SLICES {
        return refused(
            ErrorCode::Input,
            format!("a parallel plan accepts at most {MAX_SLICES} blast radii"),
        );
    }
    let mut slices: Vec<Slice> = Vec::with_capacity(items.len());
    let mut seen: Vec<(String, String)> = Vec::new();
    for item in items {
        let Json::Obj(item) = item else {
            return refused(ErrorCode::Input, "each blast radius must be an object");
        };
        let given = obj_get(item, "id").unwrap_or(&NULL);
        let id = match given {
            Json::Str(id) if is_slice_id(id) => id.clone(),
            other => {
                return refused(
                    ErrorCode::Input,
                    format!(
                        "blast-radius id must match [a-z][a-z0-9-]{{0,31}}, got {}",
                        repr_text(other)?
                    ),
                )
            }
        };
        if matches!(id.as_str(), "join" | "rev" | "qa" | "sec" | "rel")
            || slices.iter().any(|slice| slice.id == id)
        {
            return refused(
                ErrorCode::Input,
                format!(
                    "blast-radius id {} is reserved or repeated",
                    repr_text(&jstr(&id))?
                ),
            );
        }
        let paths = match obj_get(item, "paths") {
            Some(Json::Arr(paths)) if !paths.is_empty() => paths,
            _ => {
                return refused(
                    ErrorCode::Input,
                    format!("slice {id}: paths must be a non-empty array"),
                )
            }
        };
        let mut cleaned: Vec<String> = Vec::with_capacity(paths.len());
        for path in paths {
            let norm = slice_path(path, &id)?;
            if cleaned.contains(&norm) {
                return refused(
                    ErrorCode::Input,
                    format!("slice {id}: repeated path {norm}"),
                );
            }
            if let Some((owner, other)) = seen.iter().find(|(_, other)| paths_overlap(&norm, other))
            {
                return refused(
                    ErrorCode::Input,
                    format!("blast radii overlap: {id}:{norm} and {owner}:{other}"),
                );
            }
            seen.push((id.clone(), norm.clone()));
            cleaned.push(norm);
        }
        let agent_ref = obj_get(item, "agent").unwrap_or(&NULL);
        let capability = obj_get(item, "capability").unwrap_or(&NULL);
        let named_capability = match capability {
            Json::Str(name) if !name.is_empty() => Some(name),
            _ => None,
        };
        let agent = if truthy(agent_ref) {
            let ident = str_text(agent_ref)?;
            match find_agent(agents, &ident)? {
                Some(agent) => agent,
                None => {
                    let message = unknown_agent_message(&ident)?;
                    return refused(
                        ErrorCode::Input,
                        format!("slice {id}: {}", repr_text(&jstr(&message))?),
                    );
                }
            }
        } else if let Some(name) = named_capability {
            match offering(agents, capability).into_iter().next() {
                Some(agent) => agent,
                None => {
                    return refused(
                        ErrorCode::Input,
                        format!("slice {id}: no agent offers capability {name}"),
                    )
                }
            }
        } else {
            return refused(
                ErrorCode::Input,
                format!("slice {id}: set agent or capability"),
            );
        };
        let Some(default_capability) = default_lane_capability(&agent.id) else {
            return refused(
                ErrorCode::Input,
                format!(
                    "slice {id}: {} is a control or gate agent; a lane needs an implementer",
                    agent.id
                ),
            );
        };
        let capability = named_capability.map_or(default_capability, String::as_str);
        if !agent
            .capabilities
            .iter()
            .any(|offered| offered == capability)
        {
            return refused(
                ErrorCode::Input,
                format!("slice {id}: {} does not offer {capability}", agent.id),
            );
        }
        let title = match obj_get(item, "title") {
            Some(given) if truthy(given) => given.clone(),
            _ => jstr(&format!("Blast radius {id}: {}", cleaned.join(", "))),
        };
        let title = match &title {
            Json::Str(text) if !py_strip(text).is_empty() => py_strip(text).to_string(),
            _ => {
                return refused(
                    ErrorCode::Input,
                    format!("slice {id}: title must be a non-empty string"),
                )
            }
        };
        let acceptance = match obj_get(item, "acceptance") {
            Some(given) if truthy(given) => given.clone(),
            _ => Json::Arr(vec![jstr(&format!(
                "edits stay inside {}",
                cleaned.join(", ")
            ))]),
        };
        let acceptance: Vec<String> = match &acceptance {
            Json::Arr(lines) => lines
                .iter()
                .map(|line| match line {
                    Json::Str(text) if !text.is_empty() => Some(text.clone()),
                    _ => None,
                })
                .collect::<Option<Vec<String>>>()
                .map_or_else(
                    || {
                        refused(
                            ErrorCode::Input,
                            format!("slice {id}: acceptance must be an array of strings"),
                        )
                    },
                    Ok,
                )?,
            _ => {
                return refused(
                    ErrorCode::Input,
                    format!("slice {id}: acceptance must be an array of strings"),
                )
            }
        };
        slices.push(Slice {
            id,
            paths: cleaned,
            agent: agent.id.clone(),
            capability: capability.to_string(),
            title,
            acceptance,
        });
    }
    Ok(slices)
}

/// The blocks of `_FENCE = r"```blast-radii[^\n]*\n(.*?)```"` (`DOTALL`), as `findall` returns them: left to right, not overlapping.
fn blast_radii_blocks(brief: &str) -> Vec<&str> {
    const OPEN: &str = "```blast-radii";
    let mut blocks = Vec::new();
    let mut from = 0;
    while let Some(found) = brief[from..].find(OPEN) {
        let after = from + found + OPEN.len();
        let Some(newline) = brief[after..].find('\n') else {
            break;
        };
        let body = after + newline + 1;
        let Some(end) = brief[body..].find("```") else {
            break;
        };
        blocks.push(&brief[body..body + end]);
        from = body + end + 3;
    }
    blocks
}

/// `load_slices`: the one source of blast radii, or `None`.
fn load_slices(
    brief: &str,
    request: &PlanRequest,
    agents: &[Agent],
) -> Planned<Option<Vec<Slice>>> {
    let mut found: Vec<(&str, Json)> = Vec::new();
    let blocks = blast_radii_blocks(brief);
    if blocks.len() > 1 {
        return refused(
            ErrorCode::Input,
            "brief has more than one ```blast-radii``` block",
        );
    }
    if let Some(block) = blocks.first() {
        let parsed = parse_text(block, "the blast-radii block")?;
        if parsed != Json::Null {
            found.push(("the brief", parsed));
        }
    }
    if let Some(text) = request
        .slices_json
        .as_deref()
        .filter(|text| !text.is_empty())
    {
        found.push(("--slices-json", parse_text(text, "--slices-json")?));
    }
    if let Some(file) = &request.slices_file {
        found.push((
            "--slices",
            parse_text(read_text(file, "the slice file")?, "the slice file")?,
        ));
    }
    match found.as_slice() {
        [] => Ok(None),
        [(_, raw)] => normalize_slices(raw, agents).map(Some),
        _ => {
            let names: Vec<&str> = found.iter().map(|(name, _)| *name).collect();
            refused(
                ErrorCode::Input,
                format!("pass blast radii only once; got {}", names.join(" and ")),
            )
        }
    }
}

/// `_GATE_ROWS[name]`: the suffix, capability, agent, title and lane of a merged-branch gate.
fn gate_row(
    name: &str,
) -> Option<(
    &'static str,
    &'static str,
    &'static str,
    &'static str,
    Fields,
)> {
    let role = |role: &str| vec![("role".to_string(), jstr(role))];
    Some(match name {
        "review" => (
            "rev",
            "gate.review",
            "A09",
            "Greptile review of the merged branch",
            vec![
                ("role".to_string(), jstr("review")),
                ("review_source".to_string(), jstr("greptile")),
                ("review_scope".to_string(), jstr("merged-branch")),
            ],
        ),
        "quality" => (
            "qa",
            "gate.quality",
            "A08",
            "Quality gate on the merged branch",
            role("gate"),
        ),
        "security" => (
            "sec",
            "gate.security",
            "A10",
            "Security gate on the merged branch",
            role("gate"),
        ),
        "release" => (
            "rel",
            "release.plan",
            "A12",
            "Release gate for the merged branch",
            role("gate"),
        ),
        _ => return None,
    })
}

fn gate_task_spec(gate: &str, ids: &[Json]) -> Json {
    Json::Obj(vec![
        ("gate".to_string(), jstr(gate)),
        ("for".to_string(), Json::Arr(ids.to_vec())),
    ])
}

/// `parallel_rows`: the lanes, the join and the merged-branch gates of the risk class.
fn parallel_rows(slices: &[Slice], risk: &str) -> Planned<Vec<PlanRow>> {
    let Some(needed) = gates_by_risk(risk) else {
        return refused(
            ErrorCode::Input,
            format!("unknown risk class {}", repr_text(&jstr(risk))?),
        );
    };
    let ids: Vec<Json> = slices.iter().map(|slice| jstr(&slice.id)).collect();
    let mut rows: Vec<PlanRow> = slices
        .iter()
        .map(|slice| PlanRow {
            suffix: slice.id.clone(),
            capability: slice.capability.clone(),
            agent: slice.agent.clone(),
            title: jstr(&slice.title),
            deps: Json::Arr(Vec::new()),
            gates: Json::Null,
            risk: Json::Null,
            acceptance: Json::Arr(slice.acceptance.iter().map(|line| jstr(line)).collect()),
            lane: Some(vec![
                ("role".to_string(), jstr("slice")),
                (
                    "blast_radius".to_string(),
                    Json::Arr(slice.paths.iter().map(|path| jstr(path)).collect()),
                ),
            ]),
        })
        .collect();
    rows.push(PlanRow {
        suffix: "join".to_string(),
        capability: "ci.pipeline".to_string(),
        agent: "A11".to_string(),
        title: jstr("Merge blast-radius branches onto the integration branch"),
        deps: Json::Arr(ids.clone()),
        gates: Json::Arr(Vec::new()),
        risk: Json::Null,
        acceptance: Json::Arr(vec![jstr(
            "every slice branch is merged into the integration branch",
        )]),
        lane: Some(vec![
            ("role".to_string(), jstr("join")),
            ("slices".to_string(), Json::Arr(ids.clone())),
        ]),
    });
    let mut predecessors: Vec<&str> = Vec::new();
    for name in needed.iter().filter(|name| **name != "release") {
        let Some((suffix, capability, agent, title, lane)) = gate_row(name) else {
            return not_started("a gate has no row");
        };
        rows.push(PlanRow {
            suffix: suffix.to_string(),
            capability: capability.to_string(),
            agent: agent.to_string(),
            title: jstr(title),
            deps: strings(&["join"]),
            gates: gate_task_spec(name, &ids),
            risk: Json::Null,
            acceptance: Json::Arr(Vec::new()),
            lane: Some(lane),
        });
        predecessors.push(suffix);
    }
    if needed.contains(&"release") {
        let Some((suffix, capability, agent, title, lane)) = gate_row("release") else {
            return not_started("a gate has no row");
        };
        rows.push(PlanRow {
            suffix: suffix.to_string(),
            capability: capability.to_string(),
            agent: agent.to_string(),
            title: jstr(title),
            deps: if predecessors.is_empty() {
                strings(&["join"])
            } else {
                strings(&predecessors)
            },
            gates: gate_task_spec("release", &ids),
            risk: Json::Null,
            acceptance: Json::Arr(Vec::new()),
            lane: Some(lane),
        });
    }
    Ok(rows)
}

/// `lane_notes(lane, prefix, suffix)`.
fn lane_notes(lane: &Fields, prefix: &str, suffix: &str) -> Planned<Fields> {
    match obj_get(lane, "role") {
        Some(Json::Str(role)) if role == "slice" => {
            let Some(radius) = obj_get(lane, "blast_radius") else {
                return not_started("python-trace: a slice lane has no blast_radius");
            };
            Ok(vec![
                ("role".to_string(), jstr("slice")),
                ("blast_radius".to_string(), radius.clone()),
                (
                    "branch".to_string(),
                    jstr(&format!("swarm/{prefix}/{suffix}")),
                ),
                ("isolate".to_string(), jstr("worktree")),
            ])
        }
        Some(Json::Str(role)) if role == "join" => {
            let Some(Json::Arr(ids)) = obj_get(lane, "slices") else {
                return not_started("python-trace: a join lane has no slices");
            };
            let branches: Vec<Json> = ids
                .iter()
                .map(|id| match id {
                    Json::Str(name) => Ok(jstr(&format!("swarm/{prefix}/{name}"))),
                    _ => not_started("python-trace: a join lane names a slice that is not text"),
                })
                .collect::<Planned<Vec<Json>>>()?;
            Ok(vec![
                ("role".to_string(), jstr("join")),
                ("merge_branches".to_string(), Json::Arr(branches)),
            ])
        }
        _ => Ok(lane
            .iter()
            .filter(|(key, _)| key != "slices")
            .cloned()
            .collect()),
    }
}

/// `_json_safe`: whole floats become integers, a number JavaScript cannot hold exactly is refused. `Err` is the E-CONTRACT of the refusal.
fn json_safe(value: &Json) -> Result<Json, ()> {
    Ok(match value {
        Json::Float(number) => {
            if !number.is_finite() || (number.fract() == 0.0 && number.abs() > JS_SAFE_INT as f64) {
                return Err(());
            }
            if number.fract() == 0.0 {
                Json::Int(*number as i64)
            } else {
                Json::Float(*number)
            }
        }
        Json::Int(number) => {
            if number.unsigned_abs() > JS_SAFE_INT as u64 {
                return Err(());
            }
            Json::Int(*number)
        }
        Json::Arr(items) => Json::Arr(items.iter().map(json_safe).collect::<Result<_, _>>()?),
        Json::Obj(fields) => Json::Obj(
            fields
                .iter()
                .map(|(key, item)| json_safe(item).map(|safe| (key.clone(), safe)))
                .collect::<Result<_, _>>()?,
        ),
        other => other.clone(),
    })
}

/// A JSON column as `TaskStore._row` reads it: `json.loads(value or default)`.
fn json_column(row: &Fields, name: &str, default: &str) -> Planned<Json> {
    match column(row, name) {
        Json::Null => parse_text(default, name),
        Json::Str(text) if text.is_empty() => parse_text(default, name),
        Json::Str(text) => parse_text(text, name),
        _ => not_started(format!("stored-row: {name} is not text")),
    }
}

/// `TaskStore._row`'s `notes_json`: the parsed object, `{}` for an absent, empty or non-object value.
fn notes_of(row: &Fields) -> Planned<Fields> {
    match column(row, "notes") {
        Json::Null => Ok(Vec::new()),
        Json::Str(text) if text.is_empty() => Ok(Vec::new()),
        Json::Str(text) => match parse_text(text, "notes")? {
            Json::Obj(fields) => Ok(fields),
            _ => Ok(Vec::new()),
        },
        _ => not_started("stored-row: notes is not text"),
    }
}

fn text_column(row: &Fields, name: &str) -> Planned<String> {
    match column(row, name) {
        Json::Str(text) => Ok(text.clone()),
        _ => not_started(format!("stored-row: {name} is not text")),
    }
}

/// One element of `_tasks_from_store`: the task as the plan file and the reply show it, with its required gates.
fn task_view(row: &Fields) -> Planned<Json> {
    let notes = notes_of(row)?;
    let gates = match obj_get(&notes, "gates") {
        None | Some(Json::Null) => {
            let risk = text_column(row, "risk_class")?;
            gates_by_risk(&risk).map_or_else(
                || not_started("stored-row: risk_class is not a risk class"),
                |names| Ok(strings(names)),
            )?
        }
        Some(Json::Arr(items)) => Json::Arr(items.clone()),
        Some(_) => return not_started("stored-row: notes.gates is not a list"),
    };
    let mut view: Fields = Vec::with_capacity(14);
    for name in ["task_id", "capability", "agent_id", "title"] {
        view.push((name.to_string(), column(row, name).clone()));
    }
    view.push((
        "depends_on".to_string(),
        json_column(row, "depends_on", "[]")?,
    ));
    for name in ["risk_class", "state", "dag_depth"] {
        view.push((name.to_string(), column(row, name).clone()));
    }
    view.push(("inputs".to_string(), json_column(row, "inputs", "[]")?));
    view.push((
        "acceptance".to_string(),
        json_column(row, "acceptance", "[]")?,
    ));
    view.push(("budget".to_string(), json_column(row, "budget", "{}")?));
    view.push(("priority".to_string(), column(row, "priority").clone()));
    view.push(("gates".to_string(), gates));
    view.push(("notes".to_string(), Json::Obj(notes)));
    json_column(row, "outputs", "[]")?;
    Ok(Json::Obj(view))
}

/// The `(dag_depth, created_at)` sort key of `store.list`.
fn list_key(row: &Fields) -> Planned<(i64, f64)> {
    let depth = match column(row, "dag_depth") {
        Json::Int(depth) => *depth,
        _ => return not_started("stored-row: dag_depth is not an integer"),
    };
    let created = match column(row, "created_at") {
        Json::Float(at) => *at,
        Json::Int(at) => *at as f64,
        _ => return not_started("stored-row: created_at is not a number"),
    };
    Ok((depth, created))
}

/// `store.list(correlation_id=corr)` over `rows`, which may hold several correlations: the rows of `corr`, stably sorted by
/// `dag_depth, created_at`.
fn correlation_rows<'a>(rows: &'a [&'a Fields], corr: &str) -> Planned<Vec<&'a Fields>> {
    let mut keyed: Vec<((i64, f64), &Fields)> = Vec::new();
    for row in rows {
        if column(row, "correlation_id") == &jstr(corr) {
            keyed.push((list_key(row)?, row));
        }
    }
    keyed.sort_by(|left, right| {
        left.0
             .0
            .cmp(&right.0 .0)
            .then(left.0 .1.total_cmp(&right.0 .1))
    });
    Ok(keyed.into_iter().map(|(_, row)| row).collect())
}

/// `_read_plan_tasks(plan_file) is not None`: the file is there, not blank, valid JSON, an object with a list `tasks`.
fn plan_file_is_recorded(text: &Option<String>) -> Planned<bool> {
    let Some(text) = text else {
        return Ok(false);
    };
    if py_strip(text).is_empty() {
        return Ok(false);
    }
    match parse_text(text, "a plan file")? {
        Json::Obj(body) => Ok(matches!(obj_get(&body, "tasks"), Some(Json::Arr(_)))),
        _ => Ok(false),
    }
}

fn stored_plan_file<'a>(facts: &'a PlanFacts, corr: &str) -> Planned<&'a Option<String>> {
    facts
        .plan_files
        .iter()
        .find(|file| file.correlation_id == corr)
        .map(|file| &file.text)
        .map_or_else(
            || not_started("stored-row: no plan file fact for the correlation"),
            Ok,
        )
}

/// `_existing(store, prefix, rows)`: the stored task of each row, in row order (a row with no task is skipped).
fn existing_tasks<'a>(facts: &'a PlanFacts, prefix: &str, rows: &[PlanRow]) -> Vec<&'a Fields> {
    rows.iter()
        .filter_map(|row| {
            let wanted = jstr(&format!("{prefix}-{}", row.suffix));
            facts
                .prefix_tasks
                .iter()
                .find(|task| column(task, "task_id") == &wanted)
        })
        .collect()
}

/// Everything `_reusable` compares a stored plan against.
struct Compare<'a> {
    rows: &'a [PlanRow],
    pattern: &'a str,
    brief: &'a str,
    brief_sha: &'a str,
    plan_sha: &'a str,
    wanted_correlation: Option<&'a str>,
    request: &'a PlanRequest,
}

/// `_reusable`: the correlation to reuse when the prefix already holds exactly this plan.
fn reusable(existing: &[&Fields], compare: &Compare<'_>) -> Planned<Option<String>> {
    let mut owners: Vec<String> = Vec::new();
    for task in existing {
        let owner = text_column(task, "correlation_id")?;
        if !owners.contains(&owner) {
            owners.push(owner);
        }
    }
    if existing.len() != compare.rows.len() || owners.len() != 1 {
        return Ok(None);
    }
    let corr = owners.remove(0);
    if compare.wanted_correlation.is_some_and(|want| want != corr) {
        return Ok(None);
    }
    let excerpt: String = compare.brief.chars().take(BRIEF_EXCERPT_CHARS).collect();
    for (task, row) in existing.iter().zip(compare.rows) {
        let notes = notes_of(task)?;
        let same_brief = match obj_get(&notes, "brief_sha256") {
            Some(recorded) => py_eq(recorded, &jstr(compare.brief_sha)),
            None => obj_get(&notes, "brief_excerpt")
                .is_some_and(|recorded| py_eq(recorded, &jstr(&excerpt))),
        };
        let same_plan = match obj_get(&notes, "plan_sha256") {
            Some(recorded) => py_eq(recorded, &jstr(compare.plan_sha)),
            None => compare.pattern != "custom",
        };
        let same_pattern = obj_get(&notes, "pattern")
            .is_some_and(|recorded| py_eq(recorded, &jstr(compare.pattern)));
        if !same_pattern || !same_brief || !same_plan {
            return Ok(None);
        }
        let risk = if truthy(&row.risk) {
            row.risk.clone()
        } else {
            jstr(&compare.request.risk_class)
        };
        let acceptance = if truthy(&row.acceptance) {
            row.acceptance.clone()
        } else {
            strings(&compare.request.acceptance)
        };
        let stored = (
            column(task, "risk_class"),
            column(task, "priority"),
            &json_column(task, "acceptance", "[]")?,
        );
        if !py_eq(stored.0, &risk)
            || !py_eq(stored.1, &jstr(&compare.request.priority))
            || !py_eq(stored.2, &acceptance)
        {
            return Ok(None);
        }
    }
    Ok(Some(corr))
}

/// The reply of a dry run, or the facts it needs.
struct Prepared {
    brief: String,
    pattern: String,
    rows: Vec<PlanRow>,
    correlation_id: String,
    brief_sha: String,
    plan_sha: String,
}

/// Steps 1 to 6 of `run()`: validation, the rows and the two hashes.
fn prepare(facts: &PlanFacts, request: &PlanRequest) -> Planned<Prepared> {
    if let Some(graph_id) = &request.graph_id {
        if !is_graph_id(graph_id) {
            return refused(
                ErrorCode::Input,
                format!(
                    "invalid --graph-id {}: expected ut-<base36>-<8 hex>",
                    repr_text(&jstr(graph_id))?
                ),
            );
        }
    }
    let mut brief = match (&request.brief_file, &request.brief_text) {
        (Some(file), _) => read_text(file, "the brief file")?.to_string(),
        (None, Some(text)) => text.clone(),
        (None, None) => String::new(),
    };
    if brief.is_empty() && request.plan_file.is_none() {
        if !request.dry_run {
            return refused(
                ErrorCode::Input,
                "provide --brief FILE, --brief-text, or --plan plan.json",
            );
        }
        brief = DRY_RUN_BRIEF.to_string();
    }
    let correlation_id = request
        .correlation_id
        .clone()
        .filter(|id| !id.is_empty())
        .unwrap_or_else(|| facts.new_correlation_id.clone());
    let wants_slices = request.slices_file.is_some()
        || request
            .slices_json
            .as_deref()
            .is_some_and(|text| !text.is_empty())
        || brief.contains("```blast-radii");
    if request.plan_file.is_some() && wants_slices {
        return refused(
            ErrorCode::Input,
            "--plan is a custom DAG; it cannot be combined with blast-radius slices",
        );
    }
    let (pattern, rows) = if let Some(file) = &request.plan_file {
        ("custom".to_string(), load_custom(file, &facts.agents)?)
    } else if let Some(slices) = load_slices(&brief, request, &facts.agents)? {
        (
            "parallel".to_string(),
            parallel_rows(&slices, &request.risk_class)?,
        )
    } else if request.pattern == "parallel" {
        return refused(
            ErrorCode::Input,
            "pattern parallel needs a ```blast-radii``` block, --slices, or --slices-json",
        );
    } else if let Some(table) = pattern_table(&request.pattern) {
        (request.pattern.clone(), pattern_rows(table))
    } else {
        return not_started(
            "python-trace: `PATTERNS[args.pattern]` raises KeyError for this pattern",
        );
    };
    let brief_sha = sha256_hex(brief.as_bytes());
    let hashed = Json::Arr(rows.iter().map(PlanRow::hashed).collect());
    let plan_sha = sha256_hex(to_python_dumps_sorted(&hashed).as_bytes());
    Ok(Prepared {
        brief,
        pattern,
        rows,
        correlation_id,
        brief_sha,
        plan_sha,
    })
}

fn lanes_lead(rows: &[PlanRow], terminator: &str) -> String {
    let lanes = rows.iter().filter(|row| row.is_slice()).count();
    if lanes == 0 {
        String::new()
    } else {
        format!("parallel blast radii: {lanes} lanes, Greptile review after merge{terminator}")
    }
}

fn dry_run_reply(prepared: &Prepared, request: &PlanRequest) -> Planned<PlanDerived> {
    let prefix = match request.prefix.as_deref() {
        Some(prefix) if !prefix.is_empty() => prefix.to_string(),
        _ => derived_prefix(&prepared.correlation_id, 4),
    };
    let tasks: Vec<Json> = prepared
        .rows
        .iter()
        .map(|row| {
            Json::Obj(vec![
                (
                    "task_id".to_string(),
                    jstr(&format!("{prefix}-{}", row.suffix)),
                ),
                ("capability".to_string(), jstr(&row.capability)),
                ("agent".to_string(), jstr(&row.agent)),
                ("depends_on".to_string(), row.deps.clone()),
            ])
        })
        .collect();
    let summary = format!(
        "dry-run: {}would create {} tasks",
        lanes_lead(&prepared.rows, "; "),
        prepared.rows.len()
    );
    Ok(PlanDerived {
        outcome: Outcome::DryRun,
        reply: vec![
            ("status".to_string(), jstr("ok")),
            ("correlation_id".to_string(), jstr(&prepared.correlation_id)),
            ("pattern".to_string(), jstr(&prepared.pattern)),
            ("dry_run".to_string(), Json::Bool(true)),
            ("tasks".to_string(), Json::Arr(tasks)),
            ("summary".to_string(), jstr(&summary)),
        ],
        ctx_correlation_id: request.correlation_id.clone().filter(|id| !id.is_empty()),
        ops: Vec::new(),
        files: None,
        effects: Vec::new(),
        kickoff: None,
    })
}

/// The loop that creates the rows, with the failure points of `run()` in their order.
fn creation_ops(
    prepared: &Prepared,
    request: &PlanRequest,
    prefix: &str,
    now: f64,
) -> Planned<Vec<NewTask>> {
    let mut depth: HashMap<&str, i64> = HashMap::new();
    let mut created: Vec<NewTask> = Vec::with_capacity(prepared.rows.len());
    let excerpt: String = prepared.brief.chars().take(BRIEF_EXCERPT_CHARS).collect();
    for (index, row) in prepared.rows.iter().enumerate() {
        let task_id = format!("{prefix}-{}", row.suffix);
        let risk = if truthy(&row.risk) {
            row.risk.clone()
        } else {
            jstr(&request.risk_class)
        };
        let Json::Arr(deps) = &row.deps else {
            return not_started("value-shape: depends_on is not a list");
        };
        let mut deepest = -1;
        let mut depends_on = Vec::with_capacity(deps.len());
        for dep in deps {
            let Json::Str(name) = dep else {
                return not_started(
                    "python-trace: depends_on names something that is not a task id",
                );
            };
            let Some(at) = depth.get(name.as_str()) else {
                return not_started(
                    "python-trace: depends_on names a task defined later or not at all",
                );
            };
            deepest = deepest.max(*at);
            depends_on.push(format!("{prefix}-{name}"));
        }
        let dag_depth = 1 + deepest;
        depth.insert(&row.suffix, dag_depth);
        let mut notes: Fields = vec![
            ("pattern".to_string(), jstr(&prepared.pattern)),
            ("brief_excerpt".to_string(), jstr(&excerpt)),
            ("brief_sha256".to_string(), jstr(&prepared.brief_sha)),
            ("plan_sha256".to_string(), jstr(&prepared.plan_sha)),
        ];
        match &row.gates {
            Json::Null => {}
            Json::Obj(spec) => {
                let (Some(gate), Some(targets)) = (obj_get(spec, "gate"), obj_get(spec, "for"))
                else {
                    return not_started("python-trace: a gate task has no `gate` or no `for`");
                };
                let Json::Arr(targets) = targets else {
                    return not_started("value-shape: a gate task's `for` is not a list");
                };
                let ids: Vec<Json> = targets
                    .iter()
                    .map(|target| match target {
                        Json::Str(name) => Ok(jstr(&format!("{prefix}-{name}"))),
                        _ => not_started("value-shape: a gate target is not a task id"),
                    })
                    .collect::<Planned<_>>()?;
                set(&mut notes, "gate", gate.clone());
                set(&mut notes, "gate_for", Json::Arr(ids));
                set(&mut notes, "gates", Json::Arr(Vec::new()));
            }
            gates @ Json::Arr(_) => set(&mut notes, "gates", gates.clone()),
            _ => return not_started("value-shape: gates is neither a list nor an object"),
        }
        if let Some(lane) = &row.lane {
            for (key, value) in lane_notes(lane, prefix, &row.suffix)? {
                set(&mut notes, &key, value);
            }
        }
        let acceptance = if truthy(&row.acceptance) {
            row.acceptance.clone()
        } else {
            strings(&request.acceptance)
        };
        let risk_class = match &risk {
            Json::Str(name) if budget_of(name).is_some() => name.clone(),
            _ => {
                return not_started(
                    "python-trace: `_budget(risk)` raises KeyError for this risk class",
                )
            }
        };
        let budget = budget_of(&risk_class).unwrap_or(Json::Null);
        if !matches!(row.title, Json::Str(_) | Json::Null) {
            return not_started(
                "value-shape: a title that is not text or null is bound or coerced by sqlite",
            );
        }
        if created.iter().any(|task| task.task_id == task_id) {
            return refused(
                ErrorCode::Contract,
                format!("prefix {prefix} already in use; pass a new --prefix"),
            );
        }
        created.push(NewTask {
            task_id,
            correlation_id: prepared.correlation_id.clone(),
            capability: row.capability.clone(),
            title: row.title.clone(),
            agent_id: row.agent.clone(),
            dag_depth,
            depends_on,
            acceptance,
            budget,
            risk_class,
            priority: request.priority.clone(),
            notes,
            created_at: now + index as f64 * CREATED_AT_STEP,
        });
    }
    Ok(created)
}

/// The creation transaction's operations: each task is created and then moved `CREATED -> VALIDATED -> PLANNED`.
fn operations(created: Vec<NewTask>, pattern: &str) -> Vec<PlanOp> {
    let mut ops = Vec::with_capacity(created.len() * 3);
    for task in created {
        let task_id = task.task_id.clone();
        ops.push(PlanOp::Create(Box::new(task)));
        ops.push(PlanOp::Transition(Step {
            task_id: task_id.clone(),
            from: "CREATED",
            to: "VALIDATED",
            actor: "A01",
            reason: "brief validated by A01".to_string(),
        }));
        ops.push(PlanOp::Transition(Step {
            task_id,
            from: "VALIDATED",
            to: "PLANNED",
            actor: "A01",
            reason: format!("pattern={pattern}"),
        }));
    }
    ops
}

/// The rows of the new tasks as the store holds them once the plan is committed (state `PLANNED`).
fn planned_rows(created: &[NewTask]) -> Vec<Fields> {
    created
        .iter()
        .map(|task| {
            let mut row = task.insert_values();
            set(&mut row, "state", jstr("PLANNED"));
            row
        })
        .collect()
}

/// The `created` summary line of one task.
fn summary_line(task: &NewTask) -> String {
    let deps = if task.depends_on.is_empty() {
        "-".to_string()
    } else {
        task.depends_on.join(",")
    };
    format!(
        "{:<14} {:<4} d={} \u{2190} {}",
        task.task_id, task.agent_id, task.dag_depth, deps
    )
}

/// `_execution_result`: the kickoff of a controller run, or the reply unchanged. `after_write` says whether Python has already
/// committed when it gets here (a creation): a refusal then cannot be reproduced and is declined instead.
fn execution_step(
    signing: &Signing,
    corr: &str,
    tasks: Vec<Json>,
    plan_text: Option<&str>,
    reply: &mut Fields,
    after_write: bool,
) -> Planned<Option<Kickoff>> {
    if signing.agent_session {
        return Ok(None);
    }
    if let Some(error) = &signing.config_error {
        return refused(ErrorCode::Policy, error);
    }
    if !signing.real_key {
        return Ok(None);
    }
    let contract = |message: &str| -> Planned<Option<Kickoff>> {
        if after_write {
            not_started(format!(
                "after-write: Python commits the plan and then raises E-CONTRACT ({message})"
            ))
        } else {
            refused(ErrorCode::Contract, message)
        }
    };
    if tasks.is_empty() {
        return contract("controller assignment requires a persisted plan");
    }
    let view = |task: &Json, key: &str| -> Json {
        match task {
            Json::Obj(fields) => obj_get(fields, key).cloned().unwrap_or(Json::Null),
            _ => Json::Null,
        }
    };
    let Json::Obj(first_notes) = view(&tasks[0], "notes") else {
        return not_started("kickoff-input: a task has no notes object");
    };
    let Some(pattern) = obj_get(&first_notes, "pattern").cloned() else {
        return not_started("kickoff-input: the first task has no `notes.pattern`");
    };
    let mut plan: Fields = Vec::new();
    if let Some(text) = plan_text {
        match parse_python_json(text) {
            Ok(Json::Obj(body)) => {
                if obj_get(&body, "correlation_id") != Some(&jstr(corr)) {
                    return contract("persisted controller plan correlation mismatch");
                }
                plan = body;
            }
            Ok(_) => return contract("persisted controller plan correlation mismatch"),
            Err(_) => return not_started(
                "json-not-reproduced: the persisted plan is not JSON that Rust reads like Python",
            ),
        }
    }
    set(&mut plan, "correlation_id", jstr(corr));
    set(&mut plan, "pattern", pattern.clone());
    set(&mut plan, "tasks", Json::Arr(tasks.clone()));
    let mut risk_index = 0;
    let mut acceptance: Vec<Json> = Vec::new();
    let mut totals = [0i64; 3];
    let mut satisfied: Vec<String> = Vec::new();
    for task in &tasks {
        match view(task, "risk_class") {
            Json::Str(name) => match RISK_ORDER.iter().position(|known| *known == name) {
                Some(at) => risk_index = risk_index.max(at),
                None => {
                    return not_started(
                        "kickoff-input: a task has a risk class outside low, medium, high",
                    )
                }
            },
            _ => {
                return not_started(
                    "kickoff-input: a task has a risk class outside low, medium, high",
                )
            }
        }
        let Json::Arr(lines) = view(task, "acceptance") else {
            return not_started("kickoff-input: a task's acceptance is not a list");
        };
        for line in lines {
            if !matches!(line, Json::Str(_)) {
                return not_started(
                    "kickoff-input: a task's acceptance holds an item that is not text",
                );
            }
            if !acceptance.contains(&line) {
                acceptance.push(line);
            }
        }
        let Json::Obj(budget) = view(task, "budget") else {
            return not_started("kickoff-input: a task's budget is not an object");
        };
        for (total, name) in totals
            .iter_mut()
            .zip(["max_tokens", "max_wall_s", "max_cost_usd"])
        {
            let amount = match obj_get(&budget, name) {
                None => 0,
                Some(Json::Int(amount)) => *amount,
                Some(_) => {
                    return not_started(
                        "kickoff-input: a task's budget holds a number that is not an integer",
                    )
                }
            };
            *total = total.checked_add(amount).map_or_else(
                || not_started("kickoff-input: the budget sum overflows"),
                Ok,
            )?;
        }
        if let (Json::Str(id), Json::Str(state)) = (view(task, "task_id"), view(task, "state")) {
            if SATISFIED_STATES.contains(&state.as_str()) {
                satisfied.push(id);
            }
        }
    }
    let mut ready: Vec<Json> = Vec::new();
    for task in &tasks {
        let (Json::Str(id), Json::Str(state), Json::Arr(deps)) = (
            view(task, "task_id"),
            view(task, "state"),
            view(task, "depends_on"),
        ) else {
            return not_started("kickoff-input: a task has no id, state or dependency list");
        };
        if !READY_STATES.contains(&state.as_str()) {
            continue;
        }
        let mut done = true;
        for dep in &deps {
            let Json::Str(name) = dep else {
                return not_started("kickoff-input: a dependency is not a task id");
            };
            done &= satisfied.contains(name);
        }
        if done {
            ready.push(jstr(&id));
        }
    }
    let risk = RISK_ORDER[risk_index];
    let payload = Json::Obj(vec![
        ("task_id".to_string(), jstr(corr)),
        ("correlation_id".to_string(), jstr(corr)),
        ("agent_id".to_string(), jstr("A01")),
        ("capability".to_string(), jstr("plan.execute")),
        ("ready_tasks".to_string(), Json::Arr(ready)),
        ("inputs".to_string(), Json::Arr(vec![Json::Obj(plan)])),
        ("acceptance".to_string(), Json::Arr(acceptance)),
        (
            "budget".to_string(),
            Json::Obj(
                ["max_tokens", "max_wall_s", "max_cost_usd"]
                    .iter()
                    .zip(totals)
                    .map(|(name, total)| (name.to_string(), Json::Int(total)))
                    .collect(),
            ),
        ),
        ("risk_class".to_string(), jstr(risk)),
    ]);
    let Ok(payload) = json_safe(&payload) else {
        return contract("controller assignment carries a number JSON cannot round-trip");
    };
    set(reply, "tasks", Json::Arr(tasks));
    set(reply, "pattern", pattern);
    Ok(Some(Kickoff {
        correlation_id: corr.to_string(),
        risk_class: risk.to_string(),
        payload,
    }))
}

/// The reply and operations of a call that found the prefix already holding this plan.
fn reuse_reply(facts: &PlanFacts, corr: &str, pattern: &str) -> Planned<PlanDerived> {
    check_safe_id(corr, "correlation id").map_err(PlanError::Refused)?;
    let plan_text = stored_plan_file(facts, corr)?;
    let recorded = plan_file_is_recorded(plan_text)?;
    let borrowed: Vec<&Fields> = facts.correlation_tasks.iter().collect();
    let tasks: Vec<Json> = correlation_rows(&borrowed, corr)?
        .into_iter()
        .map(task_view)
        .collect::<Planned<_>>()?;
    let summary = format!(
        "reused existing plan for correlation {corr} ({} tasks, no changes)",
        tasks.len()
    );
    let mut reply: Fields = vec![
        ("status".to_string(), jstr("ok")),
        ("correlation_id".to_string(), jstr(corr)),
        ("pattern".to_string(), jstr(pattern)),
        ("reused".to_string(), Json::Bool(true)),
        ("tasks".to_string(), Json::Arr(tasks.clone())),
        (
            "plan_file".to_string(),
            if recorded {
                jstr(&format!("{}/{corr}.json", facts.plans_dir))
            } else {
                Json::Null
            },
        ),
        ("summary".to_string(), jstr(&summary)),
    ];
    let kickoff = execution_step(
        &facts.signing,
        corr,
        tasks,
        plan_text.as_deref().filter(|_| recorded),
        &mut reply,
        false,
    )?;
    Ok(PlanDerived {
        outcome: Outcome::Reused,
        reply,
        ctx_correlation_id: Some(corr.to_string()),
        ops: Vec::new(),
        files: None,
        effects: Vec::new(),
        kickoff,
    })
}

/// Derives what `orch_plan.py` does for one call.
///
/// Python's order is kept: `--graph-id`, the brief, the plan or the slices, the rows and their hashes, the dry run, the signing check,
/// the prefix (the 4 character one, then the 6 character one when another correlation holds those tasks), reuse or conflict, the creation
/// loop, then the plan file, the effects and the kickoff. `now` is the host's single clock reading. Nothing is written by this function.
///
/// # Errors
///
/// [`PlanError::Refused`] carries Python's own `SwarmError` (code and exact message); [`PlanError::NotStarted`] marks everything Python
/// must answer, see the module documentation.
pub fn derive_plan(facts: &PlanFacts, request: &PlanRequest, now: f64) -> Planned<PlanDerived> {
    let prepared = prepare(facts, request)?;
    if request.dry_run {
        return dry_run_reply(&prepared, request);
    }
    if !facts.signing.agent_session {
        if let Some(error) = &facts.signing.config_error {
            return refused(ErrorCode::Policy, error);
        }
    }
    let corr = prepared.correlation_id.as_str();
    let prefix = match request.prefix.as_deref() {
        Some(prefix) => prefix.to_string(),
        None => {
            let short = derived_prefix(corr, 4);
            let holders = existing_tasks(facts, &short, &prepared.rows);
            let mut other = !holders.is_empty();
            for task in &holders {
                other &= text_column(task, "correlation_id")? != corr;
            }
            if other {
                derived_prefix(corr, 6)
            } else {
                short
            }
        }
    };
    let existing = existing_tasks(facts, &prefix, &prepared.rows);
    if !existing.is_empty() {
        let compare = Compare {
            rows: &prepared.rows,
            pattern: &prepared.pattern,
            brief: &prepared.brief,
            brief_sha: &prepared.brief_sha,
            plan_sha: &prepared.plan_sha,
            wanted_correlation: request
                .correlation_id
                .as_deref()
                .filter(|id| !id.is_empty()),
            request,
        };
        if let Some(reused) = reusable(&existing, &compare)? {
            return reuse_reply(facts, &reused, &prepared.pattern);
        }
        let mut owners: Vec<String> = Vec::new();
        for task in &existing {
            let owner = text_column(task, "correlation_id")?;
            if !owners.contains(&owner) {
                owners.push(owner);
            }
        }
        owners.sort();
        let hint = if request
            .prefix
            .as_deref()
            .is_some_and(|text| !text.is_empty())
        {
            "pass a new --prefix"
        } else {
            "pass a new --correlation-id or --prefix for this brief"
        };
        return refused(
            ErrorCode::Contract,
            format!(
                "prefix {prefix} already used by correlation {} with a different brief/pattern/plan/risk-class/priority/acceptance; {hint}",
                owners.join(",")
            ),
        );
    }
    let created = creation_ops(&prepared, request, &prefix, now)?;
    let after = planned_rows(&created);
    let mut every: Vec<&Fields> = facts.correlation_tasks.iter().collect();
    every.extend(after.iter());
    let plan_tasks: Vec<Json> = correlation_rows(&every, corr)?
        .into_iter()
        .map(task_view)
        .collect::<Planned<_>>()?;
    let plan = vec![
        ("correlation_id".to_string(), jstr(corr)),
        ("pattern".to_string(), jstr(&prepared.pattern)),
        ("risk_class".to_string(), jstr(&request.risk_class)),
        ("brief".to_string(), jstr(&prepared.brief)),
        ("tasks".to_string(), Json::Arr(plan_tasks.clone())),
    ];
    let plan_text = to_python_json(&Json::Obj(plan));
    let task_ids: Vec<String> = plan_tasks
        .iter()
        .filter_map(|task| match task {
            Json::Obj(fields) => match obj_get(fields, "task_id") {
                Some(Json::Str(id)) => Some(id.clone()),
                _ => None,
            },
            _ => None,
        })
        .collect();
    let lines: Vec<String> = created.iter().map(summary_line).collect();
    let summary = format!(
        "{}created {} tasks for correlation {corr}\n{}",
        lanes_lead(&prepared.rows, "\n"),
        created.len(),
        lines.join("\n")
    );
    let mut reply: Fields = vec![
        ("status".to_string(), jstr("ok")),
        ("correlation_id".to_string(), jstr(corr)),
        ("pattern".to_string(), jstr(&prepared.pattern)),
        ("tasks".to_string(), Json::Arr(plan_tasks.clone())),
        (
            "plan_file".to_string(),
            jstr(&format!("{}/{corr}.json", facts.plans_dir)),
        ),
        ("summary".to_string(), jstr(&summary)),
    ];
    let kickoff = execution_step(
        &facts.signing,
        corr,
        plan_tasks,
        Some(&plan_text),
        &mut reply,
        true,
    )?;
    let effects = vec![
        PlanEffect::BindGraph(GraphBinding {
            correlation_id: corr.to_string(),
            explicit_graph_id: request.graph_id.clone(),
            brief: prepared.brief.clone(),
            task_ids,
        }),
        PlanEffect::Emit {
            event_type: "plan.updated".to_string(),
            payload: vec![
                ("pattern".to_string(), jstr(&prepared.pattern)),
                ("task_count".to_string(), Json::Int(created.len() as i64)),
            ],
        },
    ];
    Ok(PlanDerived {
        outcome: Outcome::Created,
        reply,
        ctx_correlation_id: Some(corr.to_string()),
        ops: operations(created, &prepared.pattern),
        files: Some(PlanFiles {
            plan_name: format!("{corr}.json"),
            plan_text,
            latest_correlation: corr.to_string(),
        }),
        effects,
        kickoff,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transition_plan::legal_targets;
    use crate::{finish, parse_json};

    const VECTORS: &str = include_str!("../tests/vectors/plan_derive.json");

    /// Every decline class of the module documentation, with what it covers. A case of the vectors may only name one of these.
    const DECLINES: [(&str, &str); 9] = [
        ("python-trace", "Python raises an exception its generic handler prints with a traceback"),
        ("value-shape", "a custom plan value Python iterates, coerces or fails on after the commit"),
        ("unreadable-file", "a brief, plan or slice file that exists and cannot be read"),
        ("json-not-reproduced", "JSON Python reads and Rust does not, or text that is not JSON (the decoder's messages are not reproduced)"),
        ("non-ascii-repr", "a message that embeds the repr of a non-ASCII value, or an agent reference Python lower-cases by Unicode rules"),
        ("unicode-digit", "a task id that ends in a non-ASCII number, which `\\d` reads as a digit"),
        ("after-write", "Python commits and then raises"),
        ("kickoff-input", "a stored value the controller kickoff does not reproduce"),
        ("stored-row", "a stored row or plan file Rust cannot read the way Python does"),
    ];

    fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
        match value {
            Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
            other => panic!("not an object looking for {key}: {other:?}"),
        }
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

    fn optional_text(value: &Json) -> Option<String> {
        match value {
            Json::Null => None,
            other => Some(text(other).to_string()),
        }
    }

    fn file_read(value: &Json) -> Option<FileRead> {
        match value {
            Json::Null => None,
            Json::Obj(fields) => Some(match obj_get(fields, "text") {
                Some(content) => FileRead::Text(text(content).to_string()),
                None => FileRead::Unreadable,
            }),
            other => panic!("not a file read: {other:?}"),
        }
    }

    fn doc() -> Json {
        parse_json(VECTORS).expect("the vector file parses")
    }

    fn agents() -> Vec<Agent> {
        items(member(&doc(), "agents"))
            .iter()
            .map(|agent| Agent {
                id: text(member(agent, "id")).to_string(),
                code: text(member(agent, "code")).to_string(),
                slug: text(member(agent, "slug")).to_string(),
                capabilities: items(member(agent, "capabilities"))
                    .iter()
                    .map(|c| text(c).to_string())
                    .collect(),
            })
            .collect()
    }

    /// How the Rust planner must answer a case.
    #[derive(Debug, PartialEq)]
    enum Answer {
        Plan,
        NotStarted,
    }

    struct Case {
        name: String,
        group: String,
        answer: Answer,
        why: Option<String>,
        now: f64,
        request: PlanRequest,
        facts: PlanFacts,
        expect: Json,
    }

    fn decode_request(value: &Json) -> PlanRequest {
        PlanRequest {
            pattern: text(member(value, "pattern")).to_string(),
            risk_class: text(member(value, "risk_class")).to_string(),
            priority: text(member(value, "priority")).to_string(),
            prefix: optional_text(member(value, "prefix")),
            acceptance: items(member(value, "acceptance"))
                .iter()
                .map(|line| text(line).to_string())
                .collect(),
            graph_id: optional_text(member(value, "graph_id")),
            brief_file: file_read(member(value, "brief_file")),
            brief_text: optional_text(member(value, "brief_text")),
            plan_file: file_read(member(value, "plan_file")),
            slices_json: optional_text(member(value, "slices_json")),
            slices_file: file_read(member(value, "slices_file")),
            correlation_id: optional_text(member(value, "correlation_id")),
            dry_run: flag(member(value, "dry_run")),
        }
    }

    fn decode_facts(value: &Json, roster: &[Agent]) -> PlanFacts {
        let signing = member(value, "signing");
        PlanFacts {
            new_correlation_id: text(member(value, "new_correlation_id")).to_string(),
            agents: roster.to_vec(),
            plans_dir: text(member(value, "plans_dir")).to_string(),
            signing: Signing {
                agent_session: flag(member(signing, "agent_session")),
                config_error: optional_text(member(signing, "config_error")),
                real_key: flag(member(signing, "real_key")),
            },
            prefix_tasks: items(member(value, "prefix_tasks"))
                .iter()
                .map(fields)
                .collect(),
            correlation_tasks: items(member(value, "correlation_tasks"))
                .iter()
                .map(fields)
                .collect(),
            plan_files: items(member(value, "plan_files"))
                .iter()
                .map(|file| StoredPlanFile {
                    correlation_id: text(member(file, "correlation_id")).to_string(),
                    text: optional_text(member(file, "text")),
                })
                .collect(),
        }
    }

    fn cases() -> Vec<Case> {
        let document = doc();
        let roster = agents();
        items(member(&document, "cases"))
            .iter()
            .map(|case| Case {
                name: text(member(case, "name")).to_string(),
                group: text(member(case, "group")).to_string(),
                answer: match text(member(case, "rust")) {
                    "plan" => Answer::Plan,
                    "not_started" => Answer::NotStarted,
                    other => panic!("unknown answer {other}"),
                },
                why: optional_text(member(case, "why")),
                now: number(member(case, "now")),
                request: decode_request(member(case, "request")),
                facts: decode_facts(member(case, "facts"), &roster),
                expect: member(case, "expect").clone(),
            })
            .collect()
    }

    fn case_named(name: &str) -> Case {
        cases()
            .into_iter()
            .find(|case| case.name == name)
            .unwrap_or_else(|| panic!("no case {name}"))
    }

    fn derive(case: &Case) -> Planned<PlanDerived> {
        derive_plan(&case.facts, &case.request, case.now)
    }

    /// The printed reply of a call that returned, as `AgentScript.main` prints it.
    fn printed(reply: &Fields) -> String {
        finish(reply.clone(), "A01", "orch_plan").stdout
    }

    /// The reply object inside a printed `--json` document: everything but the trailing `agent` and `script`.
    fn reply_of(stdout: &str) -> Fields {
        let mut reply = fields(&parse_json(stdout).expect("stdout is JSON"));
        assert_eq!(reply.pop().map(|(key, _)| key), Some("script".to_string()));
        assert_eq!(reply.pop().map(|(key, _)| key), Some("agent".to_string()));
        reply
    }

    /// The audit rows a plan's operations insert, as `(task_id, from, to, actor, reason)`.
    fn audit_rows(ops: &[PlanOp]) -> Vec<(String, Option<String>, String, String, String)> {
        ops.iter()
            .map(|op| match op {
                PlanOp::Create(task) => (
                    task.task_id.clone(),
                    None,
                    "CREATED".to_string(),
                    "system".to_string(),
                    "created".to_string(),
                ),
                PlanOp::Transition(step) => (
                    step.task_id.clone(),
                    Some(step.from.to_string()),
                    step.to.to_string(),
                    step.actor.to_string(),
                    step.reason.clone(),
                ),
            })
            .collect()
    }

    fn assert_refusal(name: &str, error: &SeamError, expect: &Json, stdout: &str) {
        assert_eq!(
            error.code().as_str(),
            text(member(expect, "code")),
            "{name}: code"
        );
        assert_eq!(
            error.message(),
            text(member(expect, "message")),
            "{name}: message"
        );
        assert_eq!(
            Json::Obj(error.details().to_vec()),
            *member(expect, "details"),
            "{name}: details"
        );
        let document = Json::Obj(vec![
            ("agent".to_string(), jstr("A01")),
            ("script".to_string(), jstr("orch_plan")),
            ("status".to_string(), jstr("error")),
            ("error".to_string(), error.to_json()),
        ]);
        assert_eq!(
            format!("{}\n", to_python_json(&document)),
            stdout,
            "{name}: printed error"
        );
    }

    fn assert_created_rows(name: &str, derived: &PlanDerived, expect: &Json) {
        let recorded = items(member(expect, "tasks"));
        let creates: Vec<&NewTask> = derived
            .ops
            .iter()
            .filter_map(|op| match op {
                PlanOp::Create(task) => Some(task.as_ref()),
                PlanOp::Transition(_) => None,
            })
            .collect();
        assert_eq!(
            creates.len(),
            recorded.len(),
            "{name}: number of created tasks"
        );
        for (task, row) in creates.iter().zip(recorded) {
            assert_eq!(
                text(member(row, "task_id")),
                task.task_id,
                "{name}: creation order"
            );
            for (column, value) in task.insert_values() {
                if column == "state" || column == "updated_at" {
                    continue;
                }
                assert_eq!(
                    &value,
                    member(row, &column),
                    "{name}: {} column {column}",
                    task.task_id
                );
            }
            assert_eq!(text(member(row, "state")), "PLANNED", "{name}: final state");
        }
        let transitions: Vec<(String, Option<String>, String, String, String)> =
            items(member(expect, "transitions"))
                .iter()
                .map(|row| {
                    (
                        text(member(row, "task_id")).to_string(),
                        optional_text(member(row, "from_state")),
                        text(member(row, "to_state")).to_string(),
                        text(member(row, "actor")).to_string(),
                        text(member(row, "reason")).to_string(),
                    )
                })
                .collect();
        assert_eq!(audit_rows(&derived.ops), transitions, "{name}: audit rows");
    }

    fn assert_effects(name: &str, derived: &PlanDerived, expect: &Json) {
        let recorded = member(expect, "graph_bind");
        let updated = items(member(expect, "plan_updated"));
        if derived.outcome != Outcome::Created {
            assert!(derived.effects.is_empty(), "{name}: no effects");
            assert_eq!(*recorded, Json::Null, "{name}: no binding");
            assert!(updated.is_empty(), "{name}: no plan.updated");
            return;
        }
        let [PlanEffect::BindGraph(bind), PlanEffect::Emit {
            event_type,
            payload,
        }] = derived.effects.as_slice()
        else {
            panic!(
                "{name}: effects are a binding then an event: {:?}",
                derived.effects
            );
        };
        assert_eq!(
            text(member(recorded, "correlation_id")),
            bind.correlation_id,
            "{name}: bind correlation"
        );
        assert_eq!(
            optional_text(member(recorded, "explicit_graph_id")),
            bind.explicit_graph_id,
            "{name}: bind graph id"
        );
        assert_eq!(
            text(member(recorded, "brief")),
            bind.brief,
            "{name}: bind brief"
        );
        let ids: Vec<String> = items(member(recorded, "task_ids"))
            .iter()
            .map(|id| text(id).to_string())
            .collect();
        assert_eq!(ids, bind.task_ids, "{name}: bind tasks");
        assert_eq!(updated.len(), 1, "{name}: one plan.updated");
        assert_eq!(
            text(member(&updated[0], "type")),
            event_type,
            "{name}: event type"
        );
        assert_eq!(
            *member(&updated[0], "payload"),
            Json::Obj(payload.clone()),
            "{name}: event payload"
        );
        assert_eq!(
            optional_text(member(&updated[0], "correlation_id")).as_deref(),
            derived.ctx_correlation_id.as_deref(),
            "{name}: event correlation"
        );
    }

    /// Replays one recorded case and compares everything Python left behind.
    fn replay(case: &Case) {
        let name = case.name.as_str();
        let outcome = derive(case);
        if case.answer == Answer::NotStarted {
            let why = case
                .why
                .as_deref()
                .unwrap_or_else(|| panic!("{name}: a decline names its class"));
            match outcome {
                Err(PlanError::NotStarted(message)) => {
                    assert!(
                        message.starts_with(&format!("{why}:")),
                        "{name}: declined as {message:?}, recorded as {why}"
                    );
                }
                other => panic!("{name}: must decline ({why}), got {other:?}"),
            }
            return;
        }
        let expect = &case.expect;
        let stdout = match member(expect, "stdout") {
            Json::Null => None,
            other => Some(text(other)),
        };
        if *member(expect, "error") != Json::Null {
            let Err(PlanError::Refused(error)) = outcome else {
                panic!("{name}: must refuse like Python, got {outcome:?}");
            };
            assert_refusal(
                name,
                &error,
                member(expect, "error"),
                stdout.expect("an error is printed"),
            );
            return;
        }
        let derived = outcome.unwrap_or_else(|error| panic!("{name}: {error}"));
        match stdout {
            Some(stdout) => {
                assert_eq!(printed(&derived.reply), stdout, "{name}: printed reply");
                assert_eq!(
                    Json::Obj(derived.reply.clone()),
                    Json::Obj(reply_of(stdout)),
                    "{name}: reply object"
                );
            }
            None => assert_eq!(
                Json::Obj(derived.reply.clone()),
                *member(expect, "reply"),
                "{name}: reply object"
            ),
        }
        assert_eq!(
            derived.ctx_correlation_id,
            optional_text(member(expect, "exit_record_correlation")),
            "{name}: correlation of the exit record"
        );
        assert_created_rows(name, &derived, expect);
        assert_effects(name, &derived, expect);
        match (&derived.files, member(expect, "plan_file")) {
            (None, Json::Null) => assert_eq!(
                *member(expect, "latest_correlation"),
                Json::Null,
                "{name}: latest_correlation"
            ),
            (Some(files), recorded) => {
                assert_eq!(
                    files.plan_name,
                    text(member(recorded, "name")),
                    "{name}: plan file name"
                );
                assert_eq!(
                    files.plan_text,
                    text(member(recorded, "text")),
                    "{name}: plan file bytes"
                );
                assert_eq!(
                    Some(&files.latest_correlation),
                    optional_text(member(expect, "latest_correlation")).as_ref(),
                    "{name}: latest_correlation"
                );
            }
            (None, recorded) => {
                panic!("{name}: Python wrote a plan file, the plan has none: {recorded:?}")
            }
        }
        match (&derived.kickoff, member(expect, "kickoff")) {
            (None, Json::Null) => {}
            (Some(kickoff), recorded) => {
                assert_eq!(
                    kickoff.correlation_id,
                    text(member(recorded, "correlation_id")),
                    "{name}: kickoff correlation"
                );
                assert_eq!(
                    kickoff.risk_class,
                    text(member(recorded, "risk_class")),
                    "{name}: kickoff risk"
                );
                assert_eq!(
                    kickoff.payload,
                    *member(recorded, "payload"),
                    "{name}: kickoff payload"
                );
                assert!(
                    flag(member(expect, "kickoff_verified")),
                    "{name}: Python verified its assignment"
                );
            }
            (None, recorded) => {
                panic!("{name}: Python signed a kickoff, the plan has none: {recorded:?}")
            }
        }
        for op in &derived.ops {
            if let PlanOp::Transition(step) = op {
                assert!(
                    legal_targets(step.from).is_some_and(|to| to.contains(&step.to)),
                    "{name}: {} -> {} is legal",
                    step.from,
                    step.to
                );
            }
        }
    }

    #[test]
    fn every_recorded_case_replays_to_what_python_did() {
        let all = cases();
        assert!(all.len() >= 350, "the corpus is {} cases", all.len());
        let failures: Vec<String> = all
            .iter()
            .filter_map(|case| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| replay(case)))
                    .err()
                    .map(|payload| {
                        let why = payload.downcast_ref::<String>().cloned().or_else(|| {
                            payload
                                .downcast_ref::<&str>()
                                .map(|text| (*text).to_string())
                        });
                        why.unwrap_or_else(|| format!("{}: a panic without text", case.name))
                    })
            })
            .collect();
        assert!(
            failures.is_empty(),
            "{} of {} cases differ:\n{}",
            failures.len(),
            all.len(),
            failures.join("\n")
        );
    }

    #[test]
    fn the_cases_cover_every_behaviour_the_planner_has() {
        let all = cases();
        let planned = |case: &Case| case.answer == Answer::Plan;
        let of_group = |group: &str| all.iter().filter(|case| case.group == group).count();
        for group in [
            "pattern",
            "brief",
            "dry-run",
            "prefix",
            "reuse",
            "graph",
            "signing",
            "kickoff",
            "custom",
            "parallel",
            "parallel-slices",
            "parallel-declined",
        ] {
            assert!(
                of_group(group) >= 4,
                "group {group} has {} cases",
                of_group(group)
            );
        }
        for pattern in PATTERN_NAMES {
            for risk in ["low", "medium", "high"] {
                let name = format!("pattern-{pattern}-{risk}");
                let case = case_named(&name);
                assert_eq!(
                    derive(&case).map(|d| d.outcome),
                    Ok(Outcome::Created),
                    "{name}"
                );
            }
        }
        let outcome = |case: &Case| derive(case).map(|derived| derived.outcome).ok();
        for wanted in [Outcome::DryRun, Outcome::Reused, Outcome::Created] {
            assert!(
                all.iter()
                    .any(|case| planned(case) && outcome(case) == Some(wanted)),
                "no case ends as {wanted:?}"
            );
        }
        let refused = |case: &Case, needle: &str| matches!(derive(case), Err(PlanError::Refused(error)) if error.message().contains(needle));
        for needle in [
            "already used by correlation",
            "ends in .rN",
            "no agent matches",
            "no agent offers capability",
            "does not offer",
            "already in use; pass a new --prefix",
            "provide --brief FILE",
            "cannot be combined with blast-radius slices",
            "pattern parallel needs",
            "pass blast radii only once",
            "blast radii overlap",
            "is a control or gate agent",
            "invalid --graph-id",
            "fail-closed: SWARM_REQUIRE_KEY=1",
            "persisted controller plan correlation mismatch",
            "cannot round-trip",
            "invalid correlation id",
            "brief has more than one",
            "must be repo-relative",
            "must stay inside the repo",
            "is reserved or repeated",
            "title must be a non-empty string",
            "acceptance must be an array of strings",
        ] {
            assert!(
                all.iter().any(|case| refused(case, needle)),
                "no case is refused with {needle:?}"
            );
        }
        let created = |case: &Case, wanted: &dyn Fn(&PlanDerived) -> bool| {
            derive(case).is_ok_and(|derived| wanted(&derived))
        };
        assert!(
            all.iter().any(|case| created(case, &|d| d.kickoff.is_some()
                && d.outcome == Outcome::Reused)),
            "no reuse with a kickoff"
        );
        assert!(
            all.iter().any(|case| created(case, &|d| d.kickoff.is_some()
                && d.outcome == Outcome::Created)),
            "no creation with a kickoff"
        );
        assert!(
            all.iter()
                .any(|case| created(case, &|d| d.outcome == Outcome::Reused
                    && matches!(member_of(&d.reply, "plan_file"), Json::Null))),
            "no reuse without a recorded plan file"
        );
    }

    fn member_of<'a>(reply: &'a Fields, key: &str) -> &'a Json {
        obj_get(reply, key).unwrap_or(&Json::Null)
    }

    #[test]
    fn the_declines_are_exactly_the_documented_classes() {
        let all = cases();
        let recorded: std::collections::BTreeSet<String> =
            all.iter().filter_map(|case| case.why.clone()).collect();
        let documented: std::collections::BTreeSet<String> = DECLINES
            .iter()
            .map(|(class, _)| class.to_string())
            .collect();
        assert_eq!(
            recorded, documented,
            "every class has a case and every case names a class"
        );
        for case in &all {
            assert_eq!(
                case.answer == Answer::NotStarted,
                case.why.is_some(),
                "{}: only a decline names a class",
                case.name
            );
        }
        for (class, what) in DECLINES {
            let exercised = all
                .iter()
                .filter(|case| case.why.as_deref() == Some(class))
                .count();
            assert!(
                exercised >= 2,
                "decline class {class} ({what}) has {exercised} cases"
            );
        }
    }

    fn constant<'a>(doc: &'a Json, key: &str) -> &'a Json {
        member(member(doc, "constants"), key)
    }

    #[test]
    fn the_pattern_tables_are_python_s() {
        let document = doc();
        let recorded = constant(&document, "patterns");
        let names: Vec<&str> = items(constant(&document, "pattern_names"))
            .iter()
            .map(text)
            .collect();
        assert_eq!(names, PATTERN_NAMES);
        for name in PATTERN_NAMES {
            let table = pattern_table(name).expect("a pattern has a table");
            let rows = pattern_rows(table);
            let want = items(member(recorded, name));
            assert_eq!(rows.len(), want.len(), "{name}: rows");
            for (row, python) in rows.iter().zip(want) {
                let Json::Arr(hashed) = row.hashed() else {
                    panic!("a hashed row is a list");
                };
                assert_eq!(hashed, items(python), "{name}: row {}", row.suffix);
            }
        }
        assert!(pattern_table("custom").is_none() && pattern_table("parallel").is_none());
        let counts: Vec<usize> = PATTERN_NAMES
            .iter()
            .map(|name| pattern_table(name).map_or(0, <[PatternRow]>::len))
            .collect();
        assert_eq!(counts, [13, 8, 8, 16, 14, 15], "rows per pattern");
    }

    #[test]
    fn cluster_and_web3_pin_the_risk_of_their_implementer_rows_only() {
        let pinned = |name: &str| -> Vec<&'static str> {
            pattern_table(name)
                .expect("table")
                .iter()
                .filter(|row| row.risk.is_some())
                .map(|row| row.suffix)
                .collect()
        };
        assert_eq!(pinned("cluster"), ["gitops"]);
        assert_eq!(pinned("web3"), ["sol", "dapp"]);
        for name in ["feature", "hotfix", "dependency", "cloud-native"] {
            assert!(pinned(name).is_empty(), "{name}");
        }
    }

    #[test]
    fn the_budgets_gate_sets_and_lane_tables_are_python_s() {
        let document = doc();
        for (risk, wanted) in fields(constant(&document, "budgets")) {
            assert_eq!(budget_of(&risk), Some(wanted), "budget of {risk}");
        }
        assert_eq!(budget_of("extreme"), None);
        for (risk, wanted) in fields(constant(&document, "gates_by_risk")) {
            assert_eq!(
                gates_by_risk(&risk).map(strings),
                Some(wanted),
                "gates of {risk}"
            );
        }
        let lane_agents: Vec<&str> = items(constant(&document, "lane_agents"))
            .iter()
            .map(text)
            .collect();
        let defaults = fields(constant(&document, "default_capability"));
        assert_eq!(defaults.len(), lane_agents.len());
        for agent in &lane_agents {
            assert_eq!(
                default_lane_capability(agent).map(jstr).as_ref(),
                obj_get(&defaults, agent),
                "default capability of {agent}"
            );
        }
        for agent in agents()
            .iter()
            .map(|agent| agent.id.as_str())
            .filter(|id| !lane_agents.contains(id))
        {
            assert_eq!(
                default_lane_capability(agent),
                None,
                "{agent} is not a lane agent"
            );
        }
        for (name, wanted) in fields(constant(&document, "gate_rows")) {
            let (suffix, capability, agent, title, lane) = gate_row(&name).expect("a gate row");
            let Json::Arr(parts) = &wanted else {
                panic!("a gate row is a list")
            };
            assert_eq!(
                [jstr(suffix), jstr(capability), jstr(agent), jstr(title)].as_slice(),
                items(&parts[0]),
                "gate row {name}"
            );
            assert_eq!(Json::Obj(lane), parts[1], "lane of {name}");
        }
        assert_eq!(MAX_SLICES as f64, number(constant(&document, "max_slices")));
        assert_eq!(
            BRIEF_EXCERPT_CHARS as f64,
            number(constant(&document, "brief_excerpt_chars"))
        );
        assert_eq!(
            CREATED_AT_STEP,
            number(constant(&document, "created_at_step"))
        );
        assert_eq!(
            JS_SAFE_INT as f64,
            number(constant(&document, "js_safe_int"))
        );
        let reserved: Vec<&str> = items(constant(&document, "reserved"))
            .iter()
            .map(text)
            .collect();
        for id in &reserved {
            let slices = [("x", "p/x"), (id, "p/y")].map(|(slice_id, path)| {
                Json::Obj(vec![
                    ("id".to_string(), jstr(slice_id)),
                    ("paths".to_string(), Json::Arr(vec![jstr(path)])),
                    ("agent".to_string(), jstr("A05")),
                ])
            });
            let outcome = normalize_slices(&Json::Arr(slices.to_vec()), &agents());
            assert!(
                matches!(outcome, Err(PlanError::Refused(error)) if error.message().contains("is reserved or repeated")),
                "{id}"
            );
        }
        assert_eq!(reserved.len(), 5);
        let states: Vec<&str> = items(constant(&document, "satisfied_states"))
            .iter()
            .map(text)
            .collect();
        let mut ours = SATISFIED_STATES.to_vec();
        ours.sort_unstable();
        assert_eq!(ours, states);
    }

    fn units() -> Json {
        member(&doc(), "units").clone()
    }

    #[test]
    fn str_isspace_is_python_s_over_every_code_point() {
        let python: Vec<u32> = items(member(&units(), "isspace"))
            .iter()
            .map(|c| number(c) as u32)
            .collect();
        let ours: Vec<u32> = (0..=0x10FFFF_u32)
            .filter(|c| char::from_u32(*c).is_some_and(py_isspace))
            .collect();
        assert_eq!(ours, python);
        for entry in items(member(&units(), "strip")) {
            assert_eq!(
                py_strip(text(member(entry, "text"))),
                text(member(entry, "stripped"))
            );
        }
    }

    #[test]
    fn sha256_is_hashlib_s() {
        for entry in items(member(&units(), "sha256")) {
            let hex = text(member(entry, "hex"));
            let bytes: Vec<u8> = (0..hex.len())
                .step_by(2)
                .map(|at| u8::from_str_radix(&hex[at..at + 2], 16).expect("hex"))
                .collect();
            assert_eq!(
                sha256_hex(&bytes),
                text(member(entry, "digest")),
                "{} bytes",
                bytes.len()
            );
        }
    }

    #[test]
    fn repr_is_python_s_for_ascii_and_declined_for_the_rest() {
        for entry in items(member(&units(), "repr_strings")) {
            let source = text(member(entry, "text"));
            let want = Some(text(member(entry, "repr"))).filter(|repr| repr.is_ascii());
            assert_eq!(repr_str(source).as_deref(), want, "repr of {source:?}");
        }
        for entry in items(member(&units(), "repr_values")) {
            let value =
                parse_python_json(text(member(entry, "json"))).expect("a representable value");
            let want = Some(text(member(entry, "repr"))).filter(|repr| repr.is_ascii());
            assert_eq!(
                repr_json(&value).as_deref(),
                want,
                "repr of {}",
                text(member(entry, "json"))
            );
        }
    }

    #[test]
    fn equality_is_python_s() {
        for entry in items(member(&units(), "eq")) {
            let left = parse_python_json(text(member(entry, "left"))).expect("json");
            let right = parse_python_json(text(member(entry, "right"))).expect("json");
            assert_eq!(
                py_eq(&left, &right),
                flag(member(entry, "equal")),
                "{} == {}",
                text(member(entry, "left")),
                text(member(entry, "right"))
            );
            assert_eq!(
                py_eq(&right, &left),
                flag(member(entry, "equal")),
                "symmetric"
            );
        }
    }

    #[test]
    fn the_rework_suffix_pattern_is_python_s_or_declined_for_a_unicode_digit() {
        let mut answered = 0;
        for entry in items(member(&units(), "rework_suffix")) {
            let id = text(member(entry, "id"));
            match ends_in_rework_suffix(id) {
                Ok(matches) => {
                    assert_eq!(matches, flag(member(entry, "matches")), "{id:?}");
                    answered += 1;
                }
                Err(PlanError::NotStarted(why)) => {
                    assert!(
                        why.starts_with("unicode-digit:")
                            && id.chars().any(|ch| !ch.is_ascii() && ch.is_numeric()),
                        "{id:?}: {why}"
                    );
                }
                Err(other) => panic!("{id:?}: {other:?}"),
            }
        }
        assert!(answered >= 20, "only {answered} ids are answered");
    }

    #[test]
    fn the_blast_radii_fence_is_findall_of_the_python_regex() {
        for entry in items(member(&units(), "fence")) {
            let brief = text(member(entry, "brief"));
            let want: Vec<&str> = items(member(entry, "blocks")).iter().map(text).collect();
            assert_eq!(blast_radii_blocks(brief), want, "{brief:?}");
        }
    }

    #[test]
    fn graph_ids_and_derived_prefixes_are_python_s() {
        for entry in items(member(&units(), "graph_id")) {
            assert_eq!(
                is_graph_id(text(member(entry, "text"))),
                flag(member(entry, "valid")),
                "{:?}",
                text(member(entry, "text"))
            );
        }
        for entry in items(member(&units(), "derived_prefix")) {
            let corr = text(member(entry, "corr"));
            assert_eq!(
                derived_prefix(corr, 4),
                text(member(entry, "four")),
                "{corr:?}"
            );
            assert_eq!(
                derived_prefix(corr, 6),
                text(member(entry, "six")),
                "{corr:?}"
            );
        }
    }

    #[test]
    fn candidate_prefixes_follow_the_explicit_prefix_or_the_two_derived_ones() {
        assert_eq!(candidate_prefixes("abcd1234-ffff", Some("PX")), ["PX"]);
        assert_eq!(candidate_prefixes("abcd1234-ffff", Some("")), [""]);
        assert_eq!(
            candidate_prefixes("abcd1234-ffff", None),
            ["Tabcd", "Tabcd12"]
        );
    }

    #[test]
    fn a_plan_creates_each_task_then_validates_and_plans_it() {
        let case = case_named("pattern-hotfix-medium");
        let derived = derive(&case).expect("a plan");
        assert_eq!(derived.ops.len(), 3 * 8);
        for (task, chunk) in (0..8).zip(derived.ops.chunks(3)) {
            let [PlanOp::Create(created), PlanOp::Transition(validate), PlanOp::Transition(plan)] =
                chunk
            else {
                panic!("task {task}: a create and two transitions: {chunk:?}");
            };
            assert_eq!((validate.from, validate.to), ("CREATED", "VALIDATED"));
            assert_eq!((plan.from, plan.to), ("VALIDATED", "PLANNED"));
            assert_eq!(validate.task_id, created.task_id);
            assert_eq!(plan.task_id, created.task_id);
            assert_eq!(validate.reason, "brief validated by A01");
            assert_eq!(plan.reason, "pattern=hotfix");
            assert_eq!((validate.actor, plan.actor), ("A01", "A01"));
        }
    }

    #[test]
    fn created_at_steps_by_the_row_index_and_depth_follows_dependencies() {
        let case = case_named("pattern-feature-medium");
        let derived = derive(&case).expect("a plan");
        let tasks: Vec<&NewTask> = derived
            .ops
            .iter()
            .filter_map(|op| match op {
                PlanOp::Create(task) => Some(task.as_ref()),
                PlanOp::Transition(_) => None,
            })
            .collect();
        for (index, task) in tasks.iter().enumerate() {
            assert_eq!(
                task.created_at,
                case.now + index as f64 * CREATED_AT_STEP,
                "{}",
                task.task_id
            );
        }
        let depth = |suffix: &str| {
            tasks
                .iter()
                .find(|task| task.task_id.ends_with(&format!("-{suffix}")))
                .map(|task| task.dag_depth)
        };
        assert_eq!(
            ["req", "arch", "ux", "data", "be", "fe", "qa", "build", "rel", "obs"].map(depth),
            [0, 1, 1, 2, 3, 2, 4, 5, 6, 7].map(Some)
        );
    }

    #[test]
    fn a_plan_makes_no_decision_about_files_it_cannot_read_before_python_would() {
        let mut case = case_named("custom-valid-single");
        case.request.plan_file = Some(FileRead::Unreadable);
        case.request.graph_id = Some("not a graph id".to_string());
        let Err(PlanError::Refused(error)) = derive(&case) else {
            panic!("the graph id is checked before any file is read");
        };
        assert!(error.message().contains("invalid --graph-id"));
        case.request.graph_id = None;
        assert!(
            matches!(derive(&case), Err(PlanError::NotStarted(why)) if why.starts_with("unreadable-file:"))
        );
    }

    #[test]
    fn a_roster_is_not_needed_for_a_pattern_and_a_pattern_needs_no_files() {
        let mut case = case_named("pattern-dependency-low");
        case.facts.agents.clear();
        case.facts.plan_files.clear();
        let derived = derive(&case).expect("a plan");
        assert_eq!(derived.outcome, Outcome::Created);
        assert_eq!(derived.ops.len(), 24);
    }

    #[test]
    fn the_host_s_fresh_correlation_is_used_only_when_the_request_names_none() {
        let mut case = case_named("pattern-feature-low");
        let fresh = derive(&case).expect("a plan");
        assert_eq!(
            fresh.ctx_correlation_id.as_deref(),
            Some(case.facts.new_correlation_id.as_str())
        );
        case.request.correlation_id = Some("named-by-the-caller".to_string());
        let named = derive(&case).expect("a plan");
        assert_eq!(
            named.ctx_correlation_id.as_deref(),
            Some("named-by-the-caller")
        );
        let PlanOp::Create(first) = &named.ops[0] else {
            panic!("a create first")
        };
        assert_eq!(first.correlation_id, "named-by-the-caller");
        assert!(first.task_id.starts_with("Tname-"), "{}", first.task_id);
    }

    #[test]
    fn an_empty_prefix_is_a_prefix_for_a_creation_and_not_for_a_dry_run() {
        let mut case = case_named("prefix-explicit-empty");
        let derived = derive(&case).expect("a plan");
        let PlanOp::Create(first) = &derived.ops[0] else {
            panic!("a create first")
        };
        assert_eq!(first.task_id, "-req");
        case.request.dry_run = true;
        let dry = derive(&case).expect("a dry run");
        assert!(
            matches!(member_of(&dry.reply, "tasks"), Json::Arr(tasks) if matches!(&tasks[0], Json::Obj(f) if obj_get(f, "task_id") == Some(&jstr("Tc0ff-req"))))
        );
    }
}
