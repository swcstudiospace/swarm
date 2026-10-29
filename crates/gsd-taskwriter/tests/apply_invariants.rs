//! Apply invariants of `TaskStoreWriter::transition` (U3.1d).
//!
//! Stores are built by the REAL Python `TaskStore` (`python3 -c`, no network): the schema is Python's, and `TaskStore.get`
//! is the oracle for the row the writer returns. The Python build runs once; every test copies the resulting database file.
//! The repository root is `SWARM_REPO_ROOT` or two levels above this crate.
//!
//! What is proved here:
//!
//! - every one of the 351 oracle vectors of `gsd-route` (`tests/vectors/transition.json`, produced by the real
//!   `TaskStore.transition`) replays against a Python-seeded store: the whole store (`tasks`, `transitions`, `verdicts`,
//!   `artifacts`, `sqlite_sequence`) equals a store built from the vector's expected values, the refusals carry Python's
//!   code, message and details and leave the store file byte-identical, and the row `Applied` returns prints exactly as
//!   `TaskStore.get` prints it on the committed store;
//! - a plan that tampers with the decision is refused before the first write, with a typed error;
//! - the conditional `UPDATE` is what catches a row that changed under the transaction, and the write lock makes that
//!   unreachable otherwise;
//! - the decider runs once, inside the lock, on the row read inside the lock.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{mpsc, Arc, LazyLock, Mutex, PoisonError};
use std::time::Duration;

use gsd_codes::ErrorCode;
use gsd_route::pydumps::to_python_dumps;
use gsd_route::transition_plan::{
    ApprovalFacts, AuditRow, Facts, PlanEffect, Request, TransitionError, TransitionPlan,
};
use gsd_route::{jstr, obj_get, parse_json, Fields, Json};
use gsd_taskwriter::{
    open_for_write, Applied, ApplyCause, Decider, ErrorKind, HostDecider, PlanValidator,
    PlanViolation, TaskStoreWriter, WriteState, WriterConfig,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};
use tokio::sync::oneshot;

const VECTORS: &str = include_str!("../../gsd-route/tests/vectors/transition.json");

/// Creates an empty Task Store through Python's `TaskStore` (schema and migrations are Python's).
const TEMPLATE_SCRIPT: &str =
    "import sys\nfrom swarm.taskstore import TaskStore\nTaskStore(sys.argv[1]).conn.close()\n";

/// Prints `TaskStore.get(task_id)` as `json.dumps` for each `[db_path, task_id]` of the JSON list in `argv[1]`, one line each.
const ORACLE_SCRIPT: &str = "import json, sys\nfrom swarm.taskstore import TaskStore\nfor path, task_id in json.loads(sys.argv[1]):\n    store = TaskStore(path)\n    print(json.dumps(store.get(task_id)))\n    store.conn.close()\n";

const NOW: f64 = 1_700_000_000.25;

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

fn python(script: &str, arg: &str) -> String {
    let root = repo_root();
    assert!(
        root.join("swarm/taskstore.py").is_file(),
        "no swarm/taskstore.py under {}: set SWARM_REPO_ROOT to the repository root",
        root.display()
    );
    let output = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(arg)
        .current_dir(&root)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env_remove("SWARM_TASKSTORE_WAL")
        .output()
        .expect("run python3");
    assert!(
        output.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).expect("utf8")
}

/// The Python-built empty store, as bytes; built once.
static TEMPLATE: LazyLock<Vec<u8>> = LazyLock::new(|| {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("tasks.db");
    python(TEMPLATE_SCRIPT, &db.to_string_lossy());
    assert!(
        !dir.path().join("tasks.db-wal").exists(),
        "python left a WAL file behind"
    );
    std::fs::read(&db).expect("read template")
});

// ---- raw SQL access, independent of the writer under test -----------------------------------------------------------------

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("raw connection")
}

async fn exec(path: &Path, sql: &str) {
    let mut conn = connect(path).await;
    sqlx::query(sql).execute(&mut conn).await.expect(sql);
    conn.close().await.expect("close");
}

/// Inserts one `tasks` row with the values exactly as given (a NULL, an integer or a text where Python would not put one is
/// the point of some vectors).
async fn insert_task(path: &Path, row: &Fields) {
    let names: Vec<&str> = row.iter().map(|(name, _)| name.as_str()).collect();
    let sql = format!(
        "INSERT INTO tasks ({}) VALUES ({})",
        names.join(", "),
        vec!["?"; names.len()].join(", ")
    );
    let mut insert = sqlx::query(&sql);
    for (name, value) in row {
        insert = match value {
            Json::Null => insert.bind(None::<String>),
            Json::Int(number) => insert.bind(*number),
            Json::Float(number) => insert.bind(*number),
            Json::Str(text) => insert.bind(text.as_str()),
            other => panic!("column {name}: no SQL value for {other:?}"),
        };
    }
    let mut conn = connect(path).await;
    insert.execute(&mut conn).await.expect("insert task");
    conn.close().await.expect("close");
}

