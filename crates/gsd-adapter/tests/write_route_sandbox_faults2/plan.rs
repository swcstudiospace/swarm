//! The `orch_plan` route on the sandbox (Phase 14.5, pin P2): the verified shadow of the host derivation, fault class by fault class, and
//! the route itself on the sandbox engine.
//!
//! The requests are the recorded vector cases of `gsd-route` (`tests/vectors/plan_derive.json`), read through the production decoder, so
//! the guest of these tests runs the same `plan_wire::invoke` the wasm guest runs. The guest is [`InProcessGuest`], honest or faulty on
//! purpose, or a scripted guest of this file.
//!
//! What is proved here:
//!
//! - every one of the recorded cases verifies through the shadow with the honest guest, and no fault is recorded;
//! - each fault class of the shadow: a guest error (the code it reports is the failover), a spent deadline (`E-TIMEOUT`, no failover),
//!   fuel, garbage, a mangled reply, an oversized reply, an identity failure (the guest is never called), and host facts that differ
//!   from the encoded facts (`E-CONTRACT`);
//! - the route on the sandbox engine prints the bytes the host route prints when the guest is honest, one guest check per answer, and
//!   keeps the host's answer, with the fault recorded, when the guest faults;
//! - a spent deadline inside the write transaction ends the call as `E-TIMEOUT` and writes nothing: the store and the plan file are as
//!   they were.

use std::fs;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Once};
use std::time::Duration;

use super::*;
use gsd_adapter::sandbox_engine::SandboxEngine;
use gsd_adapter::strangler_seam::{CoreBackend, Invocation, RustEngine};
use gsd_failure::ErrorCode;
use gsd_route::plan_derive::{derive_plan, PlanFacts, PlanRequest};
use gsd_route::plan_wire::{decode_request, encode_outcome, encode_request, MODE};
use gsd_route::pydumps::{parse_python_json, to_python_dumps};
use gsd_route::{jstr, obj_get, parse_json, Json};
use sqlx::sqlite::{SqliteConnectOptions, SqliteRow};
use sqlx::{Column, ConnectOptions, Connection, Row, TypeInfo, ValueRef};
use tempfile::TempDir;

const VECTORS: &str = include_str!("../../../gsd-route/tests/vectors/plan_derive.json");

/// The correlation the route tests plan under, so the plan file has a known name.
const CORRELATION: &str = "c0ffee01-aaaa-4bbb-8ccc-0000000000aa";

/// The brief the route tests plan.
const BRIEF: &str = "Build the orders service.";

/// The Python-built empty store: the schema and the migrations are Python's.
const TEMPLATE_SCRIPT: &str =
    "import sys\nfrom swarm.taskstore import TaskStore\nTaskStore(sys.argv[1]).conn.close()\n";

static TEMPLATE: LazyLock<Vec<u8>> = LazyLock::new(|| {
    let dir = TempDir::new().expect("tempdir");
    let db = dir.path().join("tasks.db");
    let output = std::process::Command::new("python3")
        .arg("-c")
        .arg(TEMPLATE_SCRIPT)
        .arg(&db)
        .current_dir(repo_root())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("SWARM_PURE_PYTHON", "1")
        .env_remove("SWARM_TASKSTORE_WAL")
        .output()
        .expect("run python3");
    assert!(
        output.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    fs::read(&db).expect("read template")
});

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

fn block<F: Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime")
        .block_on(future)
}

// ---- the recorded cases --------------------------------------------------------------------------------------------

fn document() -> Json {
    parse_json(VECTORS).expect("the vector file parses")
}

fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
    match value {
        Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
        other => panic!("not an object looking for {key}: {other:?}"),
    }
}

fn items(value: &Json) -> &[Json] {
    match value {
        Json::Arr(items) => items,
        other => panic!("not a list: {other:?}"),
    }
}

fn name_of(case: &Json) -> &str {
    match member(case, "name") {
        Json::Str(name) => name,
        other => panic!("a case has no name: {other:?}"),
    }
}

/// The wire request of one recorded case: its `request` and `facts`, with the roster the document holds once.
fn wire_of(document: &Json, case: &Json) -> Json {
    let mut facts = member(case, "facts").clone();
    if let Json::Obj(inner) = &mut facts {
        inner.push(("agents".to_string(), member(document, "agents").clone()));
    }
    Json::Obj(vec![
        ("mode".to_string(), jstr(MODE)),
        ("now".to_string(), member(case, "now").clone()),
        ("facts".to_string(), facts),
        ("request".to_string(), member(case, "request").clone()),
    ])
}

