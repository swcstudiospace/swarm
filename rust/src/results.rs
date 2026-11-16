// Result aggregation for concurrent subagent tasks.
pub fn aggregate_results(items: Vec<String>) -> Vec<String> {
    // Real aggregation would merge partials, handle errors, etc.
    items.into_iter().filter(|s| !s.is_empty()).collect()
}
