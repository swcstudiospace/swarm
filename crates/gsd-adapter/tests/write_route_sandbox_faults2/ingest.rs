//! Faults of the `ingest` route on the sandbox (Phase 14.5, pin P2), and of its reconcile stage.
//!
//! Three things are proved here. The verification protocol: an honest guest agrees with the host bytes, and each fault of the
//! guest is a [`Disagreement`] with its code (through [`verify_once`]). The route: the sandbox engine asks the guest to verify the
//! plan and then the reconcile stage, the host's answer is what the call returns, and a fault is recorded with its code. The
//! timeout: a spent deadline is `E-TIMEOUT` with the store byte-for-byte as it was. Every store here is built by the REAL Python
//! `TaskStore` (the schema is Python's); the repository root is `SWARM_REPO_ROOT` or two levels above this crate.

use std::fs;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use gsd_adapter::sandbox_engine::{SandboxEngine, ShadowIngest, WriteGuest};
use gsd_adapter::sandbox_shadow::Disagreement;
use gsd_adapter::strangler_seam::{CoreBackend, Invocation, RustEngine};
use gsd_failure::ErrorCode;
use gsd_route::ingest_plan::{plan_ingest, IngestFacts, Mode};
use gsd_route::reconcile_plan::{KeyPolicy, ReconcileFacts};
use gsd_route::{core_invoke, ingest_wire, jstr, Fields, Json, Reply};
use gsd_sandbox::Limits;
use gsd_taskwriter::{
    open_for_write, FixedApprovalSource, IngestCause, IngestRequest, TaskStoreWriter, WriterConfig,
};
use sqlx::sqlite::{SqliteConnectOptions, SqliteRow};
use sqlx::{ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};

use super::{shadow, test_limits, verify_once, Behaviour, InProcessGuest};

const NOW: f64 = 1_700_000_000.25;
const DEADLINE: Duration = Duration::from_secs(30);

