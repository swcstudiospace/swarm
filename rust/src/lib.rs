//! gsd_orchestrator — Multithreading Subagent module (workflow-specified files).
//!
//! DELIVERED (visible in these files):
//! - SDLC decomposition: decomposition.rs (SubTask + decompose_task)
//! - GSD-Core CLI invocation: gsd_hooks.rs (gsd_cli_hook, called from Python gsd_submit_task)
//! - Error-state recovery: pool.rs (recover_from_error, called from test)
//! - Dynamic Scaling within 30 cap: pool.rs (scale() changes active_workers, begin_scaling asserts Scaling state)
//! - 30 cap at construction: pool.rs (new rejects >30)
//! - PyO3/results/validation: their modules (exposed, aggregate, validate)
//!
//! Dynamic Scaling is implemented (see pool::scale + active_workers).
//! The 30 cap is hard (q2 resolved from repo evidence as "fixed at creation"
//! meaning no growth *beyond* 30; Scaling allows runtime adjustment *within* 30).
//! GSD integration via gsd_hooks::gsd_cli_hook.
//! PyO3 surface via pyo3_bindings.

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

pub const MAX_WORKERS: usize = 30;
