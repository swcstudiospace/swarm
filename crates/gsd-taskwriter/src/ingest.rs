//! The host writer side of `orch_status --ingest` (phase 14.5, unit U3.5e part 1): [`TaskStoreWriter::ingest`].
//!
//! One call is `ingest()` of `scripts/orch_status.py` after the result file was read: `apply_result` and `reconcile` of
//! `swarm/results.py`, planned by the pure [`gsd_route::ingest_plan::plan_ingest`] and written here in ONE `BEGIN IMMEDIATE` transaction.
//! The route glue (part 2, `gsd-adapter`) reads the file, parses and validates the result, calls [`TaskStoreWriter::ingest`], replays the
//! returned events after the commit and renders the reply. This module opens no file, reads no clock and prints nothing.
//!
//! # Inventory: what Python reads, in which order, and where the planner's facts come from
//!
//! `ingest()` reads, in this order (the number is the order; `T` is the task, `C` its correlation):
//!
//! 1. the result file (glue), `parse_result`, then `tid = ctx.task_id or result.task_id` (glue; a result with no text id emits
//!    `task.result.rejected` with `task_id: null` and raises `E-CONTRACT`, before any store access: glue);
//! 2. `store.get(tid)`: `SELECT * FROM tasks WHERE task_id=?`, OUTSIDE any lock. A miss is `E-INPUT unknown task <tid>` with
//!    `details.task_id`, no event ([`IngestCause::Refused`]);
//! 3. `validate_result` (glue; its answer is [`IngestRequest::result`]), then `agent_id = result.agent_id or T.agent_id or "agent"`
//!    ([`gsd_route::ingest_plan::ingest_agent_id`], from the row read here);
//! 4. `apply_result`: for an IN_REVIEW result of a gate task (`T.notes.gate` truthy) `_gate_result_error`: `store.history(T)`
//!    (`transitions WHERE task_id=? ORDER BY id`, the `ts` of the rows entering CLAIMED: the lease start) and, per target of
//!    `notes.gate_for`, `gate_verdict_value_since` (`SELECT envelope_json FROM verdicts WHERE task_id=target AND gate=? ORDER BY id
//!    DESC`, every row verified with `validate_verdict`); for a PLANNED or RETRY task `deps_satisfied` (`store.list(C)`); then the
//!    fresh `store.get(T)` under the write lock (`notes.ingest_keys`, `attempt`, `state`);
//! 5. `reconcile(C)`: `store.list(C, state=IN_REVIEW)`; per such task `latest_verdicts(verified_only=True)` (`tasks` row,
//!    `SELECT * FROM verdicts WHERE task_id=? AND ts>=notes.verdicts_since ORDER BY id`, `json.loads(findings)` of EVERY row, then
//!    `_check_row` of the highest-id row of each gate: `validate_verdict`, which verifies the signature), `required_gates`, `store.list(C)`
//!    for the leased gate tasks, `missing_gate_reasons`, `unaccepted_gates` (`store.get(issuer)` for each `gate_task` a passing row
//!    names, in or outside `C`), `_stalled_gates`, the rework (`store.list(C)`, `store.create` of a rerun id, which must not exist
//!    anywhere in the store);
//! 6. `store.get(tid)` for the reply.
//!
//! The snapshot [`TaskStoreWriter::ingest`] takes inside the transaction, as the planner's [`IngestFacts`]:
//!
//! | planner fact | read from | order / filter |
//! |---|---|---|
//! | the task and `reconcile.tasks` | `tasks`, every row with `correlation_id = C`, columns as stored (JSON still text) | `rowid` ([`Tx::task_rows_of_correlation`]) |
//! | `reconcile.other_tasks` | `tasks.task_id, state` with another correlation | `rowid` ([`Tx::task_states_outside`]) |
//! | `claims` | `transitions.ts` of the rows of T with `to_state = 'CLAIMED'` | `id` ([`Tx::claim_stamps`]) |
//! | `reconcile.verdicts`, reconcile's rows | `verdicts` of each task of C that is IN_REVIEW now, and of T when it is IN_REVIEW now or the result reports IN_REVIEW, with `ts >= notes.verdicts_since` | `id`; `findings` parsed on every row, a bad one declines the call ([`Tx::verdict_rows`]) |
//! | `reconcile.verdicts`, the gate check's rows | `verdicts` of each target of `T.notes.gate_for`, `gate = T.notes.gate`, any `ts` | `id` ([`Tx::gate_verdict_rows`]) |
//! | `sig_ok` of a row | the host's `verify_envelope` with this call's keys, for the highest-id row of each gate of a reconcile task and for every row of a gate check | [`ApprovalKeys::row_sig_ok`](crate::approval) |
//! | `reconcile.key_policy` | `SWARM_REQUIRE_KEY`, `real_key_configured()`, `insecure_dev_key()` from the same key snapshot | [`ApprovalKeys`] |
//! | `agent_id`, `digest` | the row's `agent_id` and the result; the digest is `sha256` of the compact key-sorted JSON of the result (`result_digest`), computed here so a caller cannot pass another | |
//! | `result`, `meta`, `advisory`, `mode` | the request ([`IngestRequest`]); the mode is always ingest (the headless runner keeps its Python path) | |
//!
//! Every row is read AFTER the lock was taken, in one transaction, so the task row, the correlation, the claims and the verdict rows are
//! one snapshot (Python reads some of them before the lock; the difference is visible only to a concurrent writer).
//!
//! # The plan, the op list and what is replayed
//!
//! `plan_ingest` returns an ordered [`IngestOp`] list. Every op except [`IngestOp::Emit`] is a [`WriterOp`] (same order, same values; a
//! `CreateTask` is mapped field by field and refused, not started, when a cloned column is not text or `dag_depth` is `NULL`); the list is
//! applied by [`TaskStoreWriter::apply_ops_approving`]'s own path with this call's guard, in the one transaction. The `Emit`s are returned
//! in [`IngestApplied::events`], in list order, for the glue to replay AFTER the commit with its own `emit`.
//!
//! `task.result.rejected` is not a store write: `reject(...)` in `swarm/results.py` only calls `emit` (headless adds a `FAILED`
//! transition, which this route never takes). A refusal therefore writes nothing: the planner returns the events Python emitted before it
//! raised ([`IngestError::events`]), and the transaction is rolled back with nothing in it.
//!
//! # What the glue must do with the answer (part 2)
//!
//! * `Ok(applied)`: replay `applied.events` in order, then `emit_inline_transition(store, applied.task, applied.before, applied.after)`,
//!   then the live refresh once if `applied.applied.effects` is not empty; the reply is `{"status": "ok", "task": applied.task,
//!   "reconcile": applied.notes_log, "summary": "ingested task.result for <tid> → <applied.reply> (now <task.state>)"}`.
//! * `Err(e)` with [`IngestError::write_state`] `NotStarted`: nothing is committed and the store is byte-identical; reply `E-DEP` with
//!   `details.write_state = "not_started"` ([`IngestError::to_seam_error`] has the code and message) and let Python answer. This is what
//!   a planner `NotStarted`, a [`Declined`](gsd_envelope::Declined) key shape, a busy store, a refused APPROVED and an invalid op list
//!   all look like.
//! * `Err(e)` with `Final` and [`IngestCause::Refused`]: Python's own refusal; replay [`IngestError::events`] (the
//!   `task.result.rejected` it emitted), then print [`IngestError::to_seam_error`] as is. The store is unchanged.
//! * `Err(e)` with `Final` and another cause: a database failure at or after the first write (or a failed commit). Nothing is ever
//!   re-run; the glue prints `E-INTERNAL` as the transition route does.
//!
//! # Keys
//!
//! [`ApprovalSource::keys`] is called ONCE per call, before the transaction opens, for EVERY call, `--advisory` included. The mode
//! that can approve is not the only one that verifies a signature: an IN_REVIEW result of a gate task checks the targets' rows with
//! `validate_verdict` whatever `--advisory` says. A key shape Python reads from a file ([`Declined`](gsd_envelope::Declined)) therefore
//! makes every ingest not started, as it does for the APPROVED transition. Keys never leave [`ApprovalKeys`]; no error, `Debug` or
//! returned value holds one.
//!
//! # APPROVED (security review F-7)
//!
//! The planner decides an approval from the facts above, and the host does not take its word. The op list goes through the same
//! `resolve_and_write` as every list, with an `ApprovalGuard` (see `ops.rs`) unless `--advisory`: before each APPROVED step is written
//! the writer re-reads the task row and the gate facts INSIDE the transaction, after the earlier steps of the list, with the
//! `gather` of the transition route, runs `decide_transition` on them and requires the plan to be the planner's. A gate task that
//! this very list moved to IN_REVIEW is therefore seen as accepted, and a verdict that expired while the call waited for the lock is
//! expired (one clock reading, [`ApprovalSource::clock`], after the rows). A difference refuses the whole list as
//! [`OpsCause::ApprovalRefused`], not started, rolled back. With `--advisory` there is no guard: a list that reaches APPROVED is refused
//! in phase one.
//!
//! # Idempotency (design item 11, D-P24 (3))
//!
//! `notes.ingest_keys` is the planner's LAST write (a `SetNotes` after the transition, the notes, the artifact rows and the feedback). A
//! replayed `(task, attempt, digest)` whose move the state machine still accepts plans no store write, only the
//! `task.result.deduplicated` event, and its reply is the stored one ([`IngestApplied::replayed`]). A replay the state machine refuses
//! (`IN_REVIEW` to `IN_REVIEW`, anything from DONE) is not deduplicated: it is Python's own `E-CONTRACT illegal transition`, with the
//! rejection event, as before. Two concurrent calls with one result serialize on the write lock; the second one reads the first one's
//! commit and is a replay.

