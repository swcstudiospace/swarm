//! Transition faults of the Task Store writer (phase 14.5, unit U3.2d): the same `--transition` write under a race, a killed
//! process, a held lock and a store that is not the one Python built.
//!
//! Stores are built by the REAL Python `TaskStore` (the schema, the envelope migration and the WAL opt-in `SWARM_TASKSTORE_WAL=1`
//! are Python's own); the repository root is `SWARM_REPO_ROOT` or two levels above this crate. Every scenario runs on a
//! rollback-journal store and on a WAL store; the writer never sets a journal mode itself.
//!
//! What is proved here:
//!
//! - **race**: 8 writers, each with its own connection, start the SAME transition from the SAME state at the same moment, 20
//!   times. Exactly one wins; every loser is a typed refusal of the state machine (`E-CONTRACT`, final); the store holds exactly
//!   one audit row for that from-state (two for an auto-escalation, in order), no half state, and passes `integrity_check`;
//! - **kill -9** (feature `fault-injection`): a child process parks at a named pause point inside the write transaction and the
//!   parent SIGKILLs it. The store is then both-or-neither: the row unchanged AND no audit row, or both present. Never
//!   one without the other. The next opener recovers the store (a rollback journal, a WAL), the same call applies cleanly, and
//!   the result equals the store of an uninterrupted run, `sqlite_sequence` included. A write large enough to spill the page
//!   cache leaves a HOT journal behind (the database file itself modified); the next opener rolls it back. A contender
//!   waiting for the write lock of a killed writer gets it and commits;
//! - **lock held past the busy timeout**: a second connection holds `BEGIN IMMEDIATE`; the writer answers the typed
//!   not-started [`ErrorKind::Busy`] error with the decider never called and the store unchanged, and the same call applies once
//!   the lock is gone;
//! - **foreign and corrupt stores**: a store without the audit table or the envelope columns, a database of another program, a
//!   file that is not a database and a truncated one are refused as not started, byte for byte unchanged. A damaged page that the
//!   schema read does not touch is met later: before the first write it is still not started; at the audit INSERT, after the state
//!   `UPDATE`, it is a final `E-INTERNAL` and the `UPDATE` is rolled back with it (never a state without its audit row);
//! - **key canary**: a random 64-hex value made at run time and put in `SWARM_SIGNING_KEY` and `SWARM_ED25519_KEY` of every
//!   child process is in no store file (database, journal, WAL) and in no output of the child. The value is never printed.
//!
//! The killed-process tests re-run this very test binary as the child (`killed::zz_child_process_entry` with `GSD_FAULTS_CHILD` set);
//! the entry does nothing in a normal run, and the parent tests fail if it did not park the child, because they wait for it.
//! They exist only with `--features fault-injection`; without it one ignored test says so.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use gsd_codes::ErrorCode;
use gsd_route::transition_plan::{Facts, Request, TransitionError, TransitionPlan};
use gsd_taskwriter::{
    open_for_write, ApplyCause, ApplyError, Decider, ErrorKind, HostDecider, TaskStoreWriter,
    WriteState, WriterConfig,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};

const NOW: f64 = 1_700_000_000.25;

/// Seeds the three tasks through Python's own `TaskStore`: T-1 is IN_PROGRESS, T-2 is IN_REVIEW at the rework cap (a transition to
/// CHANGES_REQUESTED is the two-step auto-escalation) and T-3 is CLAIMED.
const SEED_SCRIPT: &str = r#"
import sys
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
for task_id, state, loops in (("T-1", "IN_PROGRESS", 0), ("T-2", "IN_REVIEW", 2), ("T-3", "CLAIMED", 0)):
    s.create(task_id=task_id, correlation_id="C-1", capability="code.backend", title=task_id)
    s.conn.execute("UPDATE tasks SET state=?, rework_loops=? WHERE task_id=?", (state, loops, task_id))
s.conn.commit()
s.conn.close()
"#;

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

/// The store Python builds, as bytes, for a rollback-journal (`wal` false) or a WAL store; built once per mode.
fn template(wal: bool) -> &'static [u8] {
    static DELETE: LazyLock<Vec<u8>> = LazyLock::new(|| build_template(false));
    static WAL: LazyLock<Vec<u8>> = LazyLock::new(|| build_template(true));
    if wal {
        &WAL
    } else {
        &DELETE
    }
}

fn build_template(wal: bool) -> Vec<u8> {
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
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("SWARM_GSD_STATE_FILE", "")
        .env_remove("SWARM_SIGNING_KEY")
        .env_remove("SWARM_ED25519_KEY")
        .env_remove("SUBSTRATE_TOKEN")
        .env_remove("SUBSTRATE_URL")
        .env("SWARM_DIR", dir.path().join(".swarm"));
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
    std::fs::read(&db).expect("read seeded db")
}

/// A private copy of the Python-built store.
struct Fixture {
    _dir: tempfile::TempDir,
    path: PathBuf,
    wal: bool,
}

impl Fixture {
    fn new(wal: bool) -> Self {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("tasks.db");
        std::fs::write(&path, template(wal)).expect("copy template");
        Self {
            _dir: dir,
            path,
            wal,
        }
    }

    /// The journal, WAL or shared-memory file of the store (only the killed-process scenarios look at them).
    #[cfg(feature = "fault-injection")]
    fn sidecar(&self, suffix: &str) -> PathBuf {
        let mut name = self.path.file_name().expect("file name").to_os_string();
        name.push(suffix);
        self.path.with_file_name(name)
    }

