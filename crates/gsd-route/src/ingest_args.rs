//! The argv grammar of the host `orch_status --ingest` route (phase 14.5, unit U3.5e part 2).
//!
//! [`parse_ingest_args`] answers `Some` for exactly the argv the Rust route serves and `None` for everything else, so a caller
//! that gets `None` leaves the call to the Python body. The oracle is the real `argparse` parser of `scripts/orch_status.py`.
//! `--dry-run`, `--transition`, `--history`, `--reason`, an abbreviation, an unknown flag, a positional, a missing value, a
//! `--` separator and `-h` are outside the grammar: argparse accepts some of them, and this route does not model them.

/// The values of one served `orch_status --ingest` call.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct IngestArgs {
    /// The path of `--ingest`, as written (the glue reads that path; it is also `meta.ingest`).
    pub path: String,
    /// `--advisory` was given.
    pub advisory: bool,
    /// `--root` or `--repo`; `None` is argparse's default `.`.
    pub root: Option<String>,
    /// `--task-id`. The environment's `SWARM_TASK_ID` is the host's concern, not this parser's.
    pub context_task_id: Option<String>,
    /// `--correlation-id`.
    pub correlation_id: Option<String>,
}

/// Parses `argv` for the host ingest route; `None` when the call is outside the grammar (see the module documentation).
pub fn parse_ingest_args(argv: &[String]) -> Option<IngestArgs> {
    let mut out = IngestArgs::default();
    let (mut json, mut ingest) = (false, None);
    let mut index = 0;
    while index < argv.len() {
        let token = argv[index].as_str();
        let (name, inline) = match token.split_once('=') {
            Some((head, value)) if head.starts_with("--") => (head, Some(value)),
            _ => (token, None),
        };
        match name {
            "--json" | "--advisory" => {
                if inline.is_some() {
                    return None;
                }
                if name == "--json" {
                    json = true;
                } else {
                    out.advisory = true;
                }
            }
            "--ingest" | "--root" | "--repo" | "--task-id" | "--correlation-id" => {
                let value = match inline {
                    Some(value) => value.to_string(),
                    None => {
                        let next = argv.get(index + 1)?;
                        if next.is_empty() || next.starts_with('-') {
                            return None;
                        }
                        index += 1;
                        next.clone()
                    }
                };
                if value.is_empty() {
                    return None;
                }
                let slot = match name {
                    "--ingest" => &mut ingest,
                    "--root" | "--repo" => &mut out.root,
                    "--task-id" => &mut out.context_task_id,
                    _ => &mut out.correlation_id,
                };
                if slot.is_some() {
                    return None;
                }
                *slot = Some(value);
            }
            _ => return None,
        }
        index += 1;
    }
    if !json {
        return None;
    }
    out.path = ingest?;
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(items: &[&str]) -> Vec<String> {
        items.iter().copied().map(str::to_string).collect()
    }

    #[test]
    fn a_served_ingest_keeps_the_path_the_advisory_flag_and_the_ids() {
        let parsed = parse_ingest_args(&argv(&[
            "--json",
            "--advisory",
            "--ingest",
            "dir/result.json",
            "--root",
            "/work",
            "--task-id",
            "T-1",
            "--correlation-id=C-1",
        ]))
        .expect("served");
        assert_eq!(parsed.path, "dir/result.json");
        assert!(parsed.advisory);
        assert_eq!(parsed.root.as_deref(), Some("/work"));
        assert_eq!(parsed.context_task_id.as_deref(), Some("T-1"));
        assert_eq!(parsed.correlation_id.as_deref(), Some("C-1"));
    }

    #[test]
    fn an_ingest_outside_the_grammar_is_left_to_python() {
        let cases: &[&[&str]] = &[
            &["--ingest", "result.json"],
            &["--json", "--ingest"],
            &["--json", "--ingest", "--advisory"],
            &["--json", "--ingest=result.json", "--dry-run"],
            &[
                "--json",
                "--transition",
                "T-1",
                "DONE",
                "--ingest",
                "result.json",
            ],
            &["--json", "--history", "T-1", "--ingest", "result.json"],
            &["--json", "--ingest", "result.json", "--reason", "why"],
            &["--json", "--ing", "result.json"],
            &["--json", "--ingest", "result.json", "positional"],
            &["--json", "-h"],
        ];
        for items in cases {
            assert_eq!(parse_ingest_args(&argv(items)), None, "{items:?}");
        }
    }
}
