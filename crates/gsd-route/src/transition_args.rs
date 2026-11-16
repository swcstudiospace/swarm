//! The argv grammar of the host `orch_status --transition` route (phase 14.5, unit U3.2a).
//!
//! [`parse_transition_args`] answers `Some` for exactly the argv the Rust route serves and `None` for everything else, so a caller that
//! gets `None` leaves the call to the Python body. The oracle is the real `argparse` parser of `scripts/orch_status.py` (built by
//! `AgentScript.parser()`, `allow_abbrev=False`): whatever this function accepts, argparse parses to the same values, and every
//! argv argparse rejects, reads as a help request or parses with a meaning this function does not model is `None`. The vectors in
//! `tests/vectors/transition_args.json` come from that parser (`tests/parity/gen_write_argv_vectors.py`).
//!
//! The grammar is `--transition TASK_ID STATE` together with `--json` (both required), and any of `--reason TEXT`, `--root PATH`,
//! `--repo PATH` (the same destination as `--root`), `--task-id ID`, `--correlation-id ID` and `--dry-run`, in any order. A value
//! option takes its value as the next argv element or after an `=` in the same element. A value in its own element that starts with
//! `-` is `None`: argparse reads such an element as an option (or as a number, or as text, by rules this function does not copy).
//! `TASK_ID` and `STATE` are not empty, `--transition` and each value option (`--root` and `--repo` share one slot) appear at most
//! once, and only `--json` and `--dry-run` may repeat: argparse keeps the last of a repeated option, and the route does not model that.
//! `--ingest`, `--history`, `--advisory`, an abbreviation, an unknown flag, an extra positional, a missing value, a `--` separator
//! and `-h` are all outside the grammar. The selector's `parse_write_argv` (`scripts/orch_status.py`) serves the same set.

/// The values of one served `orch_status --transition` call.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TransitionArgs {
    /// The `TASK_ID` of `--transition`.
    pub task_id: String,
    /// The `STATE` of `--transition`, exactly as written (the decision refuses a name that is not a task state).
    pub state: String,
    /// `--reason`; the empty string when the flag is absent, as argparse's default.
    pub reason: String,
    /// `--root` or `--repo`; `None` is argparse's default `.`.
    pub root: Option<String>,
    /// `--dry-run` was given.
    pub dry_run: bool,
    /// `--task-id`, the id of the run-log record (the environment's `SWARM_TASK_ID` is the host's concern, not this parser's).
    pub context_task_id: Option<String>,
    /// `--correlation-id`.
    pub correlation_id: Option<String>,
}

