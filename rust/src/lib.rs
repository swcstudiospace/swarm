//! gsd_orchestrator - Multithreading Subagent module per ultrathink workflow.
//! Real implementations (not shims) for the required modules.

pub mod pool;
pub use pool::{SubagentPool, PoolState};

pub mod decomposition;
pub use decomposition::{decompose_task, SubTask};

pub mod executor;
pub use executor::execute_parallel;

pub mod pyo3_bindings;
pub use pyo3_bindings::exposed_apis;

pub mod validation;
pub use validation::MAX as MAX_SUBAGENTS;

pub mod gsd_hooks;
pub use gsd_hooks::gsd_cli_hook;

pub mod results;
pub use results::aggregate_results;

pub const MAX_WORKERS: usize = 30;
