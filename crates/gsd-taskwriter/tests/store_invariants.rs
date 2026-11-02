//! Store-layer invariants of the Task Store writer (U3.1a).
//!
//! Test stores are built by the REAL Python `TaskStore` (a short script run with `python3 -c`, no network), so the schema,
//! the envelope-column migration and the WAL opt-in (`SWARM_TASKSTORE_WAL=1`) are Python's own, and `TaskStore.get` is the
//! oracle for the Python-shaped task dict. The repository root is `SWARM_REPO_ROOT` or two levels above this crate. The
//! Python build is done once per journal mode and the resulting database file is copied for each test.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::Duration;

use gsd_codes::ErrorCode;
use gsd_route::{parse_json, Json};
use gsd_taskwriter::{
    open_for_write, ErrorKind, NewArtifact, NewVerdict, PlanColumn, StateChange, WriteState,
    WriteStore, WriterConfig, WriterError,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Connection, Row, SqliteConnection};

/// Builds the store through Python's `TaskStore` and prints `TaskStore.get` of every seeded task as JSON.
const SEED_SCRIPT: &str = r#"
import json, sys
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
s.create(task_id="T-1", correlation_id="C-1", capability="code.backend", title="plain")
s.create(task_id="T-2", correlation_id="C-1", capability="code.frontend", title="caf\u00e9 \u2603", risk_class="medium",
         priority="P1", depends_on=["T-1"], inputs=[{"k": 1, "f": 2.5}],
         notes={"gates": ["review"], "nested": {"a": [1, 2.0, None, True]}, "text": "d\u00e9j\u00e0"})
for task_id, notes in (("T-3", None), ("T-4", "[1, 2]"), ("T-5", "")):
    s.create(task_id=task_id, correlation_id="C-2", capability="x")
    s.conn.execute("UPDATE tasks SET notes=? WHERE task_id=?", (notes, task_id))
s.conn.commit()
print(json.dumps({t: s.get(t) for t in ("T-1", "T-2", "T-3", "T-4", "T-5")}))
s.conn.close()
"#;

const NOW: f64 = 1_700_000_000.25;

struct Template {
    db: Vec<u8>,
    tasks: Json,
}

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

fn build_template(wal: bool) -> Template {
    let root = repo_root();
    assert!(
        root.join("swarm/taskstore.py").is_file(),
        "no swarm/taskstore.py under {}: set SWARM_REPO_ROOT to the repository root",
        root.display()
    );
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("tasks.db");
    let mut command = Command::new("python3");
    command
        .arg("-c")
        .arg(SEED_SCRIPT)
        .arg(&db)
        .current_dir(&root)
        .env("PYTHONDONTWRITEBYTECODE", "1");
    if wal {
        command.env("SWARM_TASKSTORE_WAL", "1");
    } else {
        command.env_remove("SWARM_TASKSTORE_WAL");
    }
    let output = command.output().expect("run python3");
    assert!(
        output.status.success(),
        "python seed failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        !db.with_file_name("tasks.db-wal").exists(),
        "python left a WAL file behind; the copied database file would be incomplete"
    );
    let tasks =
        parse_json(&String::from_utf8(output.stdout).expect("utf8")).expect("python printed JSON");
    Template {
        db: std::fs::read(&db).expect("read seeded db"),
        tasks,
    }
}

fn template(wal: bool) -> &'static Template {
    static DELETE: LazyLock<Template> = LazyLock::new(|| build_template(false));
    static WAL: LazyLock<Template> = LazyLock::new(|| build_template(true));
    if wal {
        &WAL
    } else {
        &DELETE
    }
}

/// A private copy of the Python-built store.
struct Fixture {
    _dir: tempfile::TempDir,
    path: PathBuf,
}

impl Fixture {
    fn new(wal: bool) -> Self {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("tasks.db");
        std::fs::write(&path, &template(wal).db).expect("copy db");
        Self { _dir: dir, path }
    }