/// Seeds, through the real `TaskStore`, one correlation: `H-1` IN_PROGRESS and `P-1` PLANNED, with no gate.
const SEED_SCRIPT: &str = r#"
import sys
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
PATH = {"PLANNED": ["VALIDATED", "PLANNED"], "IN_PROGRESS": ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"]}
for tid, state in (("H-1", "IN_PROGRESS"), ("P-1", "PLANNED")):
    s.create(task_id=tid, correlation_id="C-1", capability="code.backend", title=tid, notes={"gates": []}, agent_id="A05@seed")
    for step in PATH[state]:
        s.transition(tid, step, actor="A01", reason="seed")
s.conn.close()
"#;

/// The result `P-1` reports: a claim of the PLANNED task, the IN_REVIEW, one artifact. Advisory, so nothing is approved.
const RESULT: &str = r#"{"task_id":"P-1","state":"IN_REVIEW","outputs":[{"kind":"code","uri":"file://p","version":"1","digest":"d"}]}"#;

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

/// Runs `script` with `db` as its argument, from the repository root, as the other store tests do.
fn python(script: &str, db: &Path) {
    let output = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(db)
        .current_dir(repo_root())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env_remove("SWARM_TASKSTORE_WAL")
        .env("SWARM_GSD_STATE_FILE", "")
        .env(
            "SWARM_CLAUDE_STATE_DIR",
            "/root/.cache/gsd-redact-work/U37d/claude",
        )
        .output()
        .expect("run python3");
    assert!(
        output.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

/// The seeded store as bytes; built once.
static TEMPLATE: LazyLock<Vec<u8>> = LazyLock::new(|| {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("tasks.db");
    python(SEED_SCRIPT, &db);
    fs::read(&db).expect("read template")
});

fn block<F: Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime")
        .block_on(future)
}

// ---- the request and its expected reply ----------------------------------------------------------------------------------------

fn row(task_id: &str, state: &str) -> Fields {
    let text = |value: &str| Json::Str(value.to_string());
    vec![
        ("task_id".to_string(), text(task_id)),
        ("correlation_id".to_string(), text("corr-a")),
        ("parent_id".to_string(), Json::Null),
        ("dag_depth".to_string(), Json::Int(1)),
        ("capability".to_string(), text("code.backend")),
        ("agent_id".to_string(), text("A05@seed")),
        ("title".to_string(), text(task_id)),
        ("inputs".to_string(), text("[]")),
        ("outputs".to_string(), text("[]")),
        ("acceptance".to_string(), text("[]")),
        (
            "budget".to_string(),
            text("{\"max_tokens\": 200000, \"max_wall_s\": 1800, \"max_cost_usd\": 5}"),
        ),
        ("risk_class".to_string(), text("low")),
        ("priority".to_string(), text("P2")),
        ("state".to_string(), text(state)),
        ("attempt".to_string(), Json::Int(1)),
        ("max_attempts".to_string(), Json::Int(3)),
        ("rework_loops".to_string(), Json::Int(0)),
        ("depends_on".to_string(), text("[]")),
        ("assigned_to".to_string(), Json::Null),
        ("notes".to_string(), text("{\"gates\": []}")),
        ("created_at".to_string(), Json::Float(501.0)),
        ("updated_at".to_string(), Json::Float(501.0)),
    ]
}

/// A heartbeat of `T-1` in `state`, keyed by `digest`: the facts the host reads for one call.
fn facts(state: &str, digest: &str) -> IngestFacts {
    IngestFacts {
        mode: Mode::Ingest,
        task_id: "T-1".to_string(),
        agent_id: "A05@seed".to_string(),
        result: Ok(Json::Obj(vec![
            ("task_id".to_string(), jstr("T-1")),
            ("state".to_string(), jstr("IN_PROGRESS")),
        ])),
        meta: Json::Obj(vec![("ingest".to_string(), jstr("/ingest/result.json"))]),
        digest: Some(digest.to_string()),
        advisory: true,
        claims: Vec::new(),
        reconcile: ReconcileFacts {
            correlation_id: "corr-a".to_string(),
            tasks: vec![row("T-1", state)],
            verdicts: Vec::new(),
            other_tasks: Vec::new(),
            key_policy: Some(KeyPolicy {
                require_key: false,
                real_key: true,
                dev_key: false,
            }),
        },
    }
}

/// The request the host encodes for `facts`, and the reply it expects: its own answer, encoded.
fn pair(facts: &IngestFacts) -> (Vec<u8>, Vec<u8>) {
    (
        ingest_wire::encode_request(facts, NOW).into_bytes(),
        ingest_wire::encode_outcome(&plan_ingest(facts, NOW)).into_bytes(),
    )
}

// ---- the verification protocol -------------------------------------------------------------------------------------------------

#[test]
fn an_honest_guest_agrees_with_the_host_bytes() {
    let (request, expected) = pair(&facts("IN_PROGRESS", "d1"));
    let guest = InProcessGuest::new(Behaviour::Honest);
    assert_eq!(verify_once(&guest, &request, &expected), Ok(()));
    assert_eq!(guest.calls(), 1);
}

#[test]
fn a_guest_error_is_a_fault_with_its_code() {
    let (request, expected) = pair(&facts("IN_PROGRESS", "d1"));
    for code in [ErrorCode::Dep, ErrorCode::Contract, ErrorCode::Input] {
        let guest = InProcessGuest::new(Behaviour::Fail(code));
        assert_eq!(
            verify_once(&guest, &request, &expected),
            Err(Disagreement::Fault(code)),
            "{code:?}"
        );
    }
}

#[test]
fn a_spent_deadline_is_a_timeout_and_is_not_recorded() {
    let (request, expected) = pair(&facts("IN_PROGRESS", "d1"));
    let guest = InProcessGuest::new(Behaviour::Spent);
    // The guest takes 50 ms against a 20 ms deadline: the sandbox stopped it after the deadline was spent.
    let shadow = shadow(&guest, Duration::from_millis(20));
    let outcome = shadow.verify(&request, &expected);
    assert_eq!(shadow.settle(outcome.err()), Err(Disagreement::Timeout));
    assert_eq!(shadow.failover(), None);
}

#[test]
fn a_stop_before_the_budget_is_spent_is_fuel_and_a_fault() {
    let (request, expected) = pair(&facts("IN_PROGRESS", "d1"));
    let guest = InProcessGuest::new(Behaviour::Fuel);
    assert_eq!(
        verify_once(&guest, &request, &expected),
        Err(Disagreement::Fault(ErrorCode::Internal))
    );
}

#[test]
fn garbage_and_a_mangled_reply_are_contract_faults() {
    let (request, expected) = pair(&facts("IN_PROGRESS", "d1"));
    for behaviour in [Behaviour::Garbage, Behaviour::Mangled] {
        let guest = InProcessGuest::new(behaviour);
        assert_eq!(
            verify_once(&guest, &request, &expected),
            Err(Disagreement::Fault(ErrorCode::Contract)),
            "{behaviour:?}"
        );
    }
}

#[test]
fn an_oversized_reply_is_a_capacity_fault() {
    let (request, expected) = pair(&facts("IN_PROGRESS", "d1"));
    let guest = InProcessGuest::new(Behaviour::Oversized);
    assert_eq!(
        verify_once(&guest, &request, &expected),
        Err(Disagreement::Fault(ErrorCode::Capacity))
    );
}

#[test]
fn a_guest_that_fails_its_identity_check_is_never_called() {
    let (request, expected) = pair(&facts("IN_PROGRESS", "d1"));
    let guest = InProcessGuest::new(Behaviour::Prepare(ErrorCode::Dep));
    assert_eq!(
        verify_once(&guest, &request, &expected),
        Err(Disagreement::Fault(ErrorCode::Dep))
    );
    assert_eq!(guest.calls(), 0);
}

#[test]
fn host_facts_that_differ_from_the_encoded_facts_are_a_contract_fault() {
    let (request, _) = pair(&facts("IN_PROGRESS", "d1"));
    let (_, expected) = pair(&facts("IN_PROGRESS", "d2"));
    let guest = InProcessGuest::new(Behaviour::Honest);
    assert_eq!(
        verify_once(&guest, &request, &expected),
        Err(Disagreement::Fault(ErrorCode::Contract))
    );
}

// ---- the route: a real store, the sandbox engine, the writer --------------------------------------------------------------------

/// One store file's rows, typed as stored, for the tables a write touches.
#[derive(Debug, Clone, PartialEq)]
enum Cell {
    Null,
    Int(i64),
    Real(u64),
    Text(String),
    Blob(Vec<u8>),
}

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("raw connection")
}

fn cell(row: &SqliteRow, index: usize, table: &str) -> Cell {
    let raw = row.try_get_raw(index).expect("raw value");
    if raw.is_null() {
        return Cell::Null;
    }
    match raw.type_info().name() {
        "INTEGER" => Cell::Int(row.get(index)),
        "REAL" => Cell::Real(row.get::<f64, _>(index).to_bits()),
        "TEXT" => Cell::Text(row.get(index)),
        "BLOB" => Cell::Blob(row.get(index)),
        other => panic!("{table}: unexpected storage class {other}"),
    }
}

/// Every row of the four tables a write can touch, in rowid order: the whole of what a rolled-back write must leave alone.
async fn content(path: &Path) -> Vec<(&'static str, Vec<Vec<Cell>>)> {
    let mut conn = connect(path).await;
    let mut out = Vec::new();
    for table in ["tasks", "transitions", "verdicts", "artifacts"] {
        let rows = sqlx::query(&format!("SELECT * FROM {table} ORDER BY rowid"))
            .fetch_all(&mut conn)
            .await
            .expect(table);
        let cells = rows
            .iter()
            .map(|row| {
                (0..row.columns().len())
                    .map(|index| cell(row, index, table))
                    .collect()
            })
            .collect();
        out.push((table, cells));
    }
    conn.close().await.expect("close");
    out
}

async fn state_of(path: &Path, task_id: &str) -> String {
    let mut conn = connect(path).await;
    let state: String = sqlx::query_scalar("SELECT state FROM tasks WHERE task_id = ?")
        .bind(task_id)
        .fetch_one(&mut conn)
        .await
        .expect("the task row");
    conn.close().await.expect("close");
    state
}

async fn transitions_of(path: &Path) -> i64 {
    let mut conn = connect(path).await;
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM transitions")
        .fetch_one(&mut conn)
        .await
        .expect("count");
    conn.close().await.expect("close");
    count
}

/// A store and the result file of one call, in a directory of its own.
struct World {
    _dir: tempfile::TempDir,
    root: PathBuf,
    swarm: PathBuf,
    db: PathBuf,
    result: PathBuf,
    effects: PathBuf,
}

impl World {
    fn new() -> Self {
        let dir = tempfile::tempdir().expect("tempdir");
        let root = dir.path().join("repo");
        let swarm = root.join("swarm");
        fs::create_dir_all(&swarm).expect("swarm dir");
        let db = swarm.join("tasks.db");
        fs::write(&db, &*TEMPLATE).expect("copy template");
        let result = root.join("result.json");
        fs::write(&result, RESULT).expect("write result");
        let effects = root.join("effects.json");
        Self {
            _dir: dir,
            root,
            swarm,
            db,
            result,
            effects,
        }
    }

    /// The call the selector hands the binary for `orch_status --ingest <result> --advisory`.
    fn call(&self, deadline: Duration) -> Invocation {
        Invocation {
            agent: "A01".to_string(),
            script: "orch_status".to_string(),
            argv: vec![
                "--json".to_string(),
                "--ingest".to_string(),
                self.result.display().to_string(),
                "--advisory".to_string(),
            ],
            root: self.root.clone(),
            env: vec![("SWARM_DIR".to_string(), self.swarm.display().to_string())],
            deadline,
            effects_path: Some(self.effects.clone()),
        }
    }
}

/// The reply of a committed call: its `status` is `ok`.
fn is_ok(reply: &Reply) -> bool {
    let value: serde_json::Value = serde_json::from_str(&reply.stdout).expect("the reply is JSON");
    value["status"] == "ok"
}

/// A guest the sandbox stops late: it takes `sleep`, then faults with `E-TIMEOUT`.
struct SlowGuest {
    sleep: Duration,
    calls: AtomicUsize,
}

impl WriteGuest for SlowGuest {
    fn call(&self, _request: &[u8], _limits: &Limits) -> Result<Vec<u8>, ErrorCode> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        std::thread::sleep(self.sleep);
        Err(ErrorCode::Timeout)
    }
}

