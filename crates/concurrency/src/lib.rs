//! Concurrency primitives for the Multithreading Subagent (SPE-33069 n3.x).
//!
//! SOTA production-grade, thread-safe (no data races), std + crossbeam + parking_lot.
//! The dispatch loop, parallel execution, per-subtask timeouts, cancellation,
//! backoff retries, result aggregation, state machine, comprehensive error paths
//! and tests live in execution.rs (see that file).
//!
//! States (wave 1 unit 1): PoolState (Idle/Initializing/Active/Scaling/Draining/Terminating/Error),
//! TaskState (Received/Decomposed/Queued/Assigned/Executing/Aggregating/Completed/Failed/Cancelled),
//! SubagentState (Available/Busy/Failed/Terminated). Match BUILD_PROMPT.
//!
//! Execution primitives: ThreadPool::submit (work fn -> ticket for result), submit_batch, join etc for parallel subtask dispatch.
//!
//! Acceptance: cargo check -p concurrency; states defined+usable; basic test in file passes.
//!
//! Refs: SPE-33069 n3.5 etc. Ultrathink n7 wave 2. Parallel Python unit.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

pub mod execution;

pub use execution::*;
