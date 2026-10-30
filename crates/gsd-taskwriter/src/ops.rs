//! An ordered list of store operations applied in ONE `BEGIN IMMEDIATE` transaction (phase 14.5, unit U3.5d).
//!
//! The pure planners of `gsd-route` (ingest, reconcile, plan creation) answer with an ordered list of what Python's `TaskStore`
//! calls would do between `with store.transaction():` and its commit. [`TaskStoreWriter::apply_ops`] is that block: one
//! `BEGIN IMMEDIATE`, every op in order, one `COMMIT`, with the invariants of [`TaskStoreWriter::transition`]. The ops are
//! [`WriterOp`]s, one per Python call:
//!
//! | op | Python | what is written |
//! |---|---|---|
//! | [`WriterOp::Transition`] | `store.transition` | a plan of `decide_transition`, re-checked by [`PlanValidator`], applied by the conditional `UPDATE` and the audit `INSERT`s of `TaskStore._apply` |
//! | [`WriterOp::SetNotes`] | `store.set_notes(tid, **kv)` | `notes` = `json.dumps` of the notes object with `kv` merged key by key (an existing key keeps its position, a new one is appended); notes that are `NULL`, empty or not an object count as `{}`; `updated_at` |
//! | [`WriterOp::AddArtifact`] | `store.add_artifact` | one `artifacts` row, then `outputs` + `{kind, uri, version, digest}`; `updated_at` |
//! | [`WriterOp::AppendFeedback`] | `store.append_feedback` | `notes.feedback` (created at the end of the notes when absent) + the entry; `updated_at` |
//! | [`WriterOp::CreateTask`] | `store.create` | the `tasks` row (`CREATED`, Python's defaults and falsy-value rules) and its `NULL -> CREATED` audit row |
//! | [`WriterOp::SetDependsOn`] | `store.update(tid, depends_on=...)` | `depends_on` = `json.dumps` of the list; `updated_at` |
//!
//! `notes.ingest_keys` of the ingest dedup is part of `notes`, so it has no op of its own: the ingest planner ends its list with a
//! [`WriterOp::SetNotes`] holding the new key list.
//!
//! # Two phases
//!
//! 1. **Resolve** (reads only). Every op is checked against the rows as the earlier ops of the list leave them: the writer keeps an
//!    in-memory copy of each row it touches and applies every op to it as it resolves it, so `plan.from` of a second transition, the
//!    notes a later merge starts from, the outputs a second artifact appends to and an id a `CreateTask` takes are all the list's own
//!    state. Anything that cannot be written is refused here, before the first write statement.
//! 2. **Write**. The resolved steps are executed in order. What can still fail is the database itself; the transaction is then
//!    rolled back, so the writer has no partial state: either every op of the list is in the store, or none is.
//!
//! The write lock is held from the `BEGIN IMMEDIATE` to the end, so a second writer waits (and times out as
//! [`ErrorKind::Busy`], not started) and no row changes between phase one and phase two. `transitions`, `verdicts` and `artifacts`
//! are only ever inserted into. One `now` stamps every `updated_at`, `created_at` and `ts`.
//!
//! # Errors
//!
//! * Python's own answer: an op on a task that does not exist is `E-INPUT` `unknown task <id>` ([`OpsCause::UnknownTask`], final:
//!   Python answers it the same, and nothing was written).
//! * [`WriteState::NotStarted`] (`E-DEP`, Python answers): what Python accepts or fails on with an exception that is not a
//!   `SwarmError`, and Rust does not reproduce byte for byte. The list is [`OpsCause::Failed`] with [`ErrorKind::Unsupported`]:
//!   a malformed op or a value of the wrong type ([`WriterOp::list_from_json`]); a `NaN` or infinite number inside a value (the
//!   next read would refuse the row); the same key twice in one `SetNotes`, or a key named `task_id` or `self` (a `TypeError` in
//!   Python's `set_notes(task_id, **kv)`); a task id outside `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`
//!   (Python's `create` accepts anything), or one that exists already (Python raises `sqlite3.IntegrityError`); an `outputs` column
//!   that is not a JSON list (`TypeError`); a `notes.feedback` that is not a list (`AttributeError`); a stored row Rust cannot read
//!   ([`crate::Tx::task`]).
//! * A transition plan that fails [`PlanValidator`] is `E-INTERNAL`, not started ([`OpsCause::InvalidPlan`]).
//! * A transition to `APPROVED` is [`OpsCause::ApprovalRefused`], not started, unless the host attested it (next section).
//! * A database failure during phase two is final, with the transaction rolled back.
//!
//! [`OpsError::op_index`] names the op that failed.
//!
//! # APPROVED is never applied on the caller's word (security review F-7)
//!
//! [`PlanValidator`] checks the shape and the legality of a plan, not the facts behind it: a plan to `APPROVED` needs no key, no
//! passing gate and no accepted issuer to be valid. So the guarantee is structural, inside the one function every list goes through
//! (`resolve_and_write`):
//!
//! * [`TaskStoreWriter::apply_ops`] has no approval context: a list with a plan that ends in, or passes through, `APPROVED` is
//!   refused in phase one, before the first write.
//! * [`TaskStoreWriter::apply_ops_approving`] and [`TaskStoreWriter::ingest`] hand the function an `ApprovalGuard` built from an
//!   [`ApprovalSource`](crate::ApprovalSource): keys read once before the transaction, the facts of
//!   [`crate::approval`] read inside it. Immediately before each APPROVED step is written (so after the earlier steps of the same list
//!   are, which is what a reconcile that approves a task whose gate task the list has just moved to IN_REVIEW needs) the writer reads
//!   the task row and the gate facts, runs `decide_transition` on them and requires the result to equal the step's plan. Anything else
//!   rolls the whole list back.
//!
//! There is no way to reach `write_step` for an APPROVED plan without passing one of the two branches, and no constructor of the
//! guard outside this crate. The test `an_approved_plan_is_never_applied_without_the_hosts_facts` (tests/ingest.rs) fails if the
//! check is removed.

use std::fmt;

use gsd_codes::ErrorCode;
use gsd_route::pydumps::to_python_dumps;
use gsd_route::transition_plan::{
    decide_transition, AuditRow, Facts, PlanEffect, Request, TransitionPlan,
};
use gsd_route::{is_safe_id, jstr, obj_get, Fields, Json};

