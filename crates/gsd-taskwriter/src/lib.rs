//! Host-only Task Store writer for the Rust engine's write routes (phase 14.5, unit U3.1a).
//!
//! The store layer over the Python-owned `tasks.db`: [`open_for_write`] opens it without any DDL and refuses with a
//! [`WriteState::NotStarted`] error when the schema is not there, so Python migrates first. [`WriteStore::begin`] starts a
//! `BEGIN IMMEDIATE` transaction ([`Tx`]) that reads the task row as a Python-shaped dict, applies audited state changes
//! with a conditional `UPDATE`, and appends verdict and artifact rows. `transitions`, `verdicts` and `artifacts` are never
//! updated or deleted. Decisions, plan validation and the effects manifest belong to the crates above this one.
//!
//! The `fault-injection` feature adds named pause points ([`fault::PausePoint`]) so a test can hold a transaction between the
//! state `UPDATE` and the audit `INSERT`. It is off by default and must never be enabled in the shipped binary.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod error;
mod store;

pub use error::{ErrorKind, WriteState, WriterError};
#[cfg(feature = "fault-injection")]
pub use store::fault;
pub use store::{
    open_for_write, NewArtifact, NewVerdict, PlanColumn, StateChange, Tx, WriteStore, WriterConfig,
};
