//! Fault and failover checks for the `orch_status` read route, through the public API only.
//!
//! The guest-level classes (fuel, memory, malformed input, truncated reply) sit below failover and
//! are checked in the `read_route_faults` module of `sandbox_engine.rs`. Here a guest fault must
//! never surface as an answer that differs from host Rust, a refusal must come before any read,
//! and no error may carry a path or an environment value.

use std::fs;
use std::path::Path;
use std::time::Duration;

use gsd_adapter::sandbox_engine::SandboxEngine;
use gsd_adapter::strangler_seam::{invoke, CoreBackend, Invocation, RustEngine};
use gsd_failure::ErrorCode;
use gsd_route::SeamError;
use tempfile::TempDir;

type TestResult = Result<(), String>;

const DEADLINE: Duration = Duration::from_secs(30);
/// A value the environment overlay carries; no error may echo it. The space and the slash make
/// `check_safe_id` reject it.
const SECRET: &str = "CORR SECRET/7f3a";

fn message(error: SeamError) -> String {
    error.message().to_string()
}

fn engine() -> Result<SandboxEngine, String> {
    SandboxEngine::new().map_err(message)
}

fn host() -> Result<RustEngine, String> {
    RustEngine::new().map_err(message)
}

fn call(root: &Path, swarm: &Path, argv: &[&str], deadline: Duration) -> Invocation {
    Invocation {
        agent: "A01".to_string(),
        script: "orch_status".to_string(),
        argv: argv.iter().map(|s| s.to_string()).collect(),
        root: root.to_path_buf(),
        env: vec![("SWARM_DIR".to_string(), swarm.display().to_string())],
        deadline,
        effects_path: None,
    }
}

/// The same call with a secret carried in the environment overlay.
fn call_with_secret(root: &Path, swarm: &Path, argv: &[&str], deadline: Duration) -> Invocation {
    let mut invocation = call(root, swarm, argv, deadline);
    invocation
        .env
        .push(("SWARM_CORRELATION_ID".to_string(), SECRET.to_string()));
    invocation
}

#[test]
fn reads_match_host_rust_on_an_empty_store() -> TestResult {
    let root = TempDir::new().map_err(|e| e.to_string())?;
    let swarm = root.path().join(".swarm");
    let sandbox = engine()?;
    let host = host()?;

    let list = call(root.path(), &swarm, &["--json"], DEADLINE);
    let unknown = call(
        root.path(),
        &swarm,
        &["--history", "T-9999", "--json"],
        DEADLINE,
    );
    assert_eq!(sandbox.orch_status(&list), host.orch_status(&list));
    assert_eq!(sandbox.orch_status(&unknown), host.orch_status(&unknown));

    let reply = sandbox.orch_status(&list).map_err(message)?;
    assert_eq!(reply.exit_code, 0);
    Ok(())
}

#[test]
fn a_mutating_argv_is_refused_before_any_read() -> TestResult {
    let root = TempDir::new().map_err(|e| e.to_string())?;
    // A store path that is a regular file: any read fails while opening the store.
    let file_store = root.path().join("store-is-a-file");
    fs::write(&file_store, b"not a directory").map_err(|e| e.to_string())?;
    let empty = TempDir::new().map_err(|e| e.to_string())?;
    let empty_store = empty.path().join(".swarm");
    let sandbox = engine()?;

    let transition = ["--transition", "T-0000", "DONE", "--json"];
    let ingest = ["--ingest", "result.json", "--json"];
    let over_file = call(root.path(), &file_store, &transition, DEADLINE);
    let over_empty = call(empty.path(), &empty_store, &transition, DEADLINE);
    let ingest_over_file = call(root.path(), &file_store, &ingest, DEADLINE);

    let refused = sandbox
        .orch_status(&over_file)
        .err()
        .ok_or("a mutating argv was answered")?;
    assert_eq!(refused.code(), ErrorCode::Dep);
    // The refusal does not depend on the store or the root: it is decided before any read.
    let refused_elsewhere = sandbox
        .orch_status(&over_empty)
        .err()
        .ok_or("a mutating argv was answered")?;
    assert_eq!(refused_elsewhere, refused);
    let refused_ingest = sandbox
        .orch_status(&ingest_over_file)
        .err()
        .ok_or("an --ingest argv was answered")?;
    assert_eq!(refused_ingest.code(), ErrorCode::Dep);

    let read = call(root.path(), &file_store, &["--json"], DEADLINE);
    let read_error = sandbox
        .orch_status(&read)
        .err()
        .ok_or("a read over a file store was answered")?;
    assert_ne!(read_error, refused);

    let reply = invoke(&sandbox, &over_file);
    assert_eq!(reply.exit_code, 2);
    assert!(reply.stdout.contains("E-DEP"), "{}", reply.stdout);
    Ok(())
}

#[test]
fn a_spent_deadline_is_e_timeout() -> TestResult {
    let root = TempDir::new().map_err(|e| e.to_string())?;
    let swarm = root.path().join(".swarm");
    let sandbox = engine()?;
    let spent = call(root.path(), &swarm, &["--json"], Duration::ZERO);
    let error = sandbox
        .orch_status(&spent)
        .err()
        .ok_or("a spent deadline was answered")?;
    assert_eq!(error.code(), ErrorCode::Timeout);
    Ok(())
}

#[test]
fn error_messages_carry_no_path_or_secret() -> TestResult {
    let root = TempDir::new().map_err(|e| e.to_string())?;
    let root_text = root.path().display().to_string();
    let swarm = root.path().join(".swarm");
    let sandbox = engine()?;
    let failing = [
        call_with_secret(
            root.path(),
            &swarm,
            &["--transition", "T-0000", "DONE", "--json"],
            DEADLINE,
        ),
        call_with_secret(
            root.path(),
            &swarm,
            &["--history", "T-9999", "--json"],
            DEADLINE,
        ),
        call_with_secret(root.path(), &swarm, &["--json"], Duration::ZERO),
        call_with_secret(
            root.path(),
            &swarm,
            &["--history", "../escape", "--json"],
            DEADLINE,
        ),
    ];
    for case in &failing {
        let error = sandbox
            .orch_status(case)
            .err()
            .ok_or("a failing case was answered")?;
        assert!(!error.message().contains(&root_text), "{}", error.message());
        assert!(!error.message().contains(SECRET), "{}", error.message());

        let reply = invoke(&sandbox, case);
        assert_eq!(reply.exit_code, 2);
        assert!(!reply.stdout.contains(&root_text), "{}", reply.stdout);
        assert!(!reply.stdout.contains(SECRET), "{}", reply.stdout);
    }

    // The secret is not a safe id, so the environment check refuses it. The refusal must not
    // echo it, and it must be the correlation id check that refused it.
    let rejected = sandbox
        .orch_status(&failing[1])
        .err()
        .ok_or("a rejected correlation id was answered")?;
    assert_eq!(rejected.code(), ErrorCode::Input);
    assert!(
        rejected.message().contains("correlation id"),
        "{}",
        rejected.message()
    );
    Ok(())
}
