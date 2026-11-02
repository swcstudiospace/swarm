//! `create_plan` (phase 14.5, unit U3.7b): the one-transaction plan write of `scripts/orch_plan.py`.
//!
//! The oracle is the REAL Python: `scripts/orch_plan.py` runs in a scratch root (no network, signing and substrate off), and its
//! `tasks` and `transitions` rows must equal the rows the Rust writer leaves for the same derived plan. The clock columns are masked
//! (`created_at`, `updated_at`, the audit `ts`); every other cell is compared as stored. The repository root is `SWARM_REPO_ROOT` or
//! two levels above this crate.
//!
//! What is proved here:
//!
//! - the rows of a feature plan equal Python's, cell by cell, with the clock columns masked;
//! - a failure after the first write (a transition whose conditional `UPDATE` matches no row) leaves no row of the plan: the
//!   transaction rolls back and both tables are empty;
//! - an op that is not a plan step is refused before the first write (not started), so the store is untouched;
//! - `read_plan_rows` reads the candidate rows and the correlations that own them;
//! - (`fault-injection`) a plan held in the middle of a transition is invisible to another connection, and dropping it rolls every
//!   row back.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::LazyLock;

use gsd_route::plan_derive::{
    derive_plan, derived_prefix, Outcome, PlanDerived, PlanFacts, PlanOp, PlanRequest, Signing,
};
use gsd_route::{Fields, Json};
use gsd_taskwriter::create_plan::{check_plan_ops, read_plan_rows, write_plan_ops};
use gsd_taskwriter::{open_for_write, ErrorKind, WriteState, WriterConfig};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{Column, ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};

const CORRELATION: &str = "c0ffee01-aaaa-4bbb-8ccc-0000000000a1";
const OTHER: &str = "c0ffee01-aaaa-4bbb-8ccc-0000000000b2";
const NOW: f64 = 1_700_000_123.5;
const BRIEF: &str = "Build the orders service.\nAcceptance: p95 under 200 ms.";

/// The clock columns of the two tables; masked before the comparison.
const CLOCK_COLUMNS: [&str; 3] = ["created_at", "updated_at", "ts"];

/// Creates an empty Task Store through Python's `TaskStore` (schema and migrations are Python's).
const TEMPLATE_SCRIPT: &str =
    "import sys\nfrom swarm.taskstore import TaskStore\nTaskStore(sys.argv[1]).conn.close()\n";

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

/// The Python-built empty store, as bytes; built once.
static TEMPLATE: LazyLock<Vec<u8>> = LazyLock::new(|| {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("tasks.db");
    let output = Command::new("python3")
        .arg("-c")
        .arg(TEMPLATE_SCRIPT)
        .arg(&db)
        .current_dir(repo_root())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .output()
        .expect("run python3");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    std::fs::read(&db).expect("read template")
});

/// A fresh store file for one test, a copy of the Python-built template.
fn fresh_store(dir: &Path) -> PathBuf {
    let db = dir.join("tasks.db");
    std::fs::write(&db, &*TEMPLATE).expect("write template");
    db
}

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("open store")
}

/// Every row of `table` in rowid order, every cell rendered with its storage class, the clock columns masked.
async fn dump(path: &Path, table: &str) -> Vec<Vec<(String, String)>> {
    let mut conn = connect(path).await;
    let rows = sqlx::query(&format!("SELECT * FROM {table} ORDER BY rowid"))
        .fetch_all(&mut conn)
        .await
        .expect("dump");
    let mut out = Vec::new();
    for row in &rows {
        let mut line = Vec::new();
        for index in 0..row.columns().len() {
            let name = row.columns()[index].name().to_string();
            let raw = row.try_get_raw(index).expect("raw");
            let rendered = if raw.is_null() {
                "NULL".to_string()
            } else {
                match raw.type_info().name() {
                    "INTEGER" => format!("int:{}", row.get::<i64, _>(index)),
                    "REAL" => format!("real:{}", row.get::<f64, _>(index)),
                    "TEXT" => format!("text:{}", row.get::<String, _>(index)),
                    other => panic!("{table}.{name}: unexpected storage class {other}"),
                }
            };
            let rendered = if CLOCK_COLUMNS.contains(&name.as_str()) {
                "CLOCK".to_string()
            } else {
                rendered
            };
            line.push((name, rendered));
        }
        out.push(line);
    }
    conn.close().await.expect("close");
    out
}

