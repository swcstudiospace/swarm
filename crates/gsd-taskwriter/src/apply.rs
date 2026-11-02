//! `TaskStore.transition` on the write side: read inside the lock, decide, validate, apply, commit.
//!
//! [`TaskStoreWriter::transition`] is one function from `BEGIN IMMEDIATE` to `COMMIT`:
//!
//! 1. `BEGIN IMMEDIATE` (the write lock; no other writer can change the row from here to the commit);
//! 2. read the task row as `TaskStore.get` returns it;
//! 3. call the [`Decider`] once, with the row and the request. The decider is the only pluggable step: the host runs
//!    [`HostDecider`] (`decide_transition`), the sandbox path will run a guest and fall back to the host function, both
//!    before anything is written;
//! 4. validate the plan with [`PlanValidator`]; a plan that fails is refused with nothing written;
//! 5. apply it as `TaskStore._apply` does: one conditional `UPDATE ... WHERE task_id = ? AND state = ?` and one audit
//!    `INSERT` per audit row (an auto-escalation is two audited steps, as in Python), `rowcount != 1` being
//!    `E-CONTRACT concurrent transition`;
//! 6. read the row back, `COMMIT`, and return it with the audit rows and the post-commit effects.
//!
//! Every failure before the `COMMIT` rolls the transaction back. Time: one `now` is `updated_at` and every audit `ts`.
//!
//! Failures keep their kind apart in [`ApplyCause`]: a refusal of the decision carries Python's code, message and
//! `details`; an invalid plan is `E-INTERNAL` with [`WriteState::NotStarted`], which the sandbox path treats as a fault of
//! the guest and answers with the host decision; store failures keep the codes of [`WriterError`].

use std::fmt;
use std::sync::Arc;

use gsd_codes::ErrorCode;
use gsd_route::pydumps::to_python_dumps;
use gsd_route::transition_plan::{
    decide_transition, legal_targets, Facts, PlanEffect, Request, TransitionError, TransitionPlan,
    WriteState as DecisionWriteState,
};
use gsd_route::{jstr, obj_get, Fields, Json, SeamError};

use crate::approval::{self, ApprovalKeys, ApprovalSource, ApprovalVerifier};
use crate::error::{ErrorKind, WriteState, WriterError};
use crate::plan::{PlanValidator, PlanViolation, ValidatedPlan};
use crate::store::{PlanColumn, StateChange, Tx, WriteStore};

/// The JSON text columns of `tasks`. [`Facts`] holds them as stored (text); the store's Python-shaped row has them parsed.
const JSON_COLUMNS: [&str; 5] = ["inputs", "outputs", "acceptance", "budget", "depends_on"];

/// The decision step of a transition: pure, runs inside the write lock, and writes nothing.
///
/// `facts.task` is the row read inside the lock (`None` when the task does not exist); `facts.approval` holds the gate facts the
/// host read in the same transaction for a legal APPROVED target ([`crate::approval`]), and is `None` for every other transition and
/// for a writer with no approval source, which makes a legal APPROVED a not-started refusal. The writer calls the
/// decider exactly once per transition. Whatever it returns is checked by [`PlanValidator`] before it is written.
///
/// The call is synchronous and runs while the write lock is held, so an implementation must be bounded in time.
pub trait Decider: Send + Sync {
    /// Decides the transition on this snapshot.
    fn decide(
        &self,
        facts: &Facts,
        request: &Request,
        now: f64,
    ) -> Result<TransitionPlan, TransitionError>;
}

/// The host decision: `gsd_route::transition_plan::decide_transition`.
#[derive(Debug, Clone, Copy, Default)]
pub struct HostDecider;

impl Decider for HostDecider {
    fn decide(
        &self,
        facts: &Facts,
        request: &Request,
        now: f64,
    ) -> Result<TransitionPlan, TransitionError> {
        decide_transition(facts, request, now)
    }
}

/// Which stage of the transition failed.
#[derive(Debug, Clone, PartialEq)]
pub enum ApplyCause {
    /// The decider refused: Python's own answer (`Final`) or a marker that Python must answer (`NotStarted`).
    Refused,
    /// The decider's plan failed validation. Nothing was written.
    InvalidPlan(PlanViolation),
    /// The store failed (see [`ErrorKind`]).
    Store(ErrorKind),
    /// The approval verifier did not finish in time: the host's answer was never used and nothing was written.
    Verification,
}