/// The request of the named recorded case, decoded by the production decoder.
fn case_named(name: &str) -> (PlanFacts, PlanRequest, f64) {
    let document = document();
    let case = items(member(&document, "cases"))
        .iter()
        .find(|case| name_of(case) == name)
        .unwrap_or_else(|| panic!("no case {name}"));
    decode_request(&wire_of(&document, case)).expect("the case decodes")
}

/// The request bytes and the host's answer bytes of one derivation.
fn bytes_of(facts: &PlanFacts, request: &PlanRequest, now: f64) -> (Vec<u8>, Vec<u8>) {
    (
        encode_request(facts, request, now).into_bytes(),
        encode_outcome(&derive_plan(facts, request, now)).into_bytes(),
    )
}

/// A created plan's bytes: the case most of the fault tests use.
fn created_bytes() -> (Vec<u8>, Vec<u8>) {
    let (facts, request, now) = case_named("pattern-feature-low");
    bytes_of(&facts, &request, now)
}

// ---- the shadow ----------------------------------------------------------------------------------------------------

#[test]
fn every_recorded_case_verifies_with_the_honest_guest() {
    let document = document();
    let mut failed = Vec::new();
    for case in items(member(&document, "cases")) {
        let (facts, request, now) = decode_request(&wire_of(&document, case)).expect("decodes");
        let (wire, expected) = bytes_of(&facts, &request, now);
        let guest = InProcessGuest::new(Behaviour::Honest);
        if let Err(disagreement) = verify_once(&guest, &wire, &expected) {
            failed.push(format!("{}: {disagreement:?}", name_of(case)));
        }
    }
    assert!(
        failed.is_empty(),
        "{} recorded cases disagree:\n{}",
        failed.len(),
        failed.join("\n")
    );
}

#[test]
fn an_honest_guest_is_no_fault() {
    let (wire, expected) = created_bytes();
    let guest = InProcessGuest::new(Behaviour::Honest);
    let verification = shadow(&guest, Duration::from_secs(30));
    let outcome = verification.verify(&wire, &expected);
    assert_eq!(verification.settle(outcome.err()), Ok(()));
    assert_eq!(verification.failover(), None);
    assert_eq!(guest.calls(), 1);
}

#[test]
fn a_guest_error_is_the_fault_it_reports_and_is_recorded() {
    let (wire, expected) = created_bytes();
    for code in [ErrorCode::Dep, ErrorCode::Input, ErrorCode::Internal] {
        let guest = InProcessGuest::new(Behaviour::Fail(code));
        let verification = shadow(&guest, Duration::from_secs(30));
        let outcome = verification.verify(&wire, &expected);
        assert_eq!(
            verification.settle(outcome.err()),
            Err(Disagreement::Fault(code))
        );
        assert_eq!(verification.failover(), Some(code));
    }
}

#[test]
fn a_spent_deadline_is_a_timeout_and_no_failover() {
    let (wire, expected) = created_bytes();
    let guest = InProcessGuest::new(Behaviour::Spent);
    // The guest sleeps 50 ms before it answers `E-TIMEOUT`: a 20 ms call is spent when it does.
    let verification = shadow(&guest, Duration::from_millis(20));
    let outcome = verification.verify(&wire, &expected);
    assert_eq!(
        verification.settle(outcome.err()),
        Err(Disagreement::Timeout)
    );
    assert_eq!(verification.failover(), None);
}

#[test]
fn fuel_is_a_fault_of_the_guest_not_a_timeout() {
    assert_eq!(
        verify_once(
            &InProcessGuest::new(Behaviour::Fuel),
            &created_bytes().0,
            &created_bytes().1
        ),
        Err(Disagreement::Fault(ErrorCode::Internal))
    );
}

#[test]
fn garbage_and_a_mangled_reply_are_contract_faults() {
    let (wire, expected) = created_bytes();
    for behaviour in [Behaviour::Garbage, Behaviour::Mangled] {
        assert_eq!(
            verify_once(&InProcessGuest::new(behaviour), &wire, &expected),
            Err(Disagreement::Fault(ErrorCode::Contract)),
            "{behaviour:?}"
        );
    }
}

