pub mod pool;
pub use pool::{SubagentPool, PoolState};

pub mod decomposition {
    pub fn decompose_task(task: &str) -> Vec<String> {
        task.split(',').map(|s| s.trim().to_string()).collect()
    }
}

pub mod executor {
    pub fn execute_parallel(tasks: usize) -> usize { tasks }
}

pub mod pyo3_bindings {
    pub fn exposed() -> &'static str { "pyo3" }
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

pub const MAX_WORKERS: usize = 30;
