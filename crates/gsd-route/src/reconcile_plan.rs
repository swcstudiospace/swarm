//! Pure planner of `reconcile` (`swarm/results.py`) for one correlation, as an ordered list of store operations.
//!
//! [`plan_reconcile`] reads the facts of one correlation (its task rows, the verdict rows with the host's signature answer for each, the
//! states of tasks outside it that an envelope may name, and the key policy) and returns what Python's `reconcile(store, corr, emit,
//! approve=...)` would do: the transitions, the gate reruns it creates, the dependency edits, the `notes` merges and the `emit` calls, in
//! Python's order, plus the log lines it returns. It opens no database and reads no clock, environment or key (`now` is a parameter).
//!
//! # Why it simulates
//!
//! Python re-reads the store between writes. A rework creates tasks that the next IN_REVIEW target then sees as live gate tasks; a gate task
//! that was approved earlier in the loop no longer covers a later target; `store.update` of `depends_on` is visible to the next target's
//! lineage test but not to the `_ancestors` walk, which works on the list read after the reruns. The planner therefore keeps an in-memory
//! copy of the correlation, applies each of its own operations to it as it emits them, and reads every later decision from that copy.
//! The operations are valid in the order given: each [`ReconcileOp::Transition`] carries the plan of [`decide_transition`] for the row as
//! the earlier operations left it, so the host can apply the list in one transaction with the same conditional `UPDATE` guards.
//!
//! The decisions come from the committed pure modules: [`decide_transition`] for every state change (CHANGES_REQUESTED stash and cap,
//! IN_PROGRESS, VALIDATED, PLANNED, APPROVED, DONE) and [`crate::gate_facts`] for the gate facts (`latest_verdicts(verified_only=True)`,
//! `missing_gate_reasons`, `unaccepted_gates`, `required_gates`). Rust never verifies a signature: `sig_ok` arrives with each verdict row.
//!
//! # What the list means
//!
//! * [`ReconcileOp::CreateTask`] is `TaskStore.create`: the host inserts the row (state `CREATED`, `attempt` and `rework_loops` 0,
//!   `max_attempts` 3, empty `inputs` and `outputs`, no parent, no assignee, `created_at` and `updated_at` = `now`) and its audit row
//!   `NULL -> CREATED` by `system`, `created`. The two audited transitions that follow are separate [`ReconcileOp::Transition`]s.
//! * [`ReconcileOp::SetDependsOn`] is `store.update(task, depends_on=...)`; [`ReconcileOp::SetNotes`] is `store.set_notes(task, key=value)`
//!   (a dict update: an existing key keeps its position, a new one is appended). Both set `updated_at = now`.
//! * [`ReconcileOp::Emit`] is a call of the `emit` callback; the host replays it after the commit, in list order among the other emits.
//!
//! # What Python answers and Rust refuses
//!
//! Everything this planner cannot prove byte-identical returns [`NotStarted`] before anything is written, and Python answers:
//!
//! * the `leases` and `handoffs` bridges (the runner's call) are not planned at all; this is the `leases=None, handoffs=None` call of ingest;
//! * an APPROVED that the key policy would refuse. Python raises `E-POLICY` in the middle of the loop with the earlier tasks already
//!   committed, a partial result that a single transaction cannot reproduce; the same for a missing [`KeyPolicy`];
//! * a gate name or `"gate:reason"` text whose `repr` is not plain ASCII (printable, no quote, no backslash);
//! * rows Python would crash on (a `title` that is not text, a `notes.gate` that is not text, a `notes.gate_for` that is not a list, a rerun
//!   id that already exists anywhere in the store) and rows Rust reads differently: JSON Rust cannot parse, a task id with a newline or a non-ASCII digit
//!   (Python's `\d` and `$` differ from the ASCII rule used here), a rerun index beyond `u64`;
//! * every refusal of [`crate::gate_facts`] ([`Unsupported`]) and of [`decide_transition`].
//!
//! # Clock
//!
//! One `now` stands for every `time.time()` Python reads: `updated_at`, `created_at`, every audit `ts` and `now + 0.001` for
//! `notes.verdicts_since` (from [`decide_transition`]). The new rows sort after the old rows of equal `(dag_depth, created_at)`, as `rowid`
//! order does in SQLite.

use std::fmt;

use crate::gate_facts::{
    missing_gate_reasons, unaccepted_gates, verified_latest_rows, IssuerState, TaskFacts,
    Unsupported, VerdictFacts,
};
use crate::pydumps::to_python_dumps;
use crate::transition_plan::{decide_transition, ApprovalFacts, Facts, Request, TransitionPlan};
use crate::{jstr, obj_get, parse_json, Fields, Json};

/// The states whose latest gate-lineage member gets a rerun (`results.py` lines 410-411).
const RERUN_STATES: [&str; 5] = ["DONE", "IN_REVIEW", "APPROVED", "CLAIMED", "IN_PROGRESS"];

/// Gate task states that no longer cover their targets (`_stalled_gates`).
const FINISHED_STATES: [&str; 3] = ["DONE", "CANCELLED", "ESCALATED"];

/// The suffix `TaskStore` rerun ids carry: `<base>.r<N>`.
const RERUN_MARK: &str = ".r";

/// Title suffix of a rerun task.
const RERUN_TITLE_SUFFIX: &str = " (rerun)";

/// The `budget` `TaskStore.create` stores when the given one is empty.
fn default_budget() -> Json {
    Json::Obj(vec![
        ("max_tokens".to_string(), Json::Int(200_000)),
        ("max_wall_s".to_string(), Json::Int(1800)),
        ("max_cost_usd".to_string(), Json::Int(5)),
    ])
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

/// The host's key policy, the three facts `TaskStore.transition` reads for an APPROVED target.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyPolicy {
    /// `SWARM_REQUIRE_KEY == "1"` in the host's environment.
    pub require_key: bool,
    /// `real_key_configured()`: a non-dev signing key is configured.
    pub real_key: bool,
    /// `insecure_dev_key() is not None`.
    pub dev_key: bool,
}

/// Everything the host reads before it asks for a plan.
#[derive(Debug, Clone)]
pub struct ReconcileFacts {
    /// The correlation to reconcile; never empty (Python lists every correlation for an empty one).
    pub correlation_id: String,
    /// Every `tasks` row of the correlation as `dict(sqlite3.Row)` (JSON columns still text), in `rowid` order. The planner sorts them
    /// like `ORDER BY dag_depth, created_at`; rows with an equal key keep the order given, as SQLite's scan does.
    pub tasks: Vec<Fields>,
    /// The `verdicts` rows of those tasks, each with the host's signature answer.
    pub verdicts: Vec<VerdictFacts>,
    /// The id and state of every task OUTSIDE the correlation: a verdict envelope may name one as its gate task, and a rerun id must not
    /// take an id the store already holds (the `tasks` primary key spans correlations).
    pub other_tasks: Vec<IssuerState>,
    /// The key policy; `None` when the host cannot supply it, which refuses a plan that would approve.
    pub key_policy: Option<KeyPolicy>,
}

