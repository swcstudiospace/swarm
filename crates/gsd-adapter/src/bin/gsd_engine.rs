//! The `gsd_engine` binary: the Rust answer to `scripts/orch_status.py` under `GSD_ENGINE=rust`
//! (U12.6).
//!
//! It reads one JSON object from stdin, `{agent, script, argv, root, env}`, passes `argv` through
//! unchanged (the SDK runner appends `--json` to the child), and answers through [`invoke_routed`].
//! `GSD_RUST_BACKEND` names the backend:
//! unset or `host` is host Rust, `sandbox` is [`SandboxEngine`] (OD-5). Stdout is the reply and the
//! exit code is the reply's exit code. Whether a refusal or fault falls back to the Python body is
//! the caller's decision, not this binary's.

use std::env;
use std::io::{self, Read, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use gsd_adapter::route_table::invoke_routed;
use gsd_adapter::sandbox_engine::SandboxEngine;
use gsd_adapter::strangler_seam::{error_reply, CoreBackend, Invocation, PythonEngine, RustEngine};
use gsd_failure::ErrorCode;
use gsd_route::{obj_get, parse_json, Json, Reply, SeamError};

/// Environment variable that names the Rust backend.
const BACKEND_ENV: &str = "GSD_RUST_BACKEND";

/// Per-call deadline. The stdin object carries none; 600 s is the default bridge timeout
/// (`plan/rust-wasmer-omp-options.md:60`).
const DEADLINE: Duration = Duration::from_secs(600);

/// The Rust backend a call runs on.
enum Backend {
    Host,
    Sandbox,
}

fn main() -> ExitCode {
    let reply = answer();
    let mut stdout = io::stdout().lock();
    if stdout
        .write_all(reply.stdout.as_bytes())
        .and_then(|()| stdout.flush())
        .is_err()
    {
        return ExitCode::from(2);
    }
    ExitCode::from(u8::try_from(reply.exit_code).unwrap_or(2))
}

/// Answers the one call on stdin. A malformed call or an unknown backend is an `exit 2` error reply.
fn answer() -> Reply {
    let call = match read_invocation() {
        Ok(call) => call,
        Err(err) => return error_reply("", "", &err),
    };
    match backend_choice() {
        Ok(Backend::Host) => answer_with(RustEngine::new(), &call),
        Ok(Backend::Sandbox) => answer_with(SandboxEngine::new(), &call),
        Err(err) => error_reply(&call.agent, &call.script, &err),
    }
}

/// Routes `call` to `rust` through the seam. A backend that cannot be built is an error reply.
fn answer_with<R: CoreBackend>(rust: Result<R, SeamError>, call: &Invocation) -> Reply {
    let python = PythonEngine::new("python3");
    match rust {
        Ok(rust) => invoke_routed(Some("rust"), &python, &rust, call),
        Err(err) => error_reply(&call.agent, &call.script, &err),
    }
}

fn backend_choice() -> Result<Backend, SeamError> {
    let value = match env::var(BACKEND_ENV) {
        Ok(value) => value,
        Err(env::VarError::NotPresent) => String::new(),
        Err(env::VarError::NotUnicode(_)) => return Err(backend_error()),
    };
    match value.as_str() {
        "" | "host" => Ok(Backend::Host),
        "sandbox" => Ok(Backend::Sandbox),
        _ => Err(backend_error()),
    }
}

fn backend_error() -> SeamError {
    SeamError::new(
        ErrorCode::Input,
        "GSD_RUST_BACKEND must be 'host' or 'sandbox'",
    )
}

/// Reads the stdin object and builds the invocation. Stdin has no size cap of its own: the seam's
/// 100 000-byte cap covers only `--input` in the argv, which `invoke` checks after this point.
fn read_invocation() -> Result<Invocation, SeamError> {
    let mut raw = Vec::new();
    io::stdin()
        .read_to_end(&mut raw)
        .map_err(|_| input_error())?;
    let text = String::from_utf8(raw).map_err(|_| input_error())?;
    let Json::Obj(fields) = parse_json(&text).map_err(|_| input_error())? else {
        return Err(input_error());
    };
    let argv = string_list(&fields, "argv")?;
    Ok(Invocation {
        agent: text_field(&fields, "agent")?,
        script: text_field(&fields, "script")?,
        argv,
        root: PathBuf::from(text_field(&fields, "root")?),
        env: env_pairs(&fields)?,
        deadline: DEADLINE,
    })
}

fn input_error() -> SeamError {
    SeamError::new(
        ErrorCode::Input,
        "stdin must be one JSON object with agent, script, argv, root and env",
    )
}

fn field<'a>(fields: &'a [(String, Json)], key: &str) -> Result<&'a Json, SeamError> {
    obj_get(fields, key).ok_or_else(input_error)
}

fn text_field(fields: &[(String, Json)], key: &str) -> Result<String, SeamError> {
    match field(fields, key)? {
        Json::Str(text) => Ok(text.clone()),
        _ => Err(input_error()),
    }
}

fn string_list(fields: &[(String, Json)], key: &str) -> Result<Vec<String>, SeamError> {
    match field(fields, key)? {
        Json::Arr(items) => items
            .iter()
            .map(|item| match item {
                Json::Str(text) => Ok(text.clone()),
                _ => Err(input_error()),
            })
            .collect(),
        _ => Err(input_error()),
    }
}

/// Environment keys the Python selector forwards (`scripts/orch_status.py` `RUST_ENV_KEYS`).
const ENV_KEYS: [&str; 3] = ["SWARM_DIR", "SWARM_TASK_ID", "SWARM_CORRELATION_ID"];

/// The allowlisted, non-empty env pairs of the call. Other keys are dropped unread.
fn env_pairs(fields: &[(String, Json)]) -> Result<Vec<(String, String)>, SeamError> {
    match field(fields, "env")? {
        Json::Obj(pairs) => {
            let mut kept = Vec::new();
            for (name, value) in pairs {
                if !ENV_KEYS.contains(&name.as_str()) {
                    continue;
                }
                match value {
                    Json::Str(text) if text.is_empty() => {}
                    Json::Str(text) => kept.push((name.clone(), text.clone())),
                    _ => return Err(input_error()),
                }
            }
            Ok(kept)
        }
        _ => Err(input_error()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_pairs_keeps_only_the_three_selector_keys() -> Result<(), String> {
        let text = r#"{"env": {"SWARM_DIR": "/s", "SWARM_TASK_ID": "", "PATH": "/bin", "SWARM_CORRELATION_ID": "C1", "HOME": 7}}"#;
        let Json::Obj(fields) = parse_json(text).map_err(|e| e.message().to_string())? else {
            return Err("fixture is not an object".to_string());
        };
        let kept = env_pairs(&fields).map_err(|e| e.message().to_string())?;
        assert_eq!(
            kept,
            vec![
                ("SWARM_DIR".to_string(), "/s".to_string()),
                ("SWARM_CORRELATION_ID".to_string(), "C1".to_string()),
            ]
        );
        Ok(())
    }
}
