//! Per-invocation engine routing for the strangler seam (U4.2).
//!
//! `GSD_ENGINE` is read on every call. An unset or empty value resolves through the per-route default
//! table ([`default_engine`]); `python` is explicit Python. `rust` applies to three route classes only:
//! the read-only `orch_status --json` listing and history, the host `orch_status --transition --json`
//! write route, and the host `orch_status --ingest PATH --json` route. Each is opt-in: its default row stays
//! Python until its flip unit. Every other `rust` request is refused with `E-DEP` and
//! `write_state = "not_started"`, so nothing else flips (G-strangler-order). An
//! unknown value is rejected with `E-INPUT`. Today every row of the table is Python; a route's row changes

use gsd_failure::ErrorCode;

use crate::join_affinity_guard::Engine;
use crate::strangler_seam::{
    before_any_write, error_reply, invoke, CoreBackend, Entrypoint, Invocation,
};
use gsd_route::ingest_args::parse_ingest_args;
use gsd_route::transition_args::parse_transition_args;
use gsd_route::{parse_orch_status_read, Reply, SeamError};

/// Environment variable that selects the engine for each invocation.
pub const ENGINE_ENV: &str = "GSD_ENGINE";

/// Parses the `GSD_ENGINE` value. Unset or empty means `python` here; a call resolves them through
/// [`default_engine`] instead (see [`select_engine`]). The value is never echoed in the error.
pub fn parse_engine(raw: Option<&str>) -> Result<Engine, SeamError> {
    match raw {
        None | Some("") | Some("python") => Ok(Engine::Python),
        Some("rust") => Ok(Engine::Rust),
        Some(_) => Err(SeamError::new(
            ErrorCode::Input,
            "GSD_ENGINE must be 'python' or 'rust'",
        )),
    }
}

/// Returns true when the call is on the one route the Rust engine serves: `orch_status --json`
/// without transition or ingest flags.
pub fn is_read_only_route(entry: Entrypoint, argv: &[String]) -> bool {
    matches!(entry, Entrypoint::OrchStatus)
        && parse_orch_status_read(argv).is_some_and(|args| args.json)
}

/// Returns true when the call is the host write route: `orch_status --transition TASK_ID STATE --json` inside the grammar of
/// [`parse_transition_args`], the same set the selector's `rust_serves_write` serves.
pub fn is_transition_route(entry: Entrypoint, argv: &[String]) -> bool {
    matches!(entry, Entrypoint::OrchStatus) && parse_transition_args(argv).is_some()
}

/// Returns true when the call is the host ingest route: `orch_status --ingest PATH --json` inside the grammar of
/// [`parse_ingest_args`], the same set the selector's `rust_serves_ingest` serves.
pub fn is_ingest_route(entry: Entrypoint, argv: &[String]) -> bool {
    matches!(entry, Entrypoint::OrchStatus) && parse_ingest_args(argv).is_some()
}

/// Returns true when the call is the host quality gate: `qa_gate --json` inside the grammar `gates::qa_gate` serves.
pub fn is_qa_gate_route(entry: Entrypoint, argv: &[String]) -> bool {
    matches!(entry, Entrypoint::QaGate) && crate::gates::qa_gate::served(argv)
}

/// The engine the `orch_status` read route runs on when `GSD_ENGINE` is unset or empty.
///
/// Python until the flip unit (R12 of Phase 14.3) sets it, after parity evidence and the user's
/// approval. Setting it to `Engine::Rust` is the whole flip for this route.
pub const ORCH_STATUS_READ_DEFAULT: Engine = Engine::Python;

/// The engine the `orch_status --transition` write route runs on when `GSD_ENGINE` is unset or empty.
///
/// Python until the write flip unit (U3.8c of Phase 14.5) sets it, after parity and fault evidence, a quiet-host baseline and
/// the user's approval. `GSD_ENGINE=rust` reaches the route whatever this row says.
pub const ORCH_STATUS_TRANSITION_DEFAULT: Engine = Engine::Python;

