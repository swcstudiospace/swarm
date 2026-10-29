//! The host's check of a decided transition plan, before anything is written.
//!
//! A [`TransitionPlan`] comes from a [`Decider`](crate::Decider): the host function, or later a sandboxed guest that has to be
//! treated as untrusted. [`PlanValidator`] accepts a plan only if it can do nothing but what `TaskStore.transition` does:
//!
//! - it is guarded by the state the row is really in (`plan.from == row.state`);
//! - it sets only `state`, `attempt`, `rework_loops` and `notes`, each at most once, with the right JSON type, and `state`
//!   is the plan's final state;
//! - it carries one or two audit rows, all by the request's actor, chained from `plan.from` to `plan.to`;
//! - every step of that chain is legal in the host's own copy of the legal table ([`legal_targets`]), so the final state is
//!   legal too.
//!
//! Nothing else survives validation: the validated plan holds only the three typed columns, the audit rows and the effects.

use std::fmt;

use gsd_route::pydumps::to_python_dumps;
use gsd_route::transition_plan::{legal_targets, AuditRow, PlanEffect, TransitionPlan};
use gsd_route::{obj_get, Fields, Json};

/// The `tasks` columns a plan may set. `updated_at` is the host's, set to `now`.
const ALLOWED_COLUMNS: [&str; 4] = ["state", "attempt", "rework_loops", "notes"];

/// `LEGAL_TRANSITIONS` allows a second audit row only for IN_REVIEW → CHANGES_REQUESTED → ESCALATED.
const MAX_AUDIT_ROWS: usize = 2;

/// Why a plan was refused. The plan is never applied, so nothing was written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PlanViolation {
    /// The task row does not exist, so there is nothing to guard the update with.
    NoTaskRow,
    /// The row's `state` is not text.
    RowStateNotText,
    /// `plan.from` is not the state the row is in.
    FromMismatch {
        /// `plan.from`.
        plan: String,
        /// The state read inside the lock.
        row: String,
    },
    /// A column outside `state`, `attempt`, `rework_loops`, `notes`.
    UnknownColumn {
        /// The column name the plan used.
        column: String,
    },
    /// The same column twice.
    DuplicateColumn {
        /// The repeated column.
        column: String,
    },
    /// The plan does not set `state`.
    MissingStateColumn,
    /// `state` is not `plan.to`.
    StateColumnMismatch {
        /// `plan.to`.
        plan: String,
    },
    /// A column value of the wrong JSON type (`attempt` and `rework_loops` are integers, `notes` an object, `state` text).
    ColumnType {
        /// The column.
        column: String,
        /// The JSON type it must have.
        expected: &'static str,
    },
    /// A float inside `notes` that is `NaN` or infinite: Python writes it, Rust cannot read the row back.
    NonFiniteNotes,
    /// No audit row, or more than two.
    AuditRowCount {
        /// How many rows the plan holds.
        count: usize,
    },
    /// An audit row that does not start where the previous one ended (the first starts at `plan.from`).
    Unchained {
        /// Index of the row.
        index: usize,
        /// The state it must start from.
        expected: String,
        /// The state it starts from.
        found: String,
    },
    /// The last audit row does not end in `plan.to`.
    AuditEnd {
        /// `to_state` of the last audit row.
        found: String,
        /// `plan.to`.
        plan: String,
    },
    /// An audit step that is not in the legal table.
    IllegalStep {
        /// The step's `from`.
        from: String,
        /// The step's `to`.
        to: String,
    },
    /// An audit row by an actor other than the request's.
    ActorMismatch {
        /// Index of the row.
        index: usize,
    },
}

impl fmt::Display for PlanViolation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoTaskRow => write!(f, "plan for a task that has no row"),
            Self::RowStateNotText => write!(f, "task row state is not text"),
            Self::FromMismatch { plan, row } => {
                write!(f, "plan.from {plan} but the row is in {row}")
            }
            Self::UnknownColumn { column } => write!(f, "plan sets column {column}"),
            Self::DuplicateColumn { column } => write!(f, "plan sets column {column} twice"),
            Self::MissingStateColumn => write!(f, "plan does not set the state column"),
            Self::StateColumnMismatch { plan } => {
                write!(f, "state column is not plan.to {plan}")
            }
            Self::ColumnType { column, expected } => {
                write!(f, "column {column} is not {expected}")
            }
            Self::NonFiniteNotes => write!(f, "notes holds a non-finite number"),
            Self::AuditRowCount { count } => {
                write!(f, "plan has {count} audit rows, expected 1 or 2")
            }
            Self::Unchained {
                index,
                expected,
                found,
            } => write!(
                f,
                "audit row {index} starts at {found}, expected {expected}"
            ),
            Self::AuditEnd { found, plan } => {
                write!(f, "last audit row ends in {found}, plan.to is {plan}")
            }
            Self::IllegalStep { from, to } => write!(f, "illegal audit step {from} -> {to}"),
            Self::ActorMismatch { index } => {
                write!(f, "audit row {index} is not by the request's actor")
            }
        }
    }
}

impl std::error::Error for PlanViolation {}

/// The typed values of the optional columns of a plan.
#[derive(Debug, Default)]
struct Columns {
    attempt: Option<i64>,
    rework_loops: Option<i64>,
    notes: Option<String>,
}