use crate::apply::write_validated;
use crate::approval::{self, gather, ApprovalKeys, ApprovalSource};
use crate::error::{ErrorKind, WriteState, WriterError};
use crate::plan::{has_non_finite, PlanValidator, PlanViolation, ValidatedPlan};
use crate::store::{NewArtifact, NewTaskRow, TextColumn, Tx};
use crate::TaskStoreWriter;

#[cfg(feature = "fault-injection")]
use crate::store::fault::PausePoint;

/// `TaskStore.create(task_id=..., correlation_id=..., capability=..., **optional)` with Python's defaults.
///
/// `inputs`, `acceptance` and `budget` take any JSON value and follow Python's `value or default`: a falsy value (`null`, `[]`, `{}`,
/// `""`, `0`, `false`) is replaced by `[]`, `[]` and the default budget. Nothing else is validated, as in Python (a `correlation_id`
/// or `risk_class` of any text is stored), except the id rules of [`WriterOp`].
#[derive(Debug, Clone, PartialEq)]
pub struct NewTask {
    /// `task_id`; must match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, no `..`.
    pub task_id: String,
    /// `correlation_id`.
    pub correlation_id: String,
    /// `capability`.
    pub capability: String,
    /// `title`; Python's default is the empty string.
    pub title: String,
    /// `parent_id`.
    pub parent_id: Option<String>,
    /// `dag_depth`; default 0.
    pub dag_depth: i64,
    /// `inputs`; falsy becomes `[]`.
    pub inputs: Json,
    /// `acceptance`; falsy becomes `[]`.
    pub acceptance: Json,
    /// `budget`; falsy becomes `{"max_tokens": 200000, "max_wall_s": 1800, "max_cost_usd": 5}`.
    pub budget: Json,
    /// `risk_class`; default `low`.
    pub risk_class: String,
    /// `priority`; default `P2`.
    pub priority: String,
    /// `depends_on`; default empty.
    pub depends_on: Vec<String>,
    /// `agent_id`.
    pub agent_id: Option<String>,
    /// `max_attempts`; default 3.
    pub max_attempts: i64,
    /// `notes`, the initial notes object; empty by default.
    pub notes: Fields,
}

impl NewTask {
    /// A task with Python's defaults for every optional argument of `TaskStore.create`.
    pub fn new(
        task_id: impl Into<String>,
        correlation_id: impl Into<String>,
        capability: impl Into<String>,
    ) -> Self {
        Self {
            task_id: task_id.into(),
            correlation_id: correlation_id.into(),
            capability: capability.into(),
            title: String::new(),
            parent_id: None,
            dag_depth: 0,
            inputs: Json::Null,
            acceptance: Json::Null,
            budget: Json::Null,
            risk_class: "low".to_string(),
            priority: "P2".to_string(),
            depends_on: Vec::new(),
            agent_id: None,
            max_attempts: 3,
            notes: Vec::new(),
        }
    }
}

/// One store operation of an op list.
#[derive(Debug, Clone, PartialEq)]
pub enum WriterOp {
    /// `store.transition` with the plan `decide_transition` made for the row as the earlier ops leave it.
    Transition {
        /// The task.
        task_id: String,
        /// The actor of the request; every audit row of the plan must be by it ([`PlanValidator`]).
        actor: String,
        /// The decided plan.
        plan: TransitionPlan,
    },
    /// `store.set_notes(task_id, **set)`: a dict update of the notes, in the order given.
    SetNotes {
        /// The task.
        task_id: String,
        /// The keys to set; a key at most once.
        set: Fields,
    },
    /// `store.add_artifact`: an `artifacts` row and the append to `outputs`.
    AddArtifact {
        /// The task.
        task_id: String,
        /// `kind`.
        kind: String,
        /// `uri`.
        uri: String,
        /// `version`.
        version: String,
        /// `digest`.
        digest: String,
        /// `producer` of the `artifacts` row (not part of the `outputs` entry).
        producer: String,
    },
    /// `store.append_feedback`: append to `notes.feedback`.
    AppendFeedback {
        /// The task whose notes change.
        task_id: String,
        /// The entry; Python appends any value.
        entry: Json,
    },
    /// `store.create`.
    CreateTask(NewTask),
    /// `store.update(task_id, depends_on=depends_on)`: replaces the whole list.
    SetDependsOn {
        /// The task.
        task_id: String,
        /// The new `depends_on`.
        depends_on: Vec<String>,
    },
}

/// Which stage of the op list failed.
#[derive(Debug, Clone, PartialEq)]
pub enum OpsCause {
    /// An op names a task that does not exist: Python's own `E-INPUT` answer.
    UnknownTask,
    /// A transition plan failed validation. Nothing was written.
    InvalidPlan(PlanViolation),
    /// A transition to `APPROVED` that the host's own gate facts do not allow, or that came without them. Nothing is committed (the
    /// transaction is rolled back, whatever the earlier ops of the list had written inside it), and Python answers.
    ApprovalRefused,
    /// An op or a stored value Rust cannot prove byte-identical to Python ([`ErrorKind::Unsupported`], not started), or a store
    /// failure (see [`ErrorKind`]).
    Failed(ErrorKind),
}

/// A failed op list: code, message, `details` and whether anything was written. Nothing is ever left applied.
#[derive(Debug, Clone, PartialEq)]
pub struct OpsError {
    cause: OpsCause,
    op_index: Option<usize>,
    code: ErrorCode,
    message: Box<str>,
    details: Box<[(String, Json)]>,
    write_state: WriteState,
}

impl OpsError {
    /// A refusal before the first write: `E-DEP`, [`WriteState::NotStarted`], Python answers.
    fn unsupported(message: impl Into<String>) -> Self {
        Self {
            cause: OpsCause::Failed(ErrorKind::Unsupported),
            op_index: None,
            code: ErrorCode::Dep,
            message: Into::<String>::into(message).into_boxed_str(),
            details: Box::default(),
            write_state: WriteState::NotStarted,
        }
    }

    /// Python's `SwarmError(E_INPUT, f"unknown task {task_id}")`: no details, nothing written.
    fn unknown_task(task_id: &str) -> Self {
        Self {
            cause: OpsCause::UnknownTask,
            op_index: None,
            code: ErrorCode::Input,
            message: format!("unknown task {task_id}").into_boxed_str(),
            details: Box::default(),
            write_state: WriteState::Final,
        }
    }

