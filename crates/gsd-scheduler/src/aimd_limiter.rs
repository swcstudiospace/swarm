//! Additive-increase / multiplicative-decrease limiter for the effective parallelism.
//!
//! The implementation lives in [`gsd_sched_core::aimd`], which this module re-exports so the
//! `gsd_scheduler::aimd_limiter` paths stay stable. See that module for the rules, the Python
//! parity notes and the thread and blocking expectations.

pub use gsd_sched_core::aimd::{
    AimdLimiter, LimiterError, DEFAULT_RETRY_CAP, DEFAULT_STREAK_THRESHOLD,
};