/// A guest that answers every ingest plan honestly and every reconcile stage with one byte changed.
struct ReconcileLie;

impl WriteGuest for ReconcileLie {
    fn call(&self, request: &[u8], _limits: &Limits) -> Result<Vec<u8>, ErrorCode> {
        let mut bytes = core_invoke(request).map_err(|error| error.code())?;
        if request.starts_with(br#"{"mode": "reconcile""#) {
            if let Some(last) = bytes.last_mut() {
                *last = if *last == b'}' { b']' } else { b'}' };
            }
        }
        Ok(bytes)
    }
}

#[test]
fn an_honest_guest_verifies_the_plan_and_the_reconcile_stage_and_the_host_answer_stands() {
    let world = World::new();
    let engine = SandboxEngine::new().expect("sandbox engine");
    let guest = InProcessGuest::new(Behaviour::Honest);
    let call = world.call(DEADLINE);
    let reply = engine
        .ingest_via(
            &guest,
            test_limits,
            &gsd_route::ingest_args::parse_ingest_args(&call.argv).expect("an ingest argv"),
            &call,
        )
        .expect("the route answers");
    assert!(is_ok(&reply), "{}", reply.stdout);
    assert_eq!(engine.failover(), None);
    // The plan, then the reconcile stage: both were asked, both agreed.
    assert_eq!(guest.calls(), 2);
    assert_eq!(block(state_of(&world.db, "P-1")), "IN_REVIEW");

    // The same call on the host route, on a store of its own: the answer's stable fields are the same.
    let host_world = World::new();
    let host_call = host_world.call(DEADLINE);
    let host = RustEngine::new()
        .expect("host engine")
        .orch_status(&host_call)
        .expect("the host route answers");
    let sandbox: serde_json::Value = serde_json::from_str(&reply.stdout).expect("JSON");
    let host: serde_json::Value = serde_json::from_str(&host.stdout).expect("JSON");
    for key in ["status", "summary", "reconcile"] {
        assert_eq!(sandbox[key], host[key], "{key}");
    }
    assert_eq!(block(state_of(&host_world.db, "P-1")), "IN_REVIEW");
}