    /// A plan the validator refused: `E-INTERNAL`, nothing written.
    fn invalid_plan(violation: PlanViolation, task_id: &str) -> Self {
        Self {
            code: ErrorCode::Internal,
            message: format!("invalid transition plan for {task_id}: {violation}").into_boxed_str(),
            details: vec![("task_id".to_string(), jstr(task_id))].into_boxed_slice(),
            write_state: WriteState::NotStarted,
            op_index: None,
            cause: OpsCause::InvalidPlan(violation),
        }
    }

    /// An APPROVED the host's approval check refused: `E-DEP`, [`WriteState::NotStarted`] (nothing is committed), Python answers.
    /// `why` is fixed text or the policy message of the refusal (task id and gate names): never a key, a signature or an envelope.
    fn approval_refused(task_id: &str, why: &str) -> Self {
        Self {
            cause: OpsCause::ApprovalRefused,
            op_index: None,
            code: ErrorCode::Dep,
            message: format!("approval: {why} (task {task_id})").into_boxed_str(),
            details: Box::default(),
            write_state: WriteState::NotStarted,
        }
    }

    /// A store failure, with Python's `task_id` detail on a concurrent transition.
    fn store(error: &WriterError, task_id: Option<&str>) -> Self {
        let details = match (error.kind(), task_id) {
            (ErrorKind::ConcurrentTransition, Some(id)) => vec![("task_id".to_string(), jstr(id))],
            _ => Vec::new(),
        };
        Self {
            cause: OpsCause::Failed(error.kind()),
            op_index: None,
            code: error.code(),
            message: error.message().into(),
            details: details.into_boxed_slice(),
            write_state: error.write_state(),
        }
    }

    /// The same error, attributed to op number `index`.
    fn at(mut self, index: usize) -> Self {
        self.op_index = Some(index);
        self
    }

    /// Which stage failed.
    pub fn cause(&self) -> &OpsCause {
        &self.cause
    }

    /// The position in the op list of the op that failed; `None` for a failure of the transaction itself.
    pub fn op_index(&self) -> Option<usize> {
        self.op_index
    }

    /// The taxonomy code.
    pub fn code(&self) -> ErrorCode {
        self.code
    }

    /// The message without the `CODE: ` prefix, as `SwarmError` keeps it.
    pub fn message(&self) -> &str {
        &self.message
    }

    /// `SwarmError.details`: empty for `unknown task`, `task_id` for a refused plan or a concurrent transition.
    pub fn details(&self) -> &[(String, Json)] {
        &self.details
    }

    /// Whether the request may still be answered by Python.
    pub fn write_state(&self) -> WriteState {
        self.write_state
    }

    /// The seam error with the `CODE: ` message prefix and the details; the caller adds `write_state` when it renders one.
    pub fn to_seam_error(&self) -> gsd_route::SeamError {
        gsd_route::SeamError::new(self.code, &self.message).with_details(self.details.to_vec())
    }
}

impl fmt::Display for OpsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code.as_str(), self.message)
    }
}

impl std::error::Error for OpsError {}

/// One audit row written by an op list.
#[derive(Debug, Clone, PartialEq)]
pub struct OpsAudit {
    /// `task_id`.
    pub task_id: String,
    /// `from_state`; `None` for the `NULL -> CREATED` row of a created task.
    pub from: Option<String>,
    /// `to_state`.
    pub to: String,
    /// `actor`.
    pub actor: String,
    /// `reason`.
    pub reason: String,
    /// `ts`: the `now` of the call.
    pub ts: f64,
}

/// A committed op list.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct OpsApplied {
    /// The row of every task the list touched after the commit, as `TaskStore.get` returns it, in order of first touch.
    pub tasks: Vec<Fields>,
    /// The audit rows written, in insertion order.
    pub audit: Vec<OpsAudit>,
    /// Effects of the transitions, to run after the commit, in order.
    pub effects: Vec<PlanEffect>,
}

impl TaskStoreWriter {
    /// Applies `ops` in order in one `BEGIN IMMEDIATE` transaction and commits, or applies nothing.
    ///
    /// `now` is the single clock reading of the call (every `updated_at`, `created_at` and `ts`); it must be finite. An empty
    /// list opens no transaction. See the module documentation for the two phases and for what is refused. On any error the
    /// transaction is rolled back; an error whose [`OpsError::write_state`] is [`WriteState::NotStarted`] left the store
    /// byte-identical.
    pub async fn apply_ops(&self, ops: &[WriterOp], now: f64) -> Result<OpsApplied, OpsError> {
        self.apply_list(ops, now, None).await
    }

    /// [`TaskStoreWriter::apply_ops`] for a list that may move tasks to `APPROVED` (the ingest and reconcile of F-7).
    ///
    /// `apply_ops` refuses such a list in phase one, before the first write: `PlanValidator` checks the shape and the legality of a
    /// plan, not the facts behind an approval, so a plan to APPROVED must never be applied on the word of its caller. Here the host
    /// proves it itself. The keys come from `source` ONCE, before the transaction opens, and only when the list has an APPROVED step
    /// (a [`gsd_envelope::Declined`] key shape refuses there, not started). Inside the transaction, immediately before each APPROVED
    /// step is written, the writer reads the approval facts as `TaskStore.transition` does ([`crate::approval`]: the key policy, the
    /// missing and unaccepted gates, the verdict rows with their signature answers, one clock reading after the lock) against the rows
    /// the earlier steps of the list have left, runs [`decide_transition`] on them and requires the result to be the step's own plan.
    /// Any difference refuses the whole list ([`OpsCause::ApprovalRefused`]) and rolls it back.
    pub async fn apply_ops_approving(
        &self,
        ops: &[WriterOp],
        now: f64,
        source: &dyn ApprovalSource,
    ) -> Result<OpsApplied, OpsError> {
        self.apply_list(ops, now, Some(source)).await
    }