/// A plan the validator accepted: exactly what the writer is allowed to apply.
#[derive(Debug, Clone, PartialEq)]
pub struct ValidatedPlan<'p> {
    /// The state the conditional `UPDATE` guards: the row's state.
    pub from: &'p str,
    /// The final state.
    pub to: &'p str,
    /// `attempt`, when the plan sets it.
    pub attempt: Option<i64>,
    /// `rework_loops`, when the plan sets it.
    pub rework_loops: Option<i64>,
    /// `notes` as Python's `json.dumps` prints it, when the plan sets it.
    pub notes: Option<String>,
    /// One or two audit rows, chained from [`Self::from`] to [`Self::to`].
    pub audit: &'p [AuditRow],
    /// Effects to hand back after the commit.
    pub effects: &'p [PlanEffect],
}

/// Validates plans for one request: the plan's audit rows must all be by `actor`.
#[derive(Debug, Clone, Copy)]
pub struct PlanValidator<'a> {
    actor: &'a str,
}

impl<'a> PlanValidator<'a> {
    /// A validator for a request by `actor`.
    pub fn new(actor: &'a str) -> Self {
        Self { actor }
    }

    /// Accepts `plan` for the task `row` read inside the write lock, or says why not. Writes nothing.
    pub fn validate<'p>(
        &self,
        plan: &'p TransitionPlan,
        row: Option<&Fields>,
    ) -> Result<ValidatedPlan<'p>, PlanViolation> {
        let row = row.ok_or(PlanViolation::NoTaskRow)?;
        let Some(Json::Str(row_state)) = obj_get(row, "state") else {
            return Err(PlanViolation::RowStateNotText);
        };
        if plan.from != *row_state {
            return Err(PlanViolation::FromMismatch {
                plan: plan.from.clone(),
                row: row_state.clone(),
            });
        }
        let columns = self.validate_columns(plan)?;
        self.validate_audit(plan)?;
        Ok(ValidatedPlan {
            from: &plan.from,
            to: &plan.to,
            attempt: columns.attempt,
            rework_loops: columns.rework_loops,
            notes: columns.notes,
            audit: &plan.audit,
            effects: &plan.effects,
        })
    }

    /// The column allowlist and the JSON type of each value.
    fn validate_columns(&self, plan: &TransitionPlan) -> Result<Columns, PlanViolation> {
        let mut state_seen = false;
        let mut columns = Columns::default();
        for (index, (name, value)) in plan.columns.iter().enumerate() {
            if !ALLOWED_COLUMNS.contains(&name.as_str()) {
                return Err(PlanViolation::UnknownColumn {
                    column: name.clone(),
                });
            }
            if plan.columns[..index]
                .iter()
                .any(|(earlier, _)| earlier == name)
            {
                return Err(PlanViolation::DuplicateColumn {
                    column: name.clone(),
                });
            }
            let wrong = |expected| PlanViolation::ColumnType {
                column: name.clone(),
                expected,
            };
            match (name.as_str(), value) {
                ("state", Json::Str(state)) => {
                    if *state != plan.to {
                        return Err(PlanViolation::StateColumnMismatch {
                            plan: plan.to.clone(),
                        });
                    }
                    state_seen = true;
                }
                ("state", _) => return Err(wrong("text")),
                ("attempt", Json::Int(number)) => columns.attempt = Some(*number),
                ("attempt", _) => return Err(wrong("an integer")),
                ("rework_loops", Json::Int(number)) => columns.rework_loops = Some(*number),
                ("rework_loops", _) => return Err(wrong("an integer")),
                ("notes", object @ Json::Obj(_)) => {
                    if has_non_finite(object) {
                        return Err(PlanViolation::NonFiniteNotes);
                    }
                    columns.notes = Some(to_python_dumps(object));
                }
                _ => return Err(wrong("an object")),
            }
        }
        if !state_seen {
            return Err(PlanViolation::MissingStateColumn);
        }
        Ok(columns)
    }

    /// One or two audit rows by the request's actor, chained from `plan.from` to `plan.to`, every step legal.
    fn validate_audit(&self, plan: &TransitionPlan) -> Result<(), PlanViolation> {
        if plan.audit.is_empty() || plan.audit.len() > MAX_AUDIT_ROWS {
            return Err(PlanViolation::AuditRowCount {
                count: plan.audit.len(),
            });
        }
        let mut at = plan.from.as_str();
        for (index, step) in plan.audit.iter().enumerate() {
            if step.from != at {
                return Err(PlanViolation::Unchained {
                    index,
                    expected: at.to_string(),
                    found: step.from.clone(),
                });
            }
            if !legal_targets(at).is_some_and(|targets| targets.contains(&step.to.as_str())) {
                return Err(PlanViolation::IllegalStep {
                    from: step.from.clone(),
                    to: step.to.clone(),
                });
            }
            if step.actor != self.actor {
                return Err(PlanViolation::ActorMismatch { index });
            }
            at = &step.to;
        }
        if at != plan.to {
            return Err(PlanViolation::AuditEnd {
                found: at.to_string(),
                plan: plan.to.clone(),
            });
        }
        Ok(())
    }
}

/// True when a float anywhere inside `value` is `NaN` or infinite.
fn has_non_finite(value: &Json) -> bool {
    match value {
        Json::Float(number) => !number.is_finite(),
        Json::Arr(items) => items.iter().any(has_non_finite),
        Json::Obj(fields) => fields.iter().any(|(_, item)| has_non_finite(item)),
        _ => false,
    }
}