use std::fmt;

use gsd_codes::ErrorCode;
use gsd_envelope::compact_json;
use gsd_route::gate_facts::{IssuerState, TaskFacts, VerdictFacts, VerdictRow};
use gsd_route::ingest_plan::{
    ingest_agent_id, plan_ingest, IngestError as PlanError, IngestFacts, IngestOp, IngestPlan, Mode,
};
use gsd_route::plan_derive::sha256_hex;
use gsd_route::reconcile_plan::{EmitEvent, KeyPolicy, NewTask as RouteNewTask, ReconcileFacts};
use gsd_route::result_parse::ResultError;
use gsd_route::{jstr, obj_get, parse_json, Fields, Json, SeamError};

use crate::approval::{self, ApprovalKeys, ApprovalSource};
use crate::error::{ErrorKind, WriteState, WriterError};
use crate::ops::{
    resolve_and_write, ApprovalGuard, NewTask, OpsApplied, OpsCause, OpsError, WriterOp,
};
use crate::store::Tx;
use crate::TaskStoreWriter;

/// One `--ingest` call, after the glue read the file.
#[derive(Debug, Clone)]
pub struct IngestRequest {
    /// `tid`: `ctx.task_id` or the result's `task_id`; the task the result is for.
    pub task_id: String,
    /// The validated `task.result` (`parse_result` then `validate_result` of [`gsd_route::result_parse`]), or why it is not one. An invalid
    /// result is Python's `E-CONTRACT` refusal with a `task.result.rejected` event; a [`ResultError::NotStarted`] is a call Python answers.
    pub result: Result<Json, ResultError>,
    /// The `meta` stored beside the result: `{"ingest": str(path)}`.
    pub meta: Json,
    /// `--advisory`: `reconcile(approve=False)`; no task is approved.
    pub advisory: bool,
}