    async fn apply_list(
        &self,
        ops: &[WriterOp],
        now: f64,
        source: Option<&dyn ApprovalSource>,
    ) -> Result<OpsApplied, OpsError> {
        if !now.is_finite() {
            return Err(OpsError::unsupported("now is not a finite number"));
        }
        if ops.is_empty() {
            return Ok(OpsApplied::default());
        }
        let keys = match source {
            Some(source) if ops.iter().any(WriterOp::reaches_approved) => Some(
                source
                    .keys()
                    .map_err(|declined| OpsError::store(&approval::declined(declined), None))?,
            ),
            _ => None,
        };
        let mut tx = self
            .store()
            .begin()
            .await
            .map_err(|error| OpsError::store(&error, None))?;
        let guard = source
            .zip(keys.as_ref())
            .map(|(source, keys)| ApprovalGuard::new(source, keys));
        match resolve_and_write(&mut tx, ops, now, guard.as_ref()).await {
            Ok(applied) => {
                tx.commit()
                    .await
                    .map_err(|error| OpsError::store(&error, None))?;
                Ok(applied)
            }
            Err(error) => {
                // The failure above is the answer; a rollback that fails too leaves a connection sqlx discards.
                tx.rollback().await.ok();
                Err(error)
            }
        }
    }
}

/// The rows touched so far as the list leaves them, read from the store the first time a task is named.
struct Sim<'t> {
    tx: &'t mut Tx,
    rows: Vec<(String, Option<Fields>)>,
}

impl Sim<'_> {
    /// Index of the cached row of `task_id`; the store is read the first time.
    async fn slot(&mut self, task_id: &str) -> Result<usize, OpsError> {
        if let Some(index) = self.rows.iter().position(|(id, _)| id == task_id) {
            return Ok(index);
        }
        let row = self
            .tx
            .task(task_id)
            .await
            .map_err(|error| OpsError::store(&error, None))?;
        self.rows.push((task_id.to_string(), row));
        Ok(self.rows.len() - 1)
    }

    /// The cached row of a task that must exist (`TaskStore.get`'s `unknown task`).
    async fn existing(&mut self, task_id: &str) -> Result<&mut Fields, OpsError> {
        let index = self.slot(task_id).await?;
        self.rows[index]
            .1
            .as_mut()
            .ok_or_else(|| OpsError::unknown_task(task_id))
    }
}

/// One resolved write, in list order.
enum Step<'a> {
    /// A validated transition plan.
    State {
        task_id: &'a str,
        validated: ValidatedPlan<'a>,
        /// The plan as given: an APPROVED step is attested against it (see [`ApprovalGuard`]).
        plan: &'a TransitionPlan,
    },
    /// `UPDATE tasks SET <column>=?, updated_at=?`.
    Text {
        task_id: &'a str,
        column: TextColumn,
        text: String,
    },
    /// The `artifacts` row and the `outputs` text that follows it.
    Artifact {
        task_id: &'a str,
        kind: &'a str,
        uri: &'a str,
        version: &'a str,
        digest: &'a str,
        producer: &'a str,
        outputs: String,
    },
    /// The `tasks` row and its audit row, with the JSON columns as Python prints them.
    Create {
        task: &'a NewTask,
        inputs: String,
        acceptance: String,
        budget: String,
        depends_on: String,
        notes: String,
    },
}

impl WriterOp {
    /// Whether the op is a transition whose plan ends in, or passes through, `APPROVED`.
    fn reaches_approved(&self) -> bool {
        matches!(self, WriterOp::Transition { plan, .. } if plan_reaches_approved(plan))
    }
}

/// A plan reaches APPROVED when it ends there or any of its audit steps enters it (a two-step plan through APPROVED is not a legal
/// decision, but the check must not depend on `plan.to` alone).
fn plan_reaches_approved(plan: &TransitionPlan) -> bool {
    plan.to == "APPROVED" || plan.audit.iter().any(|row| row.to == "APPROVED")
}

impl Step<'_> {
    fn task_id(&self) -> &str {
        match self {
            Step::State { task_id, .. }
            | Step::Text { task_id, .. }
            | Step::Artifact { task_id, .. } => task_id,
            Step::Create { task, .. } => &task.task_id,
        }
    }

    fn reaches_approved(&self) -> bool {
        matches!(self, Step::State { plan, .. } if plan_reaches_approved(plan))
    }
}

/// The host's approval context of one op list: where the keys and the clock come from, and the keys of this call. Built only inside
/// this crate, from an [`ApprovalSource`] the caller gave, so a list cannot be attested by anything but the host's own reading.
pub(crate) struct ApprovalGuard<'a> {
    source: &'a dyn ApprovalSource,
    keys: &'a ApprovalKeys,
}

impl<'a> ApprovalGuard<'a> {
    /// A guard over the keys read for this call and the source that gave them.
    pub(crate) fn new(source: &'a dyn ApprovalSource, keys: &'a ApprovalKeys) -> Self {
        Self { source, keys }
    }
}

/// The row as [`Facts`] holds it: `dict(sqlite3.Row)`, so the JSON columns are text again and `notes_json` is gone. (The same
/// reshaping as `apply.rs` does for a single transition; that module is not this unit's to export from.)
fn decision_row(row: Fields) -> Fields {
    const JSON_COLUMNS: [&str; 5] = ["inputs", "outputs", "acceptance", "budget", "depends_on"];
    row.into_iter()
        .filter(|(name, _)| name != "notes_json")
        .map(|(name, value)| {
            if JSON_COLUMNS.contains(&name.as_str()) {
                (name, Json::Str(to_python_dumps(&value)))
            } else {
                (name, value)
            }
        })
        .collect()
}

/// F-7: an APPROVED step is written only if the host's own approval facts, read now inside the transaction, give exactly this plan.
///
/// The row is read after the earlier steps of the list were written, so a gate task the list has just moved to IN_REVIEW counts as
/// accepted, and the target's `notes.verdicts_since` is the one the list has left. Nothing is trusted from the planner but the plan.
async fn attest_approval(
    tx: &mut Tx,
    guard: &ApprovalGuard<'_>,
    task_id: &str,
    plan: &TransitionPlan,
    now: f64,
) -> Result<(), OpsError> {
    let Some(entering) = plan.audit.iter().find(|row| row.to == "APPROVED") else {
        return Err(OpsError::approval_refused(
            task_id,
            "a plan that ends in APPROVED without an audit step into it",
        ));
    };
    let row = tx
        .task(task_id)
        .await
        .map_err(|error| OpsError::store(&error, None))?
        .ok_or_else(|| OpsError::unknown_task(task_id))?;
    // The op list has no approval verifier: only the sandboxed transition route checks the gate answer (phase 14.5, pin P2).
    let approval = gather(tx, guard.keys, guard.source, &row, now)
        .await
        .map_err(|error| OpsError::store(&error, None))?
        .facts;
    let facts = Facts {
        task: Some(decision_row(row)),
        approval: Some(approval),
    };
    let request = Request {
        task_id: task_id.to_string(),
        to: "APPROVED".to_string(),
        actor: entering.actor.clone(),
        reason: entering.reason.clone(),
        manual: false,
    };
    match decide_transition(&facts, &request, now) {
        Ok(decided) if decided == *plan => Ok(()),
        Ok(_) => Err(OpsError::approval_refused(
            task_id,
            "the plan is not the host's decision for the rows as they stand",
        )),
        Err(refusal) => Err(OpsError::approval_refused(task_id, &refusal.message)),
    }
}

