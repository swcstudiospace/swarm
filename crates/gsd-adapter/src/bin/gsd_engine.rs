//! The `gsd_engine` binary: the Rust answer to `scripts/orch_status.py` under `GSD_ENGINE=rust`
//! (U12.6).
//!
//! It reads one JSON object from stdin, `{agent, script, argv, root, env}` plus, for a write call, the
//! `effects_path` of the manifest the caller replays after the commit, passes `argv` through
//! unchanged (the SDK runner appends `--json` to the child), and answers through [`invoke_routed`].
//! `GSD_RUST_BACKEND` names the backend: `host` is host Rust, `sandbox` is [`SandboxEngine`] (OD-5),
//! and unset or empty is [`DEFAULT_BACKEND`]; any other value is `E-INPUT`. Stdout is the reply and
//! the exit code is the reply's exit code. When the sandbox fails over to host Rust, stderr gets one
//! fixed line with the wire error code only (`failover_notice`); stdout and the exit code do not
//! change. Whether a refusal or fault falls back to the Python body is the caller's decision, not
//! this binary's.

use std::env;
use std::io::{self, Read, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use gsd_adapter::route_table::invoke_routed;
use gsd_adapter::sandbox_engine::{failover_notice, SandboxEngine};
use gsd_adapter::strangler_seam::{error_reply, CoreBackend, Invocation, PythonEngine, RustEngine};
use gsd_failure::ErrorCode;
use gsd_route::{obj_get, parse_json, Json, Reply, SeamError};

/// Environment variable that names the Rust backend.
const BACKEND_ENV: &str = "GSD_RUST_BACKEND";

/// Per-call deadline. The stdin object carries none; 600 s is the default bridge timeout
/// (`plan/rust-wasmer-omp-options.md:60`).
const DEADLINE: Duration = Duration::from_secs(600);

/// The Rust backend a call runs on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Backend {
    Host,
    Sandbox,
}

/// The backend an unset or empty `GSD_RUST_BACKEND` selects. Host until the flip unit (R12 of
/// Phase 14.3) sets it, after the user's approval.
const DEFAULT_BACKEND: Backend = Backend::Host;

/// One answer: the reply for stdout and, when the sandbox failed over, the notice for stderr.
struct Answer {
    reply: Reply,
    notice: Option<String>,
}

fn main() -> ExitCode {
    let answer = answer();
    ExitCode::from(emit(
        &answer,
        &mut io::stdout().lock(),
        &mut io::stderr().lock(),
    ))
}

/// Writes the notice, if any, as one line to `stderr`, then the reply to `stdout`, and returns the
/// exit code. The notice never changes stdout or the exit code, and a stderr that cannot be written
/// is ignored: a diagnostic must not turn a read into a failure.
fn emit(answer: &Answer, stdout: &mut impl Write, stderr: &mut impl Write) -> u8 {
    if let Some(line) = &answer.notice {
        let _ = writeln!(stderr, "{line}");
    }
    if stdout
        .write_all(answer.reply.stdout.as_bytes())
        .and_then(|()| stdout.flush())
        .is_err()
    {
        return 2;
    }
    u8::try_from(answer.reply.exit_code).unwrap_or(2)
}

/// Answers the one call on stdin. A malformed call or an unknown backend is an `exit 2` error reply.
fn answer() -> Answer {
    let call = match read_invocation() {
        Ok(call) => call,
        Err(err) => return plain(error_reply("", "", &err)),
    };
    match backend_choice() {
        Ok(Backend::Host) => answer_with(RustEngine::new(), &call, |_| None),
        Ok(Backend::Sandbox) => answer_with(SandboxEngine::new(), &call, |sandbox| {
            sandbox.failover().map(failover_notice)
        }),
        Err(err) => plain(error_reply(&call.agent, &call.script, &err)),
    }
}

/// An answer with no notice.
fn plain(reply: Reply) -> Answer {
    Answer {
        reply,
        notice: None,
    }
}

