//! Task-graph model, validation and node state machine.
//!
//! NOTE (remap): this module was provisionally sketched at `src/graph/*` in the
//! design notes; it is implemented here at `crates/gsd-sched-core/src/graph/*`
//! to fit this crate (`gsd-sched-core`).
//!
//! The graph is the scheduler's decision input: a set of [`Node`]s with
//! dependency edges, the [`Contract`]s that cover their capabilities, and an
//! optional explicit entry set. [`validation`] checks a graph with a pure
//! function that returns typed errors; [`state`] is the single node FSM
//! (queued/ready/running/paused/succeeded/failed/cancelled/timed_out/skipped)
//! with a legal-transition table, plus the deterministic ready order
//! (priority, then [`NodeId`]).
//!
//! Threads and blocking: pure data and pure functions only; nothing here
//! blocks, holds state, or touches the Task Store, telemetry, leases or the
//! filesystem.

pub mod model;
pub mod state;
pub mod validation;

pub use model::{Contract, Graph, Node, NodeId};
pub use state::{NodeState, State, TransitionError, LEGAL_TRANSITIONS};
pub use validation::{ValidationError, ValidationLimits};