async fn count(path: &Path, table: &str) -> i64 {
    let mut conn = connect(path).await;
    let n: i64 = sqlx::query_scalar(&format!("SELECT COUNT(*) FROM {table}"))
        .fetch_one(&mut conn)
        .await
        .expect("count");
    conn.close().await.expect("close");
    n
}

fn python_env(root: &Path) -> Vec<(&'static str, String)> {
    vec![
        (
            "SWARM_DIR",
            root.join(".swarm").to_string_lossy().into_owned(),
        ),
        ("SWARM_AGENT_SESSION", "1".to_string()),
        ("SWARM_GSD_STATE_FILE", String::new()),
        (
            "SWARM_CLAUDE_STATE_DIR",
            root.join("claude").to_string_lossy().into_owned(),
        ),
        ("SUBSTRATE_DISABLED", "1".to_string()),
        ("PYTHONDONTWRITEBYTECODE", "1".to_string()),
    ]
}

/// `scripts/orch_plan.py` for the feature pattern, in `root`, as Python runs it.
fn run_python_plan(root: &Path, correlation: &str) {
    let mut command = Command::new("python3");
    command
        .arg(repo_root().join("scripts/orch_plan.py"))
        .args([
            "--root",
            &root.to_string_lossy(),
            "--correlation-id",
            correlation,
        ])
        .args(["--brief-text", BRIEF, "--acceptance", "p95 under 200 ms"])
        .current_dir(repo_root())
        .env_remove("SWARM_CORRELATION_ID")
        .env_remove("SWARM_TASK_ID")
        .env_remove("SUBSTRATE_URL")
        .env_remove("SWARM_SIGNING_KEY")
        .env_remove("SWARM_ED25519_KEY")
        .env_remove("SWARM_ED25519_KEY_FILE");
    for (name, value) in python_env(root) {
        command.env(name, value);
    }
    let output = command.output().expect("run orch_plan.py");
    assert!(
        output.status.success(),
        "orch_plan.py failed: {}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

fn empty_facts(correlation: &str, plans_dir: &Path) -> PlanFacts {
    PlanFacts {
        new_correlation_id: correlation.to_string(),
        agents: Vec::new(),
        plans_dir: plans_dir.to_string_lossy().into_owned(),
        signing: Signing {
            agent_session: true,
            config_error: None,
            real_key: false,
        },
        prefix_tasks: Vec::new(),
        correlation_tasks: Vec::new(),
        plan_files: Vec::new(),
    }
}

fn feature_request(correlation: &str) -> PlanRequest {
    PlanRequest {
        pattern: "feature".to_string(),
        risk_class: "medium".to_string(),
        priority: "P2".to_string(),
        prefix: None,
        acceptance: vec!["p95 under 200 ms".to_string()],
        graph_id: None,
        brief_file: None,
        brief_text: Some(BRIEF.to_string()),
        plan_file: None,
        slices_json: None,
        slices_file: None,
        correlation_id: Some(correlation.to_string()),
        dry_run: false,
    }
}

fn derive_created(correlation: &str, plans_dir: &Path, request: &PlanRequest) -> PlanDerived {
    let derived = derive_plan(&empty_facts(correlation, plans_dir), request, NOW)
        .expect("the feature plan derives");
    assert_eq!(
        derived.outcome,
        Outcome::Created,
        "a fresh store creates the plan"
    );
    derived
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn create_plan_writes_the_rows_python_writes() {
    let py_root = tempfile::tempdir().expect("tempdir");
    let rs_root = tempfile::tempdir().expect("tempdir");
    std::fs::create_dir_all(py_root.path().join(".swarm")).expect("swarm dir");
    let py_db = fresh_store(&py_root.path().join(".swarm"));
    run_python_plan(py_root.path(), CORRELATION);

    let rs_db = fresh_store(rs_root.path());
    let plans_dir = rs_root.path().join(".swarm/plans");
    let derived = derive_created(CORRELATION, &plans_dir, &feature_request(CORRELATION));
    let store = open_for_write(&WriterConfig::new(&rs_db))
        .await
        .expect("open the store");
    let mut tx = store.begin().await.expect("begin");
    write_plan_ops(&mut tx, &derived.ops, NOW)
        .await
        .expect("the plan writes");
    tx.commit().await.expect("commit");
    store.close().await;

    assert_eq!(
        dump(&rs_db, "tasks").await,
        dump(&py_db, "tasks").await,
        "task rows (clock masked)"
    );
    let transitions = dump(&rs_db, "transitions").await;
    assert_eq!(
        transitions,
        dump(&py_db, "transitions").await,
        "audit rows (clock masked)"
    );
    assert_eq!(
        transitions.len(),
        3 * 13,
        "a feature plan has 13 tasks: create, VALIDATED, PLANNED each"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn create_plan_failure_after_the_first_write_leaves_no_row() {
    let root = tempfile::tempdir().expect("tempdir");
    let db = fresh_store(root.path());
    let derived = derive_created(
        CORRELATION,
        &root.path().join("plans"),
        &feature_request(CORRELATION),
    );
    // The plan interleaves per task: create, VALIDATED, PLANNED. Task 0 goes through all three; task 1 is created, then its PLANNED
    // step is issued from VALIDATED although it is still CREATED: the conditional UPDATE matches no row after earlier writes.
    let mut ops: Vec<PlanOp> = derived.ops[..4].to_vec();
    let PlanOp::Create(second) = &derived.ops[3] else {
        panic!("op 3 is the create of task 1");
    };
    ops.push(PlanOp::Transition(gsd_route::plan_derive::Step {
        task_id: second.task_id.clone(),
        from: "VALIDATED",
        to: "PLANNED",
        actor: "A01",
        reason: "pattern=feature".to_string(),
    }));
    check_plan_ops(&ops)
        .expect("every step is a plan step, so nothing is refused before the write");

    let store = open_for_write(&WriterConfig::new(&db))
        .await
        .expect("open the store");
    let mut tx = store.begin().await.expect("begin");
    let error = write_plan_ops(&mut tx, &ops, NOW)
        .await
        .expect_err("the stale step must fail");
    assert_eq!(error.kind(), ErrorKind::ConcurrentTransition, "{error}");
    assert_eq!(
        error.write_state(),
        WriteState::Final,
        "a write already happened"
    );
    tx.rollback().await.expect("rollback");
    store.close().await;

    assert_eq!(
        count(&db, "tasks").await,
        0,
        "no task row survives the failed plan"
    );
    assert_eq!(
        count(&db, "transitions").await,
        0,
        "no audit row survives the failed plan"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn create_plan_refuses_a_non_plan_step_before_any_write() {
    let root = tempfile::tempdir().expect("tempdir");
    let db = fresh_store(root.path());
    let derived = derive_created(
        CORRELATION,
        &root.path().join("plans"),
        &feature_request(CORRELATION),
    );
    let PlanOp::Transition(step) = derived.ops[1].clone() else {
        panic!("the op after the first create is a transition");
    };
    let mut ops: Vec<PlanOp> = derived.ops[..1].to_vec();
    ops.push(PlanOp::Transition(gsd_route::plan_derive::Step {
        to: "CLAIMED",
        ..step
    }));

    let refused = check_plan_ops(&ops).expect_err("CREATED -> CLAIMED is not a plan step");
    assert_eq!(refused.kind(), ErrorKind::Unsupported);
    assert_eq!(refused.write_state(), WriteState::NotStarted);

    let store = open_for_write(&WriterConfig::new(&db))
        .await
        .expect("open the store");
    let mut tx = store.begin().await.expect("begin");
    let error = write_plan_ops(&mut tx, &ops, NOW)
        .await
        .expect_err("refused");
    assert_eq!(error.write_state(), WriteState::NotStarted, "{error}");
    tx.rollback().await.expect("rollback");
    store.close().await;

    assert_eq!(
        count(&db, "tasks").await,
        0,
        "the create before the refused step was never written"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn create_plan_read_rows_finds_the_candidates_and_their_owners() {
    let root = tempfile::tempdir().expect("tempdir");
    let db = fresh_store(root.path());
    let mut other_request = feature_request(OTHER);
    other_request.prefix = Some("PX".to_string());
    let derived = derive_created(OTHER, &root.path().join("plans"), &other_request);

    let store = open_for_write(&WriterConfig::new(&db))
        .await
        .expect("open the store");
    let mut tx = store.begin().await.expect("begin");
    write_plan_ops(&mut tx, &derived.ops, NOW)
        .await
        .expect("the other plan writes");
    tx.commit().await.expect("commit");

    let mut tx = store.begin().await.expect("begin");
    let candidates: Vec<String> = ["PX-req", "PX-arch", "NOPE-req"].map(String::from).to_vec();
    let rows = read_plan_rows(&mut tx, CORRELATION, &candidates)
        .await
        .expect("the rows read");
    tx.rollback().await.expect("rollback");
    store.close().await;

    assert_eq!(
        rows.correlations,
        vec![CORRELATION.to_string(), OTHER.to_string()]
    );
    let found: Vec<&str> = rows
        .prefix_tasks
        .iter()
        .filter_map(|row| match row.iter().find(|(name, _)| name == "task_id") {
            Some((_, Json::Str(id))) => Some(id.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(
        found,
        vec!["PX-req", "PX-arch"],
        "only the candidates that exist"
    );
    assert_eq!(
        rows.correlation_tasks.len(),
        13,
        "every row of the owning correlation, once"
    );
    let every_notes_is_text = rows.correlation_tasks.iter().all(|row: &Fields| {
        matches!(
            row.iter().find(|(name, _)| name == "notes"),
            Some((_, Json::Str(_)))
        )
    });
    assert!(
        every_notes_is_text,
        "JSON columns stay text, as plan_derive reads them"
    );
    assert_eq!(
        derived_prefix(CORRELATION, 4),
        "Tc0ff",
        "the derived prefix of this correlation"
    );
}

#[cfg(feature = "fault-injection")]
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn create_plan_dropped_mid_transition_leaves_no_row() {
    use gsd_taskwriter::fault::PausePoint;

    let root = tempfile::tempdir().expect("tempdir");
    let db = fresh_store(root.path());
    let derived = derive_created(
        CORRELATION,
        &root.path().join("plans"),
        &feature_request(CORRELATION),
    );
    let store = open_for_write(&WriterConfig::new(&db))
        .await
        .expect("open the store");
    let mut gate = store.faults().arm(PausePoint::AfterStateUpdate);
    let ops = derived.ops.clone();
    let writer = tokio::spawn(async move {
        let mut tx = store.begin().await.expect("begin");
        write_plan_ops(&mut tx, &ops, NOW).await.map(|_| ())
    });
    gate.wait_reached().await;
    writer.abort();
    let _ = writer.await;

    assert_eq!(
        count(&db, "tasks").await,
        0,
        "an uncommitted plan is invisible to another connection"
    );
    assert_eq!(
        count(&db, "transitions").await,
        0,
        "no audit row is committed"
    );
}