/// Appends one `transitions` row as a vector holds it.
async fn insert_audit(path: &Path, task_id: &str, audit: &Json) {
    let mut conn = connect(path).await;
    sqlx::query(
        "INSERT INTO transitions (task_id, from_state, to_state, actor, reason, ts) VALUES (?,?,?,?,?,?)",
    )
    .bind(task_id)
    .bind(text(member(audit, "from_state")))
    .bind(text(member(audit, "to_state")))
    .bind(text(member(audit, "actor")))
    .bind(text(member(audit, "reason")))
    .bind(number(member(audit, "ts")))
    .execute(&mut conn)
    .await
    .expect("insert audit");
    conn.close().await.expect("close");
}

/// One SQLite value with its storage class; a REAL compares by bits.
#[derive(Debug, Clone, PartialEq)]
enum Cell {
    Null,
    Int(i64),
    Real(u64),
    Text(String),
    Blob(Vec<u8>),
}

/// Every row of every table of the store (`SELECT *` in rowid order), values typed as stored: the whole content.
async fn content(path: &Path) -> Vec<(&'static str, Vec<Vec<Cell>>)> {
    let mut conn = connect(path).await;
    let mut out = Vec::new();
    for table in [
        "tasks",
        "transitions",
        "verdicts",
        "artifacts",
        "sqlite_sequence",
    ] {
        let rows = sqlx::query(&format!("SELECT * FROM {table} ORDER BY rowid"))
            .fetch_all(&mut conn)
            .await
            .expect(table);
        let mut cells = Vec::new();
        for row in &rows {
            let mut line = Vec::new();
            for index in 0..row.columns().len() {
                let raw = row.try_get_raw(index).expect("raw value");
                line.push(if raw.is_null() {
                    Cell::Null
                } else {
                    match raw.type_info().name() {
                        "INTEGER" => Cell::Int(row.get(index)),
                        "REAL" => Cell::Real(row.get::<f64, _>(index).to_bits()),
                        "TEXT" => Cell::Text(row.get(index)),
                        "BLOB" => Cell::Blob(row.get(index)),
                        other => panic!("{table}: unexpected storage class {other}"),
                    }
                });
            }
            cells.push(line);
        }
        out.push((table, cells));
    }
    conn.close().await.expect("close");
    out
}

/// A copy of the Python-built store holding `rows`.
async fn seeded(path: &Path, rows: &[Fields]) {
    std::fs::write(path, &*TEMPLATE).expect("copy template");
    for row in rows {
        insert_task(path, row).await;
    }
}

async fn writer(path: &Path) -> TaskStoreWriter {
    TaskStoreWriter::new(
        open_for_write(&WriterConfig::new(path))
            .await
            .expect("open_for_write"),
    )
}

// ---- vector access -----------------------------------------------------------------------------------------------------

fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
    match value {
        Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
        other => panic!("not an object looking for {key}: {other:?}"),
    }
}

fn text(value: &Json) -> &str {
    match value {
        Json::Str(text) => text,
        other => panic!("not text: {other:?}"),
    }
}

fn flag(value: &Json) -> bool {
    match value {
        Json::Bool(flag) => *flag,
        other => panic!("not a bool: {other:?}"),
    }
}

fn items(value: &Json) -> &[Json] {
    match value {
        Json::Arr(items) => items,
        other => panic!("not a list: {other:?}"),
    }
}

fn fields(value: &Json) -> Fields {
    match value {
        Json::Obj(fields) => fields.clone(),
        other => panic!("not an object: {other:?}"),
    }
}

fn number(value: &Json) -> f64 {
    match value {
        Json::Float(number) => *number,
        Json::Int(number) => *number as f64,
        other => panic!("not a number: {other:?}"),
    }
}

fn names(value: &Json) -> Vec<String> {
    items(value)
        .iter()
        .map(|name| text(name).to_string())
        .collect()
}

fn set(row: &mut Fields, key: &str, value: Json) {
    match row.iter_mut().find(|(name, _)| name == key) {
        Some(entry) => entry.1 = value,
        None => row.push((key.to_string(), value)),
    }
}

fn request_of(case: &Json) -> Request {
    let request = member(case, "request");
    Request {
        task_id: text(member(request, "task_id")).to_string(),
        to: text(member(request, "to")).to_string(),
        actor: text(member(request, "actor")).to_string(),
        reason: text(member(request, "reason")).to_string(),
        manual: flag(member(request, "manual")),
    }
}

