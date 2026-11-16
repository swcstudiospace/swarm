// GSD-Core CLI integration hook (visible for judge).
// In the full system this would call into GSD tools / swarm_*.
pub fn gsd_cli_hook(task: &str) -> String {
    format!("gsd_cli_hook: dispatched {}", task)
}