/// A failed transition: code, message, `details` and whether anything was written.
#[derive(Debug, Clone, PartialEq)]
pub struct ApplyError {
    cause: ApplyCause,
    code: ErrorCode,
    message: String,
    details: Fields,
    write_state: WriteState,
}

impl ApplyError {
    /// A refusal of the decision: Python's code, message (without the `CODE: ` prefix) and `details`.
    fn refused(refusal: TransitionError) -> Self {
        let prefix = format!("{}: ", refusal.code.as_str());
        let message = match refusal.message.strip_prefix(&prefix) {
            Some(text) => text.to_string(),
            None => refusal.message,
        };
        Self {
            cause: ApplyCause::Refused,
            code: refusal.code,
            message,
            details: refusal.details,
            write_state: match refusal.write_state {
                DecisionWriteState::NotStarted => WriteState::NotStarted,
                DecisionWriteState::Final => WriteState::Final,
            },
        }
    }

    /// A faulty decider: `E-INTERNAL`, nothing written.
    fn invalid_plan(violation: PlanViolation, task_id: &str) -> Self {
        Self {
            code: ErrorCode::Internal,
            message: format!("invalid transition plan for {task_id}: {violation}"),
            details: vec![("task_id".to_string(), jstr(task_id))],
            write_state: WriteState::NotStarted,
            cause: ApplyCause::InvalidPlan(violation),
        }
    }

    /// The approval verifier did not finish (`E-TIMEOUT`): nothing written, the call ends with `code` and its `write_state` is final.
    fn verification(code: ErrorCode, task_id: &str) -> Self {
        Self {
            cause: ApplyCause::Verification,
            code,
            message: format!(
                "the approval check of {task_id} did not finish within the call deadline"
            ),
            details: vec![("task_id".to_string(), jstr(task_id))],
            write_state: WriteState::Final,
        }
    }

    /// A store failure. `concurrent transition` carries `task_id` in `details`, as Python's `SwarmError` does.
    fn store(error: &WriterError, task_id: &str) -> Self {
        let details = match error.kind() {
            ErrorKind::ConcurrentTransition => vec![("task_id".to_string(), jstr(task_id))],
            _ => Vec::new(),
        };
        Self {
            cause: ApplyCause::Store(error.kind()),
            code: error.code(),
            message: error.message().to_string(),
            details,
            write_state: error.write_state(),
        }
    }

    /// Which stage failed.
    pub fn cause(&self) -> &ApplyCause {
        &self.cause
    }

    /// The taxonomy code.
    pub fn code(&self) -> ErrorCode {
        self.code
    }

    /// The message without the `CODE: ` prefix, as `SwarmError` keeps it.
    pub fn message(&self) -> &str {
        &self.message
    }

    /// `SwarmError.details`: `task_id` for a refusal on an existing task, a violated plan or a concurrent transition.
    pub fn details(&self) -> &[(String, Json)] {
        &self.details
    }

    /// Whether the request may still be answered by Python.
    pub fn write_state(&self) -> WriteState {
        self.write_state
    }

    /// The seam error with the `CODE: ` message prefix and the details; the caller adds `write_state` when it renders one.
    pub fn to_seam_error(&self) -> SeamError {
        SeamError::new(self.code, &self.message).with_details(self.details.clone())
    }
}

impl fmt::Display for ApplyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code.as_str(), self.message)
    }
}

impl std::error::Error for ApplyError {}

/// One audit row as written: the `transitions` row without its `id`.
#[derive(Debug, Clone, PartialEq)]
pub struct AppliedAudit {
    /// `from_state`.
    pub from: String,
    /// `to_state`.
    pub to: String,
    /// `actor`.
    pub actor: String,
    /// `reason`.
    pub reason: String,
    /// `ts`: the `now` of the call.
    pub ts: f64,
}

/// A committed transition.
#[derive(Debug, Clone, PartialEq)]
pub struct Applied {
    /// The task row after the commit, as `TaskStore.get` returns it (what `TaskStore.transition` returns).
    pub task: Fields,
    /// The audit rows written, in insertion order.
    pub audit: Vec<AppliedAudit>,
    /// Effects for the caller to run after the commit.
    pub effects: Vec<PlanEffect>,
}