/// A committed ingest.
#[derive(Debug, Clone, PartialEq)]
pub struct IngestApplied {
    /// What `apply_result` returned: the task's state after the result (the stored reply for a replay).
    pub reply: String,
    /// The list `reconcile` returned, for `"reconcile"` in the reply.
    pub notes_log: Vec<String>,
    /// The task's state before the call: the `from` of the inline transition.
    pub before: String,
    /// The task's state after `reconcile`: the `to` of the inline transition.
    pub after: String,
    /// `store.get(tid)` once everything is written, as `TaskStore.get` returns it (JSON columns parsed, `notes_json` last).
    pub task: Fields,
    /// Every `emit` call of the run, in order; replay them after the commit.
    pub events: Vec<EmitEvent>,
    /// The rows the op list touched, the audit rows it wrote and the post-commit effects of its transitions (the live refresh).
    pub applied: OpsApplied,
}

impl IngestApplied {
    /// Whether the result was a replay of an earlier application: nothing was written for it.
    pub fn replayed(&self) -> bool {
        self.events
            .iter()
            .any(|event| event.event_type == "task.result.deduplicated")
    }
}

/// Which stage of the call failed.
#[derive(Debug, Clone, PartialEq)]
pub enum IngestCause {
    /// The planner, a key shape or a stored value is outside what Rust proves identical to Python: Python answers. Nothing is committed.
    Unproven,
    /// Python's own refusal (`E-CONTRACT`, `E-INPUT`): the answer to print. Nothing is committed.
    Refused,
    /// The op list failed in the writer (an APPROVED the host refused, an invalid plan, a database failure): see [`OpsCause`].
    Ops(OpsCause),
    /// A failure of the store around the transaction (busy, unreadable row, failed commit): see [`ErrorKind`].
    Store(ErrorKind),
}