#[test]
fn an_oversized_reply_is_a_capacity_fault() {
    let (wire, expected) = created_bytes();
    assert_eq!(
        verify_once(&InProcessGuest::new(Behaviour::Oversized), &wire, &expected),
        Err(Disagreement::Fault(ErrorCode::Capacity))
    );
}

#[test]
fn an_identity_failure_is_a_fault_and_the_guest_is_never_called() {
    let (wire, expected) = created_bytes();
    let guest = InProcessGuest::new(Behaviour::Prepare(ErrorCode::Dep));
    assert_eq!(
        verify_once(&guest, &wire, &expected),
        Err(Disagreement::Fault(ErrorCode::Dep))
    );
    assert_eq!(guest.calls(), 0);
}

#[test]
fn host_facts_that_differ_from_the_encoded_facts_are_a_contract_fault() {
    let (facts, request, now) = case_named("pattern-feature-low");
    let (wire, _) = bytes_of(&facts, &request, now);
    // The host's answer is computed from other facts (a later clock): the honest guest answers the encoded facts and disagrees.
    let (_, expected) = bytes_of(&facts, &request, now + 1.0);
    let guest = InProcessGuest::new(Behaviour::Honest);
    assert_eq!(
        verify_once(&guest, &wire, &expected),
        Err(Disagreement::Fault(ErrorCode::Contract))
    );
}

// ---- the route on the sandbox engine -------------------------------------------------------------------------------

/// A scratch repository: `<dir>/work/.swarm/tasks.db` from the Python template and an empty effects file, the way the selector makes it.
struct World {
    _dir: TempDir,
    root: PathBuf,
    swarm: PathBuf,
    effects: PathBuf,
}

/// The route reads the process environment, and this shell has a live substrate and a signing key. A live substrate makes the route
/// decline, and a configured key signs a controller kickoff whose message id and clock differ between two runs, so no two runs
/// compare byte for byte. The route tests therefore run as a child session with the substrate disabled. The kickoff is the host glue's
/// concern, outside the shadow, and its own unit test covers its signing. The variables are set once, before any route test runs.
fn hermetic_route_environment() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        std::env::set_var("SUBSTRATE_DISABLED", "1");
        std::env::set_var("SWARM_AGENT_SESSION", "1");
    });
}

impl World {
    fn new() -> Self {
        hermetic_route_environment();
        let dir = TempDir::new().expect("tempdir");
        let root = dir.path().join("work");
        let swarm = root.join(".swarm");
        fs::create_dir_all(&swarm).expect("swarm dir");
        fs::write(swarm.join("tasks.db"), &*TEMPLATE).expect("copy template");
        let effects = dir.path().join("effects.json");
        fs::write(&effects, "").expect("effects file");
        Self {
            _dir: dir,
            root,
            swarm,
            effects,
        }
    }

    /// An `orch_plan` call with `argv` over this repository.
    fn call(&self, argv: &[&str], deadline: Duration) -> Invocation {
        Invocation {
            agent: "A01".to_string(),
            script: "orch_plan".to_string(),
            argv: argv.iter().map(|arg| arg.to_string()).collect(),
            root: self.root.clone(),
            env: vec![("SWARM_DIR".to_string(), self.swarm.display().to_string())],
            deadline,
            effects_path: Some(self.effects.clone()),
        }
    }

    /// A plan of the brief under the fixed correlation.
    fn plan_call(&self, deadline: Duration) -> Invocation {
        self.call(
            &["--brief-text", BRIEF, "--correlation-id", CORRELATION],
            deadline,
        )
    }

    fn plan_file(&self) -> PathBuf {
        self.swarm.join("plans").join(format!("{CORRELATION}.json"))
    }

    fn store(&self) -> PathBuf {
        self.swarm.join("tasks.db")
    }

    fn dump(&self) -> Vec<String> {
        block(dump_tables(&self.store()))
    }

    /// The reply text with this repository's own paths replaced, so two repositories' replies compare equal.
    fn normalized(&self, stdout: &str) -> String {
        let mut text = stdout.to_string();
        for path in [
            self.swarm.display().to_string(),
            fs::canonicalize(&self.swarm)
                .map(|path| path.display().to_string())
                .unwrap_or_default(),
        ] {
            if !path.is_empty() {
                text = text.replace(&path, "<swarm>");
            }
        }
        text
    }
}

