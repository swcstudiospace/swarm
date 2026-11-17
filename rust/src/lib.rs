//! gsd_orchestrator: multithreading subagent module prototype (rust/ crate).
//!
//! Status: prototype, not the v2.4 scheduler runtime. `SubagentPool::scale` spawns
//! threads that only count, and `SubagentPool::submit` performs no work, so pool
//! dispatch is simulated. Phase 14.4 stays open (.planning/MILESTONE-AUDIT.md section 5.1).
//!
//! This crate has no PyO3 dependency; `pyo3_bindings` exposes plain Rust functions.

pub mod decomposition;
pub use decomposition::{decompose_task, SubTask};

pub mod executor;
pub use executor::execute_parallel;

pub mod pyo3_bindings;
pub use pyo3_bindings::exposed_apis;

pub mod validation;
pub use validation::{MAX as MAX_SUBAGENTS, validate_pool_size};

pub mod gsd_hooks;
pub use gsd_hooks::{gsd_cli_hook, record_audit};

pub mod results;
pub use results::aggregate_results;

pub mod pool;
pub use pool::{PoolState, SubagentPool};

pub const MAX_WORKERS: usize = 30;
