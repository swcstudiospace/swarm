//! Scheduler decision core of the GSD-Core runtime (Phase 14.4): std only, no async runtime, no protocol crates, no unsafe code.
//!
//! The Python scheduler (`scripts/swarm_run.py`) stays the oracle. This crate holds the pure decision code that a Rust engine would
//! run: the AIMD limiter, claim patterns and the claim table, ordering, backoff, the cancel-drain arithmetic, and (in later units) the
//! ports contract, the worker pool and the pass loop. It never touches the Task Store, telemetry, leases or sessions: all of that is a
//! port that the host provides.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

pub mod aimd;
pub mod backoff;
pub mod claim_pattern;
pub mod claims;
pub mod drain;
pub mod pool;
pub mod ports;
pub mod priority;
pub mod select;

#[cfg(test)]
mod jsonlite;

pub use aimd::{AimdLimiter, LimiterError, DEFAULT_RETRY_CAP, DEFAULT_STREAK_THRESHOLD};
pub use claim_pattern::{claims_conflict, claims_overlap, ClaimError, ClaimMode, PathClaim};