/// The rows of every write table, one line each. The clock columns are left out: they are the only values that differ between two
/// runs of the same plan.
async fn dump_tables(path: &Path) -> Vec<String> {
    let mut conn = SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("open the store");
    let mut out = Vec::new();
    for table in ["tasks", "transitions", "verdicts", "artifacts"] {
        let rows = sqlx::query(&format!("SELECT * FROM {table} ORDER BY rowid"))
            .fetch_all(&mut conn)
            .await
            .expect(table);
        for row in &rows {
            let cells: Vec<String> = row
                .columns()
                .iter()
                .enumerate()
                .filter(|(_, column)| !matches!(column.name(), "created_at" | "updated_at" | "ts"))
                .map(|(index, _)| cell(row, index))
                .collect();
            out.push(format!("{table}: {}", cells.join(" | ")));
        }
    }
    conn.close().await.expect("close");
    out
}

fn cell(row: &SqliteRow, index: usize) -> String {
    let raw = row.try_get_raw(index).expect("raw value");
    if raw.is_null() {
        return "NULL".to_string();
    }
    match raw.type_info().name() {
        "INTEGER" => row.get::<i64, _>(index).to_string(),
        "REAL" => format!("{:?}", row.get::<f64, _>(index)),
        "TEXT" => row.get::<String, _>(index),
        other => format!("{other}: {:?}", row.get::<Vec<u8>, _>(index)),
    }
}

/// The host route's answer for `call`, over its own repository.
fn host_answer(world: &World, call: &Invocation) -> String {
    let engine = RustEngine::new().expect("host engine");
    let reply = engine.orch_plan(call).expect("the host answers");
    world.normalized(&reply.stdout)
}

/// A guest that stalls past any deadline the tests give and then answers `E-TIMEOUT` (the sandbox's own deadline).
struct Stalled;

impl WriteGuest for Stalled {
    fn call(&self, _request: &[u8], _limits: &gsd_sandbox::Limits) -> Result<Vec<u8>, ErrorCode> {
        std::thread::sleep(Duration::from_millis(300));
        Err(ErrorCode::Timeout)
    }
}

#[test]
fn the_sandbox_route_prints_the_host_bytes_and_checks_the_creation_once() {
    let host_world = World::new();
    let host = host_answer(&host_world, &host_world.plan_call(Duration::from_secs(30)));

    let world = World::new();
    let engine = SandboxEngine::new().expect("engine");
    let guest = InProcessGuest::new(Behaviour::Honest);
    let reply = engine
        .plan_via(
            &guest,
            test_limits,
            &world.plan_call(Duration::from_secs(30)),
        )
        .expect("the sandbox answers");

    assert_eq!(world.normalized(&reply.stdout), host);
    assert_eq!(engine.failover(), None);
    assert_eq!(
        guest.calls(),
        1,
        "a creation is checked once, inside its transaction"
    );
    assert_eq!(dump_of(&world), dump_of(&host_world));
    assert_eq!(
        fs::read(world.plan_file()).expect("the plan file"),
        fs::read(host_world.plan_file()).expect("the host's plan file"),
    );
}

fn dump_of(world: &World) -> Vec<String> {
    let rows = world.dump();
    assert!(
        rows.iter().any(|row| row.starts_with("tasks:")),
        "the plan wrote its tasks"
    );
    rows
}

#[test]
fn a_faulty_guest_leaves_the_host_answer_in_place_and_records_the_fault() {
    for (behaviour, code) in [
        (Behaviour::Garbage, ErrorCode::Contract),
        (Behaviour::Fail(ErrorCode::Dep), ErrorCode::Dep),
        (Behaviour::Oversized, ErrorCode::Capacity),
        (Behaviour::Fuel, ErrorCode::Internal),
    ] {
        let host_world = World::new();
        let host = host_answer(&host_world, &host_world.plan_call(Duration::from_secs(30)));

        let world = World::new();
        let engine = SandboxEngine::new().expect("engine");
        let reply = engine
            .plan_via(
                &InProcessGuest::new(behaviour),
                test_limits,
                &world.plan_call(Duration::from_secs(30)),
            )
            .expect("the host answer stands");

        assert_eq!(world.normalized(&reply.stdout), host, "{behaviour:?}");
        assert_eq!(engine.failover(), Some(code), "{behaviour:?}");
        assert_eq!(dump_of(&world), dump_of(&host_world), "{behaviour:?}");
    }
}

