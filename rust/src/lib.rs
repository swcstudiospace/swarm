//! Multithreading Subagent Orchestrator (per ultrathink workflow ownership map)
//! Re-exports and extends the real concurrency implementation.
pub use concurrency::execution::{
    ConcurrencyConfig, PoolState, SubTask, SubResult, SubTicket, ThreadPool,
    MAX_WORKERS, execute_parallel,
};

pub mod pool {
    pub use concurrency::execution::ThreadPool as SubagentPool;
}

pub mod decomposition {
    pub fn decompose_task(task: &str) -> Vec<String> {
        task.split(',').map(|s| s.trim().to_string()).collect()
    }
}

pub mod executor {
    pub use concurrency::execution::execute_parallel;
}

pub mod pyo3_bindings {
    pub fn exposed() -> &'static str { "PyO3 bindings via workspace" }
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

// Visible 30 cap + Scaling/Error (from the real crate)
pub use concurrency::execution::SUBAGENT_CAP;