    async fn open(&self) -> WriteStore {
        open_for_write(&WriterConfig::new(&self.path))
            .await
            .expect("open_for_write")
    }

    async fn open_with_timeout(&self, busy_timeout: Duration) -> WriteStore {
        let mut config = WriterConfig::new(&self.path);
        config.busy_timeout = busy_timeout;
        open_for_write(&config).await.expect("open_for_write")
    }
}

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("raw connection")
}

/// Runs one raw statement on a separate connection (test setup only).
async fn exec(path: &Path, sql: &str) {
    let mut conn = connect(path).await;
    sqlx::query(sql).execute(&mut conn).await.expect(sql);
    conn.close().await.expect("close");
}

/// One text line per result row of a `SELECT` that builds its own text with `quote()`.
async fn lines(path: &Path, sql: &str) -> Vec<String> {
    let mut conn = connect(path).await;
    let rows = sqlx::query(sql).fetch_all(&mut conn).await.expect(sql);
    conn.close().await.expect("close");
    rows.iter().map(|row| row.get::<String, _>(0)).collect()
}

/// Schema text of `sqlite_master` plus `schema_version` and `journal_mode`: what DDL or a journal pragma would change.
async fn schema_state(path: &Path) -> Vec<String> {
    let mut state = lines(
        path,
        "SELECT type || '|' || name || '|' || tbl_name || '|' || rootpage || '|' || COALESCE(sql, '') \
         FROM sqlite_master ORDER BY type, name",
    )
    .await;
    state.extend(
        lines(
            path,
            "SELECT 'schema_version=' || schema_version FROM pragma_schema_version",
        )
        .await,
    );
    state.extend(
        lines(
            path,
            "SELECT 'journal_mode=' || journal_mode FROM pragma_journal_mode",
        )
        .await,
    );
    state
}

/// A SQL dump: the schema and every row of every table (sqlite_sequence included), in rowid order.
async fn dump(path: &Path) -> Vec<String> {
    let mut out = schema_state(path).await;
    for table in lines(
        path,
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
    )
    .await
    {
        let columns = lines(
            path,
            &format!("SELECT name FROM pragma_table_info('{table}') ORDER BY cid"),
        )
        .await;
        let expression = columns
            .iter()
            .map(|column| format!("quote(\"{column}\")"))
            .collect::<Vec<_>>()
            .join(" || '|' || ");
        for row in lines(
            path,
            &format!("SELECT {expression} FROM \"{table}\" ORDER BY rowid"),
        )
        .await
        {
            out.push(format!("{table}: {row}"));
        }
    }
    out
}

fn change<'a>(task_id: &'a str, from: &'a str, to: &'a str) -> StateChange<'a> {
    StateChange {
        task_id,
        from,
        to,
        columns: &[],
        actor: "A01",
        reason: "because",
        updated_at: NOW,
        ts: NOW + 1.0,
    }
}

fn verdict() -> NewVerdict<'static> {
    NewVerdict {
        task_id: "T-1",
        gate: "review",
        verdict: "ok",
        agent_id: "A09",
        findings: "[\"f1\"]",
        expires_at: NOW + 60.0,
        ts: NOW + 2.0,
        envelope_json: "{\"payload\": {}}",
        sig: "deadbeef",
    }
}

fn artifact() -> NewArtifact<'static> {
    NewArtifact {
        task_id: "T-1",
        kind: "doc",
        uri: "file:///a.md",
        version: "1",
        digest: "abc",
        producer: "A15",
        ts: NOW + 3.0,
    }
}