fn approval_of(value: &Json) -> Option<ApprovalFacts> {
    match value {
        Json::Null => None,
        facts => Some(ApprovalFacts {
            require_key: flag(member(facts, "require_key")),
            real_key: flag(member(facts, "real_key")),
            dev_key: flag(member(facts, "dev_key")),
            missing_gates: names(member(facts, "missing_gates")),
            unaccepted_gates: names(member(facts, "unaccepted_gates")),
        }),
    }
}

// ---- deciders ------------------------------------------------------------------------------------------------------------

/// The host decision with a call counter, a record of the facts it was given, the vector's approval facts (the writer does
/// not read gate facts yet, so a case supplies them here) and an optional tampering of the plan.
#[derive(Default)]
struct Probe {
    approval: Option<ApprovalFacts>,
    tamper: Option<fn(&mut TransitionPlan)>,
    calls: AtomicUsize,
    seen: Mutex<Vec<Facts>>,
}

impl Probe {
    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }

    fn seen_state(&self) -> Vec<Option<String>> {
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .map(|facts| {
                facts
                    .task
                    .as_ref()
                    .and_then(|row| obj_get(row, "state"))
                    .map(|state| text(state).to_string())
            })
            .collect()
    }
}

impl Decider for Probe {
    fn decide(
        &self,
        facts: &Facts,
        request: &Request,
        now: f64,
    ) -> Result<TransitionPlan, TransitionError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        let mut facts = facts.clone();
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(facts.clone());
        if self.approval.is_some() {
            facts.approval.clone_from(&self.approval);
        }
        let mut plan = HostDecider.decide(&facts, request, now)?;
        if let Some(tamper) = self.tamper {
            tamper(&mut plan);
        }
        Ok(plan)
    }
}

// ---- 1. every vector -------------------------------------------------------------------------------------------------------

/// A committed case to cross-check against Python afterwards.
struct Committed {
    id: String,
    path: PathBuf,
    task_id: String,
    rust_row: String,
}

/// How a case ended.
enum Outcome {
    Applied(Committed),
    Refused,
    NotStarted,
}

