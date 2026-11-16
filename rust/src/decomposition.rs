pub fn decompose_task(task: &str) -> Vec<String> {
    task.split(',').map(|s| s.trim().to_string()).collect()
}