/// A failed ingest: the answer, whether Python may still answer, and the events Python emitted before it raised.
#[derive(Debug, Clone, PartialEq)]
pub struct IngestError {
    cause: Box<IngestCause>,
    error: Box<SeamError>,
    write_state: WriteState,
    events: Vec<EmitEvent>,
}

impl IngestError {
    fn unproven(why: impl AsRef<str>) -> Self {
        Self {
            cause: Box::new(IngestCause::Unproven),
            error: Box::new(SeamError::new(
                ErrorCode::Dep,
                format!("ingest: Python answers this call ({})", why.as_ref()),
            )),
            write_state: WriteState::NotStarted,
            events: Vec::new(),
        }
    }

    fn store(error: &WriterError) -> Self {
        Self {
            cause: Box::new(IngestCause::Store(error.kind())),
            error: Box::new(error.to_seam_error()),
            write_state: error.write_state(),
            events: Vec::new(),
        }
    }

    fn ops(error: &OpsError) -> Self {
        Self {
            cause: Box::new(IngestCause::Ops(error.cause().clone())),
            error: Box::new(error.to_seam_error()),
            write_state: error.write_state(),
            events: Vec::new(),
        }
    }

    /// `ingest()`'s `E-INPUT unknown task <tid>` with `details.task_id`; no event.
    fn unknown_task(task_id: &str) -> Self {
        Self {
            cause: Box::new(IngestCause::Refused),
            error: Box::new(
                SeamError::new(ErrorCode::Input, format!("unknown task {task_id}"))
                    .with_details(vec![("task_id".to_string(), jstr(task_id))]),
            ),
            write_state: WriteState::Final,
            events: Vec::new(),
        }
    }

    fn from_plan(error: PlanError) -> Self {
        match error {
            PlanError::NotStarted(why) => Self::unproven(why),
            PlanError::Refused(refusal) => Self {
                cause: Box::new(IngestCause::Refused),
                error: Box::new(refusal.error),
                write_state: WriteState::Final,
                events: refusal.events,
            },
        }
    }

    /// Which stage failed.
    pub fn cause(&self) -> &IngestCause {
        &self.cause
    }

    /// The taxonomy code.
    pub fn code(&self) -> ErrorCode {
        self.error.code()
    }

    /// The message with its `CODE: ` prefix, as `str(SwarmError)` prints it.
    pub fn message(&self) -> &str {
        self.error.message()
    }