#[test]
fn a_prepare_failure_answers_from_the_host_without_calling_the_guest() {
    let host_world = World::new();
    let host = host_answer(&host_world, &host_world.plan_call(Duration::from_secs(30)));

    let world = World::new();
    let engine = SandboxEngine::new().expect("engine");
    let guest = InProcessGuest::new(Behaviour::Prepare(ErrorCode::Dep));
    let reply = engine
        .plan_via(
            &guest,
            test_limits,
            &world.plan_call(Duration::from_secs(30)),
        )
        .expect("the host answer stands");

    assert_eq!(world.normalized(&reply.stdout), host);
    assert_eq!(engine.failover(), Some(ErrorCode::Dep));
    assert_eq!(guest.calls(), 0);
}

#[test]
fn a_refusal_and_a_dry_run_are_checked_where_they_are_answered() {
    let engine = SandboxEngine::new().expect("engine");

    // No brief: Python's own refusal, answered before the store is opened, and checked there.
    let world = World::new();
    let guest = InProcessGuest::new(Behaviour::Honest);
    let refused = engine
        .plan_via(
            &guest,
            test_limits,
            &world.call(&["--correlation-id", CORRELATION], Duration::from_secs(30)),
        )
        .expect_err("a refusal");
    assert_eq!(refused.code(), ErrorCode::Input);
    assert_eq!(guest.calls(), 1);
    assert_eq!(engine.failover(), None);

    // A dry run answers with the derivation's reply and writes nothing.
    let world = World::new();
    let guest = InProcessGuest::new(Behaviour::Honest);
    let argv = [
        "--brief-text",
        BRIEF,
        "--correlation-id",
        CORRELATION,
        "--dry-run",
    ];
    let dry = engine
        .plan_via(
            &guest,
            test_limits,
            &world.call(&argv, Duration::from_secs(30)),
        )
        .expect("the dry run answers");
    assert_eq!(guest.calls(), 1);
    assert_eq!(engine.failover(), None);
    assert!(dry.stdout.contains("\"status\": \"ok\""));
    assert!(dump_of_store_is_empty(&world));
}

fn dump_of_store_is_empty(world: &World) -> bool {
    !world.dump().iter().any(|row| row.starts_with("tasks:"))
}

#[test]
fn a_spent_deadline_inside_the_transaction_writes_nothing() {
    let world = World::new();
    let before = world.dump();
    let engine = SandboxEngine::new().expect("engine");

    // The guest answers after the call's 200 ms deadline: the shadow's check, inside the write transaction, is a spent deadline.
    let error = engine
        .plan_via(
            &Stalled,
            test_limits,
            &world.plan_call(Duration::from_millis(200)),
        )
        .expect_err("E-TIMEOUT");

    assert_eq!(error.code(), ErrorCode::Timeout);
    assert_eq!(
        obj_get(error.details(), "write_state"),
        Some(&jstr("final")),
        "the timeout is final: Python must not run after it"
    );
    assert_eq!(world.dump(), before, "the store is unchanged");
    assert!(!world.plan_file().exists(), "no plan file is written");
    let effects = fs::read_to_string(&world.effects).expect("the effects file");
    assert!(
        !effects.contains("plan.updated"),
        "no effect is announced: {effects:?}"
    );
    assert_eq!(engine.failover(), None, "a timeout is not a fault");
}

#[test]
fn a_call_outside_the_grammar_is_refused_without_the_guest() {
    let world = World::new();
    let engine = SandboxEngine::new().expect("engine");
    let refused = engine
        .orch_plan(&world.call(&["--plan", "plan.json"], Duration::from_secs(30)))
        .expect_err("the roster form is Python's");
    assert_eq!(refused.code(), ErrorCode::Dep);
    assert_eq!(engine.failover(), None);
    assert!(dump_of_store_is_empty(&world));
}

#[test]
fn the_request_the_shadow_sends_is_the_plan_mode_of_the_wire() {
    let (facts, request, now) = case_named("pattern-feature-low");
    let wire = encode_request(&facts, &request, now);
    let value = parse_python_json(&wire).expect("the request is JSON");
    assert_eq!(member(&value, "mode"), &jstr(MODE));
    assert_eq!(
        to_python_dumps(&value),
        wire,
        "the request is in canonical form"
    );
}
