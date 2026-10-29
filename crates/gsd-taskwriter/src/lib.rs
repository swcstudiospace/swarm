//! Host-only Task Store writer for the Rust engine's write routes (phase 14.5, units U3.1a and U3.1d).
//!
//! The store layer over the Python-owned `tasks.db`: [`open_for_write`] opens it without any DDL and refuses with a
//! [`WriteState::NotStarted`] error when the schema is not there, so Python migrates first. [`WriteStore::begin`] starts a
//! `BEGIN IMMEDIATE` transaction ([`Tx`]) that reads the task row as a Python-shaped dict, applies audited state changes
//! with a conditional `UPDATE`, and appends verdict and artifact rows. `transitions`, `verdicts` and `artifacts` are never
//! updated or deleted.
//!
//! [`TaskStoreWriter::transition`] is `TaskStore.transition` on top of that: it reads the row inside the lock, asks a
//! [`Decider`] (the host function [`HostDecider`], later a sandboxed guest) for a plan, checks the plan with
//! [`PlanValidator`] so that a misbehaving decider can write nothing but a legal audited state change, applies it and
//! returns an [`Applied`]. Failures are [`ApplyError`]s that keep Python's code, message and details.
//!
//! The `fault-injection` feature adds named pause points ([`fault::PausePoint`]) so a test can hold a transaction between the
//! state `UPDATE` and the audit `INSERT`, or between two ops of an op list. It is off by default and must never be enabled in the shipped binary.
//!
//! [`TaskStoreWriter::apply_ops`] applies an ordered list of [`WriterOp`]s (the other `TaskStore` writes: notes, artifacts,
//! feedback, `create`, dependencies, and transitions of a decided plan) in one `BEGIN IMMEDIATE` transaction: every op or none.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod apply;
mod error;
mod ops;
mod plan;
mod store;

pub use apply::{
    Applied, AppliedAudit, ApplyCause, ApplyError, Decider, HostDecider, TaskStoreWriter,
};
pub use error::{ErrorKind, WriteState, WriterError};
pub use ops::{NewTask, OpsApplied, OpsAudit, OpsCause, OpsError, WriterOp};
pub use plan::{PlanValidator, PlanViolation, ValidatedPlan};
#[cfg(feature = "fault-injection")]
pub use store::fault;
pub use store::{
    open_for_write, NewArtifact, NewTaskRow, NewVerdict, PlanColumn, StateChange, TextColumn, Tx,
    WriteStore, WriterConfig,
};