#[test]
fn a_mangled_plan_fails_over_on_the_host_answer_and_records_the_code() {
    let world = World::new();
    let engine = SandboxEngine::new().expect("sandbox engine");
    let guest = InProcessGuest::new(Behaviour::Mangled);
    let call = world.call(DEADLINE);
    let reply = engine
        .ingest_via(
            &guest,
            test_limits,
            &gsd_route::ingest_args::parse_ingest_args(&call.argv).expect("an ingest argv"),
            &call,
        )
        .expect("the host answer stands");
    assert!(is_ok(&reply), "{}", reply.stdout);
    assert_eq!(engine.failover(), Some(ErrorCode::Contract));
    assert_eq!(
        guest.calls(),
        1,
        "the reconcile stage is not asked after a faulty plan"
    );
    assert_eq!(block(state_of(&world.db, "P-1")), "IN_REVIEW");
}

#[test]
fn a_faulty_reconcile_stage_alone_fails_over_and_the_host_answer_stands() {
    let world = World::new();
    let engine = SandboxEngine::new().expect("sandbox engine");
    let call = world.call(DEADLINE);
    let reply = engine
        .ingest_via(
            &ReconcileLie,
            test_limits,
            &gsd_route::ingest_args::parse_ingest_args(&call.argv).expect("an ingest argv"),
            &call,
        )
        .expect("the host answer stands");
    assert!(is_ok(&reply), "{}", reply.stdout);
    assert_eq!(engine.failover(), Some(ErrorCode::Contract));
    assert_eq!(block(state_of(&world.db, "P-1")), "IN_REVIEW");
}