/// The Task Store writer: transitions applied through [`TaskStoreWriter::transition`].
#[derive(Debug)]
pub struct TaskStoreWriter {
    store: WriteStore,
    approval: Option<Arc<dyn ApprovalSource>>,
}

impl TaskStoreWriter {
    /// A writer over an open store with no approval source: a legal APPROVED is a not-started refusal (Python answers), as it was
    /// before the host could read the gate facts. Every caller that never transitions to `APPROVED` is unaffected.
    pub fn new(store: WriteStore) -> Self {
        Self {
            store,
            approval: None,
        }
    }

    /// A writer that serves a transition to `APPROVED`: keys come from `approval` once per call before the transaction opens, and
    /// the gate facts are read inside it (see [`crate::approval`]).
    pub fn with_approval(store: WriteStore, approval: Arc<dyn ApprovalSource>) -> Self {
        Self {
            store,
            approval: Some(approval),
        }
    }

    /// The store underneath, for a caller that also reads or arms fault points.
    pub fn store(&self) -> &WriteStore {
        &self.store
    }

    /// Closes the store.
    pub async fn close(self) {
        self.store.close().await;
    }

    /// `TaskStore.transition`: decides and applies one transition in one `BEGIN IMMEDIATE` transaction.
    ///
    /// `now` is the single clock reading of the call (`updated_at` and every audit `ts`); it must be finite. The expiry of a verdict
    /// of an APPROVED transition is NOT compared with it: that reading was taken before the wait for the write lock, so it is compared
    /// with [`ApprovalSource::clock`], read inside the lock (see [`crate::approval`]). The decider is called exactly once, after the
    /// row is read and before any write. On any error the transaction is rolled back; an error whose [`ApplyError::write_state`] is
    /// [`WriteState::NotStarted`] left the store byte-identical.
    pub async fn transition(
        &self,
        request: &Request,
        now: f64,
        decider: &dyn Decider,
    ) -> Result<Applied, ApplyError> {
        self.transition_with_verifier(request, now, decider, None)
            .await
    }

    /// [`Self::transition`] with the approval verifier of a sandboxed call (phase 14.5, pin P2). `None` is the host path.
    ///
    /// The verifier, when given, checks the host's gate answer of an APPROVED call once its facts were read: inside the lock, after the
    /// rows and before the decider. Its `E-TIMEOUT` ends the call with nothing written, as a decider timeout does.
    pub async fn transition_with_verifier(
        &self,
        request: &Request,
        now: f64,
        decider: &dyn Decider,
        verifier: Option<&dyn ApprovalVerifier>,
    ) -> Result<Applied, ApplyError> {
        let task_id = request.task_id.as_str();
        if !now.is_finite() {
            return Err(ApplyError::store(
                &WriterError::not_started(ErrorKind::Unsupported, "now is not a finite number"),
                task_id,
            ));
        }
        // The keys are read once per call and before the lock: a key-file reference or an unsupported shape refuses here, with
        // nothing opened and nothing written, so Python answers (D-P24 (2)). Only an APPROVED target reads any key.
        let keys =
            match (&self.approval, request.to == "APPROVED") {
                (Some(source), true) => Some(source.keys().map_err(|declined| {
                    ApplyError::store(&approval::declined(declined), task_id)
                })?),
                _ => None,
            };
        let mut tx = self
            .store
            .begin()
            .await
            .map_err(|error| ApplyError::store(&error, task_id))?;
        let approval = self.approval.as_deref().zip(keys.as_ref());
        match decide_and_apply(&mut tx, request, now, decider, approval, verifier).await {
            Ok(applied) => {
                tx.commit()
                    .await
                    .map_err(|error| ApplyError::store(&error, task_id))?;
                Ok(applied)
            }
            Err(error) => {
                // The failure above is the answer. A rollback that fails too leaves a connection sqlx discards, and the lock
                // is released with it.
                tx.rollback().await.ok();
                Err(error)
            }
        }
    }
}