/// Applies every write path of the store layer in one transaction: a state change with all three extra columns, the
/// two-step escalation shape, a verdict and an artifact.
async fn write_everything(store: &WriteStore) -> Result<gsd_taskwriter::Tx, WriterError> {
    let mut tx = store.begin().await?;
    let columns = [
        PlanColumn::Attempt(1),
        PlanColumn::ReworkLoops(2),
        PlanColumn::Notes("{\"k\": 1}"),
    ];
    tx.apply_state(&StateChange {
        columns: &columns,
        ..change("T-1", "CREATED", "PLANNED")
    })
    .await?;
    tx.apply_state(&change("T-1", "PLANNED", "CHANGES_REQUESTED"))
        .await?;
    tx.apply_state(&change("T-1", "CHANGES_REQUESTED", "ESCALATED"))
        .await?;
    tx.insert_verdict(&verdict()).await?;
    tx.insert_artifact(&artifact()).await?;
    Ok(tx)
}

fn assert_error(error: &WriterError, kind: ErrorKind, write_state: WriteState, code: ErrorCode) {
    assert_eq!(
        (error.kind(), error.write_state(), error.code()),
        (kind, write_state, code),
        "{error}"
    );
}

#[tokio::test]
async fn task_row_is_the_python_dict_for_delete_and_wal_stores() {
    for wal in [false, true] {
        let fixture = Fixture::new(wal);
        let store = fixture.open().await;
        let mut tx = store.begin().await.expect("begin");
        let Json::Obj(expected) = &template(wal).tasks else {
            panic!("python printed no object");
        };
        assert_eq!(expected.len(), 5);
        for (task_id, python) in expected {
            let Json::Obj(python_fields) = python else {
                panic!("task {task_id} is not an object");
            };
            let fields = tx.task(task_id).await.expect("read").expect("row exists");
            assert_eq!(&fields, python_fields, "wal={wal} task {task_id}");
        }
        assert_eq!(tx.task("T-99").await.expect("read"), None, "wal={wal}");
    }
}

#[tokio::test]
async fn values_rust_cannot_prove_python_reads_alike_are_refused_before_any_write() {
    let cases = [
        ("notes", "'{\"a\": NaN}'", "notes"),
        (
            "notes",
            "'{\"a\": 123456789012345678901234567890}'",
            "notes",
        ),
        ("notes", "'{'", "notes"),
        ("inputs", "'NaN'", "inputs"),
        ("notes", "X'7b7d'", "notes"),
        ("inputs", "X'5b5d'", "inputs"),
    ];
    for (column, value, named) in cases {
        let fixture = Fixture::new(false);
        exec(
            &fixture.path,
            &format!("UPDATE tasks SET {column}={value} WHERE task_id='T-1'"),
        )
        .await;
        let store = fixture.open().await;
        let mut tx = store.begin().await.expect("begin");
        let error = tx
            .task("T-1")
            .await
            .expect_err(&format!("{column}={value}"));
        assert_error(
            &error,
            ErrorKind::Unsupported,
            WriteState::NotStarted,
            ErrorCode::Dep,
        );
        assert!(error.message().contains(named), "{column}={value}: {error}");
        assert_eq!(error.write_state().as_str(), "not_started");
    }
}

#[tokio::test]
async fn missing_table_or_column_is_not_started_and_the_file_is_untouched() {
    let cases = [
        "DROP TABLE tasks",
        "DROP TABLE transitions",
        "DROP TABLE verdicts",
        "DROP TABLE artifacts",
        "ALTER TABLE verdicts DROP COLUMN envelope_json",
        "ALTER TABLE verdicts DROP COLUMN sig",
        "ALTER TABLE tasks DROP COLUMN rework_loops",
    ];
    for ddl in cases {
        let fixture = Fixture::new(false);
        exec(&fixture.path, ddl).await;
        let before = std::fs::read(&fixture.path).expect("read");
        let error = open_for_write(&WriterConfig::new(&fixture.path))
            .await
            .expect_err(ddl);
        assert_error(
            &error,
            ErrorKind::SchemaNotReady,
            WriteState::NotStarted,
            ErrorCode::Dep,
        );
        let named = ddl.rsplit(' ').next().expect("last word");
        assert!(error.message().contains(named), "{ddl}: {error}");
        assert_eq!(std::fs::read(&fixture.path).expect("read"), before, "{ddl}");
    }
}