#[test]
fn a_spent_deadline_inside_the_writer_rolls_back_and_leaves_every_table_unchanged() {
    let world = World::new();
    let before = block(content(&world.db));
    let request = IngestRequest {
        task_id: "P-1".to_string(),
        result: gsd_route::result_parse::parse_result(RESULT)
            .ok()
            .flatten()
            .map(|parsed| gsd_route::result_parse::validate_result(Some(&parsed), "P-1").cloned())
            .expect("a result"),
        meta: Json::Obj(vec![("ingest".to_string(), jstr("/ingest/result.json"))]),
        advisory: true,
    };
    let guest = SlowGuest {
        sleep: Duration::from_millis(400),
        calls: AtomicUsize::new(0),
    };
    let check = ShadowIngest::new(
        &guest,
        test_limits,
        Instant::now(),
        Duration::from_millis(200),
    );
    let writer = block(async {
        TaskStoreWriter::new(
            open_for_write(&WriterConfig::new(&world.db))
                .await
                .expect("open"),
        )
    });
    let outcome =
        block(writer.ingest_with(&request, NOW, &FixedApprovalSource::new(Vec::new()), &check));
    let error = outcome.expect_err("a spent deadline refuses the call");
    assert_eq!(error.to_seam_error().code(), ErrorCode::Timeout);
    assert_eq!(error.cause(), &IngestCause::Check(ErrorCode::Timeout));
    assert_eq!(guest.calls.load(Ordering::SeqCst), 1);
    assert_eq!(check.failover(), None, "a timeout is not a fault");
    assert_eq!(
        block(content(&world.db)),
        before,
        "a rolled-back ingest changes nothing"
    );
    assert_eq!(block(state_of(&world.db, "P-1")), "PLANNED");
}

#[test]
fn a_spent_deadline_on_the_route_is_e_timeout_with_the_store_and_the_manifest_untouched() {
    let world = World::new();
    let engine = SandboxEngine::new().expect("sandbox engine");
    let before = block(content(&world.db));
    let transitions = block(transitions_of(&world.db));
    let guest = SlowGuest {
        sleep: Duration::from_millis(400),
        calls: AtomicUsize::new(0),
    };
    let call = world.call(Duration::from_millis(300));
    let error = engine
        .ingest_via(
            &guest,
            test_limits,
            &gsd_route::ingest_args::parse_ingest_args(&call.argv).expect("an ingest argv"),
            &call,
        )
        .expect_err("a spent deadline is refused");
    assert_eq!(error.code(), ErrorCode::Timeout, "{}", error.message());
    assert_eq!(
        engine.failover(),
        None,
        "a timeout is not recorded as a fault"
    );
    assert_eq!(block(content(&world.db)), before);
    assert_eq!(block(transitions_of(&world.db)), transitions);
    assert!(
        !world.effects.exists(),
        "no effects manifest for a refused call"
    );
}