/// Steps 2 to 6 of the module documentation, up to but not including the `COMMIT`.
///
/// For a legal transition to `APPROVED` with an approval source and its keys in hand, the approval facts are read here, on `tx`, after
/// the task row and before the decider: the row, the verdict rows, the clock and the gate tasks are one snapshot under the write lock.
/// Any other transition, an illegal one included, reads no verdict row and needs no key (the decision refuses an illegal target before
/// it looks at the facts). When the gate facts were read and a `verifier` is given, it checks the host's answer before the decider runs;
/// its refusal is final and nothing is written.
async fn decide_and_apply(
    tx: &mut Tx,
    request: &Request,
    now: f64,
    decider: &dyn Decider,
    approval: Option<(&dyn ApprovalSource, &ApprovalKeys)>,
    verifier: Option<&dyn ApprovalVerifier>,
) -> Result<Applied, ApplyError> {
    let task_id = request.task_id.as_str();
    let store_error = |error: WriterError| ApplyError::store(&error, task_id);
    let row = tx.task(task_id).await.map_err(store_error)?;
    let approval = match (approval, &row) {
        (Some((source, keys)), Some(row))
            if matches!(obj_get(row, "state"), Some(Json::Str(state))
                if legal_targets(state).is_some_and(|legal| legal.contains(&"APPROVED"))) =>
        {
            let gathered = approval::gather(tx, keys, source, row, now)
                .await
                .map_err(store_error)?;
            if let (Some(verifier), Some((inputs, answer))) = (verifier, &gathered.witness) {
                verifier
                    .verify(inputs, answer)
                    .map_err(|code| ApplyError::verification(code, task_id))?;
            }
            Some(gathered.facts)
        }
        _ => None,
    };
    let facts = Facts {
        task: row.clone().map(decision_row),
        approval,
    };
    let plan = decider
        .decide(&facts, request, now)
        .map_err(ApplyError::refused)?;
    let validated = PlanValidator::new(&request.actor)
        .validate(&plan, row.as_ref())
        .map_err(|violation| ApplyError::invalid_plan(violation, task_id))?;

    write_validated(tx, task_id, &validated, now)
        .await
        .map_err(store_error)?;
    let after = tx
        .task(task_id)
        .await
        .map_err(store_error)?
        .ok_or_else(|| {
            store_error(WriterError::final_error(
                ErrorKind::Database,
                ErrorCode::Internal,
                format!("task {task_id} vanished inside its own transaction"),
            ))
        })?;
    Ok(Applied {
        task: after,
        audit: validated
            .audit
            .iter()
            .map(|step| AppliedAudit {
                from: step.from.clone(),
                to: step.to.clone(),
                actor: step.actor.clone(),
                reason: step.reason.clone(),
                ts: now,
            })
            .collect(),
        effects: validated.effects.to_vec(),
    })
}

/// Python's `_apply` for a validated plan: one conditional `UPDATE` and one audit `INSERT` per audit step, in order.
///
/// The first step of an auto-escalation sets only its state, the last sets the plan's columns. Shared with
/// [`TaskStoreWriter::apply_ops`](crate::TaskStoreWriter::apply_ops), so a transition inside an op list is written by the same code.
pub(crate) async fn write_validated(
    tx: &mut Tx,
    task_id: &str,
    validated: &ValidatedPlan<'_>,
    now: f64,
) -> Result<(), WriterError> {
    let mut columns = Vec::new();
    if let Some(attempt) = validated.attempt {
        columns.push(PlanColumn::Attempt(attempt));
    }
    if let Some(loops) = validated.rework_loops {
        columns.push(PlanColumn::ReworkLoops(loops));
    }
    if let Some(notes) = &validated.notes {
        columns.push(PlanColumn::Notes(notes));
    }
    for (index, step) in validated.audit.iter().enumerate() {
        let last = index + 1 == validated.audit.len();
        tx.apply_state(&StateChange {
            task_id,
            from: &step.from,
            to: &step.to,
            columns: if last { &columns } else { &[] },
            actor: &step.actor,
            reason: &step.reason,
            updated_at: now,
            ts: now,
        })
        .await?;
    }
    Ok(())
}

/// The row as [`Facts`] holds it: `dict(sqlite3.Row)`, so the JSON columns are text again and `notes_json` is gone.
///
/// The store hands over `TaskStore._row`'s shape (JSON columns parsed). Dumping a parsed value gives text that parses back
/// to the same value, which is all the decision reads of those columns.
fn decision_row(row: Fields) -> Fields {
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
