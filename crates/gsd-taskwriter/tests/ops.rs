//! Writer ops (`TaskStoreWriter::apply_ops`, U3.5d): one transaction per op list, with the invariants of `transition`.
//!
//! Stores are built by the REAL Python `TaskStore` (`python3 -c`, no network): the schema is Python's. The Python build runs once;
//! every test copies the resulting database file. The repository root is `SWARM_REPO_ROOT` or two levels above this crate.
//!
//! What is proved here:
//!
//! - every one of the oracle vectors of `tests/vectors/ops.json` (made by `tests/parity/gen_ops_vectors.py` from the real
//!   `TaskStore` calls inside one `store.transaction()`) replays against a store seeded with the vector's initial rows: the rows of
//!   `tasks`, `transitions` and `artifacts` equal Python's, typed as stored; a refusal Python answers carries its code, message and
//!   details and leaves the store file byte-identical; a list Python accepts (or fails on with another exception type) and Rust
//!   does not reproduce is refused before the first write (`E-DEP`, not started) and leaves the file byte-identical;
//! - a list is validated before its first write: a bad last op writes nothing, whatever the earlier ops were (a trigger makes every
//!   write on the store fail loudly), and a transition plan is checked against the state the list's own earlier ops leave the row in;
//! - a database failure after the first write rolls the whole list back;
//! - the wire form refuses what it cannot read without quoting a value;
//! - (`fault-injection`) a list held between two ops is invisible, holds the write lock against a second writer, and leaves none of its
//!   ops applied when it dies there.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::LazyLock;
use std::time::Duration;

use gsd_codes::ErrorCode;
use gsd_route::transition_plan::{AuditRow, PlanEffect, TransitionPlan};
use gsd_route::{jstr, obj_get, parse_json, Fields, Json};
use gsd_taskwriter::{
    open_for_write, ErrorKind, NewTask, OpsApplied, OpsCause, OpsError, PlanViolation,
    TaskStoreWriter, WriteState, WriterConfig, WriterOp,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};

const VECTORS: &str = include_str!("vectors/ops.json");

/// Creates an empty Task Store through Python's `TaskStore` (schema and migrations are Python's).
const TEMPLATE_SCRIPT: &str =
    "import sys\nfrom swarm.taskstore import TaskStore\nTaskStore(sys.argv[1]).conn.close()\n";

const NOW: f64 = 1_700_000_123.456789;

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