/// Phase one for every op, phase two for the resulting steps, then the rows as they stand before the commit.
///
/// `guard` is the host's approval context. Without it a list that moves a task to `APPROVED` is refused in phase one, before the
/// first write; with it every such step is attested in phase two against the rows the earlier steps left in this transaction.
pub(crate) async fn resolve_and_write(
    tx: &mut Tx,
    ops: &[WriterOp],
    now: f64,
    guard: Option<&ApprovalGuard<'_>>,
) -> Result<OpsApplied, OpsError> {
    let mut sim = Sim {
        tx,
        rows: Vec::new(),
    };
    let mut steps = Vec::with_capacity(ops.len());
    for (index, op) in ops.iter().enumerate() {
        let step = resolve(&mut sim, op, now).await.map_err(|e| e.at(index))?;
        if guard.is_none() && step.reaches_approved() {
            return Err(OpsError::approval_refused(
                step.task_id(),
                "a transition to APPROVED needs the host's approval facts (apply_ops_approving)",
            )
            .at(index));
        }
        steps.push(step);
    }
    let tx = sim.tx;

    let mut applied = OpsApplied::default();
    let mut touched: Vec<&str> = Vec::new();
    for (index, step) in steps.iter().enumerate() {
        #[cfg(feature = "fault-injection")]
        if index > 0 {
            tx.faults.hit(PausePoint::BetweenOps).await;
        }
        if let (Some(guard), Step::State { task_id, plan, .. }) = (guard, step) {
            if step.reaches_approved() {
                attest_approval(tx, guard, task_id, plan, now)
                    .await
                    .map_err(|error| error.at(index))?;
            }
        }
        write_step(tx, step, now)
            .await
            .map_err(|error| OpsError::store(&error, Some(step.task_id())).at(index))?;
        if !touched.contains(&step.task_id()) {
            touched.push(step.task_id());
        }
        record(&mut applied, step, now);
    }
    for task_id in touched {
        let row = tx
            .task(task_id)
            .await
            .map_err(|error| OpsError::store(&error, None))?
            .ok_or_else(|| {
                OpsError::store(
                    &WriterError::final_error(
                        ErrorKind::Database,
                        ErrorCode::Internal,
                        format!("task {task_id} vanished inside its own transaction"),
                    ),
                    None,
                )
            })?;
        applied.tasks.push(row);
    }
    Ok(applied)
}

/// Adds what `step` wrote to the audit rows and effects of the result.
fn record(applied: &mut OpsApplied, step: &Step<'_>, now: f64) {
    match step {
        Step::State {
            task_id, validated, ..
        } => {
            for row in validated.audit {
                applied.audit.push(OpsAudit {
                    task_id: (*task_id).to_string(),
                    from: Some(row.from.clone()),
                    to: row.to.clone(),
                    actor: row.actor.clone(),
                    reason: row.reason.clone(),
                    ts: now,
                });
            }
            applied.effects.extend_from_slice(validated.effects);
        }
        Step::Create { task, .. } => applied.audit.push(OpsAudit {
            task_id: task.task_id.clone(),
            from: None,
            to: "CREATED".to_string(),
            actor: "system".to_string(),
            reason: "created".to_string(),
            ts: now,
        }),
        Step::Text { .. } | Step::Artifact { .. } => {}
    }
}

/// Executes one resolved step.
async fn write_step(tx: &mut Tx, step: &Step<'_>, now: f64) -> Result<(), WriterError> {
    match step {
        Step::State {
            task_id, validated, ..
        } => write_validated(tx, task_id, validated, now).await,
        Step::Text {
            task_id,
            column,
            text,
        } => tx.update_text(task_id, *column, text, now).await,
        Step::Artifact {
            task_id,
            kind,
            uri,
            version,
            digest,
            producer,
            outputs,
        } => {
            tx.insert_artifact(&NewArtifact {
                task_id,
                kind,
                uri,
                version,
                digest,
                producer,
                ts: now,
            })
            .await?;
            tx.update_text(task_id, TextColumn::Outputs, outputs, now)
                .await
        }
        Step::Create {
            task,
            inputs,
            acceptance,
            budget,
            depends_on,
            notes,
        } => {
            tx.insert_task(&NewTaskRow {
                task_id: &task.task_id,
                correlation_id: &task.correlation_id,
                parent_id: task.parent_id.as_deref(),
                dag_depth: task.dag_depth,
                capability: &task.capability,
                agent_id: task.agent_id.as_deref(),
                title: &task.title,
                inputs,
                acceptance,
                budget,
                risk_class: &task.risk_class,
                priority: &task.priority,
                depends_on,
                max_attempts: task.max_attempts,
                notes,
                now,
            })
            .await
        }
    }
}

