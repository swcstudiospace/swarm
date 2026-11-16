//! The one-transaction write of a planned task DAG (phase 14.5, unit U3.7b): the `with store.transaction():` block of
//! `scripts/orch_plan.py`, applied from the ops that `gsd_route::plan_derive` derives.
//!
//! The caller owns the transaction, so the reads the derivation needs and the writes it decides on share one `BEGIN IMMEDIATE`:
//!
//! 1. [`read_plan_rows`] reads, inside the transaction, the rows of the candidate ids and of every correlation they belong to, in the
//!    shape [`gsd_route::plan_derive::PlanFacts`] takes (JSON columns as text, as `dict(sqlite3.Row)` holds them).
//! 2. The caller derives the plan with `gsd_route::plan_derive::derive_plan`.
//! 3. [`write_plan_ops`] writes the derived ops in order: `store.create` as [`Tx::insert_task`] and `store.transition` as
//!    [`Tx::apply_state`]. Every op that cannot be written the way Python writes it is refused before the first write
//!    ([`check_plan_ops`]), so a refusal here leaves the store untouched and Python answers.
//! 4. The caller commits, or rolls back on any error. Nothing is committed part-way: a failure after the first write rolls the
//!    whole plan back when the transaction is dropped or rolled back.
//!
//! The two clock values differ from Python's by design: `created_at` of the `i`-th task is `now + i * 1e-6` (see `plan_derive`),
//! and the `NULL -> CREATED` audit row takes that same value as its `ts`, because [`Tx::insert_task`] stamps one time on the row
//! and its audit row. Transitions take the caller's `now`. The parity comparisons mask `created_at`, `updated_at` and every audit `ts`.

use gsd_route::plan_derive::{NewTask, PlanOp};
use gsd_route::{is_safe_id, obj_get, Fields, Json};

use crate::error::{ErrorKind, WriterError};
use crate::store::{NewTaskRow, StateChange, Tx};

/// The state changes a plan creation makes: `VALIDATED` then `PLANNED` (`orch_plan.py`). Any other step is refused.
const PLAN_STEPS: [(&str, &str); 2] = [("CREATED", "VALIDATED"), ("VALIDATED", "PLANNED")];

/// The rows of a planning call's facts, read inside the caller's transaction.
#[derive(Debug, Clone, PartialEq)]
pub struct PlanRows {
    /// The rows whose `task_id` is one of the candidate ids, with every column as stored.
    pub prefix_tasks: Vec<Fields>,
    /// Every `tasks` row of the correlations in [`PlanRows::correlations`], each row once, in `rowid` order per correlation.
    /// `plan_derive` sorts them by `dag_depth, created_at` itself.
    pub correlation_tasks: Vec<Fields>,
    /// The correlation being planned first, then every other correlation that owns a candidate id, each once.
    pub correlations: Vec<String>,
}

/// Reads the facts of a planning call inside the caller's transaction.
///
/// `candidate_ids` are the exact `<prefix>-<suffix>` ids the call could create or find. A candidate that exists names the
/// correlation that owns it; that correlation's rows and the rows of `correlation` are read. A read failure is the caller's
/// (before any write, so not started).
///
/// # Errors
///
/// Any read error of the transaction, and a stored row the writer cannot read the way Python does (see [`Tx::task_rows_of_correlation`]).
pub async fn read_plan_rows(
    tx: &mut Tx,
    correlation: &str,
    candidate_ids: &[String],
) -> Result<PlanRows, WriterError> {
    let mut correlations = vec![correlation.to_string()];
    for id in candidate_ids {
        if let Some(owner) = tx.correlation_of(id).await? {
            if !correlations.contains(&owner) {
                correlations.push(owner);
            }
        }
    }
    let mut correlation_tasks = Vec::new();
    let mut prefix_tasks = Vec::new();
    for owner in &correlations {
        for row in tx.task_rows_of_correlation(owner).await? {
            let is_candidate = matches!(
                obj_get(&row, "task_id"),
                Some(Json::Str(id)) if candidate_ids.contains(id)
            );
            if is_candidate {
                prefix_tasks.push(row.clone());
            }
            correlation_tasks.push(row);
        }
    }
    Ok(PlanRows {
        prefix_tasks,
        correlation_tasks,
        correlations,
    })
}

/// The owned text of one `store.create` row, built from the values `NewTask::insert_values` gives. Owned so that
/// [`NewTaskRow`] can borrow it.
struct StoredTask {
    task_id: String,
    correlation_id: String,
    dag_depth: i64,
    capability: String,
    agent_id: Option<String>,
    title: String,
    inputs: String,
    acceptance: String,
    budget: String,
    risk_class: String,
    priority: String,
    depends_on: String,
    max_attempts: i64,
    notes: String,
    created_at: f64,
}