    fn mode(&self) -> &'static str {
        if self.wal {
            "wal"
        } else {
            "rollback-journal"
        }
    }
}

// ---- raw SQL access, independent of the writer under test -----------------------------------------------------------------

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("raw connection")
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

type Content = Vec<(&'static str, Vec<Vec<Cell>>)>;

/// Every row of every table of the store (`SELECT *` in rowid order), values typed as stored: the whole content.
async fn content(path: &Path) -> Content {
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

async fn scalar_text(path: &Path, sql: &str) -> String {
    let mut conn = connect(path).await;
    let value: String = sqlx::query_scalar(sql)
        .fetch_one(&mut conn)
        .await
        .expect(sql);
    conn.close().await.expect("close");
    value
}

/// Compares two store contents without printing a value (a value can be large): names the first table and row that differ.
fn assert_same_content(left: &Content, right: &Content, what: &str) {
    assert_eq!(left.len(), right.len(), "{what}: different tables");
    for ((table, rows), (_, other)) in left.iter().zip(right) {
        assert_eq!(
            rows.len(),
            other.len(),
            "{what}: {table} has a different number of rows"
        );
        for (index, (row, other_row)) in rows.iter().zip(other).enumerate() {
            assert!(row == other_row, "{what}: {table} row {index} differs");
        }
    }
}

async fn scalar_int(path: &Path, sql: &str) -> i64 {
    let mut conn = connect(path).await;
    let value: i64 = sqlx::query_scalar(sql)
        .fetch_one(&mut conn)
        .await
        .expect(sql);
    conn.close().await.expect("close");
    value
}

/// `PRAGMA integrity_check` of the store: `ok` or the first problem.
async fn integrity(path: &Path) -> String {
    scalar_text(path, "PRAGMA integrity_check").await
}

/// The `transitions` rows of `task_id`, as `from>to` in insertion order (`id` order).
async fn audit_of(path: &Path, task_id: &str) -> Vec<String> {
    let mut conn = connect(path).await;
    let rows = sqlx::query(
        "SELECT coalesce(from_state, 'NULL') || '>' || to_state FROM transitions WHERE task_id=? ORDER BY id",
    )
    .bind(task_id)
    .fetch_all(&mut conn)
    .await
    .expect("audit");
    conn.close().await.expect("close");
    rows.iter().map(|row| row.get::<String, _>(0)).collect()
}

async fn state_of(path: &Path, task_id: &str) -> String {
    let mut conn = connect(path).await;
    let state: String = sqlx::query_scalar("SELECT state FROM tasks WHERE task_id=?")
        .bind(task_id)
        .fetch_one(&mut conn)
        .await
        .expect("state");
    conn.close().await.expect("close");
    state
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

async fn writer_with(path: &Path, busy: Duration) -> TaskStoreWriter {
    let mut config = WriterConfig::new(path);
    config.busy_timeout = busy;
    TaskStoreWriter::new(open_for_write(&config).await.expect("open_for_write"))
}

async fn writer(path: &Path) -> TaskStoreWriter {
    TaskStoreWriter::new(
        open_for_write(&WriterConfig::new(path))
            .await
            .expect("open_for_write"),
    )
}

/// The host decision with a call counter.
#[derive(Default)]
struct Counting {
    calls: AtomicUsize,
}

impl Counting {
    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

impl Decider for Counting {
    fn decide(
        &self,
        facts: &Facts,
        request: &Request,
        now: f64,
    ) -> Result<TransitionPlan, TransitionError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        HostDecider.decide(facts, request, now)
    }
}

/// The store content after `task_id -> to` ran once, uninterrupted, in-process: what a killed-and-repeated write must equal.
async fn oracle(wal: bool, task_id: &str, to: &str) -> Content {
    let fixture = Fixture::new(wal);
    let store = writer(&fixture.path).await;
    store
        .transition(&request(task_id, to), NOW, &HostDecider)
        .await
        .expect("the oracle transition applies");
    store.close().await;
    content(&fixture.path).await
}

/// A random 64-hex value from the kernel, made at run time. It is never written to a file or printed.
fn make_canary() -> String {
    use std::io::Read;
    let mut bytes = [0u8; 32];
    std::fs::File::open("/dev/urandom")
        .expect("open /dev/urandom")
        .read_exact(&mut bytes)
        .expect("read /dev/urandom");
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Fails (without printing the value) when `canary`, as text or as the 32 bytes it spells, is inside `bytes`.
fn assert_no_canary(canary: &str, where_: &str, bytes: &[u8]) {
    let raw: Vec<u8> = (0..canary.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&canary[at..at + 2], 16).expect("hex"))
        .collect();
    let has = |needle: &[u8]| bytes.windows(needle.len()).any(|window| window == needle);
    assert!(
        !has(canary.as_bytes()) && !has(&raw),
        "the key canary is in {where_}"
    );
}

fn assert_refused_by_the_state_machine(error: &ApplyError, what: &str) {
    assert_eq!(error.code(), ErrorCode::Contract, "{what}: {error}");
    assert_eq!(error.write_state(), WriteState::Final, "{what}: {error}");
    let typed = matches!(
        error.cause(),
        ApplyCause::Refused | ApplyCause::Store(ErrorKind::ConcurrentTransition)
    );
    assert!(typed, "{what}: untyped cause {:?}: {error}", error.cause());
    assert!(
        error.message().starts_with("illegal transition ")
            || error.message().starts_with("concurrent transition on "),
        "{what}: {error}"
    );
}

// ---- race ----------------------------------------------------------------------------------------------------------------

/// 8 writers on 8 connections of one store, released together by a barrier, each asking for the same transition.
async fn race_once(
    fixture: &Fixture,
    task_id: &str,
    to: &str,
) -> Vec<Result<Vec<String>, ApplyError>> {
    const CONTENDERS: usize = 8;
    let mut writers = Vec::new();
    for _ in 0..CONTENDERS {
        writers.push(writer(&fixture.path).await);
    }
    let barrier = Arc::new(tokio::sync::Barrier::new(CONTENDERS));
    let mut tasks = Vec::new();
    for store in writers {
        let barrier = Arc::clone(&barrier);
        let request = request(task_id, to);
        tasks.push(tokio::spawn(async move {
            barrier.wait().await;
            let outcome = store.transition(&request, NOW, &HostDecider).await;
            store.close().await;
            outcome.map(|applied| {
                applied
                    .audit
                    .iter()
                    .map(|row| format!("{}>{}", row.from, row.to))
                    .collect()
            })
        }));
    }
    let mut out = Vec::new();
    for task in tasks {
        out.push(task.await.expect("join"));
    }
    out
}

/// Table: the task, the transition, the states of the audit rows the one winner writes.
const RACES: [(&str, &str, &[&str]); 2] = [
    ("T-1", "IN_REVIEW", &["IN_PROGRESS>IN_REVIEW"]),
    (
        "T-2",
        "CHANGES_REQUESTED",
        &["IN_REVIEW>CHANGES_REQUESTED", "CHANGES_REQUESTED>ESCALATED"],
    ),
];

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn eight_writers_racing_for_one_transition_have_one_winner_and_never_a_half_state() {
    for wal in [false, true] {
        for (task_id, to, winner_audit) in RACES {
            for round in 0..20 {
                let fixture = Fixture::new(wal);
                let before = audit_of(&fixture.path, task_id).await;
                let outcomes = race_once(&fixture, task_id, to).await;
                let what = format!("{} {task_id} round {round}", fixture.mode());

                let winners: Vec<&Vec<String>> =
                    outcomes.iter().filter_map(|o| o.as_ref().ok()).collect();
                assert_eq!(winners.len(), 1, "{what}: winners");
                assert_eq!(winners[0], winner_audit, "{what}: the winner's audit rows");
                for loser in outcomes.iter().filter_map(|o| o.as_ref().err()) {
                    assert_refused_by_the_state_machine(loser, &what);
                }
                assert_eq!(outcomes.len(), 8, "{what}: every contender answered");

                // The store: one set of audit rows for the from-state, in order, appended to the earlier history.
                let after = audit_of(&fixture.path, task_id).await;
                let mut expected = before.clone();
                expected.extend(winner_audit.iter().map(ToString::to_string));
                assert_eq!(after, expected, "{what}: audit rows");
                let final_state = winner_audit.last().and_then(|step| step.split('>').nth(1));
                assert_eq!(
                    Some(state_of(&fixture.path, task_id).await.as_str()),
                    final_state,
                    "{what}: final state"
                );
                assert_eq!(integrity(&fixture.path).await, "ok", "{what}");
                // The other tasks were not touched by the race.
                for other in ["T-1", "T-2", "T-3"]
                    .into_iter()
                    .filter(|id| *id != task_id)
                {
                    assert_eq!(
                        audit_of(&fixture.path, other).await,
                        ["NULL>CREATED"],
                        "{what}: {other}"
                    );
                }
            }
        }
    }
}

// ---- lock held past the busy timeout ---------------------------------------------------------------------------------------

#[tokio::test]
async fn a_lock_held_past_the_busy_timeout_is_a_typed_not_started_error_and_the_store_is_unchanged()
{
    for wal in [false, true] {
        let fixture = Fixture::new(wal);
        let mode = fixture.mode();
        let before_content = content(&fixture.path).await;
        let before_bytes = std::fs::read(&fixture.path).expect("read store");
        let oracle = oracle(wal, "T-1", "IN_REVIEW").await;

        // Opened before the lock is taken: only `BEGIN IMMEDIATE` may meet the busy timeout.
        let store = writer_with(&fixture.path, Duration::from_secs(1)).await;
        let mut holder = connect(&fixture.path).await;
        sqlx::query("BEGIN IMMEDIATE")
            .execute(&mut holder)
            .await
            .expect("take the write lock");

        let decider = Counting::default();
        let error = store
            .transition(&request("T-1", "IN_REVIEW"), NOW, &decider)
            .await
            .expect_err("the write lock is held by another connection");
        assert_eq!(
            error.cause(),
            &ApplyCause::Store(ErrorKind::Busy),
            "{mode}: {error}"
        );
        assert_eq!(error.write_state(), WriteState::NotStarted, "{mode}");
        assert_eq!(error.code(), ErrorCode::Dep, "{mode}");
        assert_eq!(
            decider.calls(),
            0,
            "{mode}: the decider ran without the lock"
        );
        assert_same_content(
            &content(&fixture.path).await,
            &before_content,
            &format!("{mode}: content"),
        );
        if !wal {
            assert_eq!(
                std::fs::read(&fixture.path).expect("read store"),
                before_bytes,
                "{mode}: file bytes"
            );
        }

        // The lock is the only reason: the same call applies once it is released, and equals the uninterrupted store.
        sqlx::query("ROLLBACK")
            .execute(&mut holder)
            .await
            .expect("release the lock");
        holder.close().await.expect("close");
        store
            .transition(&request("T-1", "IN_REVIEW"), NOW, &decider)
            .await
            .expect("the same call applies without the lock");
        store.close().await;
        assert_same_content(&content(&fixture.path).await, &oracle, mode);
        assert_eq!(integrity(&fixture.path).await, "ok", "{mode}");
    }
}

// ---- foreign and corrupt stores --------------------------------------------------------------------------------------------

/// Drops or alters something Python's `TaskStore` would create or migrate, with the SQL that does it. Each is a not-started
/// [`ErrorKind::SchemaNotReady`] refusal, which the selector answers with the Python body (that migrates).
const BROKEN_BY_SQL: [(&str, &str); 8] = [
    ("no audit table", "DROP TABLE transitions"),
    ("no tasks table", "DROP TABLE tasks"),
    ("no verdicts table", "DROP TABLE verdicts"),
    ("no artifacts table", "DROP TABLE artifacts"),
    (
        "verdicts without envelope_json",
        "ALTER TABLE verdicts DROP COLUMN envelope_json",
    ),
    (
        "verdicts without sig",
        "ALTER TABLE verdicts DROP COLUMN sig",
    ),
    (
        "audit table without reason",
        "ALTER TABLE transitions DROP COLUMN reason",
    ),
    ("tasks without notes", "ALTER TABLE tasks DROP COLUMN notes"),
];

/// Store files that are no SQL edit of Python's store: (name, bytes, the kind of the refusal at open).
fn broken_by_bytes(wal: bool) -> Vec<(&'static str, Vec<u8>, ErrorKind)> {
    let good = template(wal);
    let mut clobbered = good.to_vec();
    clobbered[..16].copy_from_slice(b"NOT A SQLITE 3.0");
    vec![
        (
            "not a database",
            b"this is not a database file\n".repeat(200),
            ErrorKind::Database,
        ),
        // An empty file is a database with no table in it.
        ("an empty file", Vec::new(), ErrorKind::SchemaNotReady),
        (
            "only the first page",
            good[..4096].to_vec(),
            ErrorKind::Database,
        ),
        (
            "half the file",
            good[..good.len() / 2].to_vec(),
            ErrorKind::Database,
        ),
        ("a clobbered header", clobbered, ErrorKind::Database),
    ]
}

/// What the writer does with the store at `path` when it is not a store it can write: `open_for_write` refuses, not started,
/// `E-DEP`, and the file is byte for byte what it was. Returns the kind of the refusal.
async fn refused_at_open(path: &Path, what: &str) -> ErrorKind {
    let before = std::fs::read(path).expect("read store");
    let error = open_for_write(&WriterConfig::new(path))
        .await
        .expect_err(what);
    assert_eq!(
        error.write_state(),
        WriteState::NotStarted,
        "{what}: {error}"
    );
    assert_eq!(error.code(), ErrorCode::Dep, "{what}: {error}");
    assert_eq!(
        std::fs::read(path).expect("read store"),
        before,
        "{what}: the store file changed"
    );
    error.kind()
}

/// Folds the WAL into the main file, so that reading the file reads the whole store.
async fn checkpoint(path: &Path) {
    let mut conn = connect(path).await;
    sqlx::query("PRAGMA wal_checkpoint(TRUNCATE)")
        .execute(&mut conn)
        .await
        .expect("checkpoint");
    conn.close().await.expect("close");
}

#[tokio::test]
async fn a_store_python_did_not_leave_whole_is_refused_as_not_started_and_stays_byte_identical() {
    for wal in [false, true] {
        for (name, sql) in BROKEN_BY_SQL {
            let fixture = Fixture::new(wal);
            let mut conn = connect(&fixture.path).await;
            sqlx::query(sql).execute(&mut conn).await.expect(sql);
            conn.close().await.expect("close");
            checkpoint(&fixture.path).await;
            let what = format!("{} ({name})", fixture.mode());
            assert_eq!(
                refused_at_open(&fixture.path, &what).await,
                ErrorKind::SchemaNotReady,
                "{what}"
            );
        }
        for (name, bytes, kind) in broken_by_bytes(wal) {
            let dir = tempfile::tempdir().expect("tempdir");
            let path = dir.path().join("tasks.db");
            std::fs::write(&path, bytes).expect("write broken store");
            let what = format!("{} ({name})", if wal { "wal" } else { "rollback-journal" });
            assert_eq!(refused_at_open(&path, &what).await, kind, "{what}");
        }
    }
}

#[tokio::test]
async fn a_database_of_another_program_and_a_missing_file_are_refused_as_not_started() {
    let dir = tempfile::tempdir().expect("tempdir");
    let foreign = dir.path().join("other.db");
    let mut conn = SqliteConnectOptions::new()
        .filename(&foreign)
        .create_if_missing(true)
        .connect()
        .await
        .expect("create foreign db");
    for sql in [
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)",
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)",
        "INSERT INTO tasks (title) VALUES ('not a swarm task')",
    ] {
        sqlx::query(sql).execute(&mut conn).await.expect(sql);
    }
    conn.close().await.expect("close");
    assert_eq!(
        refused_at_open(&foreign, "a foreign database").await,
        ErrorKind::SchemaNotReady
    );

    // The writer creates nothing: a path with no file stays without one.
    let missing = dir.path().join("absent.db");
    let error = open_for_write(&WriterConfig::new(&missing))
        .await
        .expect_err("no store");
    assert_eq!(error.write_state(), WriteState::NotStarted);
    assert_eq!(error.code(), ErrorCode::Dep);
    assert_eq!(error.kind(), ErrorKind::StoreUnavailable);
    assert!(!missing.exists(), "the writer created a store");
}

/// A page that is damaged although the schema (page 1) reads fine. A damaged `tasks` page meets the first read of the row, before
/// any write: not started. A damaged `transitions` page meets the audit INSERT, after the state UPDATE: final, `E-INTERNAL`, and
/// the UPDATE is rolled back with it, so the file is what it was (never a state without its audit row).
#[tokio::test]
async fn a_damaged_page_is_a_typed_error_and_never_a_half_write() {
    // (the table whose root page is overwritten, whether the first write happened, the code, how often the decider ran)
    let cases = [
        ("tasks", WriteState::NotStarted, ErrorCode::Dep, 0),
        ("transitions", WriteState::Final, ErrorCode::Internal, 1),
    ];
    for wal in [false, true] {
        for (table, write_state, code, decided) in cases {
            let fixture = Fixture::new(wal);
            let what = format!("{} (damaged {table} page)", fixture.mode());
            checkpoint(&fixture.path).await;
            let page_size = usize::try_from(scalar_int(&fixture.path, "PRAGMA page_size").await)
                .expect("page size");
            let root = usize::try_from(
                scalar_int(
                    &fixture.path,
                    &format!("SELECT rootpage FROM sqlite_master WHERE name='{table}'"),
                )
                .await,
            )
            .expect("root page");
            let mut bytes = std::fs::read(&fixture.path).expect("read store");
            bytes[(root - 1) * page_size..root * page_size].fill(0xff);
            std::fs::write(&fixture.path, &bytes).expect("damage the page");

            let store = writer(&fixture.path).await;
            let decider = Counting::default();
            let error = store
                .transition(&request("T-1", "IN_REVIEW"), NOW, &decider)
                .await
                .expect_err(&what);
            store.close().await;
            assert_eq!(
                error.cause(),
                &ApplyCause::Store(ErrorKind::Database),
                "{what}: {error}"
            );
            assert_eq!(error.write_state(), write_state, "{what}: {error}");
            assert_eq!(error.code(), code, "{what}: {error}");
            assert_eq!(decider.calls(), decided, "{what}: decider calls");
            assert_eq!(
                std::fs::read(&fixture.path).expect("read store"),
                bytes,
                "{what}: the failed write left a trace in the store file"
            );
        }
    }
}

// ---- a killed process (needs the pause points) -----------------------------------------------------------------------------

#[cfg(feature = "fault-injection")]
mod killed {
    use std::io::{BufRead, BufReader};
    use std::os::unix::process::ExitStatusExt;
    use std::process::{Child, Stdio};
    use std::sync::mpsc;

    use gsd_route::Json;
    use gsd_taskwriter::fault::PausePoint;
    use gsd_taskwriter::WriterOp;

    use super::*;

    const SCENARIO_ENV: &str = "GSD_FAULTS_CHILD";
    const DB_ENV: &str = "GSD_FAULTS_DB";
    /// What the child prints, on a line of its own, once it is parked (or has committed).
    const PARKED: &str = "GSD_FAULTS_PARKED";
    /// A child that nobody killed ends itself: a failed parent must not leave a process behind.
    const CHILD_LIFETIME: Duration = Duration::from_secs(900);
    /// Generous: a loaded host takes long to start a 100 MB test binary and open a store.
    const PARK_DEADLINE: Duration = Duration::from_secs(240);

    /// Where the child stops, and what it asks for.
    #[derive(Clone, Copy, Debug)]
    enum Park {
        /// The `hit`-th time the transaction reaches `point`.
        At(PausePoint, usize),
        /// After `transition` returned `Ok`: the commit is done.
        Committed,
    }

    #[derive(Clone, Copy, Debug)]
    struct Case {
        name: &'static str,
        task: &'static str,
        to: &'static str,
        park: Park,
        /// Whether the transaction has written something by then and so left a journal.
        wrote: bool,
    }

    /// Where a killed writer can be caught, as a table. T-1 is a single audited step; T-2 is the two-step auto-escalation.
    const CASES: [Case; 7] = [
        Case {
            name: "after BEGIN IMMEDIATE",
            task: "T-1",
            to: "IN_REVIEW",
            park: Park::At(PausePoint::AfterBegin, 1),
            wrote: false,
        },
        Case {
            name: "between the state UPDATE and the audit INSERT",
            task: "T-1",
            to: "IN_REVIEW",
            park: Park::At(PausePoint::AfterStateUpdate, 1),
            wrote: true,
        },
        Case {
            name: "before COMMIT",
            task: "T-1",
            to: "IN_REVIEW",
            park: Park::At(PausePoint::BeforeCommit, 1),
            wrote: true,
        },
        Case {
            name: "escalation, first step written, second UPDATE done and its audit INSERT not",
            task: "T-2",
            to: "CHANGES_REQUESTED",
            park: Park::At(PausePoint::AfterStateUpdate, 2),
            wrote: true,
        },
        Case {
            name: "escalation, before COMMIT",
            task: "T-2",
            to: "CHANGES_REQUESTED",
            park: Park::At(PausePoint::BeforeCommit, 1),
            wrote: true,
        },
        Case {
            name: "after COMMIT",
            task: "T-1",
            to: "IN_REVIEW",
            park: Park::Committed,
            wrote: true,
        },
        Case {
            name: "escalation, after COMMIT",
            task: "T-2",
            to: "CHANGES_REQUESTED",
            park: Park::Committed,
            wrote: true,
        },
    ];

    fn committed(case: &Case) -> bool {
        matches!(case.park, Park::Committed)
    }

    // ---- the child -----------------------------------------------------------------------------------------------------

    fn announce() {
        use std::io::Write;
        let mut out = std::io::stdout().lock();
        // The libtest harness has printed `test <name> ... ` without a newline: the marker ends that line.
        writeln!(out, "{PARKED}").expect("announce");
        out.flush().expect("flush");
    }

    /// The child side of the scenarios, entered by [`zz_child_process_entry`]: opens the store, runs the write and stops where the
    /// case says. It never returns: the parent kills it, or the lifetime limit does.
    async fn child_main(scenario: &str, db: &Path) {
        std::thread::spawn(|| {
            std::thread::sleep(CHILD_LIFETIME);
            std::process::exit(99);
        });
        if scenario == "big-write" {
            return big_write_child(db).await;
        }
        let case = CASES
            .iter()
            .find(|case| case.name == scenario)
            .unwrap_or_else(|| panic!("unknown scenario {scenario}"));
        let store = Arc::new(writer(db).await);
        match case.park {
            Park::Committed => {
                store
                    .transition(&request(case.task, case.to), NOW, &HostDecider)
                    .await
                    .expect("the child's transition commits");
                announce();
            }
            Park::At(point, hit) => {
                // Earlier hits of the same point are let through, the last one is held for good.
                let mut gates: Vec<_> = (0..hit)
                    .map(|_| store.store().faults().arm(point))
                    .collect();
                let mut held = gates.pop().expect("at least one hit");
                let task = tokio::spawn({
                    let store = Arc::clone(&store);
                    let request = request(case.task, case.to);
                    async move { store.transition(&request, NOW, &HostDecider).await }
                });
                for mut gate in gates {
                    gate.wait_reached().await;
                    gate.release();
                }
                held.wait_reached().await;
                announce();
                // Held until the parent kills this process: the gate is neither released nor dropped.
                let _ = task.await;
                drop(held);
            }
        }
        std::future::pending::<()>().await;
    }

    /// A write that fills the page cache: the first op writes a few MiB, which SQLite must spill to the database file before the
    /// transaction ends, and the child parks between that op and the next one. In a rollback-journal store that is a HOT journal.
    async fn big_write_child(db: &Path) {
        let store = Arc::new(writer(db).await);
        let gate = store.store().faults().arm(PausePoint::BetweenOps);
        let ops = big_write_ops();
        let task = tokio::spawn({
            let store = Arc::clone(&store);
            async move { store.apply_ops(&ops, NOW).await }
        });
        let mut held = gate;
        held.wait_reached().await;
        announce();
        let _ = task.await;
        drop(held);
    }

    fn big_write_ops() -> Vec<WriterOp> {
        let pad = "x".repeat(4 * 1024 * 1024);
        vec![
            WriterOp::SetNotes {
                task_id: "T-3".to_string(),
                set: vec![("pad".to_string(), Json::Str(pad))],
            },
            WriterOp::SetNotes {
                task_id: "T-3".to_string(),
                set: vec![("done".to_string(), Json::Bool(true))],
            },
        ]
    }

    /// Not a test of its own: the entry of the child processes below. A normal run has no `GSD_FAULTS_CHILD` and returns at once;
    /// the parent tests fail if it did not do its work, because they wait for what it announces.
    #[test]
    fn zz_child_process_entry() {
        let (Ok(scenario), Ok(db)) = (std::env::var(SCENARIO_ENV), std::env::var(DB_ENV)) else {
            return;
        };
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .expect("runtime")
            .block_on(child_main(&scenario, Path::new(&db)));
    }

    // ---- the parent ----------------------------------------------------------------------------------------------------

    /// A child process that is killed and reaped when this value goes out of scope, so that a failing test leaves nothing behind.
    struct ChildGuard {
        child: Child,
        lines: mpsc::Receiver<String>,
    }

    impl Drop for ChildGuard {
        fn drop(&mut self) {
            self.child.kill().ok();
            self.child.wait().ok();
        }
    }

    fn spawn_child(scenario: &str, db: &Path, canary: &str) -> ChildGuard {
        let mut child = Command::new(std::env::current_exe().expect("test binary path"))
            .args([
                "--exact",
                "killed::zz_child_process_entry",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(SCENARIO_ENV, scenario)
            .env(DB_ENV, db)
            .env("SWARM_SIGNING_KEY", canary)
            .env("SWARM_ED25519_KEY", canary)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn the child process");
        let stdout = child.stdout.take().expect("child stdout");
        let (sender, lines) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if sender.send(line).is_err() {
                    return;
                }
            }
        });
        ChildGuard { child, lines }
    }

    impl ChildGuard {
        /// Waits until the child announces that it is parked; returns every line it printed.
        fn wait_parked(&mut self, what: &str) -> Vec<String> {
            let mut seen = Vec::new();
            loop {
                match self.lines.recv_timeout(PARK_DEADLINE) {
                    Ok(line) if line.ends_with(PARKED) => return seen,
                    Ok(line) => seen.push(line),
                    Err(_) => {
                        let status = self.child.try_wait().ok().flatten();
                        panic!("{what}: the child never parked (exit {status:?}, output {seen:?})");
                    }
                }
            }
        }

        /// SIGKILL (`Child::kill` is `kill -9` on Unix), then the exit status and everything the child printed to stderr.
        fn sigkill(&mut self, what: &str) -> String {
            assert!(
                self.child.try_wait().expect("try_wait").is_none(),
                "{what}: the child ended before the kill"
            );
            self.child.kill().expect("SIGKILL");
            let status = self.child.wait().expect("wait");
            assert_eq!(
                status.signal(),
                Some(9),
                "{what}: not killed by SIGKILL: {status:?}"
            );
            let mut stderr = String::new();
            if let Some(mut pipe) = self.child.stderr.take() {
                std::io::Read::read_to_string(&mut pipe, &mut stderr).ok();
            }
            stderr
        }
    }

    /// The bytes of every file of the store at this moment: the database and its journal or WAL.
    fn store_files(fixture: &Fixture) -> Vec<(&'static str, Vec<u8>)> {
        let mut files = vec![(
            "database",
            std::fs::read(&fixture.path).expect("read store"),
        )];
        for (name, suffix) in [("journal", "-journal"), ("wal", "-wal"), ("shm", "-shm")] {
            if let Ok(bytes) = std::fs::read(fixture.sidecar(suffix)) {
                files.push((name, bytes));
            }
        }
        files
    }

    /// Runs one case on one store and checks the store both-or-neither afterwards. Returns nothing: it asserts.
    async fn kill_and_check(case: &Case, wal: bool) {
        let fixture = Fixture::new(wal);
        let what = format!("{} / {}", fixture.mode(), case.name);
        let canary = make_canary();
        let before = content(&fixture.path).await;
        let audit_before = audit_of(&fixture.path, case.task).await;
        let uninterrupted = oracle(wal, case.task, case.to).await;

        let mut child = spawn_child(case.name, &fixture.path, &canary);
        let printed = child.wait_parked(&what);
        let journal_there = fixture.sidecar("-journal").exists();
        let files_at_kill = store_files(&fixture);
        let stderr = child.sigkill(&what);
        drop(child);

        // What the dead process left behind: no key in any file or output.
        for (name, bytes) in files_at_kill {
            assert_no_canary(&canary, &format!("{what}: the {name} at the kill"), &bytes);
        }
        assert_no_canary(
            &canary,
            &format!("{what}: the child's stdout"),
            printed.join("\n").as_bytes(),
        );
        assert_no_canary(
            &canary,
            &format!("{what}: the child's stderr"),
            stderr.as_bytes(),
        );
        if !wal && case.wrote && !committed(case) {
            assert!(
                journal_there,
                "{what}: the killed writer left no rollback journal, so the case did not catch a write in flight"
            );
        }

        // Both or neither, read by the next opener (which recovers a journal or a WAL on its own).
        let after = content(&fixture.path).await;
        assert_eq!(integrity(&fixture.path).await, "ok", "{what}");
        if committed(case) {
            assert_same_content(
                &after,
                &uninterrupted,
                &format!("{what}: a committed write is whole after the kill"),
            );
            // And it is final: the same call is refused by the state machine, nothing is doubled.
            let store = writer(&fixture.path).await;
            let error = store
                .transition(&request(case.task, case.to), NOW, &HostDecider)
                .await
                .expect_err("a replay of a committed transition");
            store.close().await;
            assert_refused_by_the_state_machine(&error, &what);
            assert_same_content(
                &content(&fixture.path).await,
                &uninterrupted,
                &format!("{what}: replay doubled a row"),
            );
        } else {
            assert_same_content(
                &after,
                &before,
                &format!(
                    "{what}: a killed write left a trace (row without audit row, or the reverse)"
                ),
            );
            assert_eq!(
                audit_of(&fixture.path, case.task).await,
                audit_before,
                "{what}: audit rows"
            );
            // The write lock died with the process and the store takes the same call, once.
            let store = writer(&fixture.path).await;
            store
                .transition(&request(case.task, case.to), NOW, &HostDecider)
                .await
                .unwrap_or_else(|error| {
                    panic!("{what}: the next opener cannot apply the call: {error}")
                });
            store.close().await;
            assert_same_content(
                &content(&fixture.path).await,
                &uninterrupted,
                &format!("{what}: the repeated call"),
            );
        }
        assert_eq!(integrity(&fixture.path).await, "ok", "{what}");
        assert_no_canary(
            &canary,
            &format!("{what}: the store after recovery"),
            &std::fs::read(&fixture.path).expect("read store"),
        );
        if !wal {
            assert!(
                !fixture.sidecar("-journal").exists(),
                "{what}: a rollback journal outlived the next opener's write"
            );
        }
    }

    /// 50 killed children on one journal mode, over every row of [`CASES`] in turn.
    async fn fifty_kills(wal: bool) {
        for round in 0..50 {
            kill_and_check(&CASES[round % CASES.len()], wal).await;
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn a_writer_killed_at_any_point_leaves_a_rollback_journal_store_both_or_neither_over_50_repeats(
    ) {
        fifty_kills(false).await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn a_writer_killed_at_any_point_leaves_a_wal_store_both_or_neither_over_50_repeats() {
        fifty_kills(true).await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn a_hot_journal_or_an_uncommitted_wal_left_by_a_killed_writer_is_rolled_back_by_the_next_opener(
    ) {
        for wal in [false, true] {
            for round in 0..5 {
                let fixture = Fixture::new(wal);
                let what = format!("{} big write, round {round}", fixture.mode());
                let canary = make_canary();
                let before = content(&fixture.path).await;
                let bytes_before = std::fs::read(&fixture.path).expect("read store");

                let mut child = spawn_child("big-write", &fixture.path, &canary);
                child.wait_parked(&what);
                let at_kill = store_files(&fixture);
                child.sigkill(&what);
                drop(child);

                if wal {
                    let wal_bytes = std::fs::metadata(fixture.sidecar("-wal"))
                        .map(|m| m.len())
                        .unwrap_or(0);
                    assert!(wal_bytes > 0, "{what}: no uncommitted frames in the WAL");
                } else {
                    assert!(fixture.sidecar("-journal").exists(), "{what}: no journal");
                    assert_ne!(
                        std::fs::read(&fixture.path).expect("read store"),
                        bytes_before,
                        "{what}: the page cache never spilled, so the journal is not hot"
                    );
                }
                for (name, bytes) in at_kill {
                    assert_no_canary(&canary, &format!("{what}: the {name}"), &bytes);
                }

                // The next opener rolls it back; neither op survives.
                assert_same_content(
                    &content(&fixture.path).await,
                    &before,
                    &format!("{what}: content"),
                );
                assert_eq!(integrity(&fixture.path).await, "ok", "{what}");
                if !wal {
                    assert!(
                        !fixture.sidecar("-journal").exists(),
                        "{what}: the hot journal is still there after a reader opened the store"
                    );
                }
                // ... and the store is whole: a transition on the task the big write touched applies.
                let store = writer(&fixture.path).await;
                store
                    .transition(&request("T-3", "IN_PROGRESS"), NOW, &HostDecider)
                    .await
                    .unwrap_or_else(|error| panic!("{what}: {error}"));
                store.close().await;
                assert_eq!(
                    audit_of(&fixture.path, "T-3").await,
                    ["NULL>CREATED", "CLAIMED>IN_PROGRESS"],
                    "{what}"
                );
                assert_eq!(integrity(&fixture.path).await, "ok", "{what}");
            }
        }
    }

    /// A contender that waits for the write lock of a writer that is then killed gets the lock and commits.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn a_contender_waiting_for_a_killed_writers_lock_gets_it_and_commits_once() {
        for wal in [false, true] {
            let fixture = Fixture::new(wal);
            let what = format!("{} lock holder killed", fixture.mode());
            let canary = make_canary();
            let uninterrupted = oracle(wal, "T-1", "IN_REVIEW").await;

            // The holder parks right after BEGIN IMMEDIATE, on the very transition the contender asks for.
            let mut holder = spawn_child(CASES[0].name, &fixture.path, &canary);
            holder.wait_parked(&what);
            let contender = writer_with(&fixture.path, Duration::from_secs(120)).await;
            let waiting = tokio::spawn(async move {
                let outcome = contender
                    .transition(&request("T-1", "IN_REVIEW"), NOW, &HostDecider)
                    .await;
                contender.close().await;
                outcome
            });
            // Give the contender the time to start waiting; the outcome does not depend on whether it already is.
            tokio::time::sleep(Duration::from_millis(300)).await;
            holder.sigkill(&what);
            drop(holder);

            let applied = waiting
                .await
                .expect("join")
                .unwrap_or_else(|error| panic!("{what}: the contender got no lock: {error}"));
            assert_eq!(applied.audit.len(), 1, "{what}");
            assert_same_content(&content(&fixture.path).await, &uninterrupted, &what);
            assert_eq!(integrity(&fixture.path).await, "ok", "{what}");
        }
    }
}

#[cfg(not(feature = "fault-injection"))]
#[test]
#[ignore = "the kill -9 scenarios need the pause points: run with --features fault-injection"]
fn kill_9_scenarios_need_the_fault_injection_feature() {}

#[test]
fn the_canary_is_random_hex_and_the_scan_finds_it_in_both_spellings() {
    let first = make_canary();
    assert_eq!(first.len(), 64);
    assert!(first.bytes().all(|byte| byte.is_ascii_hexdigit()));
    assert_ne!(first, make_canary(), "two canaries of one run are equal");

    let raw: Vec<u8> = (0..first.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&first[at..at + 2], 16).expect("hex"))
        .collect();
    let as_text = [b"prefix ".as_slice(), first.as_bytes(), b" suffix"].concat();
    let as_bytes = [b"\0\0".as_slice(), &raw, b"\xff"].concat();
    for (name, haystack) in [("text", as_text), ("bytes", as_bytes)] {
        let found = std::panic::catch_unwind(|| assert_no_canary(&first, name, &haystack));
        assert!(
            found.is_err(),
            "the scan missed the canary spelled as {name}"
        );
    }
    assert_no_canary(&first, "clean", b"nothing to see here");
}