/// Checks one op against the rows as the list leaves them, applies it to the cached rows and returns the write it stands for.
async fn resolve<'a>(sim: &mut Sim<'_>, op: &'a WriterOp, now: f64) -> Result<Step<'a>, OpsError> {
    match op {
        WriterOp::Transition {
            task_id,
            actor,
            plan,
        } => resolve_transition(sim, task_id, actor, plan).await,
        WriterOp::SetNotes { task_id, set } => resolve_set_notes(sim, task_id, set, now).await,
        WriterOp::AddArtifact {
            task_id,
            kind,
            uri,
            version,
            digest,
            producer,
        } => {
            let row = sim.existing(task_id).await?;
            let Some(Json::Arr(outputs)) = obj_get(row, "outputs") else {
                return Err(OpsError::unsupported(format!(
                    "task {task_id}: outputs is not a JSON list"
                )));
            };
            let mut outputs = outputs.clone();
            outputs.push(Json::Obj(vec![
                ("kind".to_string(), jstr(kind)),
                ("uri".to_string(), jstr(uri)),
                ("version".to_string(), jstr(version)),
                ("digest".to_string(), jstr(digest)),
            ]));
            let outputs = Json::Arr(outputs);
            let text = to_python_dumps(&outputs);
            set_field(row, "outputs", outputs);
            set_field(row, "updated_at", Json::Float(now));
            Ok(Step::Artifact {
                task_id,
                kind,
                uri,
                version,
                digest,
                producer,
                outputs: text,
            })
        }
        WriterOp::AppendFeedback { task_id, entry } => {
            let row = sim.existing(task_id).await?;
            let mut notes = notes_of(row);
            match notes.iter_mut().find(|(key, _)| key == "feedback") {
                Some((_, Json::Arr(items))) => items.push(entry.clone()),
                Some(_) => {
                    return Err(OpsError::unsupported(format!(
                        "task {task_id}: notes.feedback is not a list"
                    )))
                }
                None => notes.push(("feedback".to_string(), Json::Arr(vec![entry.clone()]))),
            }
            let text = store_notes(row, notes, now)?;
            Ok(Step::Text {
                task_id,
                column: TextColumn::Notes,
                text,
            })
        }
        WriterOp::CreateTask(task) => resolve_create(sim, task, now).await,
        WriterOp::SetDependsOn {
            task_id,
            depends_on,
        } => {
            let row = sim.existing(task_id).await?;
            let list = Json::Arr(depends_on.iter().map(|id| jstr(id)).collect());
            let text = to_python_dumps(&list);
            set_field(row, "depends_on", list);
            set_field(row, "updated_at", Json::Float(now));
            Ok(Step::Text {
                task_id,
                column: TextColumn::DependsOn,
                text,
            })
        }
    }
}

/// A transition: the plan must be the validator's, against the state the list has left the row in.
async fn resolve_transition<'a>(
    sim: &mut Sim<'_>,
    task_id: &'a str,
    actor: &'a str,
    plan: &'a TransitionPlan,
) -> Result<Step<'a>, OpsError> {
    let row = sim.existing(task_id).await?;
    let validated = PlanValidator::new(actor)
        .validate(plan, Some(row))
        .map_err(|violation| OpsError::invalid_plan(violation, task_id))?;
    set_field(row, "state", jstr(&plan.to));
    if let Some(attempt) = validated.attempt {
        set_field(row, "attempt", Json::Int(attempt));
    }
    if let Some(loops) = validated.rework_loops {
        set_field(row, "rework_loops", Json::Int(loops));
    }
    if let (Some(text), Some(object)) = (&validated.notes, obj_get(&plan.columns, "notes")) {
        set_field(row, "notes", jstr(text));
        set_field(row, "notes_json", object.clone());
    }
    Ok(Step::State {
        task_id,
        validated,
        plan,
    })
}

/// `set_notes`: the merge of `set` into the notes, in the order Python's `dict.update` gives.
async fn resolve_set_notes<'a>(
    sim: &mut Sim<'_>,
    task_id: &'a str,
    set: &Fields,
    now: f64,
) -> Result<Step<'a>, OpsError> {
    for (index, (key, _)) in set.iter().enumerate() {
        if set[..index].iter().any(|(earlier, _)| earlier == key) {
            return Err(OpsError::unsupported(format!(
                "set_notes sets the key {key} twice"
            )));
        }
        // `set_notes(self, task_id, **kv)`: a key of these names is a TypeError in Python.
        if key == "task_id" || key == "self" {
            return Err(OpsError::unsupported(format!(
                "set_notes key {key} is a parameter name of TaskStore.set_notes"
            )));
        }
    }
    let row = sim.existing(task_id).await?;
    let mut notes = notes_of(row);
    for (key, value) in set {
        match notes.iter_mut().find(|(name, _)| name == key) {
            Some(slot) => slot.1 = value.clone(),
            None => notes.push((key.clone(), value.clone())),
        }
    }
    let text = store_notes(row, notes, now)?;
    Ok(Step::Text {
        task_id,
        column: TextColumn::Notes,
        text,
    })
}

/// `create`: the id and value rules of [`NewTask`], then the row as Python writes it.
async fn resolve_create<'a>(
    sim: &mut Sim<'_>,
    task: &'a NewTask,
    now: f64,
) -> Result<Step<'a>, OpsError> {
    if !is_safe_id(&task.task_id) {
        return Err(OpsError::unsupported(format!(
            "create_task: a task id of {} characters is not a safe id",
            task.task_id.chars().count()
        )));
    }
    let index = sim.slot(&task.task_id).await?;
    if sim.rows[index].1.is_some() {
        return Err(OpsError::unsupported(format!(
            "create_task: task {} exists already",
            task.task_id
        )));
    }
    let notes = Json::Obj(task.notes.clone());
    for (what, value) in [
        ("inputs", &task.inputs),
        ("acceptance", &task.acceptance),
        ("budget", &task.budget),
        ("notes", &notes),
    ] {
        if has_non_finite(value) {
            return Err(OpsError::unsupported(format!(
                "create_task: {what} holds a non-finite number"
            )));
        }
    }
    let inputs = if truthy(&task.inputs) {
        task.inputs.clone()
    } else {
        Json::Arr(Vec::new())
    };
    let acceptance = if truthy(&task.acceptance) {
        task.acceptance.clone()
    } else {
        Json::Arr(Vec::new())
    };
    let budget = if truthy(&task.budget) {
        task.budget.clone()
    } else {
        Json::Obj(vec![
            ("max_tokens".to_string(), Json::Int(200_000)),
            ("max_wall_s".to_string(), Json::Int(1800)),
            ("max_cost_usd".to_string(), Json::Int(5)),
        ])
    };
    let depends_on = Json::Arr(task.depends_on.iter().map(|id| jstr(id)).collect());
    let notes_text = to_python_dumps(&notes);
    let step = Step::Create {
        task,
        inputs: to_python_dumps(&inputs),
        acceptance: to_python_dumps(&acceptance),
        budget: to_python_dumps(&budget),
        depends_on: to_python_dumps(&depends_on),
        notes: notes_text.clone(),
    };
    let optional = |value: &Option<String>| value.as_deref().map_or(Json::Null, jstr);
    sim.rows[index].1 = Some(vec![
        ("task_id".to_string(), jstr(&task.task_id)),
        ("correlation_id".to_string(), jstr(&task.correlation_id)),
        ("parent_id".to_string(), optional(&task.parent_id)),
        ("dag_depth".to_string(), Json::Int(task.dag_depth)),
        ("capability".to_string(), jstr(&task.capability)),
        ("agent_id".to_string(), optional(&task.agent_id)),
        ("title".to_string(), jstr(&task.title)),
        ("inputs".to_string(), inputs),
        ("outputs".to_string(), Json::Arr(Vec::new())),
        ("acceptance".to_string(), acceptance),
        ("budget".to_string(), budget),
        ("risk_class".to_string(), jstr(&task.risk_class)),
        ("priority".to_string(), jstr(&task.priority)),
        ("state".to_string(), jstr("CREATED")),
        ("attempt".to_string(), Json::Int(0)),
        ("max_attempts".to_string(), Json::Int(task.max_attempts)),
        ("rework_loops".to_string(), Json::Int(0)),
        ("depends_on".to_string(), depends_on),
        ("assigned_to".to_string(), Json::Null),
        ("notes".to_string(), jstr(&notes_text)),
        ("created_at".to_string(), Json::Float(now)),
        ("updated_at".to_string(), Json::Float(now)),
        ("notes_json".to_string(), notes),
    ]);
    Ok(step)
}