#[tokio::test]
async fn absent_store_file_is_not_started_and_is_not_created() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    let error = open_for_write(&WriterConfig::new(&path))
        .await
        .expect_err("absent file");
    assert_error(
        &error,
        ErrorKind::StoreUnavailable,
        WriteState::NotStarted,
        ErrorCode::Dep,
    );
    assert!(!path.exists(), "open_for_write created the file");
}

#[tokio::test]
async fn writing_issues_no_ddl_and_keeps_the_journal_mode() {
    for (wal, mode) in [(false, "journal_mode=delete"), (true, "journal_mode=wal")] {
        let fixture = Fixture::new(wal);
        let before = schema_state(&fixture.path).await;
        assert!(
            before.iter().any(|line| line == mode),
            "wal={wal}: {before:?}"
        );
        let transitions_before =
            lines(&fixture.path, "SELECT quote(count(*)) FROM transitions").await;

        let store = fixture.open().await;
        write_everything(&store)
            .await
            .expect("write")
            .commit()
            .await
            .expect("commit");
        store.close().await;

        assert_eq!(schema_state(&fixture.path).await, before, "wal={wal}");
        assert_eq!(transitions_before, ["5"]);
        assert_eq!(
            lines(&fixture.path, "SELECT quote(count(*)) FROM transitions").await,
            ["8"],
            "wal={wal}"
        );
        assert_eq!(
            lines(&fixture.path, "SELECT quote(count(*)) FROM verdicts").await,
            ["1"],
            "wal={wal}"
        );
        assert_eq!(
            lines(&fixture.path, "SELECT quote(count(*)) FROM artifacts").await,
            ["1"],
            "wal={wal}"
        );
    }
}

#[tokio::test]
async fn append_only_tables_are_never_updated_or_deleted() {
    for wal in [false, true] {
        let fixture = Fixture::new(wal);
        exec(
            &fixture.path,
            "INSERT INTO verdicts (task_id, gate, verdict) VALUES ('T-2', 'review', 'ok')",
        )
        .await;
        exec(
            &fixture.path,
            "INSERT INTO artifacts (task_id, kind) VALUES ('T-2', 'doc')",
        )
        .await;
        for table in ["transitions", "verdicts", "artifacts"] {
            for event in ["UPDATE", "DELETE"] {
                exec(
                    &fixture.path,
                    &format!(
                        "CREATE TRIGGER no_{event}_{table} BEFORE {event} ON {table} \
                         BEGIN SELECT RAISE(ABORT, 'append-only: {event} {table}'); END"
                    ),
                )
                .await;
            }
        }
        // The triggers are armed: a raw UPDATE and a raw DELETE are refused.
        let mut conn = connect(&fixture.path).await;
        for sql in [
            "UPDATE transitions SET reason='x'",
            "DELETE FROM verdicts",
            "DELETE FROM artifacts",
        ] {
            assert!(
                sqlx::query(sql).execute(&mut conn).await.is_err(),
                "{sql} was not refused"
            );
        }
        conn.close().await.expect("close");

        let store = fixture.open().await;
        write_everything(&store)
            .await
            .expect("no write path may touch a trigger")
            .commit()
            .await
            .expect("commit");
        let rows = lines(
            &fixture.path,
            "SELECT quote(count(*)) FROM transitions WHERE reason='because'",
        )
        .await;
        assert_eq!(rows, ["3"], "wal={wal}");
    }
}