/// A refusal: nothing was decided or written, the route reports `write_state = "not_started"` and Python answers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotStarted(pub String);

impl fmt::Display for NotStarted {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "reconcile not started: {}", self.0)
    }
}

impl std::error::Error for NotStarted {}

impl From<Unsupported> for NotStarted {
    fn from(unsupported: Unsupported) -> Self {
        Self(unsupported.to_string())
    }
}

/// The new task of a gate rerun, as the arguments of `TaskStore.create` (defaults already applied).
#[derive(Debug, Clone, PartialEq)]
pub struct NewTask {
    /// `<base>.r<N>`.
    pub task_id: String,
    /// The correlation.
    pub correlation_id: String,
    /// `capability` of the cloned task.
    pub capability: Json,
    /// The cloned title without a trailing `" (rerun)"`, plus `" (rerun)"`.
    pub title: String,
    /// `agent_id` of the cloned task (`null` stays `null`).
    pub agent_id: Json,
    /// `dag_depth` of the cloned task.
    pub dag_depth: Option<i64>,
    /// `depends_on` of the cloned task, as it was when the rerun was created.
    pub depends_on: Vec<String>,
    /// `acceptance`, `[]` when the clone's is empty (`acceptance or []`).
    pub acceptance: Json,
    /// `budget`, the default budget when the clone's is empty (`budget or {...}`).
    pub budget: Json,
    /// `risk_class` of the cloned task.
    pub risk_class: Json,
    /// `priority` of the cloned task.
    pub priority: Json,
    /// `notes` of the cloned task without its `result` and `meta` keys.
    pub notes: Fields,
}

/// One `emit(event_type, payload[, task_id=...])` call.
#[derive(Debug, Clone, PartialEq)]
pub struct EmitEvent {
    /// The event type.
    pub event_type: String,
    /// The payload object.
    pub payload: Fields,
    /// The `task_id=` keyword, when the call passes one (only `task.approval.skipped` does).
    pub task_id: Option<String>,
}

/// One step of the plan.
#[derive(Debug, Clone, PartialEq)]
pub enum ReconcileOp {
    /// `store.transition(task, ...)`: the plan of [`decide_transition`] for the row as the earlier operations left it.
    Transition {
        /// The task.
        task_id: String,
        /// The conditional `UPDATE`, its audit rows and effects.
        plan: TransitionPlan,
    },
    /// `store.create(...)` of a gate rerun.
    CreateTask(NewTask),
    /// `store.update(task, depends_on=...)`: the whole new list.
    SetDependsOn {
        /// The task.
        task_id: String,
        /// The new `depends_on`.
        depends_on: Vec<String>,
    },
    /// `store.set_notes(task, key=value)`.
    SetNotes {
        /// The task.
        task_id: String,
        /// The notes key.
        key: String,
        /// The value.
        value: Json,
    },
    /// An `emit` call.
    Emit(EmitEvent),
}

/// What `reconcile` does and returns.
#[derive(Debug, Clone, PartialEq)]
pub struct ReconcilePlan {
    /// The operations in Python's order.
    pub ops: Vec<ReconcileOp>,
    /// The list `reconcile` returns (`notes_log`).
    pub notes_log: Vec<String>,
}

/// One task of the simulated correlation.
#[derive(Debug, Clone)]
struct SimTask {
    task_id: String,
    state: String,
    dag_depth: Option<i64>,
    created_at: f64,
    capability: Json,
    title: Json,
    agent_id: Json,
    risk_class: Json,
    priority: Json,
    acceptance: Json,
    budget: Json,
    depends_on: Vec<String>,
    notes: Fields,
    rework_loops: Json,
    attempt: Json,
    max_attempts: Json,
    /// `notes.gate` (`null` and absent are `None`; it never changes during a reconcile).
    gate: Option<String>,
    /// The task ids in `notes.gate_for` (never changes during a reconcile).
    gate_for: Vec<String>,
}

/// A rerun created in this rework: its gate and the ids of its lineage's members.
#[derive(Debug)]
struct Created {
    task_id: String,
    gate: Option<String>,
    members: Vec<String>,
}

static NULL: Json = Json::Null;

fn column<'a>(row: &'a Fields, name: &str) -> &'a Json {
    obj_get(row, name).unwrap_or(&NULL)
}

fn text_column(row: &Fields, name: &str) -> Result<String, NotStarted> {
    match column(row, name) {
        Json::Str(text) => Ok(text.clone()),
        _ => Err(NotStarted(format!("{name} is not text"))),
    }
}

/// `json.loads(value or default)` of a JSON text column.
fn json_column(row: &Fields, name: &str, default: Json) -> Result<Json, NotStarted> {
    match column(row, name) {
        Json::Null => Ok(default),
        Json::Str(text) if text.is_empty() => Ok(default),
        Json::Str(text) => parse_json(text)
            .map_err(|_| NotStarted(format!("{name} is not JSON that Rust can read"))),
        _ => Err(NotStarted(format!("{name} is not text"))),
    }
}

/// `TaskStore._row`'s `notes_json`; text Rust cannot parse is refused, as Python may read it.
fn notes_column(row: &Fields) -> Result<Fields, NotStarted> {
    match json_column(row, "notes", Json::Obj(Vec::new()))? {
        Json::Obj(fields) => Ok(fields),
        _ => Ok(Vec::new()),
    }
}

fn string_list(value: &Json, what: &str) -> Result<Vec<String>, NotStarted> {
    let Json::Arr(items) = value else {
        return Err(NotStarted(format!("{what} is not a list")));
    };
    items
        .iter()
        .map(|item| match item {
            Json::Str(text) => Ok(text.clone()),
            _ => Err(NotStarted(format!("{what} holds a non-string"))),
        })
        .collect()
}

/// A task id Python's `\d` and `$` read the way this planner does: no newline, no non-ASCII digit.
fn plain_id(task_id: &str) -> bool {
    !task_id.contains('\n') && !task_id.chars().any(|ch| !ch.is_ascii() && ch.is_numeric())
}

