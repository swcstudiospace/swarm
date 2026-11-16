// The real PyO3 bindings live in the workspace pyo3/ crate.
// This module makes the path visible for the workflow.
pub fn exposed_apis() -> &'static str {
    "create_pool, submit_tasks, status, dispatch (see pyo3/bindings/src/lib.rs)"
}