#[tokio::test]
async fn audit_row_is_inserted_after_the_state_update() {
    // The test's trigger rejects an audit row for a task whose row is still in the audit row's from_state.
    let fixture = Fixture::new(false);
    exec(
        &fixture.path,
        "CREATE TRIGGER audit_after_state AFTER INSERT ON transitions \
         WHEN NEW.reason = 'because' AND (SELECT state FROM tasks WHERE task_id = NEW.task_id) = NEW.from_state \
         BEGIN SELECT RAISE(ABORT, 'audit row written before the state update'); END",
    )
    .await;
    let store = fixture.open().await;
    let mut tx = store.begin().await.expect("begin");
    tx.apply_state(&change("T-1", "CREATED", "PLANNED"))
        .await
        .expect("state update first, then audit");
    tx.commit().await.expect("commit");
    assert_eq!(
        lines(
            &fixture.path,
            "SELECT quote(state) FROM tasks WHERE task_id='T-1'"
        )
        .await,
        ["'PLANNED'"]
    );
}

#[tokio::test]
async fn state_change_writes_the_row_and_its_audit_and_appends_verdict_and_artifact() {
    let fixture = Fixture::new(false);
    let untouched = lines(&fixture.path, "SELECT quote(task_id)||'|'||quote(state)||'|'||quote(notes) FROM tasks WHERE task_id <> 'T-1' ORDER BY task_id").await;
    let store = fixture.open().await;
    let mut tx = write_everything(&store).await.expect("write");
    let in_tx = tx.task("T-1").await.expect("read").expect("row");
    assert_eq!(
        gsd_route::obj_get(&in_tx, "state"),
        Some(&Json::Str("ESCALATED".to_string()))
    );
    assert_eq!(gsd_route::obj_get(&in_tx, "attempt"), Some(&Json::Int(1)));
    assert_eq!(
        gsd_route::obj_get(&in_tx, "rework_loops"),
        Some(&Json::Int(2))
    );
    assert_eq!(
        gsd_route::obj_get(&in_tx, "notes_json"),
        Some(&Json::Obj(vec![("k".to_string(), Json::Int(1))]))
    );
    assert_eq!(
        gsd_route::obj_get(&in_tx, "updated_at"),
        Some(&Json::Float(NOW))
    );
    tx.commit().await.expect("commit");

    assert_eq!(
        lines(&fixture.path, "SELECT quote(state)||'|'||quote(attempt)||'|'||quote(rework_loops)||'|'||quote(notes)||'|'||quote(updated_at) FROM tasks WHERE task_id='T-1'").await,
        ["'ESCALATED'|1|2|'{\"k\": 1}'|1700000000.25"]
    );
    assert_eq!(
        lines(&fixture.path, "SELECT quote(task_id)||'|'||quote(from_state)||'|'||quote(to_state)||'|'||quote(actor)||'|'||quote(reason)||'|'||quote(ts) FROM transitions WHERE reason='because' ORDER BY id").await,
        [
            "'T-1'|'CREATED'|'PLANNED'|'A01'|'because'|1700000001.25",
            "'T-1'|'PLANNED'|'CHANGES_REQUESTED'|'A01'|'because'|1700000001.25",
            "'T-1'|'CHANGES_REQUESTED'|'ESCALATED'|'A01'|'because'|1700000001.25",
        ]
    );
    assert_eq!(
        lines(&fixture.path, "SELECT quote(task_id)||'|'||quote(gate)||'|'||quote(verdict)||'|'||quote(agent_id)||'|'||quote(findings)||'|'||quote(expires_at)||'|'||quote(ts)||'|'||quote(envelope_json)||'|'||quote(sig) FROM verdicts").await,
        ["'T-1'|'review'|'ok'|'A09'|'[\"f1\"]'|1700000060.25|1700000002.25|'{\"payload\": {}}'|'deadbeef'"]
    );
    assert_eq!(
        lines(&fixture.path, "SELECT quote(task_id)||'|'||quote(kind)||'|'||quote(uri)||'|'||quote(version)||'|'||quote(digest)||'|'||quote(producer)||'|'||quote(ts) FROM artifacts").await,
        ["'T-1'|'doc'|'file:///a.md'|'1'|'abc'|'A15'|1700000003.25"]
    );
    assert_eq!(
        lines(&fixture.path, "SELECT quote(task_id)||'|'||quote(state)||'|'||quote(notes) FROM tasks WHERE task_id <> 'T-1' ORDER BY task_id").await,
        untouched
    );
}