fn python(script: &str, arg: &str) {
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

/// Inserts rows into their tables in one transaction, with the values exactly as given (a NULL, an integer or a text where Python
/// would not put one is the point of some vectors).
async fn insert_rows(path: &Path, rows: &[(&str, Fields)]) {
    let mut conn = connect(path).await;
    sqlx::query("BEGIN")
        .execute(&mut conn)
        .await
        .expect("begin");
    for (table, row) in rows {
        let names: Vec<&str> = row.iter().map(|(name, _)| name.as_str()).collect();
        let sql = format!(
            "INSERT INTO {table} ({}) VALUES ({})",
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
        insert.execute(&mut conn).await.expect(&sql);
    }
    sqlx::query("COMMIT")
        .execute(&mut conn)
        .await
        .expect("commit");
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

type Table = (&'static str, Vec<Vec<Cell>>);

/// Every row of the four tables and the id counters (`SELECT *` in rowid order), values typed as stored: the whole content.
async fn content(path: &Path) -> Vec<Table> {
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

fn table<'a>(content: &'a [Table], name: &str) -> &'a [Vec<Cell>] {
    &content
        .iter()
        .find(|(table, _)| *table == name)
        .unwrap_or_else(|| panic!("no table {name}"))
        .1
}

/// A vector row (an object of column values) as the cells `content` reads back.
fn cells_of(row: &Json) -> Vec<Cell> {
    fields(row)
        .iter()
        .map(|(_, value)| match value {
            Json::Null => Cell::Null,
            Json::Int(number) => Cell::Int(*number),
            Json::Float(number) => Cell::Real(number.to_bits()),
            Json::Str(text) => Cell::Text(text.clone()),
            other => panic!("no cell for {other:?}"),
        })
        .collect()
}

async fn writer(path: &Path) -> TaskStoreWriter {
    TaskStoreWriter::new(
        open_for_write(&WriterConfig::new(path))
            .await
            .expect("open_for_write"),
    )
}

/// Applies `ops` on a fresh connection and closes it: what one route call does.
async fn apply(path: &Path, ops: &[WriterOp]) -> Result<OpsApplied, OpsError> {
    let store = writer(path).await;
    let result = store.apply_ops(ops, NOW).await;
    store.close().await;
    result
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

fn set(row: &mut Fields, key: &str, value: Json) {
    match row.iter_mut().find(|(name, _)| name == key) {
        Some(entry) => entry.1 = value,
        None => row.push((key.to_string(), value)),
    }
}

/// The id of the task an op of the wire form names.
fn op_task_id(op: &Json) -> &str {
    match member(op, "op") {
        Json::Str(kind) if kind == "create_task" => text(member(member(op, "task"), "task_id")),
        _ => text(member(op, "task_id")),
    }
}

// ---- 1. the oracle vectors ----------------------------------------------------------------------------------------------

/// A store seeded with the vector's initial rows, as the generator's `initial` recorded them.
async fn seeded_from(path: &Path, initial: &Json) {
    std::fs::write(path, &*TEMPLATE).expect("copy template");
    let mut rows = Vec::new();
    for name in ["tasks", "transitions", "artifacts"] {
        rows.extend(
            items(member(initial, name))
                .iter()
                .map(|row| (name, fields(row))),
        );
    }
    insert_rows(path, &rows).await;
}

/// Replays one case; the error says what differs.
async fn replay(case: &Json) -> Result<(), String> {
    let id = text(member(case, "id"));
    let fail = |what: String| Err(format!("{id}: {what}"));
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    let initial = member(case, "initial");
    seeded_from(&path, initial).await;
    let bytes = std::fs::read(&path).expect("read seeded store");
    let before = content(&path).await;
    let python = member(case, "python");
    let after = member(python, "after");

    let outcome = match WriterOp::list_from_json(member(case, "ops")) {
        Ok(ops) => apply(&path, &ops).await,
        Err(error) => Err(error),
    };
    let refusals_leave_the_file_alone = || std::fs::read(&path).expect("read store") == bytes;

    if text(member(member(case, "rust"), "outcome")) == "not_started" {
        let Err(error) = outcome else {
            return fail("Rust applied a list it must refuse before the first write".to_string());
        };
        if error.write_state() != WriteState::NotStarted || error.code() != ErrorCode::Dep {
            return fail(format!("refusal is {error}, {:?}", error.write_state()));
        }
        if !refusals_leave_the_file_alone() || content(&path).await != before {
            return fail("the store changed after a not-started refusal".to_string());
        }
        return Ok(());
    }

    match text(member(python, "outcome")) {
        "error" => {
            let Err(error) = outcome else {
                return fail("Python raised, Rust applied the list".to_string());
            };
            let expected = member(python, "error");
            let shown = format!("{}: {}", error.code().as_str(), error.message());
            if text(member(expected, "code")) != error.code().as_str()
                || text(member(expected, "message")) != shown
                || &Json::Obj(error.details().to_vec()) != member(expected, "details")
            {
                return fail(format!("error {shown} {:?}", error.details()));
            }
            if error.write_state() != WriteState::Final {
                return fail("Python's own answer is final".to_string());
            }
            if !refusals_leave_the_file_alone() || content(&path).await != before {
                return fail("the store changed after a refusal".to_string());
            }
            if after != initial {
                return fail(
                    "the vector's rolled-back store differs from its initial rows".to_string(),
                );
            }
        }
        "ok" => {
            let applied = match outcome {
                Ok(applied) => applied,
                Err(error) => return fail(format!("Python applied the list, Rust says {error}")),
            };
            let now = content(&path).await;
            for name in ["tasks", "transitions", "artifacts"] {
                let expected: Vec<Vec<Cell>> =
                    items(member(after, name)).iter().map(cells_of).collect();
                if table(&now, name) != expected.as_slice() {
                    return fail(format!("table {name} differs from Python's"));
                }
            }
            if !table(&now, "verdicts").is_empty() {
                return fail("a verdict row appeared".to_string());
            }
            check_result(case, &applied)?;
        }
        other => return fail(format!("outcome {other} must be refused not_started")),
    }
    Ok(())
}

/// What `OpsApplied` reports equals what Python's run wrote: the new audit rows, and the touched tasks in order of first touch.
fn check_result(case: &Json, applied: &OpsApplied) -> Result<(), String> {
    let id = text(member(case, "id"));
    let python = member(case, "python");
    let seen = items(member(member(case, "initial"), "transitions")).len();
    let wrote = &items(member(member(python, "after"), "transitions"))[seen..];
    if applied.audit.len() != wrote.len() {
        return Err(format!(
            "{id}: {} audit rows reported, {} written",
            applied.audit.len(),
            wrote.len()
        ));
    }
    for (reported, row) in applied.audit.iter().zip(wrote) {
        let from = match member(row, "from_state") {
            Json::Null => None,
            other => Some(text(other).to_string()),
        };
        if reported.task_id != text(member(row, "task_id"))
            || reported.from != from
            || reported.to != text(member(row, "to_state"))
            || reported.actor != text(member(row, "actor"))
            || reported.reason != text(member(row, "reason"))
            || reported.ts != NOW
        {
            return Err(format!("{id}: audit row {reported:?} differs from {row:?}"));
        }
    }
    let mut touched: Vec<&str> = Vec::new();
    for op in items(member(case, "ops")) {
        if !touched.contains(&op_task_id(op)) {
            touched.push(op_task_id(op));
        }
    }
    let reported: Vec<String> = applied
        .tasks
        .iter()
        .map(|row| text(obj_get(row, "task_id").expect("task_id")).to_string())
        .collect();
    if reported != touched {
        return Err(format!(
            "{id}: touched tasks {reported:?}, expected {touched:?}"
        ));
    }
    for row in &applied.tasks {
        let task_id = text(obj_get(row, "task_id").expect("task_id"));
        let stored = items(member(member(python, "after"), "tasks"))
            .iter()
            .find(|candidate| text(member(candidate, "task_id")) == task_id)
            .expect("task in Python's rows");
        let notes = match member(stored, "notes") {
            Json::Str(notes) => match parse_json(notes) {
                Ok(object @ Json::Obj(_)) => object,
                _ => Json::Obj(Vec::new()),
            },
            _ => Json::Obj(Vec::new()),
        };
        if obj_get(row, "state") != Some(member(stored, "state"))
            || obj_get(row, "notes_json") != Some(&notes)
            || obj_get(row, "updated_at") != Some(member(stored, "updated_at"))
        {
            return Err(format!(
                "{id}: the row returned for {task_id} differs from the stored one"
            ));
        }
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn every_vector_replays_against_a_python_seeded_store() {
    let doc = parse_json(VECTORS).expect("vectors parse");
    assert_eq!(
        text(member(&doc, "schema")),
        "gsd-taskwriter.ops-vectors.v1"
    );
    assert_eq!(member(member(&doc, "constants"), "now"), &Json::Float(NOW));
    let cases = items(member(&doc, "cases"));
    assert!(cases.len() >= 100, "{} cases", cases.len());

    let mut failures = Vec::new();
    for case in cases {
        if let Err(problem) = replay(case).await {
            failures.push(problem);
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}

#[test]
fn the_vectors_cover_every_op_kind_outcome_and_refusal_rule() {
    let doc = parse_json(VECTORS).expect("vectors parse");
    let cases = items(member(&doc, "cases"));
    let mut kinds = std::collections::BTreeSet::new();
    let mut outcomes = std::collections::BTreeSet::new();
    for case in cases {
        for op in items(member(case, "ops")) {
            kinds.insert(text(member(op, "op")).to_string());
        }
        outcomes.insert((
            text(member(member(case, "python"), "outcome")).to_string(),
            text(member(member(case, "rust"), "outcome")).to_string(),
        ));
    }
    let want_kinds = [
        "add_artifact",
        "append_feedback",
        "create_task",
        "set_depends_on",
        "set_notes",
        "transition",
    ];
    assert_eq!(kinds.into_iter().collect::<Vec<_>>(), want_kinds);
    let want_outcomes = [
        ("error", "same"),
        ("exception", "not_started"),
        ("ok", "not_started"),
        ("ok", "same"),
    ];
    assert_eq!(
        outcomes
            .iter()
            .map(|(a, b)| (a.as_str(), b.as_str()))
            .collect::<Vec<_>>(),
        want_outcomes
    );
    // The auto-escalation writes two audit rows for one transition op.
    assert!(cases.iter().any(|case| {
        let written = items(member(
            member(member(case, "python"), "after"),
            "transitions",
        ))
        .len()
            - items(member(member(case, "initial"), "transitions")).len();
        written == 2 && items(member(case, "ops")).len() == 1
    }));
}

// ---- 2. refusals before the first write, and a rollback after it -----------------------------------------------------------

fn task_row(id: &str, state: &str) -> Fields {
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

/// A copy of the Python-built store holding `rows` as `tasks`.
async fn seeded(path: &Path, rows: &[Fields]) {
    std::fs::write(path, &*TEMPLATE).expect("copy template");
    let rows: Vec<(&str, Fields)> = rows.iter().map(|row| ("tasks", row.clone())).collect();
    insert_rows(path, &rows).await;
}

/// Makes any write on the store fail loudly: a list that reaches a write is caught by these, not by a later comparison.
async fn tripwire(path: &Path) {
    for (name, when, on) in [
        ("tasks_update", "UPDATE", "tasks"),
        ("tasks_insert", "INSERT", "tasks"),
        ("transitions_insert", "INSERT", "transitions"),
        ("artifacts_insert", "INSERT", "artifacts"),
    ] {
        exec(
            path,
            &format!("CREATE TRIGGER tripwire_{name} BEFORE {when} ON {on} BEGIN SELECT RAISE(ABORT, 'tripwire: {name}'); END"),
        )
        .await;
    }
}

fn plan(from: &str, to: &str, actor: &str) -> TransitionPlan {
    TransitionPlan {
        from: from.to_string(),
        to: to.to_string(),
        columns: vec![("state".to_string(), jstr(to))],
        audit: vec![AuditRow {
            from: from.to_string(),
            to: to.to_string(),
            actor: actor.to_string(),
            reason: "r".to_string(),
        }],
        effects: vec![PlanEffect::LiveRefresh],
    }
}

fn transition(id: &str, from: &str, to: &str) -> WriterOp {
    WriterOp::Transition {
        task_id: id.to_string(),
        actor: "A01".to_string(),
        plan: plan(from, to, "A01"),
    }
}

fn notes(id: &str, pairs: &[(&str, Json)]) -> WriterOp {
    WriterOp::SetNotes {
        task_id: id.to_string(),
        set: pairs
            .iter()
            .map(|(key, value)| (key.to_string(), value.clone()))
            .collect(),
    }
}

fn artifact(id: &str, kind: &str) -> WriterOp {
    WriterOp::AddArtifact {
        task_id: id.to_string(),
        kind: kind.to_string(),
        uri: format!("file:///out/{kind}"),
        version: "1".to_string(),
        digest: "d".to_string(),
        producer: "A05".to_string(),
    }
}

fn feedback(id: &str, entry: Json) -> WriterOp {
    WriterOp::AppendFeedback {
        task_id: id.to_string(),
        entry,
    }
}

fn create(id: &str) -> WriterOp {
    WriterOp::CreateTask(NewTask::new(id, "C-1", "code"))
}

/// What a refusal looks like.
struct Expect {
    name: &'static str,
    ops: Vec<WriterOp>,
    index: usize,
    cause: OpsCause,
    code: ErrorCode,
    state: WriteState,
    says: &'static str,
}

fn unsupported(name: &'static str, ops: Vec<WriterOp>, index: usize, says: &'static str) -> Expect {
    Expect {
        name,
        ops,
        index,
        cause: OpsCause::Failed(ErrorKind::Unsupported),
        code: ErrorCode::Dep,
        state: WriteState::NotStarted,
        says,
    }
}

fn invalid_plan(
    name: &'static str,
    ops: Vec<WriterOp>,
    index: usize,
    violation: PlanViolation,
) -> Expect {
    Expect {
        name,
        ops,
        index,
        cause: OpsCause::InvalidPlan(violation),
        code: ErrorCode::Internal,
        state: WriteState::NotStarted,
        says: "invalid transition plan",
    }
}

fn unknown_task(name: &'static str, ops: Vec<WriterOp>, index: usize) -> Expect {
    Expect {
        name,
        ops,
        index,
        cause: OpsCause::UnknownTask,
        code: ErrorCode::Input,
        state: WriteState::Final,
        says: "unknown task T-missing",
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_list_is_validated_before_its_first_write() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    let mut odd_notes = task_row("T-3", "IN_PROGRESS");
    set(&mut odd_notes, "notes", jstr(r#"{"feedback": "oops"}"#));
    let mut odd_outputs = task_row("T-4", "IN_PROGRESS");
    set(&mut odd_outputs, "outputs", jstr(r#"{"a": 1}"#));
    seeded(
        &path,
        &[
            task_row("T-1", "IN_PROGRESS"),
            task_row("T-2", "PLANNED"),
            odd_notes,
            odd_outputs,
        ],
    )
    .await;
    tripwire(&path).await;
    let bytes = std::fs::read(&path).expect("read store");
    let before = content(&path).await;

    // Every list starts with ops that are fine and could be written: only the last op is bad.
    let good = || {
        vec![
            notes("T-1", &[("a", Json::Int(1))]),
            artifact("T-1", "report"),
            transition("T-2", "PLANNED", "CLAIMED"),
        ]
    };
    let with = |bad: WriterOp| {
        let mut ops = good();
        ops.push(bad);
        ops
    };
    let tampered = |edit: fn(&mut TransitionPlan)| {
        let mut plan = plan("IN_PROGRESS", "IN_REVIEW", "A01");
        edit(&mut plan);
        WriterOp::Transition {
            task_id: "T-1".to_string(),
            actor: "A01".to_string(),
            plan,
        }
    };
    let cases = vec![
        unknown_task("notes on an unknown task", with(notes("T-missing", &[])), 3),
        unknown_task(
            "artifact on an unknown task",
            with(artifact("T-missing", "k")),
            3,
        ),
        unknown_task(
            "feedback on an unknown task",
            with(feedback("T-missing", Json::Null)),
            3,
        ),
        unknown_task(
            "dependencies of an unknown task",
            with(WriterOp::SetDependsOn {
                task_id: "T-missing".to_string(),
                depends_on: vec![],
            }),
            3,
        ),
        unknown_task(
            "transition of an unknown task",
            with(transition("T-missing", "PLANNED", "CLAIMED")),
            3,
        ),
        unknown_task(
            "an op on a task the list creates later",
            vec![notes("T-missing", &[]), create("T-missing")],
            0,
        ),
        unsupported(
            "a key twice in one set_notes",
            with(notes("T-1", &[("k", Json::Int(1)), ("k", Json::Int(2))])),
            3,
            "twice",
        ),
        unsupported(
            "a key that is a parameter of set_notes",
            with(notes("T-1", &[("task_id", Json::Int(1))])),
            3,
            "task_id",
        ),
        unsupported(
            "NaN in a set_notes value",
            with(notes(
                "T-1",
                &[("k", Json::Arr(vec![Json::Float(f64::NAN)]))],
            )),
            3,
            "non-finite",
        ),
        unsupported(
            "infinity in a feedback entry",
            with(feedback(
                "T-1",
                Json::Obj(vec![("n".to_string(), Json::Float(f64::INFINITY))]),
            )),
            3,
            "non-finite",
        ),
        unsupported(
            "a feedback list that is a string",
            with(feedback("T-3", jstr("x"))),
            3,
            "not a list",
        ),
        unsupported(
            "an outputs column that is an object",
            with(artifact("T-4", "k")),
            3,
            "not a JSON list",
        ),
        unsupported(
            "a created id that exists",
            with(create("T-1")),
            3,
            "exists already",
        ),
        unsupported(
            "a created id twice",
            vec![create("T-new"), create("T-new")],
            1,
            "exists already",
        ),
        unsupported(
            "a created id that is not safe",
            with(create("bad id")),
            3,
            "safe id",
        ),
        unsupported("a created id that is empty", with(create("")), 3, "safe id"),
        unsupported(
            "NaN in a created budget",
            with(WriterOp::CreateTask(NewTask {
                budget: Json::Float(f64::NAN),
                ..NewTask::new("T-new", "C-1", "code")
            })),
            3,
            "non-finite",
        ),
        invalid_plan(
            "a plan from the wrong state",
            with(transition("T-1", "PLANNED", "CLAIMED")),
            3,
            PlanViolation::FromMismatch {
                plan: "PLANNED".to_string(),
                row: "IN_PROGRESS".to_string(),
            },
        ),
        invalid_plan(
            "a second plan from the state the first one left",
            vec![
                transition("T-1", "IN_PROGRESS", "IN_REVIEW"),
                transition("T-1", "IN_PROGRESS", "FAILED"),
            ],
            1,
            PlanViolation::FromMismatch {
                plan: "IN_PROGRESS".to_string(),
                row: "IN_REVIEW".to_string(),
            },
        ),
        invalid_plan(
            "a step that is not legal",
            with(transition("T-1", "IN_PROGRESS", "CREATED")),
            3,
            PlanViolation::IllegalStep {
                from: "IN_PROGRESS".to_string(),
                to: "CREATED".to_string(),
            },
        ),
        invalid_plan(
            "an audit row by another actor",
            with(tampered(|plan| plan.audit[0].actor = "A99".to_string())),
            3,
            PlanViolation::ActorMismatch { index: 0 },
        ),
        invalid_plan(
            "a column outside the allowlist",
            with(tampered(|plan| {
                plan.columns.push(("assigned_to".to_string(), jstr("x")))
            })),
            3,
            PlanViolation::UnknownColumn {
                column: "assigned_to".to_string(),
            },
        ),
    ];

    for case in cases {
        let error = apply(&path, &case.ops).await.expect_err(case.name);
        assert_eq!(error.cause(), &case.cause, "{}: {error}", case.name);
        assert_eq!(error.op_index(), Some(case.index), "{}", case.name);
        assert_eq!(error.code(), case.code, "{}", case.name);
        assert_eq!(error.write_state(), case.state, "{}", case.name);
        assert!(
            error.message().contains(case.says),
            "{}: {error}",
            case.name
        );
        assert_eq!(
            std::fs::read(&path).expect("read store"),
            bytes,
            "{}: the file changed",
            case.name
        );
        assert_eq!(
            content(&path).await,
            before,
            "{}: the content changed",
            case.name
        );
    }

    // A clock that is not a number is refused as well, before any transaction.
    let error = apply_at(&path, &good(), f64::NAN)
        .await
        .expect_err("now is NaN");
    assert_eq!(
        (error.write_state(), error.op_index()),
        (WriteState::NotStarted, None)
    );
    assert_eq!(content(&path).await, before);
}

async fn apply_at(path: &Path, ops: &[WriterOp], now: f64) -> Result<OpsApplied, OpsError> {
    let store = writer(path).await;
    let result = store.apply_ops(ops, now).await;
    store.close().await;
    result
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_database_failure_after_the_first_write_rolls_the_whole_list_back() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    seeded(
        &path,
        &[task_row("T-1", "IN_PROGRESS"), task_row("T-2", "PLANNED")],
    )
    .await;
    // The last write of the list fails inside SQLite; everything before it was already written in the transaction.
    exec(
        &path,
        "CREATE TRIGGER boom BEFORE UPDATE ON tasks WHEN NEW.notes LIKE '%boom%' BEGIN SELECT RAISE(ABORT, 'boom'); END",
    )
    .await;
    let bytes = std::fs::read(&path).expect("read store");
    let before = content(&path).await;
    let ops = [
        artifact("T-1", "report"),
        transition("T-2", "PLANNED", "CLAIMED"),
        create("T-new"),
        notes("T-1", &[("x", jstr("boom"))]),
    ];

    let error = apply(&path, &ops)
        .await
        .expect_err("the trigger aborts the last write");
    assert_eq!(error.cause(), &OpsCause::Failed(ErrorKind::Database));
    assert_eq!(
        (error.write_state(), error.code(), error.op_index()),
        (WriteState::Final, ErrorCode::Internal, Some(3))
    );
    assert!(error.message().contains("boom"), "{error}");
    assert_eq!(
        content(&path).await,
        before,
        "an earlier op of the list survived"
    );
    assert_eq!(std::fs::read(&path).expect("read store"), bytes);

    // The lock went with the transaction, and the same list without the failing op applies.
    let applied = apply(&path, &ops[..3]).await.expect("the lock is released");
    assert_eq!(applied.tasks.len(), 3);
    assert_ne!(content(&path).await, before);
}

// ---- 3. what a list sees of itself and what it reports ---------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn later_ops_see_the_tasks_and_states_the_earlier_ops_made() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    seeded(&path, &[task_row("T-1", "IN_PROGRESS")]).await;
    let ops = [
        create("T-new"),
        notes("T-new", &[("gate", jstr("review"))]),
        feedback("T-new", jstr("first")),
        artifact("T-new", "report"),
        WriterOp::SetDependsOn {
            task_id: "T-new".to_string(),
            depends_on: vec!["T-1".to_string()],
        },
        transition("T-new", "CREATED", "VALIDATED"),
        transition("T-new", "VALIDATED", "PLANNED"),
        transition("T-1", "IN_PROGRESS", "IN_REVIEW"),
        notes("T-new", &[("gate", jstr("quality")), ("late", Json::Null)]),
    ];

    let applied = apply(&path, &ops).await.expect("the list applies");
    let ids: Vec<&str> = applied
        .tasks
        .iter()
        .map(|row| match obj_get(row, "task_id") {
            Some(Json::Str(id)) => id.as_str(),
            other => panic!("task_id {other:?}"),
        })
        .collect();
    assert_eq!(ids, ["T-new", "T-1"], "tasks in order of first touch");
    let row = &applied.tasks[0];
    assert_eq!(obj_get(row, "state"), Some(&jstr("PLANNED")));
    assert_eq!(
        obj_get(row, "depends_on"),
        Some(&Json::Arr(vec![jstr("T-1")]))
    );
    assert_eq!(
        obj_get(row, "notes_json"),
        Some(&Json::Obj(vec![
            ("gate".to_string(), jstr("quality")),
            ("feedback".to_string(), Json::Arr(vec![jstr("first")])),
            ("late".to_string(), Json::Null),
        ])),
        "the merge keeps `gate` in place, feedback was appended after it"
    );
    assert_eq!(
        obj_get(row, "outputs"),
        Some(&Json::Arr(vec![Json::Obj(vec![
            ("kind".to_string(), jstr("report")),
            ("uri".to_string(), jstr("file:///out/report")),
            ("version".to_string(), jstr("1")),
            ("digest".to_string(), jstr("d")),
        ])]))
    );
    assert_eq!(obj_get(row, "updated_at"), Some(&Json::Float(NOW)));
    assert_eq!(obj_get(row, "created_at"), Some(&Json::Float(NOW)));
    let trail: Vec<(&str, Option<&str>, &str)> = applied
        .audit
        .iter()
        .map(|a| (a.task_id.as_str(), a.from.as_deref(), a.to.as_str()))
        .collect();
    assert_eq!(
        trail,
        [
            ("T-new", None, "CREATED"),
            ("T-new", Some("CREATED"), "VALIDATED"),
            ("T-new", Some("VALIDATED"), "PLANNED"),
            ("T-1", Some("IN_PROGRESS"), "IN_REVIEW"),
        ]
    );
    assert!(applied.audit.iter().all(|a| a.ts == NOW));
    assert_eq!(applied.effects, [PlanEffect::LiveRefresh; 3]);

    let now = content(&path).await;
    assert_eq!(table(&now, "transitions").len(), 4);
    assert_eq!(table(&now, "artifacts").len(), 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_empty_list_opens_no_transaction_and_a_busy_store_refuses_a_real_one() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    seeded(&path, &[task_row("T-1", "IN_PROGRESS")]).await;
    let mut config = WriterConfig::new(&path);
    config.busy_timeout = Duration::from_millis(100);
    let store = TaskStoreWriter::new(open_for_write(&config).await.expect("open"));
    let mut raw = connect(&path).await;
    sqlx::query("BEGIN IMMEDIATE")
        .execute(&mut raw)
        .await
        .expect("hold the write lock");

    assert_eq!(
        store.apply_ops(&[], NOW).await.expect("nothing to do"),
        OpsApplied::default()
    );
    let error = store
        .apply_ops(&[notes("T-1", &[("a", Json::Int(1))])], NOW)
        .await
        .expect_err("the write lock is held");
    assert_eq!(error.cause(), &OpsCause::Failed(ErrorKind::Busy));
    assert_eq!(
        (error.write_state(), error.code(), error.op_index()),
        (WriteState::NotStarted, ErrorCode::Dep, None)
    );

    sqlx::query("ROLLBACK")
        .execute(&mut raw)
        .await
        .expect("release");
    store.close().await;
    // A writer with the default 30 s timeout: the 100 ms one above may not even get its pooled connection back on a loaded host.
    apply(&path, &[notes("T-1", &[("a", Json::Int(1))])])
        .await
        .expect("the lock is free");
}

// ---- 4. the wire form -------------------------------------------------------------------------------------------------------

fn json(text: &str) -> Json {
    parse_json(text).expect("test json")
}

#[test]
fn the_wire_form_reads_every_op_with_pythons_defaults() {
    let ops = WriterOp::list_from_json(&json(
        r#"[
          {"op": "set_notes", "task_id": "T-1", "set": {"a": 1, "b": [null]}},
          {"op": "add_artifact", "task_id": "T-1", "kind": "k", "uri": "u", "version": "1", "digest": "", "producer": "p"},
          {"op": "append_feedback", "task_id": "T-1", "entry": {"gate": "review"}},
          {"op": "create_task", "task": {"task_id": "T-n", "correlation_id": "C", "capability": "code"}},
          {"op": "create_task", "task": {"task_id": "T-m", "correlation_id": "C", "capability": "code", "title": "x", "parent_id": "T-1",
            "dag_depth": 2, "inputs": [1], "acceptance": ["a"], "budget": {"b": 1}, "risk_class": "high", "priority": "P0",
            "depends_on": ["T-1"], "agent_id": "A05", "max_attempts": 5, "notes": {"n": 1}}},
          {"op": "set_depends_on", "task_id": "T-1", "depends_on": ["T-a"]},
          {"op": "transition", "task_id": "T-1", "actor": "A01", "plan": {"from": "PLANNED", "to": "CLAIMED",
            "columns": {"state": "CLAIMED", "attempt": 1}, "audit": [{"from": "PLANNED", "to": "CLAIMED", "actor": "A01", "reason": "r"}],
            "effects": [{"kind": "live_refresh"}]}}
        ]"#,
    ))
    .expect("the list reads");
    let mut full = NewTask::new("T-m", "C", "code");
    full.title = "x".to_string();
    full.parent_id = Some("T-1".to_string());
    full.dag_depth = 2;
    full.inputs = Json::Arr(vec![Json::Int(1)]);
    full.acceptance = Json::Arr(vec![jstr("a")]);
    full.budget = Json::Obj(vec![("b".to_string(), Json::Int(1))]);
    full.risk_class = "high".to_string();
    full.priority = "P0".to_string();
    full.depends_on = vec!["T-1".to_string()];
    full.agent_id = Some("A05".to_string());
    full.max_attempts = 5;
    full.notes = vec![("n".to_string(), Json::Int(1))];
    let mut claim = plan("PLANNED", "CLAIMED", "A01");
    claim.columns.push(("attempt".to_string(), Json::Int(1)));
    let want = vec![
        notes(
            "T-1",
            &[("a", Json::Int(1)), ("b", Json::Arr(vec![Json::Null]))],
        ),
        WriterOp::AddArtifact {
            task_id: "T-1".to_string(),
            kind: "k".to_string(),
            uri: "u".to_string(),
            version: "1".to_string(),
            digest: String::new(),
            producer: "p".to_string(),
        },
        feedback("T-1", Json::Obj(vec![("gate".to_string(), jstr("review"))])),
        WriterOp::CreateTask(NewTask::new("T-n", "C", "code")),
        WriterOp::CreateTask(full),
        WriterOp::SetDependsOn {
            task_id: "T-1".to_string(),
            depends_on: vec!["T-a".to_string()],
        },
        WriterOp::Transition {
            task_id: "T-1".to_string(),
            actor: "A01".to_string(),
            plan: claim,
        },
    ];
    assert_eq!(ops, want);
}

#[test]
fn the_wire_form_refuses_what_it_cannot_read_and_never_quotes_a_value() {
    let secret = "SECRET-VALUE";
    let create_with = |member: &str| {
        format!(
            r#"{{"op": "create_task", "task": {{"task_id": "T", "correlation_id": "C", "capability": "k", {member}}}}}"#
        )
    };
    let table: Vec<(String, &str)> = vec![
        (r#"[1]"#.to_string(), "op is not an object"),
        (r#"[{"task_id": "T"}]"#.to_string(), "no text member op"),
        (r#"[{"op": "SECRET-VALUE"}]"#.to_string(), "unknown operation"),
        (r#"[{"op": "set_notes", "task_id": "T"}]"#.to_string(), "has no set"),
        (r#"[{"op": "set_notes", "task_id": "T", "set": {}, "extra": 1}]"#.to_string(), "unknown key extra"),
        (r#"[{"op": "set_notes", "task_id": ["SECRET-VALUE"], "set": {}}]"#.to_string(), "task_id is not text"),
        (r#"[{"op": "set_notes", "task_id": "T", "set": "SECRET-VALUE"}]"#.to_string(), "set is not an object"),
        (r#"[{"op": "add_artifact", "task_id": "T", "kind": "k", "uri": "u", "version": "1", "producer": "p"}]"#.to_string(), "has no digest"),
        (r#"[{"op": "add_artifact", "task_id": "T", "kind": 5, "uri": "u", "version": "1", "digest": "", "producer": "p"}]"#.to_string(), "kind is not text"),
        (r#"[{"op": "append_feedback", "task_id": "T"}]"#.to_string(), "has no entry"),
        (r#"[{"op": "set_depends_on", "task_id": "T", "depends_on": ["a", 5]}]"#.to_string(), "depends_on is not a list of text"),
        (r#"[{"op": "set_depends_on", "task_id": "T", "depends_on": "SECRET-VALUE"}]"#.to_string(), "depends_on is not a list"),
        (format!("[{}]", create_with(r#""dag_depth": "SECRET-VALUE""#)), "dag_depth is not an integer"),
        (format!("[{}]", create_with(r#""title": null"#)), "title is not text"),
        (format!("[{}]", create_with(r#""notes": "SECRET-VALUE""#)), "notes is not an object"),
        (format!("[{}]", create_with(r#""bogus": 1"#)), "unknown key bogus"),
        (
            r#"[{"op": "transition", "task_id": "T", "actor": "A", "plan": {"from": "PLANNED", "to": "CLAIMED", "columns": {}, "audit": [], "effects": [{"kind": "SECRET-VALUE"}]}}]"#
                .to_string(),
            "kind is not a known effect",
        ),
        (
            r#"[{"op": "transition", "task_id": "T", "actor": "A", "plan": {"from": "PLANNED", "to": "CLAIMED", "columns": {}, "audit": [{"from": "a", "to": "b", "reason": "r"}], "effects": []}}]"#
                .to_string(),
            "has no actor",
        ),
        (
            r#"[{"op": "transition", "task_id": "T", "actor": "A", "plan": {"from": "PLANNED", "to": "CLAIMED", "columns": [], "audit": [], "effects": []}}]"#
                .to_string(),
            "columns is not an object",
        ),
    ];
    for (wire, says) in table {
        let error = WriterOp::list_from_json(&json(&wire)).expect_err(&wire);
        assert_eq!(
            error.cause(),
            &OpsCause::Failed(ErrorKind::Unsupported),
            "{wire}"
        );
        assert_eq!(
            (error.write_state(), error.code(), error.op_index()),
            (WriteState::NotStarted, ErrorCode::Dep, Some(0)),
            "{wire}"
        );
        assert!(error.message().contains(says), "{wire}: {error}");
        assert!(
            !error.message().contains(secret),
            "the message quotes a value: {error}"
        );
    }

    let error = WriterOp::list_from_json(&json(
        r#"[{"op": "set_notes", "task_id": "T", "set": {}}, {"op": "set_notes", "task_id": "T", "set": {}}, {"op": "nope"}]"#,
    ))
    .expect_err("the third op is unknown");
    assert_eq!(error.op_index(), Some(2), "the position of the bad op");
    let error = WriterOp::list_from_json(&json(r#"{"op": "set_notes"}"#)).expect_err("not a list");
    assert_eq!(
        (error.write_state(), error.op_index()),
        (WriteState::NotStarted, None)
    );
}

// ---- 5. fault points: a list held between two ops --------------------------------------------------------------------------

#[cfg(feature = "fault-injection")]
mod fault_points {
    use std::sync::Arc;

    use super::*;
    use gsd_taskwriter::fault::PausePoint;

    fn three_ops() -> Vec<WriterOp> {
        vec![
            notes("T-1", &[("a", Json::Int(1))]),
            artifact("T-1", "report"),
            transition("T-2", "PLANNED", "CLAIMED"),
        ]
    }

    async fn two_tasks(path: &Path) {
        seeded(
            path,
            &[task_row("T-1", "IN_PROGRESS"), task_row("T-2", "PLANNED")],
        )
        .await;
    }

    fn spawn_apply(
        store: &Arc<TaskStoreWriter>,
        ops: Vec<WriterOp>,
    ) -> tokio::task::JoinHandle<Result<OpsApplied, OpsError>> {
        let store = Arc::clone(store);
        tokio::spawn(async move { store.apply_ops(&ops, NOW).await })
    }

    #[tokio::test]
    async fn a_list_held_between_two_ops_is_invisible_holds_the_lock_and_dies_with_none_applied() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("tasks.db");
        two_tasks(&path).await;
        let bytes = std::fs::read(&path).expect("read store");
        let before = content(&path).await;
        let holder = Arc::new(writer(&path).await);
        let mut config = WriterConfig::new(&path);
        config.busy_timeout = Duration::from_millis(300);
        let contender = TaskStoreWriter::new(open_for_write(&config).await.expect("open"));

        let mut gate = holder.store().faults().arm(PausePoint::BetweenOps);
        let task = spawn_apply(&holder, three_ops());
        gate.wait_reached().await;

        // The first op is written inside the held transaction; nobody else sees it, and nobody else can write.
        assert_eq!(content(&path).await, before, "an uncommitted op leaked");
        let refused = contender
            .apply_ops(&[notes("T-2", &[("b", Json::Int(2))])], NOW)
            .await
            .expect_err("the list holds the write lock");
        assert_eq!(refused.cause(), &OpsCause::Failed(ErrorKind::Busy));
        assert_eq!(refused.write_state(), WriteState::NotStarted);

        // The process dies between two ops: none of them may survive.
        task.abort();
        assert!(task.await.expect_err("aborted").is_cancelled());
        let survivor = contender
            .store()
            .begin()
            .await
            .expect("the abandoned transaction released the lock");
        survivor.rollback().await.expect("rollback");
        assert_eq!(
            content(&path).await,
            before,
            "an op of the dead list survived"
        );
        assert_eq!(std::fs::read(&path).expect("read store"), bytes);
        contender.close().await;
    }

    #[tokio::test]
    async fn releasing_the_pause_commits_every_op_together() {
        let dir = tempfile::tempdir().expect("tempdir");
        let held = dir.path().join("held.db");
        let direct = dir.path().join("direct.db");
        two_tasks(&held).await;
        two_tasks(&direct).await;
        let before = content(&held).await;
        let holder = Arc::new(writer(&held).await);

        let mut gate = holder.store().faults().arm(PausePoint::BetweenOps);
        let task = spawn_apply(&holder, three_ops());
        gate.wait_reached().await;
        assert_eq!(content(&held).await, before);
        gate.release();
        task.await
            .expect("join")
            .expect("the list applies once released");

        apply(&direct, &three_ops())
            .await
            .expect("the same list without a pause");
        assert_eq!(content(&held).await, content(&direct).await);
        assert_ne!(content(&held).await, before);
    }

    #[tokio::test]
    async fn a_one_op_list_has_no_point_between_ops() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("tasks.db");
        two_tasks(&path).await;
        let holder = Arc::new(writer(&path).await);
        let _armed = holder.store().faults().arm(PausePoint::BetweenOps);
        let done = tokio::time::timeout(
            Duration::from_secs(10),
            holder.apply_ops(&three_ops()[..1], NOW),
        )
        .await;
        assert!(done.expect("a single op never waits").is_ok());
    }
}

// ---- 6. append-only tables and journal modes ---------------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn no_op_updates_or_deletes_an_append_only_table_and_a_wal_store_behaves_alike() {
    let list = [
        create("T-new"),
        notes("T-new", &[("gate", jstr("review"))]),
        artifact("T-new", "report"),
        feedback("T-1", jstr("f")),
        WriterOp::SetDependsOn {
            task_id: "T-new".to_string(),
            depends_on: vec!["T-1".to_string()],
        },
        transition("T-1", "IN_PROGRESS", "IN_REVIEW"),
        transition("T-new", "CREATED", "VALIDATED"),
    ];
    let mut results = Vec::new();
    for wal in [false, true] {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("tasks.db");
        seeded(&path, &[task_row("T-1", "IN_PROGRESS")]).await;
        if wal {
            exec(&path, "PRAGMA journal_mode=WAL").await;
        }
        // Rows of these tables are only ever appended: a statement that rewrites or removes one aborts the transaction.
        for table in ["transitions", "verdicts", "artifacts"] {
            for verb in ["UPDATE", "DELETE"] {
                exec(
                    &path,
                    &format!("CREATE TRIGGER append_only_{verb}_{table} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only {table}'); END"),
                )
                .await;
            }
        }
        apply(&path, &list)
            .await
            .unwrap_or_else(|error| panic!("wal={wal}: {error}"));
        results.push(content(&path).await);
    }
    assert_eq!(
        results[0], results[1],
        "a WAL store and a rollback-journal store end up the same"
    );
    assert_eq!(table(&results[0], "transitions").len(), 3);
    assert_eq!(table(&results[0], "artifacts").len(), 1);
}