impl StoredTask {
    fn of(task: &NewTask) -> Result<Self, WriterError> {
        let values = task.insert_values();
        let text = |name: &str| match obj_get(&values, name) {
            Some(Json::Str(text)) => Ok(text.clone()),
            _ => Err(unsupported(format!(
                "created task {}: {name} is not text",
                task.task_id
            ))),
        };
        let number = |name: &str| match obj_get(&values, name) {
            Some(Json::Int(value)) => Ok(*value),
            _ => Err(unsupported(format!(
                "created task {}: {name} is not an integer",
                task.task_id
            ))),
        };
        let created_at = match obj_get(&values, "created_at") {
            Some(Json::Float(value)) => *value,
            _ => {
                return Err(unsupported(format!(
                    "created task {}: created_at is not a float",
                    task.task_id
                )))
            }
        };
        if !matches!(obj_get(&values, "parent_id"), Some(Json::Null)) {
            return Err(unsupported(format!(
                "created task {}: parent_id is set",
                task.task_id
            )));
        }
        if !matches!(obj_get(&values, "state"), Some(Json::Str(state)) if state == "CREATED") {
            return Err(unsupported(format!(
                "created task {}: state is not CREATED",
                task.task_id
            )));
        }
        let agent_id = match obj_get(&values, "agent_id") {
            Some(Json::Str(agent)) => Some(agent.clone()),
            Some(Json::Null) | None => None,
            Some(_) => {
                return Err(unsupported(format!(
                    "created task {}: agent_id is not text",
                    task.task_id
                )))
            }
        };
        Ok(Self {
            task_id: text("task_id")?,
            correlation_id: text("correlation_id")?,
            dag_depth: number("dag_depth")?,
            capability: text("capability")?,
            agent_id,
            // A NULL title (a custom row with `"title": null`) is not a text column value this writer binds; Python stores NULL.
            title: match obj_get(&values, "title") {
                Some(Json::Str(title)) => title.clone(),
                _ => {
                    return Err(unsupported(format!(
                        "created task {}: title is not text",
                        task.task_id
                    )))
                }
            },
            inputs: text("inputs")?,
            acceptance: text("acceptance")?,
            budget: text("budget")?,
            risk_class: text("risk_class")?,
            priority: text("priority")?,
            depends_on: text("depends_on")?,
            max_attempts: number("max_attempts")?,
            notes: text("notes")?,
            created_at,
        })
    }

    fn row(&self) -> NewTaskRow<'_> {
        NewTaskRow {
            task_id: &self.task_id,
            correlation_id: &self.correlation_id,
            parent_id: None,
            dag_depth: self.dag_depth,
            capability: &self.capability,
            agent_id: self.agent_id.as_deref(),
            title: &self.title,
            inputs: &self.inputs,
            acceptance: &self.acceptance,
            budget: &self.budget,
            risk_class: &self.risk_class,
            priority: &self.priority,
            depends_on: &self.depends_on,
            max_attempts: self.max_attempts,
            notes: &self.notes,
            now: self.created_at,
        }
    }
}

fn unsupported(message: String) -> WriterError {
    WriterError::not_started(ErrorKind::Unsupported, message)
}

/// Refuses, before the first write, every op [`write_plan_ops`] does not reproduce: a created row whose values are not the shape
/// `NewTask::insert_values` gives, and a step that is not `CREATED -> VALIDATED` or `VALIDATED -> PLANNED`.
///
/// # Errors
///
/// A not-started [`ErrorKind::Unsupported`] error for the first op that cannot be written.
pub fn check_plan_ops(ops: &[PlanOp]) -> Result<(), WriterError> {
    for op in ops {
        match op {
            PlanOp::Create(task) => {
                StoredTask::of(task)?;
            }
            PlanOp::Transition(step) => {
                if !PLAN_STEPS.contains(&(step.from, step.to)) {
                    return Err(unsupported(format!(
                        "transition {} -> {} of {} is not a plan step",
                        step.from, step.to, step.task_id
                    )));
                }
                if !is_safe_id(&step.task_id) {
                    return Err(unsupported(format!(
                        "transition of an unsafe task id {}",
                        step.task_id
                    )));
                }
            }
        }
    }
    Ok(())
}

/// Writes the derived ops in order inside the caller's transaction: `store.create` as [`Tx::insert_task`], `store.transition` as
/// [`Tx::apply_state`] stamped with `now`. [`check_plan_ops`] runs first, so a refusal before the first write is not started.
///
/// # Errors
///
/// The refusal of [`check_plan_ops`], or the first failing write. A failure after the first write is final; the caller must roll
/// the transaction back and must not commit it.
pub async fn write_plan_ops(
    tx: &mut Tx,
    ops: &[PlanOp],
    now: f64,
) -> Result<PlanWritten, WriterError> {
    check_plan_ops(ops)?;
    let mut written = PlanWritten::default();
    for op in ops {
        match op {
            PlanOp::Create(task) => {
                let stored = StoredTask::of(task)?;
                tx.insert_task(&stored.row()).await?;
                written.created += 1;
            }
            PlanOp::Transition(step) => {
                tx.apply_state(&StateChange {
                    task_id: &step.task_id,
                    from: step.from,
                    to: step.to,
                    columns: &[],
                    actor: step.actor,
                    reason: &step.reason,
                    updated_at: now,
                    ts: now,
                })
                .await?;
                written.transitions += 1;
            }
        }
    }
    Ok(written)
}

/// What [`write_plan_ops`] wrote.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct PlanWritten {
    /// `store.create` calls.
    pub created: usize,
    /// `store.transition` calls.
    pub transitions: usize,
}
