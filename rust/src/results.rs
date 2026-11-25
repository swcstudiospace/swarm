// Result aggregation for concurrent subagent tasks.
pub fn aggregate_results(items: Vec<String>) -> Vec<String> {
    // Real aggregation would merge partials, handle errors, etc.
    items.into_iter().filter(|s| !s.is_empty()).collect()
}
/// Merge partial results, count successes/failures, surface first error.
pub fn merge_aggregate(items: Vec<Result<String, String>>) -> (Vec<String>, usize, Option<String>) {
    let mut ok = vec![];
    let mut errs = 0usize;
    let mut first_err = None;
    for r in items {
        match r {
            Ok(s) if !s.is_empty() => ok.push(s),
            Ok(_) => {}
            Err(e) => {
                errs += 1;
                if first_err.is_none() { first_err = Some(e); }
            }
        }
    }
    (ok, errs, first_err)
}
// (backward compat alias kept via the top definition; no duplicate)