/// The engine the `orch_status --ingest` route runs on when `GSD_ENGINE` is unset or empty.
///
/// Python until the write flip unit (U3.8c of Phase 14.5) sets it. `GSD_ENGINE=rust` reaches the route whatever this row says.
pub const ORCH_STATUS_INGEST_DEFAULT: Engine = Engine::Python;

/// The per-route default engine: the engine that answers a call when `GSD_ENGINE` is unset or empty.
///
/// The `orch_status` read route resolves to [`ORCH_STATUS_READ_DEFAULT`] and the `--transition` route to
/// [`ORCH_STATUS_TRANSITION_DEFAULT`]; every other route is Python until its own phase adds a row. An explicit `GSD_ENGINE`
/// never goes through this table.
pub fn default_engine(entry: Entrypoint, argv: &[String]) -> Engine {
    if is_read_only_route(entry, argv) {
        ORCH_STATUS_READ_DEFAULT
    } else if is_transition_route(entry, argv) {
        ORCH_STATUS_TRANSITION_DEFAULT
    } else if is_ingest_route(entry, argv) {
        ORCH_STATUS_INGEST_DEFAULT
    } else {
        Engine::Python
    }
}

/// Applies the requested engine to one call. `rust` on any route outside the read-only and transition sets is `E-DEP`,
/// marked `write_state = "not_started"` because nothing was written.
pub fn route_for_call(
    requested: Engine,
    entry: Entrypoint,
    argv: &[String],
) -> Result<Engine, SeamError> {
    match requested {
        Engine::Python => Ok(Engine::Python),
        Engine::Rust
            if is_read_only_route(entry, argv)
                || is_transition_route(entry, argv)
                || is_ingest_route(entry, argv)
                || is_qa_gate_route(entry, argv) =>
        {
            Ok(Engine::Rust)
        }
        Engine::Rust => Err(before_any_write(SeamError::new(
            ErrorCode::Dep,
            "no rust route for this entrypoint and flags; only orch_status --json (read), orch_status --transition --json and orch_status --ingest PATH --json are served; unset GSD_ENGINE or use python",
        ))),
    }
}

/// Resolves `GSD_ENGINE` and the route for one call to the engine that must answer it.
///
/// Unset or empty takes the route's table default; `python` and `rust` are explicit and `rust` still
/// goes through [`route_for_call`], so it is `E-DEP` on a route Rust does not serve.
pub fn select_engine(
    env_value: Option<&str>,
    entry: Entrypoint,
    argv: &[String],
) -> Result<Engine, SeamError> {
    let requested = match env_value {
        None | Some("") => default_engine(entry, argv),
        Some(_) => parse_engine(env_value)?,
    };
    route_for_call(requested, entry, argv)
}