async fn replay(case: Json, dir: PathBuf) -> Outcome {
    let case = &case;
    let id = text(member(case, "id"));
    let now = number(member(case, "now"));
    let request = request_of(case);
    let expect = member(case, "expect");
    let outcome = text(member(expect, "outcome"));
    let rows: Vec<Fields> = match member(case, "task") {
        Json::Null => Vec::new(),
        row => vec![fields(row)],
    };
    let path = dir.join(format!("{id}.db"));
    seeded(&path, &rows).await;
    let before = std::fs::read(&path).expect("read store");

    let probe = Probe {
        approval: approval_of(member(case, "approval")),
        ..Probe::default()
    };
    let store = writer(&path).await;
    let result = store.transition(&request, now, &probe).await;
    store.close().await;

    if flag(member(case, "rust_not_started")) || outcome == "exception" {
        let error = result.expect_err(id);
        assert_eq!(error.write_state(), WriteState::NotStarted, "{id}: {error}");
        assert_eq!(error.code(), ErrorCode::Dep, "{id}");
        assert_eq!(
            std::fs::read(&path).expect("read store"),
            before,
            "{id}: store changed"
        );
        return Outcome::NotStarted;
    }
    assert_eq!(probe.calls(), 1, "{id}: decider calls");
    match outcome {
        "error" => {
            let error = result.expect_err(id);
            let wanted = member(expect, "error");
            assert_eq!(error.write_state(), WriteState::Final, "{id}");
            assert_eq!(error.cause(), &ApplyCause::Refused, "{id}");
            assert_eq!(
                error.code().as_str(),
                text(member(wanted, "code")),
                "{id}: code"
            );
            let seam = error.to_seam_error();
            assert_eq!(
                seam.message(),
                text(member(wanted, "message")),
                "{id}: message"
            );
            assert_eq!(
                Json::Obj(seam.details().to_vec()),
                *member(wanted, "details"),
                "{id}: details"
            );
            assert_eq!(
                std::fs::read(&path).expect("read store"),
                before,
                "{id}: store changed"
            );
            Outcome::Refused
        }
        "ok" => {
            let applied = result.unwrap_or_else(|error| panic!("{id}: {error}"));
            let row = rows.first().expect("an ok case has a task");
            let mut expected_row = row.clone();
            let diff = fields(member(expect, "row_diff"));
            for (name, value) in &diff {
                set(&mut expected_row, name, value.clone());
            }
            let expected_path = dir.join(format!("{id}.expected.db"));
            seeded(&expected_path, &[expected_row]).await;
            for audit in items(member(expect, "audit")) {
                insert_audit(&expected_path, &request.task_id, audit).await;
            }
            assert_eq!(
                content(&path).await,
                content(&expected_path).await,
                "{id}: store"
            );

            for (name, value) in &diff {
                assert_eq!(
                    obj_get(&applied.task, name),
                    Some(value),
                    "{id}: returned {name}"
                );
            }
            let wanted_audit: Vec<(String, String, String, String, f64)> =
                items(member(expect, "audit"))
                    .iter()
                    .map(|audit| {
                        (
                            text(member(audit, "from_state")).to_string(),
                            text(member(audit, "to_state")).to_string(),
                            text(member(audit, "actor")).to_string(),
                            text(member(audit, "reason")).to_string(),
                            number(member(audit, "ts")),
                        )
                    })
                    .collect();
            let written: Vec<_> = applied
                .audit
                .iter()
                .map(|audit| {
                    (
                        audit.from.clone(),
                        audit.to.clone(),
                        audit.actor.clone(),
                        audit.reason.clone(),
                        audit.ts,
                    )
                })
                .collect();
            assert_eq!(written, wanted_audit, "{id}: audit");
            assert_eq!(
                applied.effects,
                vec![PlanEffect::LiveRefresh],
                "{id}: effects"
            );
            Outcome::Applied(Committed {
                id: id.to_string(),
                path,
                task_id: request.task_id.clone(),
                rust_row: to_python_dumps(&Json::Obj(applied.task)),
            })
        }
        other => panic!("{id}: unknown outcome {other}"),
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn every_vector_replays_against_a_python_seeded_store() {
    let doc = parse_json(VECTORS).expect("vector file parses");
    let cases = items(member(&doc, "cases"));
    assert_eq!(cases.len(), 351);
    let dir = tempfile::tempdir().expect("tempdir");
    LazyLock::force(&TEMPLATE);
    // Every case has its own store file, so the cases replay concurrently.
    let mut running = tokio::task::JoinSet::new();
    for case in cases {
        running.spawn(replay(case.clone(), dir.path().to_path_buf()));
    }
    let (mut committed, mut refused, mut not_started) = (Vec::new(), 0, 0);
    while let Some(done) = running.join_next().await {
        match done.expect("a case panicked") {
            Outcome::Applied(done) => committed.push(done),
            Outcome::Refused => refused += 1,
            Outcome::NotStarted => not_started += 1,
        }
    }
    assert_eq!((committed.len(), refused, not_started), (121, 196, 34));

    // The row `Applied` returns prints exactly as `TaskStore.get` prints it on the committed store.
    let jobs = Json::Arr(
        committed
            .iter()
            .map(|done| {
                Json::Arr(vec![
                    jstr(&done.path.to_string_lossy()),
                    jstr(&done.task_id),
                ])
            })
            .collect(),
    );
    let printed = python(ORACLE_SCRIPT, &to_python_dumps(&jobs));
    let lines: Vec<&str> = printed.lines().collect();
    assert_eq!(lines.len(), committed.len());
    for (done, line) in committed.iter().zip(lines) {
        assert_eq!(done.rust_row, line, "{}: row as Python prints it", done.id);
    }
}

// ---- 2. a tampered plan writes nothing -----------------------------------------------------------------------------------

fn task(id: &str, state: &str) -> Fields {
    let mut row: Fields = vec![
        ("task_id".to_string(), jstr(id)),
        ("correlation_id".to_string(), jstr("C-1")),
        ("parent_id".to_string(), Json::Null),
        ("dag_depth".to_string(), Json::Int(0)),
        ("capability".to_string(), jstr("code")),
        ("agent_id".to_string(), Json::Null),
        ("title".to_string(), jstr("t")),
        ("inputs".to_string(), jstr("[]")),
        ("outputs".to_string(), jstr("[]")),
        ("acceptance".to_string(), jstr("[]")),
        ("budget".to_string(), jstr("{}")),
        ("risk_class".to_string(), jstr("low")),
        ("priority".to_string(), jstr("P2")),
        ("state".to_string(), jstr(state)),
        ("attempt".to_string(), Json::Int(0)),
        ("max_attempts".to_string(), Json::Int(3)),
        ("rework_loops".to_string(), Json::Int(0)),
        ("depends_on".to_string(), jstr("[]")),
        ("assigned_to".to_string(), Json::Null),
        ("notes".to_string(), Json::Null),
        ("created_at".to_string(), Json::Float(1.0)),
        ("updated_at".to_string(), Json::Float(1.0)),
    ];
    set(&mut row, "state", jstr(state));
    row
}

fn request(task_id: &str, to: &str) -> Request {
    Request {
        task_id: task_id.to_string(),
        to: to.to_string(),
        actor: "A01".to_string(),
        reason: "because".to_string(),
        manual: true,
    }
}

/// Makes any write on the store fail loudly: a plan that reaches the `UPDATE` or the audit `INSERT` is caught by these, not
/// by a later comparison.
async fn tripwire(path: &Path) {
    exec(
        path,
        "CREATE TRIGGER tripwire_update BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT, 'tripwire: update'); END",
    )
    .await;
    exec(
        path,
        "CREATE TRIGGER tripwire_audit BEFORE INSERT ON transitions BEGIN SELECT RAISE(ABORT, 'tripwire: audit'); END",
    )
    .await;
}

/// IN_REVIEW → CHANGES_REQUESTED with loops to spare: columns `state`, `rework_loops`, `notes`; one audit row.
fn single_hop() -> (Fields, Request) {
    (
        task("T-1", "IN_REVIEW"),
        request("T-1", "CHANGES_REQUESTED"),
    )
}

/// The same request on a task at the rework cap: columns `state`; two audit rows (the auto-escalation).
fn two_hop() -> (Fields, Request) {
    let mut row = task("T-1", "IN_REVIEW");
    set(&mut row, "rework_loops", Json::Int(2));
    (row, request("T-1", "CHANGES_REQUESTED"))
}

fn wrong_type(column: &str, expected: &'static str) -> PlanViolation {
    PlanViolation::ColumnType {
        column: column.to_string(),
        expected,
    }
}

/// What a test case changes in the plan the host decision made.
type Tamper = fn(&mut TransitionPlan);
/// Builds the seeded task row and the request of a case.
type Base = fn() -> (Fields, Request);

#[tokio::test]
async fn a_tampered_plan_writes_nothing_and_is_a_typed_internal_error() {
    let cases: Vec<(&str, Base, Tamper, PlanViolation)> = vec![
        (
            "extra column title",
            single_hop,
            |plan| plan.columns.push(("title".into(), jstr("pwned"))),
            PlanViolation::UnknownColumn {
                column: "title".into(),
            },
        ),
        (
            "extra column updated_at",
            single_hop,
            |plan| plan.columns.push(("updated_at".into(), Json::Float(0.0))),
            PlanViolation::UnknownColumn {
                column: "updated_at".into(),
            },
        ),
        (
            "extra column task_id",
            single_hop,
            |plan| plan.columns.push(("task_id".into(), jstr("T-2"))),
            PlanViolation::UnknownColumn {
                column: "task_id".into(),
            },
        ),
        (
            "duplicate column",
            single_hop,
            |plan| plan.columns.push(("rework_loops".into(), Json::Int(9))),
            PlanViolation::DuplicateColumn {
                column: "rework_loops".into(),
            },
        ),
        (
            "wrong from",
            single_hop,
            |plan| plan.from = "PLANNED".into(),
            PlanViolation::FromMismatch {
                plan: "PLANNED".into(),
                row: "IN_REVIEW".into(),
            },
        ),
        (
            "a whole plan for another state",
            single_hop,
            |plan| {
                plan.from = "PLANNED".into();
                plan.to = "CLAIMED".into();
                plan.columns = vec![("state".into(), jstr("CLAIMED"))];
                plan.audit[0].from = "PLANNED".into();
                plan.audit[0].to = "CLAIMED".into();
            },
            PlanViolation::FromMismatch {
                plan: "PLANNED".into(),
                row: "IN_REVIEW".into(),
            },
        ),
        (
            "three audit rows",
            two_hop,
            |plan| plan.audit.push(plan.audit[1].clone()),
            PlanViolation::AuditRowCount { count: 3 },
        ),
        (
            "no audit row",
            single_hop,
            |plan| plan.audit.clear(),
            PlanViolation::AuditRowCount { count: 0 },
        ),
        (
            "unchained second row",
            two_hop,
            |plan| plan.audit[1].from = "IN_PROGRESS".into(),
            PlanViolation::Unchained {
                index: 1,
                expected: "CHANGES_REQUESTED".into(),
                found: "IN_PROGRESS".into(),
            },
        ),
        (
            "first row does not start at from",
            single_hop,
            |plan| plan.audit[0].from = "PLANNED".into(),
            PlanViolation::Unchained {
                index: 0,
                expected: "IN_REVIEW".into(),
                found: "PLANNED".into(),
            },
        ),
        (
            "audit ends elsewhere than plan.to",
            two_hop,
            |plan| plan.audit[1].to = "CANCELLED".into(),
            PlanViolation::AuditEnd {
                found: "CANCELLED".into(),
                plan: "ESCALATED".into(),
            },
        ),
        (
            "illegal final state",
            single_hop,
            |plan| {
                plan.to = "DONE".into();
                plan.audit[0].to = "DONE".into();
                plan.columns[0].1 = jstr("DONE");
            },
            PlanViolation::IllegalStep {
                from: "IN_REVIEW".into(),
                to: "DONE".into(),
            },
        ),
        (
            "state column is not plan.to",
            single_hop,
            |plan| plan.columns[0].1 = jstr("FAILED"),
            PlanViolation::StateColumnMismatch {
                plan: "CHANGES_REQUESTED".into(),
            },
        ),
        (
            "no state column",
            single_hop,
            |plan| {
                plan.columns.remove(0);
            },
            PlanViolation::MissingStateColumn,
        ),
        (
            "state column is a number",
            single_hop,
            |plan| plan.columns[0].1 = Json::Int(3),
            wrong_type("state", "text"),
        ),
        (
            "attempt is text",
            single_hop,
            |plan| plan.columns.push(("attempt".into(), jstr("5"))),
            wrong_type("attempt", "an integer"),
        ),
        (
            "rework_loops is a float",
            single_hop,
            |plan| plan.columns[1].1 = Json::Float(1.0),
            wrong_type("rework_loops", "an integer"),
        ),
        (
            "notes is text",
            single_hop,
            |plan| plan.columns[2].1 = jstr("{}"),
            wrong_type("notes", "an object"),
        ),
        (
            "notes holds NaN",
            single_hop,
            |plan| {
                plan.columns[2].1 = Json::Obj(vec![(
                    "deep".into(),
                    Json::Arr(vec![Json::Float(f64::NAN)]),
                )]);
            },
            PlanViolation::NonFiniteNotes,
        ),
        (
            "audit row by another actor",
            single_hop,
            |plan| plan.audit[0].actor = "A99".into(),
            PlanViolation::ActorMismatch { index: 0 },
        ),
    ];
    let dir = tempfile::tempdir().expect("tempdir");
    for (name, base, tamper, violation) in cases {
        let (row, request) = base();
        let path = dir.path().join("tasks.db");
        seeded(&path, &[row]).await;
        tripwire(&path).await;
        let before = std::fs::read(&path).expect("read store");

        let probe = Probe {
            tamper: Some(tamper),
            ..Probe::default()
        };
        let store = writer(&path).await;
        let error = store
            .transition(&request, NOW, &probe)
            .await
            .expect_err(name);
        store.close().await;

        assert_eq!(error.cause(), &ApplyCause::InvalidPlan(violation), "{name}");
        assert_eq!(error.code(), ErrorCode::Internal, "{name}");
        assert_eq!(error.write_state(), WriteState::NotStarted, "{name}");
        assert_eq!(
            Json::Obj(error.details().to_vec()),
            Json::Obj(vec![("task_id".to_string(), jstr("T-1"))]),
            "{name}: details"
        );
        assert_eq!(probe.calls(), 1, "{name}: decider calls");
        assert_eq!(
            std::fs::read(&path).expect("read store"),
            before,
            "{name}: store changed"
        );
    }
}

#[test]
fn a_plan_for_a_missing_row_or_a_row_without_text_state_is_refused() {
    let plan = TransitionPlan {
        from: "IN_REVIEW".into(),
        to: "FAILED".into(),
        columns: vec![("state".into(), jstr("FAILED"))],
        audit: vec![AuditRow {
            from: "IN_REVIEW".into(),
            to: "FAILED".into(),
            actor: "A01".into(),
            reason: String::new(),
        }],
        effects: vec![PlanEffect::LiveRefresh],
    };
    let validator = PlanValidator::new("A01");
    assert_eq!(
        validator.validate(&plan, None).expect_err("no row"),
        PlanViolation::NoTaskRow
    );
    let numeric: Fields = vec![("state".into(), Json::Int(7))];
    assert_eq!(
        validator
            .validate(&plan, Some(&numeric))
            .expect_err("state"),
        PlanViolation::RowStateNotText
    );
    let row: Fields = vec![("state".into(), jstr("IN_REVIEW"))];
    let accepted = validator.validate(&plan, Some(&row)).expect("a valid plan");
    assert_eq!(
        (accepted.from, accepted.to, accepted.notes, accepted.attempt),
        ("IN_REVIEW", "FAILED", None, None)
    );
}

#[tokio::test]
async fn a_request_with_a_non_finite_clock_never_reaches_the_store() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    let (row, request) = single_hop();
    seeded(&path, &[row]).await;
    let before = std::fs::read(&path).expect("read store");
    let probe = Probe::default();
    let store = writer(&path).await;
    for now in [f64::NAN, f64::INFINITY] {
        let error = store
            .transition(&request, now, &probe)
            .await
            .expect_err("refused");
        assert_eq!(error.write_state(), WriteState::NotStarted);
        assert_eq!(error.code(), ErrorCode::Dep);
        assert_eq!(error.cause(), &ApplyCause::Store(ErrorKind::Unsupported));
    }
    store.close().await;
    assert_eq!(probe.calls(), 0);
    assert_eq!(std::fs::read(&path).expect("read store"), before);
}

// ---- 3. the conditional UPDATE and the write lock ----------------------------------------------------------------------

/// Why this test shape: `BEGIN IMMEDIATE` holds the write lock from before the row is read to the commit, so no other
/// connection can change the row in between and the conditional `UPDATE` cannot lose in normal operation (the lock tests below
/// prove that). The guard is still the statement that decides, so the trigger makes SQLite skip the matched row exactly as a
/// row that no longer matches `state = ?` would be skipped: `rows_affected` is 0 and the transaction must end as Python's
/// `E-CONTRACT concurrent transition`, with everything rolled back, including a first step that already succeeded.
#[tokio::test]
async fn an_update_that_matches_no_row_is_a_concurrent_transition_and_rolls_everything_back() {
    let cases: Vec<(&str, Base, &str, &str)> = vec![
        (
            "single step",
            single_hop,
            "CREATE TRIGGER skip BEFORE UPDATE ON tasks BEGIN SELECT RAISE(IGNORE); END",
            "IN_REVIEW",
        ),
        (
            "second step of an escalation",
            two_hop,
            "CREATE TRIGGER skip BEFORE UPDATE ON tasks WHEN NEW.state = 'ESCALATED' BEGIN SELECT RAISE(IGNORE); END",
            "CHANGES_REQUESTED",
        ),
    ];
    let dir = tempfile::tempdir().expect("tempdir");
    for (name, base, trigger, expected_from) in cases {
        let (row, request) = base();
        let path = dir.path().join("tasks.db");
        seeded(&path, &[row]).await;
        exec(&path, trigger).await;
        let before = std::fs::read(&path).expect("read store");

        let probe = Probe::default();
        let store = writer(&path).await;
        let error = store
            .transition(&request, NOW, &probe)
            .await
            .expect_err(name);
        store.close().await;

        assert_eq!(
            error.cause(),
            &ApplyCause::Store(ErrorKind::ConcurrentTransition),
            "{name}"
        );
        assert_eq!(error.code(), ErrorCode::Contract, "{name}");
        assert_eq!(error.write_state(), WriteState::Final, "{name}");
        assert_eq!(
            error.to_seam_error().message(),
            format!("E-CONTRACT: concurrent transition on T-1: expected {expected_from}"),
            "{name}"
        );
        assert_eq!(
            Json::Obj(error.details().to_vec()),
            Json::Obj(vec![("task_id".to_string(), jstr("T-1"))]),
            "{name}: details"
        );
        assert_eq!(probe.calls(), 1, "{name}: decider calls");
        assert_eq!(
            std::fs::read(&path).expect("read store"),
            before,
            "{name}: store changed"
        );
    }
}

/// Blocks inside the decision until the test releases it, so the test can act while the transaction holds the lock.
struct Gate {
    entered: Mutex<Option<oneshot::Sender<()>>>,
    go: Mutex<mpsc::Receiver<()>>,
    probe: Probe,
}

impl Decider for Gate {
    fn decide(
        &self,
        facts: &Facts,
        request: &Request,
        now: f64,
    ) -> Result<TransitionPlan, TransitionError> {
        if let Some(entered) = self
            .entered
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take()
        {
            entered.send(()).expect("test is waiting");
        }
        self.go
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .recv()
            .expect("test releases the gate");
        self.probe.decide(facts, request, now)
    }
}

fn is_busy(error: &sqlx::Error) -> bool {
    error
        .as_database_error()
        .and_then(|database| database.code())
        .and_then(|code| code.parse::<i32>().ok())
        .is_some_and(|code| matches!(code & 0xff, 5 | 6))
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_decider_runs_once_inside_the_lock_on_the_row_read_inside_the_lock() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    seeded(&path, &[task("T-1", "PLANNED")]).await;
    // A change that lands before the transaction begins is what the decider sees: the row is read inside the lock.
    exec(
        &path,
        "UPDATE tasks SET state='CLAIMED' WHERE task_id='T-1'",
    )
    .await;

    // Both contenders connect before the lock is taken: only their writes may wait for it.
    let mut raw = SqliteConnectOptions::new()
        .filename(&path)
        .busy_timeout(Duration::from_millis(100))
        .connect()
        .await
        .expect("raw connection");
    let mut config = WriterConfig::new(&path);
    config.busy_timeout = Duration::from_millis(100);
    let other = TaskStoreWriter::new(open_for_write(&config).await.expect("open"));

    let store = Arc::new(writer(&path).await);
    let (entered_tx, entered_rx) = oneshot::channel();
    let (go_tx, go_rx) = mpsc::channel();
    let gate = Arc::new(Gate {
        entered: Mutex::new(Some(entered_tx)),
        go: Mutex::new(go_rx),
        probe: Probe::default(),
    });
    let held = tokio::spawn({
        let (store, gate) = (Arc::clone(&store), Arc::clone(&gate));
        async move {
            store
                .transition(&request("T-1", "IN_PROGRESS"), NOW, gate.as_ref())
                .await
        }
    });
    entered_rx.await.expect("the decider is running");

    // While the decider runs: a raw writer on its own connection is locked out ...
    let refused = sqlx::query("UPDATE tasks SET state='FAILED' WHERE task_id='T-1'")
        .execute(&mut raw)
        .await
        .expect_err("the write lock is held");
    assert!(is_busy(&refused), "{refused}");
    // ... and so is a second writer, whose decider is never called.
    let contender = Probe::default();
    let error = other
        .transition(&request("T-1", "IN_PROGRESS"), NOW, &contender)
        .await
        .expect_err("the write lock is held");
    assert_eq!(error.cause(), &ApplyCause::Store(ErrorKind::Busy));
    assert_eq!(error.write_state(), WriteState::NotStarted);
    assert_eq!(contender.calls(), 0);
    other.close().await;

    go_tx.send(()).expect("decider is waiting");
    let applied = held.await.expect("join").expect("transition");
    assert_eq!(obj_get(&applied.task, "state"), Some(&jstr("IN_PROGRESS")));
    assert_eq!(gate.probe.calls(), 1);
    assert_eq!(gate.probe.seen_state(), vec![Some("CLAIMED".to_string())]);

    // The lock is gone with the transaction.
    sqlx::query("UPDATE tasks SET state='FAILED' WHERE task_id='T-1'")
        .execute(&mut raw)
        .await
        .expect("the lock is released");
}

#[cfg(feature = "fault-injection")]
#[tokio::test]
async fn nothing_is_visible_before_the_commit() {
    use gsd_taskwriter::fault::PausePoint;

    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    seeded(&path, &[task("T-1", "IN_PROGRESS")]).await;
    let untouched = content(&path).await;

    let store = Arc::new(writer(&path).await);
    let mut gate = store.store().faults().arm(PausePoint::BeforeCommit);
    let committing = tokio::spawn({
        let store = Arc::clone(&store);
        async move {
            store
                .transition(&request("T-1", "IN_REVIEW"), NOW, &HostDecider)
                .await
        }
    });
    gate.wait_reached().await;
    assert_eq!(
        content(&path).await,
        untouched,
        "a reader sees the old store"
    );
    gate.release();
    let applied = committing.await.expect("join").expect("transition");
    assert_eq!(obj_get(&applied.task, "state"), Some(&jstr("IN_REVIEW")));
    assert_ne!(content(&path).await, untouched);
}

// ---- 4 and 5. one call per path, history only grows -----------------------------------------------------------------------

#[tokio::test]
async fn the_decider_is_called_once_on_every_path_that_reaches_the_row() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    seeded(&path, &[task("T-1", "IN_PROGRESS")]).await;
    let store = writer(&path).await;
    for (to, task_id, outcome) in [
        ("IN_REVIEW", "T-1", "applied"),
        ("DONE", "T-1", "illegal"),
        ("FAILED", "T-missing", "unknown task"),
    ] {
        let probe = Probe::default();
        let result = store.transition(&request(task_id, to), NOW, &probe).await;
        assert_eq!(result.is_ok(), outcome == "applied", "{outcome}");
        assert_eq!(probe.calls(), 1, "{outcome}: decider calls");
    }
    store.close().await;
}