    /// `SwarmError.details` (`task_id` for a refusal on an existing task; empty otherwise).
    pub fn details(&self) -> &[(String, Json)] {
        self.error.details()
    }

    /// The seam error to print; the caller adds `write_state` to its details when it renders a not-started one.
    pub fn to_seam_error(&self) -> SeamError {
        (*self.error).clone()
    }

    /// Whether Python may still answer. [`WriteState::NotStarted`] means the store is byte-identical to before the call: whatever the
    /// transaction wrote before the failure was rolled back.
    pub fn write_state(&self) -> WriteState {
        self.write_state
    }

    /// The events Python emitted before it raised (`task.result.rejected` for an `E-CONTRACT`), to replay in order. Empty for every
    /// other failure.
    pub fn events(&self) -> &[EmitEvent] {
        &self.events
    }
}

impl fmt::Display for IngestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.error.message())
    }
}

impl std::error::Error for IngestError {}

impl TaskStoreWriter {
    /// `orch_status.ingest` for one result, in one `BEGIN IMMEDIATE` transaction: the auto-claim, the reported transition, the notes,
    /// the artifact rows, the gate-target feedback, the replay key and the whole of `reconcile`, or nothing.
    ///
    /// `now` is the single clock reading of the call (every `updated_at`, `ts` and `created_at`); it must be finite. `source` gives the
    /// keys once, before the transaction (see the module documentation: for every call), and the clock a verdict's expiry is compared
    /// with, read inside it. On any error the transaction is rolled back; an [`IngestError::write_state`] of
    /// [`WriteState::NotStarted`] left the store byte-identical.
    ///
    /// # Errors
    ///
    /// [`IngestError`]; see the module documentation for what the glue does with each kind.
    pub async fn ingest(
        &self,
        request: &IngestRequest,
        now: f64,
        source: &dyn ApprovalSource,
    ) -> Result<IngestApplied, IngestError> {
        if !now.is_finite() {
            return Err(IngestError::unproven("now is not a finite number"));
        }
        let keys = source
            .keys()
            .map_err(|declined| IngestError::store(&approval::declined(declined)))?;
        let mut tx = self
            .store()
            .begin()
            .await
            .map_err(|error| IngestError::store(&error))?;
        match run(&mut tx, request, now, source, &keys).await {
            Ok(applied) => {
                tx.commit()
                    .await
                    .map_err(|error| IngestError::store(&error))?;
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

/// Everything between `BEGIN IMMEDIATE` and `COMMIT`.
async fn run(
    tx: &mut Tx,
    request: &IngestRequest,
    now: f64,
    source: &dyn ApprovalSource,
    keys: &ApprovalKeys,
) -> Result<IngestApplied, IngestError> {
    let facts = snapshot(tx, request, keys).await?;
    let IngestPlan {
        ops,
        reply,
        notes_log,
        before,
        after,
    } = plan_ingest(&facts, now).map_err(IngestError::from_plan)?;
    let (writer_ops, events) = writer_ops(ops)?;
    // `--advisory` approves nothing: no guard, so a list that reaches APPROVED is refused before its first write.
    let guard = (!request.advisory).then(|| ApprovalGuard::new(source, keys));
    let applied = resolve_and_write(tx, &writer_ops, now, guard.as_ref())
        .await
        .map_err(|error| IngestError::ops(&error))?;
    let task = tx
        .task(&request.task_id)
        .await
        .map_err(|error| IngestError::store(&error))?
        .ok_or_else(|| {
            IngestError::store(&WriterError::final_error(
                ErrorKind::Database,
                ErrorCode::Internal,
                format!(
                    "task {} vanished inside its own transaction",
                    request.task_id
                ),
            ))
        })?;
    Ok(IngestApplied {
        reply,
        notes_log,
        before,
        after,
        task,
        events,
        applied,
    })
}

/// The text of a column of a raw row; `None` for `NULL`, a decline for anything but text.
fn text_of<'a>(row: &'a Fields, name: &str) -> Result<Option<&'a str>, IngestError> {
    match obj_get(row, name) {
        Some(Json::Str(text)) => Ok(Some(text)),
        Some(Json::Null) | None => Ok(None),
        Some(_) => Err(IngestError::unproven(format!("column {name} is not text"))),
    }
}

/// The notes object of a raw row when it is readable JSON; the planner refuses a row it cannot read by itself.
fn notes_of(row: &Fields) -> Option<Fields> {
    match obj_get(row, "notes") {
        Some(Json::Str(text)) => match parse_json(text) {
            Ok(Json::Obj(fields)) => Some(fields),
            _ => None,
        },
        _ => None,
    }
}

/// Reads every fact the planner needs, inside the caller's transaction, and verifies the verdict rows it may read.
async fn snapshot(
    tx: &mut Tx,
    request: &IngestRequest,
    keys: &ApprovalKeys,
) -> Result<IngestFacts, IngestError> {
    let store = |error: WriterError| IngestError::store(&error);
    let task_id = request.task_id.as_str();
    let Some(correlation) = tx.correlation_of(task_id).await.map_err(store)? else {
        return Err(IngestError::unknown_task(task_id));
    };
    let tasks = tx
        .task_rows_of_correlation(&correlation)
        .await
        .map_err(store)?;
    let outside = tx.task_states_outside(&correlation).await.map_err(store)?;
    let claims = tx.claim_stamps(task_id).await.map_err(store)?;
    let Some(own) = tasks
        .iter()
        .find(|row| matches!(obj_get(row, "task_id"), Some(Json::Str(id)) if id == task_id))
    else {
        return Err(IngestError::unproven(
            "the task is not in its own correlation",
        ));
    };
    let agent_id = match &request.result {
        Ok(result) => ingest_agent_id(result, own).map_err(IngestError::from_plan)?,
        Err(_) => "agent".to_string(),
    };
    let reports_review = matches!(&request.result, Ok(Json::Obj(fields))
        if matches!(obj_get(fields, "state"), Some(Json::Str(state)) if state == "IN_REVIEW"));

    // The rows `reconcile` reads: the highest-id row of each gate, from `notes.verdicts_since`, of every task that is IN_REVIEW when it
    // runs. That is every IN_REVIEW task of the correlation now, and this one when its result reports IN_REVIEW.
    let mut rows: Vec<VerdictRow> = Vec::new();
    let mut verify: Vec<i64> = Vec::new();
    for row in &tasks {
        let Some(id) = text_of(row, "task_id")? else {
            return Err(IngestError::unproven("a task without an id"));
        };
        let in_review = text_of(row, "state")? == Some("IN_REVIEW");
        if !(in_review || (id == task_id && reports_review)) {
            continue;
        }
        let target = TaskFacts::from_row(
            id,
            &correlation,
            text_of(row, "risk_class")?,
            text_of(row, "notes")?,
        )
        .map_err(|unsupported| IngestError::unproven(unsupported.to_string()))?;
        let found = tx
            .verdict_rows(id, target.verdicts_since)
            .await
            .map_err(store)?;
        let mut newest: Vec<(&str, i64)> = Vec::new();
        for row in &found {
            match newest.iter_mut().find(|(gate, _)| *gate == row.gate) {
                Some(slot) => slot.1 = slot.1.max(row.id),
                None => newest.push((&row.gate, row.id)),
            }
        }
        verify.extend(newest.iter().map(|(_, id)| *id));
        rows.extend(found);
    }

    // The rows the gate check of a gate task's IN_REVIEW result reads: every row of `(target, gate)`, whatever its `ts`.
    if reports_review {
        let notes = notes_of(own).unwrap_or_default();
        if let (Some(Json::Str(gate)), Some(Json::Arr(targets))) =
            (obj_get(&notes, "gate"), obj_get(&notes, "gate_for"))
        {
            for target in targets {
                let Json::Str(target) = target else {
                    continue;
                };
                for row in tx.gate_verdict_rows(target, gate).await.map_err(store)? {
                    verify.push(row.id);
                    if !rows.iter().any(|known| known.id == row.id) {
                        rows.push(row);
                    }
                }
            }
        }
    }

    let mut verdicts = Vec::with_capacity(rows.len());
    for row in rows {
        let checked = verify.contains(&row.id);
        let mut entry = VerdictFacts::new(row, false);
        if checked {
            entry.sig_ok = keys.row_sig_ok(&entry).map_err(store)?;
        }
        verdicts.push(entry);
    }

    let digest = match &request.result {
        Ok(result) => Some(sha256_hex(&compact_json(result))),
        Err(_) => None,
    };
    Ok(IngestFacts {
        mode: Mode::Ingest,
        task_id: task_id.to_string(),
        agent_id,
        result: request.result.clone(),
        meta: request.meta.clone(),
        digest,
        advisory: request.advisory,
        claims,
        reconcile: ReconcileFacts {
            correlation_id: correlation,
            tasks,
            verdicts,
            other_tasks: outside
                .into_iter()
                .map(|(task_id, state)| IssuerState { task_id, state })
                .collect(),
            key_policy: Some(KeyPolicy {
                require_key: keys.require_key(),
                real_key: keys.real_key(),
                dev_key: keys.dev_key(),
            }),
        },
    })
}

/// The planner's ops as the writer's: the same order, the `Emit`s taken out for the caller to replay.
fn writer_ops(ops: Vec<IngestOp>) -> Result<(Vec<WriterOp>, Vec<EmitEvent>), IngestError> {
    let mut writes = Vec::with_capacity(ops.len());
    let mut events = Vec::new();
    for op in ops {
        writes.push(match op {
            IngestOp::Transition { task_id, plan } => WriterOp::Transition {
                task_id,
                // Every audit row of a plan is by one actor (`PlanValidator` checks it); an empty plan fails the validator.
                actor: plan
                    .audit
                    .first()
                    .map(|row| row.actor.clone())
                    .unwrap_or_default(),
                plan,
            },
            IngestOp::SetNotes { task_id, entries } => WriterOp::SetNotes {
                task_id,
                set: entries,
            },
            IngestOp::AddArtifact(artifact) => WriterOp::AddArtifact {
                task_id: artifact.task_id,
                kind: artifact.kind,
                uri: artifact.uri,
                version: artifact.version,
                digest: artifact.digest,
                producer: artifact.producer,
            },
            IngestOp::AppendFeedback { task_id, entry } => {
                WriterOp::AppendFeedback { task_id, entry }
            }
            IngestOp::CreateTask(task) => WriterOp::CreateTask(new_task(&task)?),
            IngestOp::SetDependsOn {
                task_id,
                depends_on,
            } => WriterOp::SetDependsOn {
                task_id,
                depends_on,
            },
            IngestOp::Emit(event) => {
                events.push(event);
                continue;
            }
        });
    }
    Ok((writes, events))
}

/// The planner's rerun as `TaskStore.create` takes it; a cloned value the writer cannot store as Python would is a decline.
fn new_task(task: &RouteNewTask) -> Result<NewTask, IngestError> {
    let text = |value: &Json, what: &str| match value {
        Json::Str(text) => Ok(text.clone()),
        _ => Err(IngestError::unproven(format!(
            "a rerun's {what} is not text"
        ))),
    };
    let mut row = NewTask::new(
        task.task_id.clone(),
        task.correlation_id.clone(),
        text(&task.capability, "capability")?,
    );
    row.title = task.title.clone();
    row.dag_depth = task
        .dag_depth
        .ok_or_else(|| IngestError::unproven("a rerun of a task with no dag_depth"))?;
    row.depends_on = task.depends_on.clone();
    row.acceptance = task.acceptance.clone();
    row.budget = task.budget.clone();
    row.risk_class = text(&task.risk_class, "risk_class")?;
    row.priority = text(&task.priority, "priority")?;
    row.agent_id = match &task.agent_id {
        Json::Null => None,
        other => Some(text(other, "agent_id")?),
    };
    row.notes = task.notes.clone();
    Ok(row)
}