#[tokio::test]
async fn conditional_update_race_is_e_contract_and_writes_nothing() {
    let cases = [
        (
            "T-1",
            "PLANNED",
            "concurrent transition on T-1: expected PLANNED",
        ),
        (
            "T-99",
            "CREATED",
            "concurrent transition on T-99: expected CREATED",
        ),
    ];
    for (task_id, from, message) in cases {
        let fixture = Fixture::new(false);
        let before = dump(&fixture.path).await;
        let store = fixture.open().await;
        let mut tx = store.begin().await.expect("begin");
        let error = tx
            .apply_state(&change(task_id, from, "CLAIMED"))
            .await
            .expect_err("no row in state `from`");
        assert_error(
            &error,
            ErrorKind::ConcurrentTransition,
            WriteState::Final,
            ErrorCode::Contract,
        );
        assert_eq!(error.message(), message);
        assert_eq!(
            error.to_seam_error().message(),
            format!("E-CONTRACT: {message}")
        );
        tx.rollback().await.expect("rollback");
        assert_eq!(dump(&fixture.path).await, before, "{task_id}");
    }
}

#[tokio::test]
async fn a_duplicate_plan_column_is_refused_before_the_update_runs() {
    let fixture = Fixture::new(false);
    let before = dump(&fixture.path).await;
    let store = fixture.open().await;
    let mut tx = store.begin().await.expect("begin");
    let columns = [PlanColumn::Attempt(1), PlanColumn::Attempt(2)];
    let error = tx
        .apply_state(&StateChange {
            columns: &columns,
            ..change("T-1", "CREATED", "PLANNED")
        })
        .await
        .expect_err("duplicate column");
    assert_error(
        &error,
        ErrorKind::Unsupported,
        WriteState::NotStarted,
        ErrorCode::Dep,
    );
    tx.rollback().await.expect("rollback");
    assert_eq!(dump(&fixture.path).await, before);
}

#[tokio::test]
async fn rollback_and_drop_leave_the_store_byte_identical_and_release_the_lock() {
    for wal in [false, true] {
        let fixture = Fixture::new(wal);
        let before = dump(&fixture.path).await;
        let file_before = std::fs::read(&fixture.path).expect("read");
        let store = fixture.open().await;

        write_everything(&store)
            .await
            .expect("write")
            .rollback()
            .await
            .expect("rollback");
        assert_eq!(dump(&fixture.path).await, before, "rollback, wal={wal}");

        // Dropped without commit or rollback: the same connection (pool of one) must be reusable at once.
        drop(write_everything(&store).await.expect("write"));
        let mut again = store
            .begin()
            .await
            .expect("begin after a dropped transaction");
        assert_eq!(dump(&fixture.path).await, before, "drop, wal={wal}");
        again
            .apply_state(&change("T-1", "CREATED", "PLANNED"))
            .await
            .expect("writer still works");
        again.rollback().await.expect("rollback");
        store.close().await;

        if !wal {
            assert_eq!(std::fs::read(&fixture.path).expect("read"), file_before);
        }
        assert_eq!(dump(&fixture.path).await, before, "wal={wal}");
    }
}