/// Routes `call` to `rust` through the seam. A backend that cannot be built is an error reply.
/// `notice_of` reads the notice, if any, off the backend once the call is answered.
fn answer_with<R: CoreBackend>(
    rust: Result<R, SeamError>,
    call: &Invocation,
    notice_of: impl Fn(&R) -> Option<String>,
) -> Answer {
    let python = PythonEngine::new("python3");
    match rust {
        Ok(rust) => {
            let reply = invoke_routed(Some("rust"), &python, &rust, call);
            Answer {
                reply,
                notice: notice_of(&rust),
            }
        }
        Err(err) => plain(error_reply(&call.agent, &call.script, &err)),
    }
}

/// The backend `GSD_RUST_BACKEND` names for this process; unset or empty is [`DEFAULT_BACKEND`].
fn backend_choice() -> Result<Backend, SeamError> {
    match env::var(BACKEND_ENV) {
        Ok(value) => backend_named(&value),
        Err(env::VarError::NotPresent) => backend_named(""),
        Err(env::VarError::NotUnicode(_)) => Err(backend_error()),
    }
}

/// The backend a `GSD_RUST_BACKEND` value names: empty is [`DEFAULT_BACKEND`], `host` and `sandbox`
/// are explicit, anything else is `E-INPUT`.
fn backend_named(value: &str) -> Result<Backend, SeamError> {
    match value {
        "" => Ok(DEFAULT_BACKEND),
        "host" => Ok(Backend::Host),
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
    invocation_from(&String::from_utf8(raw).map_err(|_| input_error())?)
}

/// The invocation the stdin object `text` describes: `agent`, `script`, `argv`, `root` and `env`, plus the optional
/// `effects_path` of a write call (a non-empty string; the selector owns the file).
fn invocation_from(text: &str) -> Result<Invocation, SeamError> {
    let Json::Obj(fields) = parse_json(text).map_err(|_| input_error())? else {
        return Err(input_error());
    };
    let argv = string_list(&fields, "argv")?;
    let effects_path = match obj_get(&fields, "effects_path") {
        None | Some(Json::Null) => None,
        Some(Json::Str(path)) if !path.is_empty() => Some(PathBuf::from(path)),
        Some(_) => return Err(input_error()),
    };
    Ok(Invocation {
        agent: text_field(&fields, "agent")?,
        script: text_field(&fields, "script")?,
        argv,
        root: PathBuf::from(text_field(&fields, "root")?),
        env: env_pairs(&fields)?,
        deadline: DEADLINE,
        effects_path,
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
    fn effects_path_is_an_optional_non_empty_string() -> Result<(), String> {
        let call = |extra: &str| {
            invocation_from(&format!(
                r#"{{"agent": "A01", "script": "orch_status", "argv": ["--json"], "root": "/r", "env": {{}}{extra}}}"#
            ))
            .map_err(|e| e.message().to_string())
        };
        assert_eq!(call("")?.effects_path, None);
        assert_eq!(call(r#", "effects_path": null"#)?.effects_path, None);
        assert_eq!(
            call(r#", "effects_path": "/tmp/m.json""#)?.effects_path,
            Some(PathBuf::from("/tmp/m.json"))
        );
        for bad in [r#", "effects_path": """#, r#", "effects_path": 7"#] {
            assert!(call(bad).is_err(), "{bad}");
        }
        Ok(())
    }

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

    /// The line the sandbox failover prints, as `tests/parity/harness.py` parses it.
    const NOTICE: &str = "gsd_engine: sandbox fault E-DEP; answered by host Rust";

    fn reply(stdout: &str, exit_code: i32) -> Reply {
        Reply {
            exit_code,
            stdout: stdout.to_string(),
        }
    }

    /// What `emit` wrote to stdout and stderr, and the exit code it returned.
    fn emitted(answer: &Answer) -> (Vec<u8>, Vec<u8>, u8) {
        let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
        let code = emit(answer, &mut stdout, &mut stderr);
        (stdout, stderr, code)
    }

    #[test]
    fn backend_named_resolves_empty_to_the_default_and_keeps_explicit_names() {
        assert_eq!(backend_named("").ok(), Some(DEFAULT_BACKEND));
        assert_eq!(backend_named("host").ok(), Some(Backend::Host));
        assert_eq!(backend_named("sandbox").ok(), Some(Backend::Sandbox));
    }

    #[test]
    fn a_misspelt_backend_is_e_input_and_is_not_echoed() {
        for value in ["Sandbox", "hostt", " host", "host ", "wasm", "SANDBOX"] {
            match backend_named(value) {
                Ok(_) => panic!("{value:?} was accepted"),
                Err(error) => {
                    assert_eq!(error.code(), ErrorCode::Input, "{value:?}");
                    assert!(!error.message().contains(value), "{value:?}");
                }
            }
        }
    }

    /// The one default-asserting test of the backend: the flip unit (R12 of Phase 14.3) edits this
    /// test, and only this test, when it sets `DEFAULT_BACKEND`.
    #[test]
    fn default_backend_is_host_until_the_flip() {
        assert_eq!(DEFAULT_BACKEND, Backend::Host);
    }

    #[test]
    fn a_failover_prints_one_fixed_line_to_stderr_and_leaves_stdout_alone() {
        let host = plain(reply("{\"ok\": true}\n", 0));
        let failed_over = Answer {
            reply: reply("{\"ok\": true}\n", 0),
            notice: Some(NOTICE.to_string()),
        };
        let (host_out, host_err, host_code) = emitted(&host);
        let (out, err, code) = emitted(&failed_over);
        assert_eq!(out, host_out);
        assert_eq!(code, host_code);
        assert!(host_err.is_empty(), "no notice without a failover");
        assert_eq!(err, format!("{NOTICE}\n").into_bytes());
        assert_eq!(err.iter().filter(|&&byte| byte == b'\n').count(), 1);
    }

    #[test]
    fn exit_codes_are_the_replys_with_or_without_a_notice() {
        for notice in [None, Some(NOTICE.to_string())] {
            for (exit_code, expected) in [(0, 0), (1, 1), (2, 2), (300, 2), (-1, 2)] {
                let answer = Answer {
                    reply: reply("x", exit_code),
                    notice: notice.clone(),
                };
                assert_eq!(emitted(&answer).2, expected, "{exit_code} {notice:?}");
            }
        }
    }

    /// A writer that always fails.
    struct Broken;

    impl Write for Broken {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::Error::other("closed"))
        }

        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::other("closed"))
        }
    }

    #[test]
    fn an_unwritable_stderr_does_not_change_stdout_or_the_exit_code() {
        let answer = Answer {
            reply: reply("{\"ok\": true}\n", 0),
            notice: Some(NOTICE.to_string()),
        };
        let mut stdout = Vec::new();
        assert_eq!(emit(&answer, &mut stdout, &mut Broken), 0);
        assert_eq!(stdout, b"{\"ok\": true}\n");
        // An unwritable stdout is still exit 2, notice or not.
        let mut stderr = Vec::new();
        assert_eq!(emit(&answer, &mut Broken, &mut stderr), 2);
    }

    #[test]
    fn the_host_backend_carries_no_notice_and_a_notice_is_passed_through() -> Result<(), String> {
        let root = tempfile::TempDir::new().map_err(|e| e.to_string())?;
        let swarm = root.path().join(".swarm");
        let call = Invocation {
            agent: "A01".to_string(),
            script: "orch_status".to_string(),
            argv: vec!["--json".to_string()],
            root: root.path().to_path_buf(),
            env: vec![("SWARM_DIR".to_string(), swarm.display().to_string())],
            deadline: Duration::from_secs(30),
            effects_path: None,
        };
        let host = answer_with(RustEngine::new(), &call, |_| None);
        assert_eq!(host.reply.exit_code, 0);
        assert_eq!(host.notice, None);
        let noticed = answer_with(RustEngine::new(), &call, |_| Some(NOTICE.to_string()));
        assert_eq!(noticed.notice.as_deref(), Some(NOTICE));
        assert_eq!(noticed.reply.stdout, host.reply.stdout);
        Ok(())
    }
}
