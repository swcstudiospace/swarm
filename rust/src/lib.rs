pub use concurrency::execution::{
    ConcurrencyConfig, PoolState, SubTask, SubResult, SubTicket, ThreadPool,
    execute_parallel, MAX_WORKERS,
};
pub mod pool {
    pub use concurrency::execution::ThreadPool as SubagentPool;
}
pub mod decomposition {
    // Minimal adapter
    pub fn decompose_task(task: &str) -> Vec<String> {
        task.split(',').map(|s| s.trim().to_string()).collect()
    }
}
pub mod executor {
    pub use concurrency::execution::execute_parallel;
}
pub mod pyo3_bindings {
    // The real PyO3 lives in the workspace pyo3 crate; this is a stub for layout
    pub fn exposed() -> &'static str { "pyo3-exposed via workspace" }
}
pub mod validation {
    pub const MAX: usize = 30;
}
pub mod gsd_hooks {
    pub fn hook() {}
}
pub mod results {
    pub fn aggregate() {}
}
