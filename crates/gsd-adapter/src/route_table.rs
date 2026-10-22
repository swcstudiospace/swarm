//! Per-invocation engine routing for the strangler seam (U4.2).
//!
//! `GSD_ENGINE` is read on every call. `python` is the default. `rust` applies to one route class
//! only: the read-only `orch_status --json` listing and history. Every other `rust` request is
//! refused with `E-DEP`, so nothing past read-only flips (G-strangler-order). An unknown value is
//! rejected with `E-INPUT`.

use gsd_failure::ErrorCode;

use crate::join_affinity_guard::Engine;
use crate::strangler_seam::{
    error_reply, invoke, parse_orch_status_read, CoreBackend, Entrypoint, Invocation, Reply,
    SeamError,
};

/// Environment variable that selects the engine for each invocation.
pub const ENGINE_ENV: &str = "GSD_ENGINE";

/// Parses the `GSD_ENGINE` value. Unset or empty means `python`; the value is never echoed in the error.
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

/// Applies the requested engine to one call. `rust` on any route outside the read-only set is `E-DEP`.
pub fn route_for_call(
    requested: Engine,
    entry: Entrypoint,
    argv: &[String],
) -> Result<Engine, SeamError> {
    match requested {
        Engine::Python => Ok(Engine::Python),
        Engine::Rust if is_read_only_route(entry, argv) => Ok(Engine::Rust),
        Engine::Rust => Err(SeamError::new(
            ErrorCode::Dep,
            "no rust route for this entrypoint and flags; only read-only orch_status --json is served; unset GSD_ENGINE or use python",
        )),
    }
}

/// Resolves `GSD_ENGINE` and the route for one call to the engine that must answer it.
pub fn select_engine(
    env_value: Option<&str>,
    entry: Entrypoint,
    argv: &[String],
) -> Result<Engine, SeamError> {
    let requested = parse_engine(env_value)?;
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
    use std::path::PathBuf;
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
                &argv(&["--history", "T-A", "--json"])
            ),
            Ok(Engine::Rust)
        ));
        assert_eq!(
            code_of(route_for_call(
                Engine::Rust,
                Entrypoint::OrchStatus,
                &argv(&["--transition", "T", "DONE", "--json"])
            )),
            Some(ErrorCode::Dep)
        );
        assert_eq!(
            code_of(route_for_call(
                Engine::Rust,
                Entrypoint::OrchStatus,
                &argv(&["--ingest", "r.json", "--json"])
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

    fn call_for(script: &str, flags: &[&str], root: &PathBuf) -> Invocation {
        Invocation {
            agent: "A01".to_string(),
            script: script.to_string(),
            argv: argv(flags),
            root: root.clone(),
            env: Vec::new(),
            deadline: Duration::from_secs(5),
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
            &call_for(
                "orch_status",
                &["--transition", "T", "DONE", "--json"],
                &root,
            ),
        );
        assert_eq!(refused.exit_code, 2);
        let value: serde_json::Value =
            serde_json::from_str(&refused.stdout).map_err(|e| e.to_string())?;
        assert_eq!(value["error"]["code"].as_str(), Some("E-DEP"));
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
}