impl SimTask {
    fn load(row: &Fields, correlation_id: &str) -> Result<Self, NotStarted> {
        let task_id = text_column(row, "task_id")?;
        if !plain_id(&task_id) {
            return Err(NotStarted(
                "a task id with a newline or a non-ASCII digit".to_string(),
            ));
        }
        if text_column(row, "correlation_id")? != correlation_id {
            return Err(NotStarted("a row of another correlation".to_string()));
        }
        let dag_depth = match column(row, "dag_depth") {
            Json::Null => None,
            Json::Int(depth) => Some(*depth),
            _ => return Err(NotStarted("dag_depth is not an integer".to_string())),
        };
        let created_at = match column(row, "created_at") {
            Json::Int(stamp) => *stamp as f64,
            Json::Float(stamp) => *stamp,
            _ => return Err(NotStarted("created_at is not a number".to_string())),
        };
        let notes = notes_column(row)?;
        let gate = match obj_get(&notes, "gate") {
            None | Some(Json::Null) => None,
            Some(Json::Str(gate)) => Some(gate.clone()),
            Some(_) => return Err(NotStarted("notes.gate is not text".to_string())),
        };
        let gate_for = match obj_get(&notes, "gate_for") {
            None => Vec::new(),
            Some(Json::Arr(items)) => items
                .iter()
                .filter_map(|item| match item {
                    Json::Str(id) => Some(id.clone()),
                    _ => None,
                })
                .collect(),
            Some(_) => return Err(NotStarted("notes.gate_for is not a list".to_string())),
        };
        Ok(Self {
            state: text_column(row, "state")?,
            dag_depth,
            created_at,
            capability: column(row, "capability").clone(),
            title: column(row, "title").clone(),
            agent_id: column(row, "agent_id").clone(),
            risk_class: column(row, "risk_class").clone(),
            priority: column(row, "priority").clone(),
            acceptance: json_column(row, "acceptance", Json::Arr(Vec::new()))?,
            budget: json_column(row, "budget", Json::Obj(Vec::new()))?,
            depends_on: string_list(
                &json_column(row, "depends_on", Json::Arr(Vec::new()))?,
                "depends_on",
            )?,
            notes,
            rework_loops: column(row, "rework_loops").clone(),
            attempt: column(row, "attempt").clone(),
            max_attempts: column(row, "max_attempts").clone(),
            gate,
            gate_for,
            task_id,
        })
    }

    /// The row as `decide_transition` reads it: the columns of the decision, with `notes` as text.
    fn decision_row(&self) -> Fields {
        vec![
            ("task_id".to_string(), jstr(&self.task_id)),
            ("state".to_string(), jstr(&self.state)),
            ("rework_loops".to_string(), self.rework_loops.clone()),
            ("attempt".to_string(), self.attempt.clone()),
            ("max_attempts".to_string(), self.max_attempts.clone()),
            (
                "notes".to_string(),
                Json::Str(to_python_dumps(&Json::Obj(self.notes.clone()))),
            ),
        ]
    }

    /// `(dag_depth, created_at)`, the sort key of `TaskStore.list`.
    fn order_key(&self) -> (Option<i64>, f64) {
        (self.dag_depth, self.created_at)
    }
}

/// `RERUN_SUFFIX.sub("", task_id)`: the id without its trailing `.r<digits>` groups.
fn lineage_base(task_id: &str) -> &str {
    let mut base = task_id;
    while let Some(at) = base.rfind(RERUN_MARK) {
        let digits = &base[at + RERUN_MARK.len()..];
        if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
            break;
        }
        base = &base[..at];
    }
    base
}

/// `_rerun_index`: `N` for `<base>.rN`, 0 for the original gate task.
fn rerun_index(task_id: &str) -> Result<u64, NotStarted> {
    let Some(at) = task_id.rfind(RERUN_MARK) else {
        return Ok(0);
    };
    let digits = &task_id[at + RERUN_MARK.len()..];
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Ok(0);
    }
    digits
        .parse::<u64>()
        .map_err(|_| NotStarted("a rerun index beyond u64".to_string()))
}

/// Python's `repr` of a list of plain strings, `['a', 'b']`.
fn py_repr_list(names: &[String]) -> Result<String, NotStarted> {
    let plain = |name: &String| {
        name.chars()
            .all(|ch| (' '..='~').contains(&ch) && ch != '\'' && ch != '\\')
    };
    if !names.iter().all(plain) {
        return Err(NotStarted("a name needs an escaped repr".to_string()));
    }
    let quoted: Vec<String> = names.iter().map(|name| format!("'{name}'")).collect();
    Ok(format!("[{}]", quoted.join(", ")))
}

/// The in-memory copy of the correlation plus the plan built so far.
struct Sim<'a> {
    correlation_id: &'a str,
    now: f64,
    verdicts: &'a [VerdictFacts],
    other_tasks: &'a [IssuerState],
    key_policy: Option<&'a KeyPolicy>,
    /// `TaskStore.list(correlation_id=...)` order.
    tasks: Vec<SimTask>,
    ops: Vec<ReconcileOp>,
    notes_log: Vec<String>,
}

