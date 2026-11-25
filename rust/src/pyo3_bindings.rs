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
/// PyO3-exposed GSD hook (Python calls this to dispatch to GSD-Core agents).
pub fn py_gsd_submit(task_desc: &str) -> String {
    let r = crate::gsd_hooks::gsd_cli_hook(task_desc);
    r.message
}

/// PyO3-exposed validation (Python can call before decomposition).
pub fn py_validate_task(id: &str, kind: &str) -> Result<(), String> {
    let mut m = std::collections::HashMap::new();
    m.insert("id".into(), id.into());
    m.insert("kind".into(), kind.into());
    crate::validation::validate_task(&m)
}

/// PyO3-exposed result aggregation (Python collects from subagents).
pub fn py_aggregate(items: Vec<String>) -> Vec<String> {
    crate::results::aggregate_results(items)
}
