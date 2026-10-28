//! The `gsd_engine` binary end to end: the backend default and its validation, and the failover
//! notice that must stay off stderr whenever no failover happened.
//!
//! A sandbox failover cannot be forced from outside the process (the guest, its pin and its limits
//! are fixed), so the notice text, its code mapping and the stdout-unchanged property are checked in
//! the unit tests of `sandbox_engine.rs` and `bin/gsd_engine.rs`. Here the binary is run as the
//! Python selector runs it.

use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

use tempfile::TempDir;

type TestResult = Result<(), String>;

/// What one run of the binary produced.
struct Run {
    code: Option<i32>,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

/// Runs the binary once on `argv` with `GSD_RUST_BACKEND` set to `backend`, or removed for `None`.
fn run(backend: Option<&str>, root: &Path, argv: &[&str]) -> Result<Run, String> {
    let swarm = root.join(".swarm");
    let request = serde_json::json!({
        "agent": "A01",
        "script": "orch_status",
        "argv": argv,
        "root": root.display().to_string(),
        "env": {"SWARM_DIR": swarm.display().to_string()},
    })
    .to_string();
    let mut command = Command::new(env!("CARGO_BIN_EXE_gsd_engine"));
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    match backend {
        Some(value) => command.env("GSD_RUST_BACKEND", value),
        None => command.env_remove("GSD_RUST_BACKEND"),
    };
    let mut child = command.spawn().map_err(|e| e.to_string())?;
    child
        .stdin
        .take()
        .ok_or("no stdin pipe")?
        .write_all(request.as_bytes())
        .map_err(|e| e.to_string())?;
    let output = child.wait_with_output().map_err(|e| e.to_string())?;
    Ok(Run {
        code: output.status.code(),
        stdout: output.stdout,
        stderr: output.stderr,
    })
}

#[test]
fn a_read_prints_no_failover_notice_under_any_backend_setting() -> TestResult {
    let root = TempDir::new().map_err(|e| e.to_string())?;
    let calls: [&[&str]; 2] = [&["--json"], &["--history", "T-9999", "--json"]];
    for argv in calls {
        let host = run(Some("host"), root.path(), argv)?;
        assert!(host.stderr.is_empty(), "the host backend printed a notice");
        for backend in [None, Some(""), Some("sandbox")] {
            let other = run(backend, root.path(), argv)?;
            assert!(
                other.stderr.is_empty(),
                "backend {backend:?} printed to stderr: {}",
                String::from_utf8_lossy(&other.stderr)
            );
            assert_eq!(other.code, host.code, "backend {backend:?}");
            assert_eq!(other.stdout, host.stdout, "backend {backend:?}");
        }
    }
    let list = run(None, root.path(), &["--json"])?;
    assert_eq!(list.code, Some(0));
    assert!(!list.stdout.is_empty());
    Ok(())
}

#[test]
fn a_misspelt_backend_is_an_e_input_exit_two_without_a_notice() -> TestResult {
    let root = TempDir::new().map_err(|e| e.to_string())?;
    for backend in ["Sandbox", "hostt", "wasm", " host"] {
        let refused = run(Some(backend), root.path(), &["--json"])?;
        assert_eq!(refused.code, Some(2), "backend {backend:?}");
        assert!(refused.stderr.is_empty(), "backend {backend:?}");
        let value: serde_json::Value =
            serde_json::from_slice(&refused.stdout).map_err(|e| e.to_string())?;
        assert_eq!(value["error"]["code"].as_str(), Some("E-INPUT"));
        let text = String::from_utf8_lossy(&refused.stdout);
        assert!(!text.contains(backend), "the refusal echoed {backend:?}");
    }
    Ok(())
}