impl<'a> Sim<'a> {
    fn load(facts: &'a ReconcileFacts, now: f64) -> Result<Self, NotStarted> {
        if facts.correlation_id.is_empty() {
            return Err(NotStarted("an empty correlation id".to_string()));
        }
        let mut tasks = facts
            .tasks
            .iter()
            .map(|row| SimTask::load(row, &facts.correlation_id))
            .collect::<Result<Vec<_>, _>>()?;
        tasks.sort_by(|a, b| {
            a.order_key()
                .partial_cmp(&b.order_key())
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        Ok(Self {
            correlation_id: &facts.correlation_id,
            now,
            verdicts: &facts.verdicts,
            other_tasks: &facts.other_tasks,
            key_policy: facts.key_policy.as_ref(),
            tasks,
            ops: Vec::new(),
            notes_log: Vec::new(),
        })
    }

    fn find(&self, task_id: &str) -> Result<&SimTask, NotStarted> {
        self.tasks
            .iter()
            .find(|task| task.task_id == task_id)
            .ok_or_else(|| NotStarted(format!("unknown task {task_id}")))
    }

    fn find_mut(&mut self, task_id: &str) -> Result<&mut SimTask, NotStarted> {
        self.tasks
            .iter_mut()
            .find(|task| task.task_id == task_id)
            .ok_or_else(|| NotStarted(format!("unknown task {task_id}")))
    }

    /// `TaskFacts` of a task as the simulation holds it now (its `notes.verdicts_since` moves after a rework).
    fn task_facts(&self, task_id: &str) -> Result<TaskFacts, NotStarted> {
        let task = self.find(task_id)?;
        let risk = match &task.risk_class {
            Json::Str(risk) => Some(risk.as_str()),
            _ => None,
        };
        let notes = to_python_dumps(&Json::Obj(task.notes.clone()));
        Ok(TaskFacts::from_row(
            task_id,
            self.correlation_id,
            risk,
            Some(&notes),
        )?)
    }

    /// The state of every task a verdict may name: the simulated ones first, then the ones outside the correlation.
    fn issuer_states(&self) -> Vec<IssuerState> {
        let own = self.tasks.iter().map(|task| IssuerState {
            task_id: task.task_id.clone(),
            state: task.state.clone(),
        });
        own.chain(self.other_tasks.iter().cloned()).collect()
    }

    /// `store.transition(task, to, reason=...)` by `A01`: decides on the simulated row, applies the plan to it and records the operation.
    fn transition(
        &mut self,
        task_id: &str,
        to: &str,
        reason: &str,
    ) -> Result<TransitionPlan, NotStarted> {
        let facts = Facts {
            task: Some(self.find(task_id)?.decision_row()),
            approval: self.key_policy.map(|policy| ApprovalFacts {
                require_key: policy.require_key,
                real_key: policy.real_key,
                dev_key: policy.dev_key,
                missing_gates: Vec::new(),
                unaccepted_gates: Vec::new(),
            }),
        };
        let request = Request {
            task_id: task_id.to_string(),
            to: to.to_string(),
            actor: "A01".to_string(),
            reason: reason.to_string(),
            manual: false,
        };
        let plan = decide_transition(&facts, &request, self.now)
            .map_err(|refusal| NotStarted(refusal.message))?;
        let task = self.find_mut(task_id)?;
        task.state = plan.to.clone();
        for (name, value) in &plan.columns {
            match (name.as_str(), value) {
                ("attempt", _) => task.attempt = value.clone(),
                ("rework_loops", _) => task.rework_loops = value.clone(),
                ("notes", Json::Obj(notes)) => task.notes = notes.clone(),
                _ => {}
            }
        }
        self.ops.push(ReconcileOp::Transition {
            task_id: task_id.to_string(),
            plan: plan.clone(),
        });
        Ok(plan)
    }

    /// `reconcile`'s body for one IN_REVIEW task.
    fn reconcile_one(&mut self, task_id: &str, approve: bool) -> Result<(), NotStarted> {
        let verdicts = self.verdicts;
        let task = self.task_facts(task_id)?;
        let latest = verified_latest_rows(&task, verdicts, false, self.now)?;
        let failing: Vec<String> = task
            .required_gates
            .iter()
            .filter(|gate| {
                latest.iter().any(|(name, facts)| {
                    *name == gate.as_str() && facts.row.verdict.as_deref() == Some("fail")
                })
            })
            .cloned()
            .collect();
        if !failing.is_empty() {
            return self.rework(task_id, &failing);
        }
        // CR-01: a leased gate task of a required gate for this target holds it.
        let leased = self.tasks.iter().any(|gate_task| {
            gate_task.gate_for.iter().any(|target| target == task_id)
                && matches!(gate_task.state.as_str(), "CLAIMED" | "IN_PROGRESS")
                && gate_task
                    .gate
                    .as_ref()
                    .is_some_and(|gate| task.required_gates.contains(gate))
        });
        if leased {
            return Ok(());
        }
        let missing = missing_gate_reasons(&task, verdicts, self.now)?;
        let unaccepted = unaccepted_gates(&task, verdicts, &missing, &self.issuer_states())?;
        let mut reasons: Vec<(String, String)> = missing
            .iter()
            .map(|(gate, outcome)| (gate.clone(), outcome.as_str().to_string()))
            .collect();
        reasons.extend(
            unaccepted
                .into_iter()
                .map(|gate| (gate, "unaccepted".to_string())),
        );
        if reasons.is_empty() {
            return self.approve(task_id, approve);
        }
        self.stall(task_id, &reasons)
    }

    /// The failing-gate branch: rework or escalation, then the reruns of the gate lineages.
    fn rework(&mut self, task_id: &str, failing: &[String]) -> Result<(), NotStarted> {
        let names = py_repr_list(failing)?;
        let before = match self.find(task_id)?.rework_loops {
            Json::Int(loops) => loops,
            _ => return Err(NotStarted("rework_loops is not an integer".to_string())),
        };
        let plan = self.transition(
            task_id,
            "CHANGES_REQUESTED",
            &format!("gates failed: {names}"),
        )?;
        if plan.to == "ESCALATED" {
            self.ops.push(ReconcileOp::Emit(EmitEvent {
                event_type: "escalation.request".to_string(),
                payload: vec![
                    ("task_id".to_string(), jstr(task_id)),
                    ("reason_code".to_string(), jstr("E-CONTRACT")),
                    (
                        "evidence".to_string(),
                        Json::Arr(failing.iter().map(|gate| jstr(gate)).collect()),
                    ),
                    (
                        "options".to_string(),
                        Json::Arr(
                            ["human review", "cancel", "waive gate (L3)"]
                                .iter()
                                .map(|option| jstr(option))
                                .collect(),
                        ),
                    ),
                ],
                task_id: None,
            }));
            self.notes_log
                .push(format!("{task_id}: ESCALATED after {before} rework loops"));
            return Ok(());
        }
        self.transition(task_id, "IN_PROGRESS", "rework loop")?;
        let Json::Int(loops) = self.find(task_id)?.rework_loops else {
            return Err(NotStarted("rework_loops is not an integer".to_string()));
        };
        self.notes_log.push(format!(
            "{task_id}: CHANGES_REQUESTED → rework #{loops} ({names})"
        ));
        let created = self.rerun_lineages(task_id)?;
        self.wire_dependants(&created)
    }

    /// Reopens the gate tasks that target `task_id`: one rerun per lineage, cloned from its latest member.
    fn rerun_lineages(&mut self, task_id: &str) -> Result<Vec<Created>, NotStarted> {
        let mut lineages: Vec<(String, Vec<String>)> = Vec::new();
        for index in 0..self.tasks.len() {
            let member = &self.tasks[index];
            let targeted = member.gate_for.iter().any(|target| target == task_id)
                || (member.gate.as_deref().is_none_or(str::is_empty)
                    && member.depends_on.iter().any(|dep| dep == task_id)
                    && self.task_facts(&member.task_id)?.required_gates.is_empty());
            if !targeted {
                continue;
            }
            let base = lineage_base(&member.task_id).to_string();
            let id = member.task_id.clone();
            match lineages.iter_mut().find(|(known, _)| *known == base) {
                Some((_, ids)) => ids.push(id),
                None => lineages.push((base, vec![id])),
            }
        }
        let mut created: Vec<Created> = Vec::new();
        for (base, member_ids) in lineages {
            let mut latest: Option<(&str, u64)> = None;
            for id in &member_ids {
                let index = rerun_index(id)?;
                if latest.is_none_or(|(_, best)| index > best) {
                    latest = Some((id.as_str(), index));
                }
            }
            let Some((latest_id, index)) = latest else {
                continue;
            };
            let source = self.find(latest_id)?.clone();
            if !RERUN_STATES.contains(&source.state.as_str()) {
                continue;
            }
            let next = index
                .checked_add(1)
                .ok_or_else(|| NotStarted("a rerun index beyond u64".to_string()))?;
            let new_id = format!("{base}{RERUN_MARK}{next}");
            if self.tasks.iter().any(|task| task.task_id == new_id)
                || self.other_tasks.iter().any(|task| task.task_id == new_id)
            {
                return Err(NotStarted(format!("the rerun id {new_id} exists")));
            }
            self.create_rerun(&new_id, &source)?;
            created.push(Created {
                task_id: new_id,
                gate: source.gate.clone(),
                members: member_ids,
            });
        }
        Ok(created)
    }

    /// `store.create` of one rerun from `source`, then its two audited transitions.
    fn create_rerun(&mut self, new_id: &str, source: &SimTask) -> Result<(), NotStarted> {
        let Json::Str(title) = &source.title else {
            return Err(NotStarted("a gate task title is not text".to_string()));
        };
        let title = format!(
            "{}{RERUN_TITLE_SUFFIX}",
            title.strip_suffix(RERUN_TITLE_SUFFIX).unwrap_or(title)
        );
        let notes: Fields = source
            .notes
            .iter()
            .filter(|(key, _)| key != "result" && key != "meta")
            .cloned()
            .collect();
        let new_task = NewTask {
            task_id: new_id.to_string(),
            correlation_id: self.correlation_id.to_string(),
            capability: source.capability.clone(),
            title,
            agent_id: source.agent_id.clone(),
            dag_depth: source.dag_depth,
            depends_on: source.depends_on.clone(),
            acceptance: if truthy(&source.acceptance) {
                source.acceptance.clone()
            } else {
                Json::Arr(Vec::new())
            },
            budget: if truthy(&source.budget) {
                source.budget.clone()
            } else {
                default_budget()
            },
            risk_class: source.risk_class.clone(),
            priority: source.priority.clone(),
            notes,
        };
        let row = SimTask {
            task_id: new_task.task_id.clone(),
            state: "CREATED".to_string(),
            dag_depth: new_task.dag_depth,
            created_at: self.now,
            capability: new_task.capability.clone(),
            title: jstr(&new_task.title),
            agent_id: new_task.agent_id.clone(),
            risk_class: new_task.risk_class.clone(),
            priority: new_task.priority.clone(),
            acceptance: new_task.acceptance.clone(),
            budget: new_task.budget.clone(),
            depends_on: new_task.depends_on.clone(),
            notes: new_task.notes.clone(),
            rework_loops: Json::Int(0),
            attempt: Json::Int(0),
            max_attempts: Json::Int(3),
            gate: source.gate.clone(),
            gate_for: source.gate_for.clone(),
        };
        let at = self
            .tasks
            .iter()
            .position(|task| task.order_key() > row.order_key())
            .unwrap_or(self.tasks.len());
        self.tasks.insert(at, row);
        self.ops.push(ReconcileOp::CreateTask(new_task));
        self.transition(new_id, "VALIDATED", "")?;
        self.transition(new_id, "PLANNED", "gate rerun")?;
        Ok(())
    }

    /// Once every rerun of this rework exists: anything downstream of a lineage member waits for the lineage's rerun; a release-gate
    /// rerun also waits for the other gate reruns and for the scan reruns upstream of it.
    fn wire_dependants(&mut self, created: &[Created]) -> Result<(), NotStarted> {
        // `rows = store.list(...)` is read once: `_ancestors` sees the `depends_on` of this snapshot, not the edits made below.
        let rows: Vec<(String, String, Vec<String>)> = self
            .tasks
            .iter()
            .map(|task| {
                (
                    task.task_id.clone(),
                    task.state.clone(),
                    task.depends_on.clone(),
                )
            })
            .collect();
        for (task_id, state, depends_on) in &rows {
            if state == "DONE" {
                continue;
            }
            let release_rerun = created
                .iter()
                .any(|rerun| rerun.task_id == *task_id && rerun.gate.as_deref() == Some("release"));
            let upstream = if release_rerun {
                ancestors(depends_on, &rows)
            } else {
                Vec::new()
            };
            let waits_for = |rerun: &Created| {
                let after_member = rerun
                    .members
                    .iter()
                    .any(|member| depends_on.contains(member));
                let release_waits = release_rerun
                    && (rerun
                        .gate
                        .as_deref()
                        .is_some_and(|gate| !gate.is_empty() && gate != "release")
                        || upstream.iter().any(|id| id == lineage_base(&rerun.task_id)));
                rerun.task_id != *task_id
                    && !depends_on.contains(&rerun.task_id)
                    && (after_member || release_waits)
            };
            let extra: Vec<String> = created
                .iter()
                .filter(|rerun| waits_for(rerun))
                .map(|rerun| rerun.task_id.clone())
                .collect();
            if extra.is_empty() {
                continue;
            }
            let mut merged = depends_on.clone();
            merged.extend(extra);
            self.find_mut(task_id)?.depends_on = merged.clone();
            self.ops.push(ReconcileOp::SetDependsOn {
                task_id: task_id.clone(),
                depends_on: merged,
            });
        }
        Ok(())
    }

    /// No required gate is missing: approve, or skip under `--advisory`.
    fn approve(&mut self, task_id: &str, approve: bool) -> Result<(), NotStarted> {
        if !approve {
            self.ops.push(ReconcileOp::Emit(EmitEvent {
                event_type: "task.approval.skipped".to_string(),
                payload: vec![
                    ("task_id".to_string(), jstr(task_id)),
                    (
                        "reason".to_string(),
                        jstr("advisory ingest: approval not attempted"),
                    ),
                ],
                task_id: Some(task_id.to_string()),
            }));
            self.notes_log
                .push(format!("{task_id}: advisory, approval not attempted"));
            return Ok(());
        }
        self.transition(task_id, "APPROVED", "all required gates pass")?;
        self.transition(task_id, "DONE", "approved")?;
        self.notes_log.push(format!("{task_id}: DONE"));
        Ok(())
    }

    /// WR-08: gates missing for a reason other than `fail` that no live gate task covers escalate once per distinct stall.
    fn stall(&mut self, task_id: &str, reasons: &[(String, String)]) -> Result<(), NotStarted> {
        let live: Vec<Option<&str>> = self
            .tasks
            .iter()
            .filter(|gate_task| {
                gate_task.gate_for.iter().any(|target| target == task_id)
                    && !FINISHED_STATES.contains(&gate_task.state.as_str())
            })
            .map(|gate_task| gate_task.gate.as_deref())
            .collect();
        let mut stall: Vec<String> = reasons
            .iter()
            .filter(|(gate, reason)| reason != "fail" && !live.contains(&Some(gate.as_str())))
            .map(|(gate, reason)| format!("{gate}:{reason}"))
            .collect();
        stall.sort();
        let recorded = obj_get(&self.find(task_id)?.notes, "gate_stall").unwrap_or(&NULL);
        let unchanged = match recorded {
            Json::Arr(items) => {
                items.len() == stall.len()
                    && items
                        .iter()
                        .zip(&stall)
                        .all(|(item, entry)| matches!(item, Json::Str(text) if text == entry))
            }
            other => !truthy(other) && stall.is_empty(),
        };
        if unchanged {
            return Ok(());
        }
        let names = py_repr_list(&stall)?;
        let value = Json::Arr(stall.iter().map(|entry| jstr(entry)).collect());
        let task = self.find_mut(task_id)?;
        match task.notes.iter_mut().find(|(key, _)| key == "gate_stall") {
            Some(entry) => entry.1 = value.clone(),
            None => task.notes.push(("gate_stall".to_string(), value.clone())),
        }
        self.ops.push(ReconcileOp::SetNotes {
            task_id: task_id.to_string(),
            key: "gate_stall".to_string(),
            value: value.clone(),
        });
        if stall.is_empty() {
            return Ok(());
        }
        self.ops.push(ReconcileOp::Emit(EmitEvent {
            event_type: "escalation.request".to_string(),
            payload: vec![
                ("task_id".to_string(), jstr(task_id)),
                ("reason_code".to_string(), jstr("E-CONTRACT")),
                ("evidence".to_string(), value),
                (
                    "options".to_string(),
                    Json::Arr(
                        ["re-run gate", "human review", "cancel"]
                            .iter()
                            .map(|option| jstr(option))
                            .collect(),
                    ),
                ),
            ],
            task_id: None,
        }));
        self.notes_log.push(format!(
            "{task_id}: gate stall {names} → escalation.request"
        ));
        Ok(())
    }
}

/// `_ancestors`: every id `depends_on` reaches, directly or through other rows of the snapshot.
fn ancestors(depends_on: &[String], rows: &[(String, String, Vec<String>)]) -> Vec<String> {
    let mut seen: Vec<String> = Vec::new();
    let mut frontier: Vec<String> = depends_on.to_vec();
    while let Some(dep) = frontier.pop() {
        if seen.contains(&dep) {
            continue;
        }
        let Some((_, _, next)) = rows.iter().find(|(id, _, _)| *id == dep) else {
            continue;
        };
        seen.push(dep);
        frontier.extend(next.iter().cloned());
    }
    seen
}

/// Plans `reconcile(store, correlation_id, emit, approve=approve)` without the lease and handoff bridges.
///
/// Every IN_REVIEW task of the correlation is visited once, in `(dag_depth, created_at)` order, as Python's loop does: a failing required
/// gate sends it to rework (CHANGES_REQUESTED, then IN_PROGRESS) or, past `MAX_REWORK_LOOPS`, to ESCALATED with an `escalation.request`,
/// and a rework recreates one rerun per gate lineage; a task with every gate passing and accepted is approved (APPROVED, then DONE), or
/// only reported under `approve == false`; a task held by a leased gate task is left alone; any other missing gate that no live gate task
/// covers is recorded in `notes.gate_stall` with an `escalation.request`.
///
/// # Errors
///
/// [`NotStarted`] when Python must answer: see the module documentation for the list.
pub fn plan_reconcile(
    facts: &ReconcileFacts,
    approve: bool,
    now: f64,
) -> Result<ReconcilePlan, NotStarted> {
    let mut sim = Sim::load(facts, now)?;
    let queue: Vec<String> = sim
        .tasks
        .iter()
        .filter(|task| task.state == "IN_REVIEW")
        .map(|task| task.task_id.clone())
        .collect();
    for task_id in &queue {
        sim.reconcile_one(task_id, approve)?;
    }
    Ok(ReconcilePlan {
        ops: sim.ops,
        notes_log: sim.notes_log,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gate_facts::VerdictRow;

    const VECTORS: &str = include_str!("../tests/vectors/reconcile.json");

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

    fn integer(value: &Json) -> i64 {
        match value {
            Json::Int(number) => *number,
            other => panic!("not an integer: {other:?}"),
        }
    }

    fn set(row: &mut Fields, key: &str, value: Json) {
        match row.iter_mut().find(|(name, _)| name == key) {
            Some(entry) => entry.1 = value,
            None => row.push((key.to_string(), value)),
        }
    }

    /// One recorded case: the facts Python ran on and the answer it gave.
    struct Case {
        name: String,
        correlation_id: String,
        approve: bool,
        now: f64,
        key_policy: Option<KeyPolicy>,
        refused: bool,
        before: Vec<Fields>,
        verdicts: Vec<VerdictFacts>,
        max_transition_id: i64,
        expect: Json,
    }

    impl Case {
        fn parse(value: &Json) -> Self {
            let policy = member(value, "key_policy");
            let expect = member(value, "expect").clone();
            let refused = text(member(value, "rust")) == "not_started"
                || matches!(&expect, Json::Obj(f) if obj_get(f, "python_error").is_some());
            Self {
                name: text(member(value, "name")).to_string(),
                correlation_id: text(member(value, "correlation_id")).to_string(),
                approve: flag(member(value, "approve")),
                now: number(member(value, "now")),
                key_policy: Some(KeyPolicy {
                    require_key: flag(member(policy, "require_key")),
                    real_key: flag(member(policy, "real_key")),
                    dev_key: flag(member(policy, "dev_key")),
                }),
                refused,
                before: items(member(value, "tasks_before"))
                    .iter()
                    .map(fields)
                    .collect(),
                verdicts: items(member(value, "verdicts"))
                    .iter()
                    .map(|row| {
                        let stored = |key: &str| match member(row, key) {
                            Json::Null => None,
                            other => Some(text(other).to_string()),
                        };
                        VerdictFacts::new(
                            VerdictRow {
                                id: integer(member(row, "id")),
                                task_id: text(member(row, "task_id")).to_string(),
                                gate: text(member(row, "gate")).to_string(),
                                verdict: stored("verdict"),
                                envelope_json: stored("envelope_json"),
                                ts: number(member(row, "ts")),
                            },
                            flag(member(row, "sig_ok")),
                        )
                    })
                    .collect(),
                max_transition_id: integer(member(value, "transitions_before_max")),
                expect,
            }
        }

        /// What the host reads: the rows of the correlation, and the id and state of every other task.
        fn facts(&self) -> ReconcileFacts {
            let (mine, others): (Vec<&Fields>, Vec<&Fields>) = self
                .before
                .iter()
                .partition(|row| text(member_of(row, "correlation_id")) == self.correlation_id);
            ReconcileFacts {
                correlation_id: self.correlation_id.clone(),
                tasks: mine.into_iter().cloned().collect(),
                verdicts: self.verdicts.clone(),
                other_tasks: others
                    .into_iter()
                    .map(|row| IssuerState {
                        task_id: text(member_of(row, "task_id")).to_string(),
                        state: text(member_of(row, "state")).to_string(),
                    })
                    .collect(),
                key_policy: self.key_policy.clone(),
            }
        }
    }

    fn member_of<'a>(row: &'a Fields, key: &str) -> &'a Json {
        obj_get(row, key).unwrap_or_else(|| panic!("no column {key}"))
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

    /// The database as the host leaves it: `tasks` and `transitions` rows with the columns SQLite holds, text for JSON columns.
    struct Store {
        tasks: Vec<Fields>,
        transitions: Vec<Fields>,
        next_id: i64,
        events: Vec<EmitEvent>,
        now: f64,
    }

    impl Store {
        fn row(&mut self, task_id: &str) -> &mut Fields {
            self.tasks
                .iter_mut()
                .find(|row| text(member_of(row, "task_id")) == task_id)
                .unwrap_or_else(|| {
                    panic!("the plan names {task_id}, which does not exist at that point")
                })
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
                ("id".to_string(), Json::Int(self.next_id)),
                ("task_id".to_string(), jstr(task_id)),
                ("from_state".to_string(), from.map_or(Json::Null, jstr)),
                ("to_state".to_string(), jstr(to)),
                ("actor".to_string(), jstr(actor)),
                ("reason".to_string(), jstr(reason)),
                ("ts".to_string(), Json::Float(self.now)),
            ];
            self.next_id += 1;
            self.transitions.push(row);
        }

        /// The write each operation makes, with the guards the host's conditional `UPDATE` and unique key give.
        fn apply(&mut self, op: &ReconcileOp) {
            let now = Json::Float(self.now);
            match op {
                ReconcileOp::Transition { task_id, plan } => {
                    let row = self.row(task_id);
                    assert_eq!(
                        text(member_of(row, "state")),
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
                ReconcileOp::CreateTask(new) => {
                    assert!(
                        !self
                            .tasks
                            .iter()
                            .any(|row| text(member_of(row, "task_id")) == new.task_id),
                        "{} exists already",
                        new.task_id
                    );
                    let depth = new.dag_depth.map_or(Json::Null, Json::Int);
                    let depends = Json::Arr(new.depends_on.iter().map(|id| jstr(id)).collect());
                    self.tasks.push(vec![
                        ("task_id".to_string(), jstr(&new.task_id)),
                        ("correlation_id".to_string(), jstr(&new.correlation_id)),
                        ("parent_id".to_string(), Json::Null),
                        ("dag_depth".to_string(), depth),
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
                ReconcileOp::SetDependsOn {
                    task_id,
                    depends_on,
                } => {
                    let list = Json::Arr(depends_on.iter().map(|id| jstr(id)).collect());
                    let row = self.row(task_id);
                    set(row, "depends_on", Json::Str(to_python_dumps(&list)));
                    set(row, "updated_at", now);
                }
                ReconcileOp::SetNotes {
                    task_id,
                    key,
                    value,
                } => {
                    let row = self.row(task_id);
                    let mut notes = match parse_json(text(member_of(row, "notes"))) {
                        Ok(Json::Obj(notes)) => notes,
                        _ => Vec::new(),
                    };
                    set(&mut notes, key, value.clone());
                    set(row, "notes", Json::Str(to_python_dumps(&Json::Obj(notes))));
                    set(row, "updated_at", now);
                }
                ReconcileOp::Emit(event) => self.events.push(event.clone()),
            }
        }
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

    /// Replays one recorded case: plan, apply the operations to a copy of the facts, and compare everything Python left behind.
    fn replay(case: &Case) {
        let outcome = plan_reconcile(&case.facts(), case.approve, case.now);
        if case.refused {
            assert!(
                outcome.is_err(),
                "{}: Rust must answer not_started, planned {outcome:?}",
                case.name
            );
            return;
        }
        let plan = outcome.unwrap_or_else(|refusal| panic!("{}: {refusal}", case.name));
        let mut store = Store {
            tasks: case.before.clone(),
            transitions: Vec::new(),
            next_id: case.max_transition_id + 1,
            events: Vec::new(),
            now: case.now,
        };
        for op in &plan.ops {
            store.apply(op);
        }
        let want = member(&case.expect, "tasks");
        assert_rows(&case.name, "tasks", &store.tasks, items(want));
        assert_rows(
            &case.name,
            "transitions",
            &store.transitions,
            items(member(&case.expect, "transitions")),
        );
        let wanted_events = items(member(&case.expect, "events"));
        assert_eq!(
            store.events.len(),
            wanted_events.len(),
            "{}: number of events",
            case.name
        );
        for (got, want) in store.events.iter().zip(wanted_events) {
            assert_eq!(
                got.event_type,
                text(member(want, "type")),
                "{}: event type",
                case.name
            );
            assert_eq!(
                Json::Obj(got.payload.clone()),
                *member(want, "payload"),
                "{}: payload of {}",
                case.name,
                got.event_type
            );
            let task_id = match member(want, "task_id") {
                Json::Null => None,
                other => Some(text(other).to_string()),
            };
            assert_eq!(
                got.task_id, task_id,
                "{}: task_id keyword of {}",
                case.name, got.event_type
            );
        }
        let log: Vec<&str> = items(member(&case.expect, "notes_log"))
            .iter()
            .map(text)
            .collect();
        assert_eq!(plan.notes_log, log, "{}: notes_log", case.name);
    }

    #[test]
    fn every_recorded_case_replays_to_the_state_python_left() {
        let all = cases();
        assert!(
            all.len() >= 200,
            "the vector file has only {} cases",
            all.len()
        );
        for case in &all {
            replay(case);
        }
    }

    #[test]
    fn the_cases_cover_every_branch_and_both_kinds_of_refusal() {
        let all = cases();
        let planned: Vec<&Case> = all.iter().filter(|case| !case.refused).collect();
        let mut kinds = [false; 6];
        for case in &planned {
            let log = items(member(&case.expect, "notes_log"))
                .iter()
                .map(text)
                .collect::<Vec<_>>()
                .join("\n");
            kinds[0] |= log.contains("→ rework #");
            kinds[1] |= log.contains("ESCALATED after");
            kinds[2] |= log.contains(": DONE");
            kinds[3] |= log.contains("advisory, approval not attempted");
            kinds[4] |= log.contains("gate stall");
            kinds[5] |= items(member(&case.expect, "tasks")).len() > case.before.len();
        }
        assert!(kinds.iter().all(|seen| *seen), "branches seen: {kinds:?}");
        assert!(all.iter().any(
            |case| matches!(&case.expect, Json::Obj(f) if obj_get(f, "python_error").is_some())
        ));
        assert!(all.iter().any(|case| case.refused
            && !matches!(&case.expect, Json::Obj(f) if obj_get(f, "python_error").is_some())));
    }

    #[test]
    fn the_constants_match_python() {
        let doc = parse_json(VECTORS).expect("the vector file parses");
        let constants = member(&doc, "constants");
        assert_eq!(
            integer(member(constants, "max_rework_loops")),
            crate::transition_plan::MAX_REWORK_LOOPS
        );
        assert_eq!(member(constants, "default_budget"), &default_budget());
        let states: Vec<&str> = items(member(constants, "rerun_states"))
            .iter()
            .map(text)
            .collect();
        assert_eq!(states, RERUN_STATES);
        let all: Vec<&str> = items(member(constants, "states"))
            .iter()
            .map(text)
            .collect();
        assert_eq!(all, crate::transition_plan::STATES);
    }

    #[test]
    fn lineage_base_strips_every_trailing_rerun_group() {
        for (id, base, index) in [
            ("G", "G", Ok(0)),
            ("G.r1", "G", Ok(1)),
            ("G.r12", "G", Ok(12)),
            ("G.r1.r2", "G", Ok(2)),
            ("G.r2.r1", "G", Ok(1)),
            ("G.r1x.r2", "G.r1x", Ok(2)),
            ("G.rx", "G.rx", Ok(0)),
            ("G.r", "G.r", Ok(0)),
            ("G.r1x", "G.r1x", Ok(0)),
            ("G.r1.r", "G.r1.r", Ok(0)),
            (".r5", "", Ok(5)),
            ("G.r007", "G", Ok(7)),
            ("g.R3", "g.R3", Ok(0)),
        ] {
            assert_eq!(lineage_base(id), base, "base of {id}");
            assert_eq!(rerun_index(id), index, "index of {id}");
        }
        assert!(rerun_index("G.r99999999999999999999").is_err());
    }

    #[test]
    fn a_repr_is_only_produced_for_plain_names() {
        let plain = vec!["review".to_string(), "quality".to_string()];
        assert_eq!(
            py_repr_list(&plain),
            Ok("['review', 'quality']".to_string())
        );
        assert_eq!(py_repr_list(&[]), Ok("[]".to_string()));
        for odd in ["re'view", "back\\slash", "tab\t", "é", "nl\n"] {
            assert!(py_repr_list(&[odd.to_string()]).is_err(), "{odd:?}");
        }
    }

    fn refusal(facts: &ReconcileFacts, approve: bool) -> String {
        match plan_reconcile(facts, approve, 3000.0) {
            Err(NotStarted(reason)) => reason,
            Ok(plan) => panic!("expected a refusal, planned {plan:?}"),
        }
    }

    #[test]
    fn an_approval_without_key_facts_is_refused_but_an_advisory_run_is_not() {
        let apply = case_named("approve-low-apply");
        let mut facts = apply.facts();
        facts.key_policy = None;
        assert!(refusal(&facts, true).contains("approval facts not supplied"));
        let advisory = case_named("approve-low-advisory");
        let mut facts = advisory.facts();
        facts.key_policy = None;
        let plan =
            plan_reconcile(&facts, false, 3000.0).expect("an advisory run needs no key facts");
        assert_eq!(
            plan.notes_log,
            vec!["T-1: advisory, approval not attempted".to_string()]
        );
    }

    #[test]
    fn a_key_policy_that_refuses_approval_is_not_started_even_after_earlier_tasks_planned() {
        let case = case_named("approve-order-follows-depth-then-creation");
        for policy in [
            KeyPolicy {
                require_key: false,
                real_key: false,
                dev_key: false,
            },
            KeyPolicy {
                require_key: true,
                real_key: false,
                dev_key: true,
            },
        ] {
            let mut facts = case.facts();
            facts.key_policy = Some(policy);
            assert!(refusal(&facts, true).contains("fail-closed"));
        }
        let mut facts = case.facts();
        facts.key_policy = Some(KeyPolicy {
            require_key: false,
            real_key: false,
            dev_key: true,
        });
        assert!(plan_reconcile(&facts, true, 3000.0).is_ok());
    }

    #[test]
    fn a_rerun_id_taken_anywhere_in_the_store_is_not_started() {
        let case = case_named("rework-fail-with-done-gate-task");
        let mut facts = case.facts();
        facts.other_tasks.push(IssuerState {
            task_id: "G-review.r1".to_string(),
            state: "DONE".to_string(),
        });
        assert!(refusal(&facts, true).contains("G-review.r1 exists"));
        let mut facts = case.facts();
        let mut twin = facts.tasks[1].clone();
        set(&mut twin, "task_id", jstr("G-review.r1"));
        facts.tasks.push(twin);
        assert!(
            plan_reconcile(&facts, true, 3000.0).is_ok(),
            "the lineage then numbers past the existing member"
        );
    }

    #[test]
    fn rows_the_planner_cannot_read_like_python_are_not_started() {
        let case = case_named("rework-fail-with-done-gate-task");
        let corrupt: [(&str, &str, Json); 12] = [
            ("T-1", "notes", jstr("{not json")),
            ("T-1", "notes", Json::Int(5)),
            ("T-1", "depends_on", jstr("{\"a\": 1}")),
            ("T-1", "depends_on", jstr("[1, 2]")),
            ("T-1", "state", Json::Null),
            ("T-1", "correlation_id", jstr("corr-elsewhere")),
            ("T-1", "created_at", Json::Null),
            ("T-1", "dag_depth", jstr("deep")),
            ("G-review", "notes", jstr("{\"gate\": 5}")),
            ("G-review", "notes", jstr("{\"gate_for\": null}")),
            ("G-review", "title", Json::Null),
            ("T-1", "rework_loops", Json::Null),
        ];
        for (task, column, value) in corrupt {
            let mut facts = case.facts();
            let row = facts
                .tasks
                .iter_mut()
                .find(|row| text(member_of(row, "task_id")) == task)
                .expect("the task is in the case");
            set(row, column, value.clone());
            assert!(
                plan_reconcile(&facts, true, 3000.0).is_err(),
                "{task}.{column} = {value:?}"
            );
        }
        let mut facts = case.facts();
        facts.correlation_id = String::new();
        assert!(refusal(&facts, true).contains("empty correlation"));
    }

    #[test]
    fn a_rerun_index_beyond_u64_is_not_started() {
        let case = case_named("lineage-two-members");
        let mut facts = case.facts();
        let row = facts
            .tasks
            .iter_mut()
            .find(|row| text(member_of(row, "task_id")) == "G-review.r1")
            .expect("the member is in the case");
        set(row, "task_id", jstr("G-review.r18446744073709551615"));
        assert!(refusal(&facts, true).contains("beyond u64"));
    }

    #[test]
    fn the_plan_is_empty_when_nothing_is_in_review() {
        let case = case_named("approve-only-in-review-no-tasks");
        let plan = plan_reconcile(&case.facts(), true, case.now).expect("nothing to do is a plan");
        assert!(plan.ops.is_empty() && plan.notes_log.is_empty());
    }

    #[test]
    fn task_rows_in_any_order_with_distinct_sort_keys_plan_the_same_operations() {
        for name in [
            "approve-order-follows-depth-then-creation",
            "release-rerun-waits-for-other-reruns",
            "lineage-multi-target-both-fail",
        ] {
            let case = case_named(name);
            let facts = case.facts();
            let want = plan_reconcile(&facts, true, case.now).expect("the case plans");
            let mut shuffled = facts.clone();
            shuffled.tasks.reverse();
            assert_eq!(
                plan_reconcile(&shuffled, true, case.now),
                Ok(want),
                "{name}"
            );
        }
    }
}