/// Parses `argv` for the host transition route; `None` when the call is outside the grammar (see the module documentation).
pub fn parse_transition_args(argv: &[String]) -> Option<TransitionArgs> {
    let mut out = TransitionArgs::default();
    let (mut json, mut transition_seen) = (false, false);
    let mut reason = None;
    let mut index = 0;
    while index < argv.len() {
        let token = argv[index].as_str();
        let (name, inline) = match token.split_once('=') {
            Some((head, value)) if head.starts_with("--") => (head, Some(value)),
            _ => (token, None),
        };
        match name {
            "--json" | "--dry-run" => {
                if inline.is_some() {
                    return None;
                }
                if name == "--json" {
                    json = true;
                } else {
                    out.dry_run = true;
                }
            }
            "--transition" => {
                if inline.is_some() || transition_seen {
                    return None;
                }
                let pair = argv.get(index + 1..index + 3)?;
                if pair
                    .iter()
                    .any(|part| part.is_empty() || part.starts_with('-'))
                {
                    return None;
                }
                let (task_id, state) = (&pair[0], &pair[1]);
                (out.task_id, out.state) = (task_id.clone(), state.clone());
                transition_seen = true;
                index += 2;
            }
            "--reason" | "--root" | "--repo" | "--task-id" | "--correlation-id" => {
                let slot = match name {
                    "--reason" => &mut reason,
                    "--root" | "--repo" => &mut out.root,
                    "--task-id" => &mut out.context_task_id,
                    _ => &mut out.correlation_id,
                };
                if slot.is_some() {
                    return None;
                }
                let value = match inline {
                    Some(value) => value.to_string(),
                    None => {
                        index += 1;
                        argv.get(index)
                            .filter(|value| !value.starts_with('-'))?
                            .clone()
                    }
                };
                *slot = Some(value);
            }
            _ => return None,
        }
        index += 1;
    }
    out.reason = reason.unwrap_or_default();
    (json && transition_seen).then_some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{obj_get, parse_json, Json};

    const VECTORS: &str = include_str!("../tests/vectors/transition_args.json");

    fn argv(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_string()).collect()
    }

    fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
        match value {
            Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
            other => panic!("not an object looking for {key}: {other:?}"),
        }
    }

    fn text(value: &Json) -> &str {
        match value {
            Json::Str(text) => text,
            other => panic!("not text: {other:?}"),
        }
    }

    fn optional_text(value: &Json) -> Option<String> {
        match value {
            Json::Null => None,
            other => Some(text(other).to_string()),
        }
    }

    fn flag(value: &Json) -> bool {
        match value {
            Json::Bool(flag) => *flag,
            other => panic!("not a bool: {other:?}"),
        }
    }

    fn items(value: &Json) -> &[Json] {
        match value {
            Json::Arr(items) => items,
            other => panic!("not a list: {other:?}"),
        }
    }

    /// What argparse parsed, as [`TransitionArgs`] (the root is always present there).
    fn expected(fields: &Json) -> TransitionArgs {
        let pair = items(member(fields, "transition"));
        TransitionArgs {
            task_id: text(&pair[0]).to_string(),
            state: text(&pair[1]).to_string(),
            reason: text(member(fields, "reason")).to_string(),
            root: optional_text(member(fields, "root")),
            dry_run: flag(member(fields, "dry_run")),
            context_task_id: optional_text(member(fields, "task_id")),
            correlation_id: optional_text(member(fields, "correlation_id")),
        }
    }

    fn cases() -> Vec<Json> {
        let doc = parse_json(VECTORS).expect("the vector file is JSON");
        items(member(&doc, "cases")).to_vec()
    }

    #[test]
    fn every_vector_is_served_exactly_as_the_file_says_and_as_argparse_parsed_it() {
        let cases = cases();
        let (mut served, mut refused) = (0, 0);
        for case in &cases {
            let name = text(member(case, "name"));
            let args: Vec<String> = items(member(case, "argv"))
                .iter()
                .map(|item| text(item).to_string())
                .collect();
            let parsed = parse_transition_args(&args);
            assert_eq!(
                parsed.is_some(),
                flag(member(case, "rust_serves")),
                "{name}: {args:?}"
            );
            match parsed {
                Some(got) => {
                    served += 1;
                    let argparse = member(case, "argparse");
                    assert!(
                        flag(member(argparse, "ok")),
                        "{name}: served, but argparse rejects {args:?}"
                    );
                    let fields = member(argparse, "fields");
                    // argparse's default for the root is `.`, which a parse without `--root` leaves as `None` here.
                    let got = TransitionArgs {
                        root: got.root.or_else(|| Some(".".to_string())),
                        ..got
                    };
                    assert_eq!(got, expected(fields), "{name}");
                    // The route also needs the parts of the namespace it does not read to be at their defaults.
                    assert!(flag(member(fields, "json")), "{name}");
                    for unread in ["ingest", "history"] {
                        assert_eq!(*member(fields, unread), Json::Null, "{name}: {unread}");
                    }
                    assert!(!flag(member(fields, "advisory")), "{name}");
                }
                None => refused += 1,
            }
        }
        // A parser that refuses everything, or accepts everything, must not pass on a thin corpus.
        assert!(cases.len() >= 300, "corpus has {} cases", cases.len());
        assert!(
            served >= 100 && refused >= 100,
            "{served} served, {refused} refused"
        );
    }

    #[test]
    fn argparse_accepting_is_not_enough_to_be_served_when_the_meaning_is_not_modelled() {
        // These parse under argparse too; they are outside the route's grammar and stay with Python.
        for case in cases() {
            let name = text(member(&case, "name"));
            if name.starts_with("unmodelled-") {
                assert!(flag(member(member(&case, "argparse"), "ok")), "{name}");
                assert!(!flag(member(&case, "rust_serves")), "{name}");
            }
        }
    }

    #[test]
    fn the_documented_grammar_in_a_table() {
        let json_transition = ["--transition", "T-1", "DONE", "--json"];
        /// The `(task_id, state, reason)` of a served argv.
        type Served = Option<(&'static str, &'static str, &'static str)>;
        let cases: [(&[&str], Served); 9] = [
            (&json_transition, Some(("T-1", "DONE", ""))),
            (
                &[
                    "--json",
                    "--reason",
                    "why not",
                    "--transition",
                    "T-1",
                    "DONE",
                ],
                Some(("T-1", "DONE", "why not")),
            ),
            (
                &["--transition", "T-1", "DONE", "--json", "--reason=a=b"],
                Some(("T-1", "DONE", "a=b")),
            ),
            (&["--transition", "T-1", "DONE"], None),
            (&["--trans", "T-1", "DONE", "--json"], None),
            (&["--transition", "T-1", "--json"], None),
            (&["--transition", "T-1", "DONE", "--json", "--reason"], None),
            (
                &["--transition", "T-1", "DONE", "--json", "--ingest", "p"],
                None,
            ),
            (&["--transition=T-1", "DONE", "--json"], None),
        ];
        for (items, want) in cases {
            let got = parse_transition_args(&argv(items))
                .map(|args| (args.task_id, args.state, args.reason));
            let want = want.map(|(a, b, c)| (a.to_string(), b.to_string(), c.to_string()));
            assert_eq!(got, want, "{items:?}");
        }
    }

    #[test]
    fn only_the_flags_may_repeat_and_repo_shares_the_root_slot() {
        let base = ["--transition", "T-1", "DONE", "--json"];
        let served = parse_transition_args(&argv(&[
            "--json",
            "--dry-run",
            "--dry-run",
            "--repo",
            "/b",
            "--transition",
            "T-1",
            "DONE",
        ]))
        .expect("served");
        assert_eq!(served.root.as_deref(), Some("/b"));
        assert!(served.dry_run);
        for extra in [
            &["--root", "/a", "--repo", "/b"][..],
            &["--root=/a", "--root", "/b"],
            &["--reason", "x", "--reason=y"],
            &["--task-id", "T-1", "--task-id", "T-2"],
            &["--correlation-id=C", "--correlation-id=D"],
            &["--transition", "T-2", "CLAIMED"],
        ] {
            let items: Vec<&str> = base.iter().chain(extra).copied().collect();
            assert_eq!(parse_transition_args(&argv(&items)), None, "{items:?}");
        }
        for empty in [
            ["--transition", "", "DONE", "--json"],
            ["--transition", "T-1", "", "--json"],
        ] {
            assert_eq!(parse_transition_args(&argv(&empty)), None, "{empty:?}");
        }
    }
}