#[tokio::test]
async fn a_contender_gets_a_not_started_busy_error_while_the_lock_is_held() {
    for wal in [false, true] {
        let fixture = Fixture::new(wal);
        // Open every store before the lock is taken: only `begin` may run into the busy timeout.
        let holder = fixture.open().await;
        let contender = fixture.open_with_timeout(Duration::from_secs(1)).await;
        let same_store = fixture.open_with_timeout(Duration::from_secs(1)).await;

        let held = holder.begin().await.expect("begin");
        let error = contender.begin().await.expect_err("write lock is held");
        assert_error(
            &error,
            ErrorKind::Busy,
            WriteState::NotStarted,
            ErrorCode::Dep,
        );
        let error = same_store.begin().await.expect_err("write lock is held");
        assert_error(
            &error,
            ErrorKind::Busy,
            WriteState::NotStarted,
            ErrorCode::Dep,
        );

        held.rollback().await.expect("rollback");
        contender
            .begin()
            .await
            .expect("lock released")
            .rollback()
            .await
            .expect("rollback");

        // One pooled connection: a second begin on the same store waits for the first transaction and gives up.
        let pooled = same_store.begin().await.expect("begin");
        let error = same_store
            .begin()
            .await
            .expect_err("only one connection in the pool");
        assert_error(
            &error,
            ErrorKind::Busy,
            WriteState::NotStarted,
            ErrorCode::Dep,
        );
        pooled.rollback().await.expect("rollback");
    }
}

#[tokio::test]
async fn a_second_writer_blocks_until_the_first_commits() {
    for wal in [false, true] {
        let fixture = Fixture::new(wal);
        let first = fixture.open().await;
        let second = fixture.open().await;
        let log: Arc<Mutex<Vec<&'static str>>> = Arc::default();

        let mut held = first.begin().await.expect("begin");
        held.apply_state(&change("T-1", "CREATED", "PLANNED"))
            .await
            .expect("first update");

        let waiter_log = Arc::clone(&log);
        let waiter = tokio::spawn(async move {
            let mut tx = second.begin().await.expect("second begin");
            waiter_log.lock().expect("log").push("second began");
            let seen = tx.task("T-1").await.expect("read").expect("row");
            let state = gsd_route::obj_get(&seen, "state").cloned();
            tx.apply_state(&change("T-1", "PLANNED", "CLAIMED"))
                .await
                .expect("second update");
            tx.commit().await.expect("second commit");
            state
        });
        for _ in 0..200 {
            tokio::task::yield_now().await;
        }
        assert!(
            log.lock().expect("log").is_empty(),
            "the second writer began while the first held the lock"
        );

        log.lock().expect("log").push("first commits");
        held.commit().await.expect("first commit");
        let seen_by_second = waiter.await.expect("second writer");

        assert_eq!(
            *log.lock().expect("log"),
            ["first commits", "second began"],
            "wal={wal}"
        );
        assert_eq!(
            seen_by_second,
            Some(Json::Str("PLANNED".to_string())),
            "wal={wal}"
        );
        assert_eq!(
            lines(&fixture.path, "SELECT quote(from_state)||'>'||quote(to_state) FROM transitions WHERE reason='because' ORDER BY id").await,
            ["'CREATED'>'PLANNED'", "'PLANNED'>'CLAIMED'"],
            "wal={wal}"
        );
    }
}

#[cfg(feature = "fault-injection")]
mod fault_points {
    use super::*;
    use gsd_taskwriter::fault::PausePoint;

    /// Runs one audited state change on its own task, so the test can hold it at a pause point.
    fn spawn_change(store: WriteStore) -> tokio::task::JoinHandle<Result<(), WriterError>> {
        tokio::spawn(async move {
            let mut tx = store.begin().await?;
            tx.apply_state(&change("T-1", "CREATED", "PLANNED")).await?;
            tx.commit().await
        })
    }

    #[tokio::test]
    async fn a_transaction_held_between_state_update_and_audit_insert_is_invisible_and_holds_the_lock(
    ) {
        for wal in [false, true] {
            let fixture = Fixture::new(wal);
            let before = dump(&fixture.path).await;
            let holder = fixture.open().await;
            let contender = fixture.open_with_timeout(Duration::from_secs(1)).await;
            let mut gate = holder.faults().arm(PausePoint::AfterStateUpdate);
            let task = spawn_change(holder);
            gate.wait_reached().await;

            assert_eq!(
                dump(&fixture.path).await,
                before,
                "uncommitted changes leaked, wal={wal}"
            );
            let error = contender.begin().await.expect_err("held mid-transaction");
            assert_error(
                &error,
                ErrorKind::Busy,
                WriteState::NotStarted,
                ErrorCode::Dep,
            );

            // The holder dies between the state UPDATE and the audit INSERT: neither may survive.
            task.abort();
            assert!(task.await.expect_err("aborted").is_cancelled());
            contender.close().await;
            let survivor = fixture.open().await;
            survivor
                .begin()
                .await
                .expect("lock released by the abandoned transaction")
                .rollback()
                .await
                .expect("rollback");
            assert_eq!(
                dump(&fixture.path).await,
                before,
                "state or audit row survived, wal={wal}"
            );
        }
    }

