//! Concurrency primitives for the Multithreading Subagent (SPE-33069 n3.x).
//!
//! SOTA production-grade, thread-safe (no data races), std + crossbeam + parking_lot.
//! The dispatch loop, parallel execution, per-subtask timeouts, cancellation,
//! backoff retries, result aggregation, state machine, comprehensive error paths
//! and tests live in execution.rs (see that file).
//!
//! Acceptance: cargo test confirms parallel progress, partial results and graceful
//! failure modes.
//!
//! Refs: SPE-33069 n3.5 etc. Ultrathink n7 wave 2. Parallel Python unit.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

pub mod execution;

pub use execution::*;