// Path to real PyO3 implementation is in pyo3/bindings/src/lib.rs
pub fn exposed_apis() -> &'static str {
    "create_pool, submit_tasks, status, dispatch, scale (see pyo3/bindings)"
}