/// Python truthiness of a JSON value, for `value or default`.
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

/// Sets `name` in a row, keeping its position, or appends it.
fn set_field(row: &mut Fields, name: &str, value: Json) {
    match row.iter_mut().find(|(column, _)| column == name) {
        Some(slot) => slot.1 = value,
        None => row.push((name.to_string(), value)),
    }
}

/// `TaskStore._row`'s `notes_json`: the notes object (`{}` for anything else).
fn notes_of(row: &Fields) -> Fields {
    match obj_get(row, "notes_json") {
        Some(Json::Obj(fields)) => fields.clone(),
        _ => Vec::new(),
    }
}

/// Stores `notes` in the cached row as `update(task_id, notes=json.dumps(notes))` would and returns the text written.
///
/// Notes that hold a `NaN` or infinite number anywhere are refused: Python writes `NaN` or `Infinity`, and the next read of the row
/// would refuse it ([`gsd_route::parse_json`]).
fn store_notes(row: &mut Fields, notes: Fields, now: f64) -> Result<String, OpsError> {
    let object = Json::Obj(notes);
    if has_non_finite(&object) {
        return Err(OpsError::unsupported(
            "the notes would hold a non-finite number",
        ));
    }
    let text = to_python_dumps(&object);
    set_field(row, "notes", jstr(&text));
    set_field(row, "notes_json", object);
    set_field(row, "updated_at", Json::Float(now));
    Ok(text)
}

// ---- the wire form of an op list ----------------------------------------------------------------------------------------------

/// An object of the wire form with a closed key set; every refusal names the key, never the value.
struct Wire<'v> {
    what: &'static str,
    fields: &'v Fields,
}

impl<'v> Wire<'v> {
    /// `value` as an object whose keys are all in `allowed`.
    fn new(value: &'v Json, what: &'static str, allowed: &[&str]) -> Result<Self, OpsError> {
        let Json::Obj(fields) = value else {
            return Err(OpsError::unsupported(format!("{what} is not an object")));
        };
        if let Some((key, _)) = fields
            .iter()
            .find(|(key, _)| !allowed.contains(&key.as_str()))
        {
            return Err(OpsError::unsupported(format!(
                "{what} has the unknown key {key}"
            )));
        }
        Ok(Self { what, fields })
    }

    fn find(&self, key: &str) -> Option<&'v Json> {
        obj_get(self.fields, key)
    }

    fn get(&self, key: &str) -> Result<&'v Json, OpsError> {
        self.find(key)
            .ok_or_else(|| OpsError::unsupported(format!("{} has no {key}", self.what)))
    }

    fn wrong(&self, key: &str, expected: &str) -> OpsError {
        OpsError::unsupported(format!("{}: {key} is not {expected}", self.what))
    }

    fn text(&self, key: &str) -> Result<String, OpsError> {
        match self.get(key)? {
            Json::Str(text) => Ok(text.clone()),
            _ => Err(self.wrong(key, "text")),
        }
    }

    /// Text, or `null` or absent as `None`.
    fn optional_text(&self, key: &str) -> Result<Option<String>, OpsError> {
        match self.find(key) {
            None | Some(Json::Null) => Ok(None),
            Some(Json::Str(text)) => Ok(Some(text.clone())),
            Some(_) => Err(self.wrong(key, "text or null")),
        }
    }

    /// Text when present; `default` when absent.
    fn text_or(&self, key: &str, default: &str) -> Result<String, OpsError> {
        match self.find(key) {
            None => Ok(default.to_string()),
            Some(_) => self.text(key),
        }
    }

    /// An integer when present; `default` when absent.
    fn int_or(&self, key: &str, default: i64) -> Result<i64, OpsError> {
        match self.find(key) {
            None => Ok(default),
            Some(Json::Int(number)) => Ok(*number),
            Some(_) => Err(self.wrong(key, "an integer")),
        }
    }

    fn items(&self, key: &str) -> Result<&'v [Json], OpsError> {
        match self.get(key)? {
            Json::Arr(items) => Ok(items),
            _ => Err(self.wrong(key, "a list")),
        }
    }

    fn texts(&self, key: &str) -> Result<Vec<String>, OpsError> {
        self.items(key)?
            .iter()
            .map(|item| match item {
                Json::Str(text) => Ok(text.clone()),
                _ => Err(self.wrong(key, "a list of text")),
            })
            .collect()
    }

    fn object(&self, key: &str) -> Result<Fields, OpsError> {
        match self.get(key)? {
            Json::Obj(fields) => Ok(fields.clone()),
            _ => Err(self.wrong(key, "an object")),
        }
    }
}