    #[tokio::test]
    async fn releasing_the_pause_commits_state_and_audit_together() {
        let fixture = Fixture::new(false);
        let holder = fixture.open().await;
        let mut gate = holder.faults().arm(PausePoint::AfterStateUpdate);
        let task = spawn_change(holder);
        gate.wait_reached().await;
        gate.release();
        task.await.expect("join").expect("change commits");
        assert_eq!(
            lines(&fixture.path, "SELECT quote(from_state)||'>'||quote(to_state) FROM transitions WHERE reason='because'").await,
            ["'CREATED'>'PLANNED'"]
        );
        assert_eq!(
            lines(
                &fixture.path,
                "SELECT quote(state) FROM tasks WHERE task_id='T-1'"
            )
            .await,
            ["'PLANNED'"]
        );
    }

    #[tokio::test]
    async fn a_pause_point_fires_once_and_the_next_transaction_runs_through() {
        let fixture = Fixture::new(false);
        let store = Arc::new(fixture.open().await);
        let mut gate = store.faults().arm(PausePoint::AfterStateUpdate);
        let worker = Arc::clone(&store);
        let task = tokio::spawn(async move {
            let mut tx = worker.begin().await?;
            tx.apply_state(&change("T-1", "CREATED", "PLANNED")).await?;
            tx.commit().await?;
            let mut tx = worker.begin().await?;
            tx.apply_state(&change("T-1", "PLANNED", "CLAIMED")).await?;
            tx.commit().await
        });
        gate.wait_reached().await;
        gate.release();
        task.await
            .expect("join")
            .expect("both transactions complete");
        assert_eq!(
            lines(
                &fixture.path,
                "SELECT quote(state) FROM tasks WHERE task_id='T-1'"
            )
            .await,
            ["'CLAIMED'"]
        );
    }

    #[tokio::test]
    async fn before_commit_holds_the_lock_with_the_writes_still_invisible() {
        let fixture = Fixture::new(false);
        let before = dump(&fixture.path).await;
        let holder = fixture.open().await;
        let contender = fixture.open_with_timeout(Duration::from_secs(1)).await;
        let mut gate = holder.faults().arm(PausePoint::BeforeCommit);
        let task = spawn_change(holder);
        gate.wait_reached().await;
        assert_eq!(dump(&fixture.path).await, before);
        let error = contender.begin().await.expect_err("held before commit");
        assert_error(
            &error,
            ErrorKind::Busy,
            WriteState::NotStarted,
            ErrorCode::Dep,
        );
        gate.release();
        task.await.expect("join").expect("commits");
        assert_ne!(dump(&fixture.path).await, before);
    }

    #[tokio::test]
    async fn after_begin_holds_the_lock_with_nothing_written() {
        let fixture = Fixture::new(false);
        let holder = fixture.open().await;
        let contender = fixture.open_with_timeout(Duration::from_secs(1)).await;
        let mut gate = holder.faults().arm(PausePoint::AfterBegin);
        let task = spawn_change(holder);
        gate.wait_reached().await;
        let error = contender
            .begin()
            .await
            .expect_err("held right after BEGIN IMMEDIATE");
        assert_error(
            &error,
            ErrorKind::Busy,
            WriteState::NotStarted,
            ErrorCode::Dep,
        );
        gate.release();
        task.await.expect("join").expect("commits");
    }
}