/// Routes one invocation to `python` or `rust` by `env_value` and answers through the seam.
///
/// A routing refusal comes back as an `exit 2` error reply, the same shape a backend failure has.
pub fn invoke_routed(
    env_value: Option<&str>,
    python: &dyn CoreBackend,
    rust: &dyn CoreBackend,
    call: &Invocation,
) -> Reply {
    let entry = Entrypoint::from_script(&call.script);
    match select_engine(env_value, entry, &call.argv) {
        Ok(Engine::Python) => invoke(python, call),
        Ok(Engine::Rust) => invoke(rust, call),
        Err(err) => error_reply(&call.agent, &call.script, &err),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::strangler_seam::{PythonEngine, RustEngine};
    use std::path::{Path, PathBuf};
    use std::time::Duration;

    type TestResult = Result<(), String>;

    fn argv(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    fn code_of(result: Result<Engine, SeamError>) -> Option<ErrorCode> {
        result.err().map(|e| e.code())
    }

    #[test]
    fn unset_and_empty_engine_default_to_python() -> TestResult {
        assert!(matches!(parse_engine(None), Ok(Engine::Python)));
        assert!(matches!(parse_engine(Some("")), Ok(Engine::Python)));
        assert!(matches!(parse_engine(Some("python")), Ok(Engine::Python)));
        assert!(matches!(parse_engine(Some("rust")), Ok(Engine::Rust)));
        Ok(())
    }

    #[test]
    fn unknown_engine_values_are_rejected_with_e_input() -> TestResult {
        assert_eq!(code_of(parse_engine(Some("go"))), Some(ErrorCode::Input));
        assert_eq!(code_of(parse_engine(Some("Rust"))), Some(ErrorCode::Input));
        assert_eq!(
            code_of(parse_engine(Some("python3"))),
            Some(ErrorCode::Input)
        );
        assert_eq!(
            code_of(select_engine(
                Some("nope"),
                Entrypoint::OrchStatus,
                &argv(&["--json"])
            )),
            Some(ErrorCode::Input)
        );
        Ok(())
    }

    #[test]
    fn python_is_always_routed_to_python() -> TestResult {
        let read = argv(&["--correlation-id", "C1", "--json"]);
        assert!(matches!(
            route_for_call(Engine::Python, Entrypoint::OrchStatus, &read),
            Ok(Engine::Python)
        ));
        assert!(matches!(
            route_for_call(Engine::Python, Entrypoint::SwarmRun, &argv(&["--json"])),
            Ok(Engine::Python)
        ));
        Ok(())
    }

    #[test]
    fn rust_serves_only_the_read_only_orch_status_route() -> TestResult {
        assert!(matches!(
            route_for_call(
                Engine::Rust,
                Entrypoint::OrchStatus,
                &argv(&["--correlation-id", "C1", "--json"])
            ),
            Ok(Engine::Rust)
        ));
        assert!(matches!(
            route_for_call(
                Engine::Rust,
                Entrypoint::OrchStatus,
                &argv(&["--ingest", "r.json", "--json"])
            ),
            Ok(Engine::Rust)
        ));
        assert!(matches!(
            route_for_call(
                Engine::Rust,
                Entrypoint::OrchStatus,
                &argv(&["--history", "T-A", "--json"])
            ),
            Ok(Engine::Rust)
        ));
        // A transition outside the write route's grammar (here: no `--json`) is still refused.
        assert_eq!(
            code_of(route_for_call(
                Engine::Rust,
                Entrypoint::OrchStatus,
                &argv(&["--transition", "T", "DONE"])
            )),
            Some(ErrorCode::Dep)
        );
        assert_eq!(
            code_of(route_for_call(
                Engine::Rust,
                Entrypoint::OrchStatus,
                &argv(&["--ingest", "r.json"])
            )),
            Some(ErrorCode::Dep)
        );
        assert_eq!(
            code_of(route_for_call(
                Engine::Rust,
                Entrypoint::OrchStatus,
                &argv(&["--history", "T-A"])
            )),
            Some(ErrorCode::Dep)
        );
        assert_eq!(
            code_of(route_for_call(
                Engine::Rust,
                Entrypoint::SwarmRun,
                &argv(&["--json"])
            )),
            Some(ErrorCode::Dep)
        );
        assert_eq!(
            code_of(route_for_call(
                Engine::Rust,
                Entrypoint::LaneVerb,
                &argv(&["--json"])
            )),
            Some(ErrorCode::Dep)
        );
        Ok(())
    }

    fn call_for(script: &str, flags: &[&str], root: &Path) -> Invocation {
        Invocation {
            agent: "A01".to_string(),
            script: script.to_string(),
            argv: argv(flags),
            root: root.to_path_buf(),
            env: Vec::new(),
            deadline: Duration::from_secs(5),
            effects_path: None,
        }
    }

    #[test]
    fn routing_refusal_is_an_exit_two_error_reply() -> TestResult {
        let root = PathBuf::from("/nonexistent-agent-swarm-root");
        let python = PythonEngine::new("python3");
        let rust = RustEngine::new().map_err(|e| e.message().to_string())?;
        let refused = invoke_routed(
            Some("rust"),
            &python,
            &rust,
            &call_for("orch_status", &["--transition", "T", "DONE"], &root),
        );
        assert_eq!(refused.exit_code, 2);
        let value: serde_json::Value =
            serde_json::from_str(&refused.stdout).map_err(|e| e.to_string())?;
        assert_eq!(value["error"]["code"].as_str(), Some("E-DEP"));
        assert_eq!(
            value["error"]["details"]["write_state"].as_str(),
            Some("not_started")
        );
        assert_eq!(value["status"].as_str(), Some("error"));
        assert_eq!(value["agent"].as_str(), Some("A01"));
        Ok(())
    }

    #[test]
    fn unknown_engine_value_is_an_e_input_error_reply() -> TestResult {
        let root = PathBuf::from("/nonexistent-agent-swarm-root");
        let python = PythonEngine::new("python3");
        let rust = RustEngine::new().map_err(|e| e.message().to_string())?;
        let reply = invoke_routed(
            Some("cobol"),
            &python,
            &rust,
            &call_for("orch_status", &["--json"], &root),
        );
        assert_eq!(reply.exit_code, 2);
        let value: serde_json::Value =
            serde_json::from_str(&reply.stdout).map_err(|e| e.to_string())?;
        assert_eq!(value["error"]["code"].as_str(), Some("E-INPUT"));
        Ok(())
    }

    #[test]
    fn unset_and_empty_resolve_through_the_route_table() -> TestResult {
        let read = argv(&["--json"]);
        let history = argv(&["--history", "T-A", "--json"]);
        for value in [None, Some("")] {
            for route in [&read, &history] {
                let chosen = select_engine(value, Entrypoint::OrchStatus, route)
                    .map_err(|e| e.message().to_string())?;
                assert_eq!(chosen, default_engine(Entrypoint::OrchStatus, route));
                assert_eq!(chosen, ORCH_STATUS_READ_DEFAULT);
            }
        }
        Ok(())
    }

    #[test]
    fn unset_and_empty_on_a_non_read_route_resolve_to_python() -> TestResult {
        let routes = [
            (
                Entrypoint::OrchStatus,
                argv(&["--transition", "T", "DONE", "--json"]),
            ),
            (
                Entrypoint::OrchStatus,
                argv(&["--ingest", "r.json", "--json"]),
            ),
            (Entrypoint::OrchStatus, argv(&["--history", "T-A"])),
            (Entrypoint::SwarmRun, argv(&["--json"])),
            (Entrypoint::LaneVerb, argv(&["--json"])),
        ];
        for value in [None, Some("")] {
            for (entry, flags) in &routes {
                let chosen =
                    select_engine(value, *entry, flags).map_err(|e| e.message().to_string())?;
                assert_eq!(chosen, Engine::Python);
            }
        }
        Ok(())
    }

    #[test]
    fn every_non_read_route_defaults_to_python_whatever_the_read_row_says() {
        assert_eq!(
            default_engine(Entrypoint::SwarmRun, &argv(&["--json"])),
            Engine::Python
        );
        assert_eq!(
            default_engine(Entrypoint::LaneVerb, &argv(&["--json"])),
            Engine::Python
        );
        for flags in [
            &["--transition", "T", "DONE", "--json"][..],
            &["--ingest", "r.json", "--json"][..],
            &["--history", "T-A"][..],
            &[][..],
        ] {
            assert_eq!(
                default_engine(Entrypoint::OrchStatus, &argv(flags)),
                Engine::Python,
                "{flags:?}"
            );
        }
    }

    #[test]
    fn explicit_python_and_rust_ignore_the_table() -> TestResult {
        let read = argv(&["--json"]);
        let write = argv(&["--transition", "T", "DONE", "--json"]);
        assert_eq!(
            select_engine(Some("python"), Entrypoint::OrchStatus, &read).ok(),
            Some(Engine::Python)
        );
        assert_eq!(
            select_engine(Some("python"), Entrypoint::OrchStatus, &write).ok(),
            Some(Engine::Python)
        );
        assert_eq!(
            select_engine(Some("rust"), Entrypoint::OrchStatus, &read).ok(),
            Some(Engine::Rust)
        );
        assert_eq!(
            select_engine(Some("rust"), Entrypoint::OrchStatus, &write).ok(),
            Some(Engine::Rust)
        );
        let outside_the_grammar = argv(&["--transition", "T", "DONE"]);
        assert_eq!(
            code_of(select_engine(
                Some("rust"),
                Entrypoint::OrchStatus,
                &outside_the_grammar
            )),
            Some(ErrorCode::Dep)
        );
        assert_eq!(
            code_of(select_engine(Some("rust"), Entrypoint::SwarmRun, &read)),
            Some(ErrorCode::Dep)
        );
        Ok(())
    }

    #[test]
    fn the_transition_route_is_exactly_the_write_grammar_on_orch_status() {
        let cases: [(Entrypoint, &[&str], bool); 8] = [
            (
                Entrypoint::OrchStatus,
                &["--transition", "T", "DONE", "--json"],
                true,
            ),
            (
                Entrypoint::OrchStatus,
                &[
                    "--json",
                    "--dry-run",
                    "--reason",
                    "r",
                    "--transition",
                    "T",
                    "DONE",
                ],
                true,
            ),
            (
                Entrypoint::OrchStatus,
                &["--transition", "T", "DONE"],
                false,
            ),
            (
                Entrypoint::OrchStatus,
                &["--trans", "T", "DONE", "--json"],
                false,
            ),
            (
                Entrypoint::OrchStatus,
                &["--transition", "T", "DONE", "--json", "--ingest", "p"],
                false,
            ),
            (Entrypoint::OrchStatus, &["--json"], false),
            (
                Entrypoint::OrchPlan,
                &["--transition", "T", "DONE", "--json"],
                false,
            ),
            (
                Entrypoint::LaneVerb,
                &["--transition", "T", "DONE", "--json"],
                false,
            ),
        ];
        for (entry, flags, expected) in cases {
            assert_eq!(
                is_transition_route(entry, &argv(flags)),
                expected,
                "{flags:?}"
            );
        }
        // A call is one route or the other, never both.
        assert!(!is_read_only_route(
            Entrypoint::OrchStatus,
            &argv(&["--transition", "T", "DONE", "--json"])
        ));
    }

    #[test]
    fn the_transition_row_resolves_through_its_own_constant() -> TestResult {
        let write = argv(&["--transition", "T", "DONE", "--json"]);
        for value in [None, Some("")] {
            let chosen = select_engine(value, Entrypoint::OrchStatus, &write)
                .map_err(|e| e.message().to_string())?;
            assert_eq!(chosen, ORCH_STATUS_TRANSITION_DEFAULT);
            assert_eq!(chosen, default_engine(Entrypoint::OrchStatus, &write));
        }
        Ok(())
    }

    /// The one default-asserting test of the write-route row: the flip unit (U3.8c of Phase 14.5) edits this test, and only
    /// this test, when it sets `ORCH_STATUS_TRANSITION_DEFAULT`.
    #[test]
    fn transition_route_default_is_python_until_the_flip() {
        assert_eq!(ORCH_STATUS_TRANSITION_DEFAULT, Engine::Python);
    }

    /// The one default-asserting test of the read-route row: the flip unit (R12 of Phase 14.3) edits
    /// this test, and only this test, when it sets `ORCH_STATUS_READ_DEFAULT`.
    #[test]
    fn read_route_default_is_python_until_the_flip() {
        assert_eq!(ORCH_STATUS_READ_DEFAULT, Engine::Python);
    }
}
