//! F-6 at the process boundary: each invocation is a new `gsd_engine` process, with no inherited
//! signing environment. A malformed request stops before routing, key loading or store writes;
//! its reply must stay identical with no key, each held key name, and each empty-present key.
//! Output assertions never print a key canary or raw child output.
//!
//! The binary's unit tests separately query the safe rustix dumpable API in subprocess-only
//! helpers, including a known dumpable baseline and an injected refusal with stdin held open.
//! A real kernel denial is not forced here: that would require changing the test host's policy.

use std::process::{Command, Output, Stdio};

const KEY_ENV_NAMES: [&str; 3] = [
    "SWARM_SIGNING_KEY",
    "SWARM_ED25519_KEY",
    "SWARM_ED25519_KEY_FILE",
];
const CANARY: &str = "f6-never-print-this-key-canary";

type TestResult = Result<(), String>;

/// EOF is deliberately not an invocation, so no backend or signing operation is needed.
fn run(key: Option<(&str, &str)>) -> Result<Output, String> {
    let root = tempfile::TempDir::new().map_err(|e| e.to_string())?;
    let mut command = Command::new(env!("CARGO_BIN_EXE_gsd_engine"));
    command
        .env_clear()
        .current_dir(root.path())
        .env("HOME", root.path())
        .env("XDG_CONFIG_HOME", root.path())
        .env("SWARM_DIR", root.path().join(".swarm"))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some((name, value)) = key {
        command.env(name, value);
    }
    command.output().map_err(|e| e.to_string())
}

fn input_refusal(output: &Output) -> TestResult {
    assert_eq!(output.status.code(), Some(2), "unexpected child exit code");
    assert!(output.stderr.is_empty(), "child wrote to stderr");
    assert!(
        !output
            .stdout
            .windows(CANARY.len())
            .any(|bytes| bytes == CANARY.as_bytes()),
        "stdout disclosed the key canary"
    );
    let reply: serde_json::Value =
        serde_json::from_slice(&output.stdout).map_err(|e| e.to_string())?;
    assert_eq!(reply["status"], "error");
    assert_eq!(reply["error"]["code"], "E-INPUT");
    Ok(())
}

fn same_reply(actual: &Output, expected: &Output) {
    // Equality assertions on the raw bytes would echo a leaked key when they fail.
    assert!(
        actual.stdout == expected.stdout,
        "key presence changed stdout"
    );
    assert!(
        actual.stderr == expected.stderr,
        "key presence changed stderr"
    );
    assert_eq!(actual.status.code(), expected.status.code());
}

#[test]
fn no_key_reaches_the_input_refusal_without_stderr() -> TestResult {
    input_refusal(&run(None)?)
}

#[test]
fn each_key_name_keeps_key_material_out_of_the_reply() -> TestResult {
    let baseline = run(None)?;
    input_refusal(&baseline)?;
    for name in KEY_ENV_NAMES {
        let output = run(Some((name, CANARY)))?;
        input_refusal(&output)?;
        same_reply(&output, &baseline);
    }
    Ok(())
}

#[test]
fn each_empty_present_key_preserves_the_reply() -> TestResult {
    let baseline = run(None)?;
    input_refusal(&baseline)?;
    for name in KEY_ENV_NAMES {
        let output = run(Some((name, "")))?;
        input_refusal(&output)?;
        same_reply(&output, &baseline);
    }
    Ok(())
}