impl WriterOp {
    /// Reads one op of the wire form. Every object has a closed key set, so an extra, missing or mistyped key is a refusal
    /// ([`OpsCause::Failed`] with [`ErrorKind::Unsupported`], not started) that names the key and never quotes a value.
    ///
    /// ```text
    /// {"op": "transition", "task_id": .., "actor": .., "plan": {"from": .., "to": .., "columns": {..},
    ///    "audit": [{"from", "to", "actor", "reason"}], "effects": [{"kind": "live_refresh"}]}}
    /// {"op": "set_notes", "task_id": .., "set": {key: value, ..}}
    /// {"op": "add_artifact", "task_id": .., "kind": .., "uri": .., "version": .., "digest": .., "producer": ..}
    /// {"op": "append_feedback", "task_id": .., "entry": <any>}
    /// {"op": "create_task", "task": {"task_id", "correlation_id", "capability"; optional "title", "parent_id", "dag_depth",
    ///    "inputs", "acceptance", "budget", "risk_class", "priority", "depends_on", "agent_id", "max_attempts", "notes"}}
    /// {"op": "set_depends_on", "task_id": .., "depends_on": [..]}
    /// ```
    ///
    /// An absent optional key of `create_task` means Python's default ([`NewTask::new`]); `null` is accepted only for `parent_id`,
    /// `agent_id`, `notes`, `depends_on` and the three values that follow `value or default`.
    pub fn from_json(value: &Json) -> Result<Self, OpsError> {
        let Json::Obj(fields) = value else {
            return Err(OpsError::unsupported("op is not an object"));
        };
        let Some(Json::Str(name)) = obj_get(fields, "op") else {
            return Err(OpsError::unsupported("op has no text member op"));
        };
        match name.as_str() {
            "transition" => {
                let wire = Wire::new(value, "transition op", &["op", "task_id", "actor", "plan"])?;
                Ok(Self::Transition {
                    task_id: wire.text("task_id")?,
                    actor: wire.text("actor")?,
                    plan: plan_from_json(wire.get("plan")?)?,
                })
            }
            "set_notes" => {
                let wire = Wire::new(value, "set_notes op", &["op", "task_id", "set"])?;
                Ok(Self::SetNotes {
                    task_id: wire.text("task_id")?,
                    set: wire.object("set")?,
                })
            }
            "add_artifact" => {
                let wire = Wire::new(
                    value,
                    "add_artifact op",
                    &[
                        "op", "task_id", "kind", "uri", "version", "digest", "producer",
                    ],
                )?;
                Ok(Self::AddArtifact {
                    task_id: wire.text("task_id")?,
                    kind: wire.text("kind")?,
                    uri: wire.text("uri")?,
                    version: wire.text("version")?,
                    digest: wire.text("digest")?,
                    producer: wire.text("producer")?,
                })
            }
            "append_feedback" => {
                let wire = Wire::new(value, "append_feedback op", &["op", "task_id", "entry"])?;
                Ok(Self::AppendFeedback {
                    task_id: wire.text("task_id")?,
                    entry: wire.get("entry")?.clone(),
                })
            }
            "create_task" => {
                let wire = Wire::new(value, "create_task op", &["op", "task"])?;
                Ok(Self::CreateTask(task_from_json(wire.get("task")?)?))
            }
            "set_depends_on" => {
                let wire = Wire::new(value, "set_depends_on op", &["op", "task_id", "depends_on"])?;
                Ok(Self::SetDependsOn {
                    task_id: wire.text("task_id")?,
                    depends_on: wire.texts("depends_on")?,
                })
            }
            _ => Err(OpsError::unsupported("op names an unknown operation")),
        }
    }

    /// Reads a list of ops of the wire form; a refusal names the position of the op.
    pub fn list_from_json(value: &Json) -> Result<Vec<Self>, OpsError> {
        let Json::Arr(items) = value else {
            return Err(OpsError::unsupported("op list is not a list"));
        };
        items
            .iter()
            .enumerate()
            .map(|(index, item)| Self::from_json(item).map_err(|error| error.at(index)))
            .collect()
    }
}

/// A transition plan of the wire form (`{"from", "to", "columns", "audit", "effects"}`).
fn plan_from_json(value: &Json) -> Result<TransitionPlan, OpsError> {
    let wire = Wire::new(
        value,
        "transition plan",
        &["from", "to", "columns", "audit", "effects"],
    )?;
    let mut audit = Vec::new();
    for row in wire.items("audit")? {
        let row = Wire::new(row, "audit row", &["from", "to", "actor", "reason"])?;
        audit.push(AuditRow {
            from: row.text("from")?,
            to: row.text("to")?,
            actor: row.text("actor")?,
            reason: row.text("reason")?,
        });
    }
    let mut effects = Vec::new();
    for effect in wire.items("effects")? {
        let effect = Wire::new(effect, "plan effect", &["kind"])?;
        match effect.text("kind")?.as_str() {
            "live_refresh" => effects.push(PlanEffect::LiveRefresh),
            _ => return Err(effect.wrong("kind", "a known effect")),
        }
    }
    Ok(TransitionPlan {
        from: wire.text("from")?,
        to: wire.text("to")?,
        columns: wire.object("columns")?,
        audit,
        effects,
    })
}

/// The `task` member of a `create_task` op.
fn task_from_json(value: &Json) -> Result<NewTask, OpsError> {
    let wire = Wire::new(
        value,
        "create_task task",
        &[
            "task_id",
            "correlation_id",
            "capability",
            "title",
            "parent_id",
            "dag_depth",
            "inputs",
            "acceptance",
            "budget",
            "risk_class",
            "priority",
            "depends_on",
            "agent_id",
            "max_attempts",
            "notes",
        ],
    )?;
    let mut task = NewTask::new(
        wire.text("task_id")?,
        wire.text("correlation_id")?,
        wire.text("capability")?,
    );
    task.title = wire.text_or("title", "")?;
    task.parent_id = wire.optional_text("parent_id")?;
    task.dag_depth = wire.int_or("dag_depth", 0)?;
    for (key, slot) in [
        ("inputs", &mut task.inputs),
        ("acceptance", &mut task.acceptance),
        ("budget", &mut task.budget),
    ] {
        if let Some(found) = wire.find(key) {
            *slot = found.clone();
        }
    }
    task.risk_class = wire.text_or("risk_class", "low")?;
    task.priority = wire.text_or("priority", "P2")?;
    match wire.find("depends_on") {
        None | Some(Json::Null) => {}
        Some(_) => task.depends_on = wire.texts("depends_on")?,
    }
    task.agent_id = wire.optional_text("agent_id")?;
    task.max_attempts = wire.int_or("max_attempts", 3)?;
    match wire.find("notes") {
        None | Some(Json::Null) => {}
        Some(_) => task.notes = wire.object("notes")?,
    }
    Ok(task)
}
