// Visibility into the real PyO3 implementation (pyo3/bindings/src/lib.rs).
// Exposes the surface the Python side (orchestration.py) calls.
pub fn exposed_apis() -> &'static [ &'static str ] {
    &[
        "create_pool / SubagentPool::new",
        "submit_tasks + wait (tickets)",
        "status / metrics",
        "scale (dynamic within cap)",
        "force_error / Error state",
    ]
}