#[tokio::test]
async fn audit_history_only_grows_and_every_row_carries_the_one_clock_reading() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    seeded(&path, &[task("T-1", "IN_PROGRESS")]).await;
    let store = writer(&path).await;

    let first: Applied = store
        .transition(&request("T-1", "IN_REVIEW"), 10.5, &HostDecider)
        .await
        .expect("first");
    let after_first = content(&path).await;
    let second = store
        .transition(&request("T-1", "CHANGES_REQUESTED"), 20.5, &HostDecider)
        .await
        .expect("second");
    store.close().await;
    let after_second = content(&path).await;

    let audit = |snapshot: &[(&str, Vec<Vec<Cell>>)]| {
        snapshot
            .iter()
            .find(|(table, _)| *table == "transitions")
            .map(|(_, rows)| rows.clone())
            .expect("transitions table")
    };
    let (rows_first, rows_second) = (audit(&after_first), audit(&after_second));
    assert_eq!(rows_first.len(), 1);
    assert_eq!(rows_second.len(), 2);
    assert_eq!(
        rows_second[0], rows_first[0],
        "the first audit row is never rewritten"
    );
    assert_eq!(rows_second[1][0], Cell::Int(2));
    assert_eq!(rows_second[1][6], Cell::Real(20.5_f64.to_bits()));
    assert_eq!(first.audit[0].ts, 10.5);
    assert_eq!(second.audit[0].ts, 20.5);
    assert_eq!(
        obj_get(&second.task, "updated_at"),
        Some(&Json::Float(20.5))
    );
    let Json::Obj(notes) =
        parse_json(text(obj_get(&second.task, "notes").expect("notes"))).expect("notes parse")
    else {
        panic!("notes is an object");
    };
    assert_eq!(
        obj_get(&notes, "verdicts_since"),
        Some(&Json::Float(20.5 + 0.001)),
        "verdicts_since is the clock reading plus the decider's 0.001"
    );
}
