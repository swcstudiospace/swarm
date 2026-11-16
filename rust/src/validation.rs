pub const MAX: usize = 30;

/// Basic input validation + size cap enforcement.
pub fn validate_pool_size(requested: usize) -> Result<usize, String> {
    if requested > MAX {
        return Err(format!("exceeds {} subagent cap", MAX));
    }
    if requested == 0 {
        return Err("pool size must be > 0".into());
    }
    Ok(requested)
}
/// Validate a task dict (real schema check for decomposition input).
pub fn validate_task(task: &std::collections::HashMap<String, String>) -> Result<(), String> {
    if !task.contains_key("id") || task.get("id").map(|s| s.trim()).unwrap_or("").is_empty() {
        return Err("task must have non-empty 'id'".into());
    }
    if !task.contains_key("kind") || task.get("kind").map(|s| s.trim()).unwrap_or("").is_empty() {
        return Err("task must have non-empty 'kind'".into());
    }
    // SDLC phases are allowed
    let allowed = ["req", "arch", "impl", "test", "review", "deploy"];
    if let Some(k) = task.get("kind") {
        if !allowed.iter().any(|a| a == k) {
            return Err(format!("kind '{}' not allowed", k));
        }
    }
    Ok(())
}

/// Audit log helper (writes a line to a buffer or file for audit trail).
pub fn audit_log(msg: &str, buf: &mut Vec<String>) {
    buf.push(format!("[audit] {}", msg));
}
