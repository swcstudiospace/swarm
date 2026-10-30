//! Faults of `TaskStoreWriter::ingest` (phase 14.5, unit U3.5e part 1).
//!
//! What must hold when the world misbehaves around one ingest:
//!
//! - **a failure between ops** leaves none applied: a trigger fails the second write of the op list (a database failure in phase two,
//!   after the first writes), and with `fault-injection` a transaction parked between two ops holds the write lock and, dropped, leaves
//!   nothing (`a_dropped_ingest_parked_between_ops_leaves_none_applied`);
//! - **the snapshot is read under the lock**: with `fault-injection`, an ingest parked right after `BEGIN IMMEDIATE` (before its first
//!   read) already holds the write lock, so no other writer can slip a commit between its reads and its writes;
//! - **kill -9 mid op list** (`fault-injection`): a child process is parked between two ops of a claim-and-approve list and killed; the
//!   next opener sees the store as it was, the same ingest then applies once and equals an uninterrupted run, and a child killed
//!   after its commit leaves a whole result whose replay is answered with the stored reply and applies nothing twice (U12.2 item 4: a
//!   reply lost after the commit, then a replay);
//! - **a busy store** is a not-started `Busy` refusal, and a lock that is released within the timeout is waited for;
//! - **two concurrent ingests of one result** apply it exactly once;
//! - **key canaries**: `SWARM_SIGNING_KEY` and `SWARM_ED25519_KEY` hold random hex in a child process that runs the production
//!   `EnvApprovalSource` through an approving ingest, a refusal, a busy store and an invalid result, printing every returned value, error
//!   and `Debug`; neither canary, as text or as the 32 bytes it spells, is in the child's output or in any store file.
//!
//! The killed-process tests re-run this very test binary as the child (`killed::zz_child_process_entry` with `GSD_INGEST_CHILD` set); the
//! entry does nothing in a normal run, and the parent tests fail if it did not park the child, because they wait for it. They exist only
//! with `--features fault-injection`.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use gsd_codes::ErrorCode;
use gsd_route::result_parse::{parse_result, validate_result};
use gsd_route::{jstr, Json};
use gsd_taskwriter::{
    open_for_write, ApprovalSource, EnvApprovalSource, ErrorKind, FixedApprovalSource, IngestCause,
    IngestRequest, TaskStoreWriter, WriteState, WriterConfig,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};

const NOW: f64 = 1_700_000_000.25;

/// Seeds, through the real `TaskStore` and the real clock, one correlation of tasks that need no gate: `H-1` and `R-1` are IN_PROGRESS,
/// `P-1` and `P-2` are PLANNED.
const SEED_SCRIPT: &str = r#"
import sys
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
PATH = {"PLANNED": ["VALIDATED", "PLANNED"], "IN_PROGRESS": ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"]}
for tid, state in (("H-1", "IN_PROGRESS"), ("R-1", "IN_PROGRESS"), ("P-1", "PLANNED"), ("P-2", "PLANNED")):
    s.create(task_id=tid, correlation_id="C-1", capability="code.backend", title=tid, notes={"gates": []}, agent_id="A05@seed")
    for step in PATH[state]:
        s.transition(tid, step, actor="A01", reason="seed")
s.conn.close()
"#;

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

/// A random 64-hex value from the kernel, made at run time. It is never written to a file or printed.
fn random_hex() -> String {
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

static TEMPLATE: LazyLock<Vec<u8>> = LazyLock::new(|| {
    let root = repo_root();
    assert!(
        root.join("swarm/taskstore.py").is_file(),
        "no swarm/taskstore.py under {}: set SWARM_REPO_ROOT to the repository root",
        root.display()
    );
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("tasks.db");
    let output = Command::new("python3")
        .arg("-c")
        .arg(SEED_SCRIPT)
        .arg(&db)
        .current_dir(&root)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("SWARM_GSD_STATE_FILE", "")
        .env("SWARM_DIR", dir.path().join(".swarm"))
        .env("SWARM_CLAUDE_STATE_DIR", dir.path().join("claude-state"))
        .env_remove("SWARM_TASKSTORE_WAL")
        .env_remove("SWARM_SIGNING_KEY")
        .env_remove("SWARM_ED25519_KEY")
        .env_remove("SUBSTRATE_TOKEN")
        .env_remove("SUBSTRATE_URL")
        .output()
        .expect("run python3");
    assert!(
        output.status.success(),
        "python seed failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    std::fs::read(&db).expect("read the seeded store")
});

/// A private copy of the Python-built store.
struct Fixture {
    _dir: tempfile::TempDir,
    path: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("tasks.db");
        std::fs::write(&path, &*TEMPLATE).expect("copy the template");
        Self { _dir: dir, path }
    }

    /// The journal or shared-memory file of the store.
    #[cfg(feature = "fault-injection")]
    fn sidecar(&self, suffix: &str) -> PathBuf {
        let mut name = self.path.file_name().expect("file name").to_os_string();
        name.push(suffix);
        self.path.with_file_name(name)
    }
}

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

/// Compares two store contents without printing a value: names the first table and row that differ.
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

#[cfg(feature = "fault-injection")]
async fn scalar_text(path: &Path, sql: &str) -> String {
    let mut conn = connect(path).await;
    let value: String = sqlx::query_scalar(sql)
        .fetch_one(&mut conn)
        .await
        .expect(sql);
    conn.close().await.expect("close");
    value
}

async fn writer(path: &Path) -> TaskStoreWriter {
    writer_with(path, Duration::from_secs(30)).await
}

async fn writer_with(path: &Path, busy: Duration) -> TaskStoreWriter {
    let mut config = WriterConfig::new(path);
    config.busy_timeout = busy;
    TaskStoreWriter::new(open_for_write(&config).await.expect("open_for_write"))
}

/// The writer's source of the in-process tests: a random key, and `HOME` somewhere that holds no key file.
fn keyed() -> FixedApprovalSource {
    FixedApprovalSource::new(vec![
        ("SWARM_SIGNING_KEY".to_string(), random_hex()),
        (
            "HOME".to_string(),
            "/nonexistent-home-for-ingest-tests".to_string(),
        ),
    ])
}

fn request(task_id: &str, body: &str) -> IngestRequest {
    let parsed = parse_result(body).expect("parse_result");
    IngestRequest {
        task_id: task_id.to_string(),
        result: validate_result(parsed.as_ref(), task_id).cloned(),
        meta: Json::Obj(vec![("ingest".to_string(), jstr("/ingest/result.json"))]),
        advisory: false,
    }
}

/// A heartbeat with one artifact: one transition-free list, three writes (the result notes, the artifact and the replay key).
fn heartbeat() -> IngestRequest {
    request(
        "H-1",
        r#"{"task_id":"H-1","state":"IN_PROGRESS","outputs":[{"kind":"code","uri":"file://once","version":"1","digest":"d"}]}"#,
    )
}

/// A claim, the reported IN_REVIEW, the notes, one artifact, the replay key, and the APPROVED and DONE of the reconcile: eight ops.
fn claim_and_approve() -> IngestRequest {
    request(
        "P-1",
        r#"{"task_id":"P-1","state":"IN_REVIEW","outputs":[{"kind":"code","uri":"file://p","version":"1","digest":"d"}]}"#,
    )
}

/// The store content after `request` ran once, uninterrupted, in-process: what a killed-and-repeated ingest must equal.
async fn oracle(request: &IngestRequest) -> Content {
    let fixture = Fixture::new();
    let store = writer(&fixture.path).await;
    store
        .ingest(request, NOW, &keyed())
        .await
        .expect("the oracle ingest applies");
    store.close().await;
    content(&fixture.path).await
}

// ---- a busy store ----------------------------------------------------------------------------------------------------------

#[tokio::test]
async fn a_busy_store_is_a_not_started_refusal_that_changes_nothing() {
    let fixture = Fixture::new();
    let before = content(&fixture.path).await;
    let bytes = std::fs::read(&fixture.path).expect("read the store");
    let mut holder = connect(&fixture.path).await;
    sqlx::query("BEGIN IMMEDIATE")
        .execute(&mut holder)
        .await
        .expect("hold the write lock");
    let store = writer_with(&fixture.path, Duration::from_millis(300)).await;
    let error = store
        .ingest(&heartbeat(), NOW, &keyed())
        .await
        .expect_err("the lock is held past the timeout");
    assert_eq!(*error.cause(), IngestCause::Store(ErrorKind::Busy));
    assert_eq!(error.write_state(), WriteState::NotStarted);
    assert_eq!(error.code(), ErrorCode::Dep);
    assert!(error.events().is_empty());
    sqlx::query("ROLLBACK")
        .execute(&mut holder)
        .await
        .expect("release");
    holder.close().await.expect("close");
    store.close().await;
    assert_eq!(
        std::fs::read(&fixture.path).expect("read the store"),
        bytes,
        "the file is byte-identical"
    );
    assert_same_content(&before, &content(&fixture.path).await, "a busy store");
}

#[tokio::test]
async fn a_lock_released_within_the_timeout_is_waited_for() {
    let fixture = Fixture::new();
    let mut holder = connect(&fixture.path).await;
    sqlx::query("BEGIN IMMEDIATE")
        .execute(&mut holder)
        .await
        .expect("hold the write lock");
    let store = writer_with(&fixture.path, Duration::from_secs(120)).await;
    let release = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(800)).await;
        sqlx::query("ROLLBACK")
            .execute(&mut holder)
            .await
            .expect("release");
        holder.close().await.expect("close");
    });
    let applied = store
        .ingest(&heartbeat(), NOW, &keyed())
        .await
        .expect("the ingest waits for the lock and then applies");
    release.await.expect("release task");
    store.close().await;
    assert_eq!(applied.reply, "IN_PROGRESS");
    assert_eq!(
        scalar_int(&fixture.path, "SELECT COUNT(*) FROM artifacts").await,
        1
    );
}

// ---- two concurrent ingests ------------------------------------------------------------------------------------------------

async fn race(
    request: IngestRequest,
    rounds: usize,
) -> Vec<Vec<Result<gsd_taskwriter::IngestApplied, gsd_taskwriter::IngestError>>> {
    let mut all = Vec::new();
    for _ in 0..rounds {
        let fixture = Fixture::new();
        let source = Arc::new(keyed());
        let barrier = Arc::new(tokio::sync::Barrier::new(2));
        let mut tasks = Vec::new();
        for _ in 0..2 {
            let (path, request, source, barrier) = (
                fixture.path.clone(),
                request.clone(),
                Arc::clone(&source),
                Arc::clone(&barrier),
            );
            tasks.push(tokio::spawn(async move {
                let store = writer(&path).await;
                barrier.wait().await;
                let outcome = store.ingest(&request, NOW, source.as_ref()).await;
                store.close().await;
                outcome
            }));
        }
        let mut outcomes = Vec::new();
        for task in tasks {
            outcomes.push(task.await.expect("join"));
        }
        let artifacts = scalar_int(&fixture.path, "SELECT COUNT(*) FROM artifacts").await;
        assert_eq!(artifacts, 1, "the result was applied exactly once");
        all.push(outcomes);
    }
    all
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn two_concurrent_ingests_of_one_heartbeat_apply_it_once() {
    for outcomes in race(heartbeat(), 3).await {
        let replies: Vec<_> = outcomes
            .iter()
            .map(|outcome| outcome.as_ref().expect("both calls are answered"))
            .collect();
        assert_eq!(
            replies.iter().filter(|applied| applied.replayed()).count(),
            1,
            "exactly one is the replay"
        );
        assert_eq!(
            replies[0].reply, replies[1].reply,
            "both answers are the one stored reply"
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn two_concurrent_ingests_of_one_review_result_apply_it_once() {
    for outcomes in race(claim_and_approve(), 3).await {
        let applied = outcomes.iter().filter(|outcome| outcome.is_ok()).count();
        assert_eq!(
            applied, 1,
            "exactly one ingest applies the claim and the approval"
        );
        let refused = outcomes
            .iter()
            .find_map(|outcome| outcome.as_ref().err())
            .expect("the other one is refused");
        assert_eq!(*refused.cause(), IngestCause::Refused);
        assert_eq!(refused.code(), ErrorCode::Contract);
        assert!(
            refused
                .message()
                .starts_with("E-CONTRACT: illegal transition DONE"),
            "{refused}"
        );
    }
}

// ---- a failure between ops -------------------------------------------------------------------------------------------------

#[tokio::test]
async fn a_database_failure_after_the_first_writes_leaves_none_applied() {
    let fixture = Fixture::new();
    let before = content(&fixture.path).await;
    // The claim, the reported state and the result notes are written before the artifact row, which this trigger fails.
    let mut conn = connect(&fixture.path).await;
    sqlx::query("CREATE TRIGGER inject_failure BEFORE INSERT ON artifacts BEGIN SELECT RAISE(ABORT, 'injected'); END")
        .execute(&mut conn)
        .await
        .expect("trigger");
    let store = writer(&fixture.path).await;
    let error = store
        .ingest(&claim_and_approve(), NOW, &keyed())
        .await
        .expect_err("the artifact insert fails");
    assert_eq!(
        error.write_state(),
        WriteState::Final,
        "a failure after the first write is definitive"
    );
    assert!(error.events().is_empty());
    assert_same_content(
        &before,
        &content(&fixture.path).await,
        "the claim, the transition and the notes were rolled back",
    );
    assert_eq!(
        scalar_int(
            &fixture.path,
            "SELECT COUNT(*) FROM transitions WHERE task_id='P-1'"
        )
        .await,
        3
    );

    // Without the fault the same ingest applies, once, to the state an uninterrupted run gives.
    sqlx::query("DROP TRIGGER inject_failure")
        .execute(&mut conn)
        .await
        .expect("drop trigger");
    conn.close().await.expect("close");
    store
        .ingest(&claim_and_approve(), NOW, &keyed())
        .await
        .expect("the retry applies");
    store.close().await;
    assert_same_content(
        &content(&fixture.path).await,
        &oracle(&claim_and_approve()).await,
        "the retry equals an uninterrupted run",
    );
}

// ---- key canaries ----------------------------------------------------------------------------------------------------------

const CHILD_ENV: &str = "GSD_INGEST_CHILD";
const DB_ENV: &str = "GSD_INGEST_DB";
/// What a child prints, on a line of its own, once it is parked (or has finished).
const PARKED: &str = "GSD_INGEST_PARKED";

/// The production source in the child: keys from its environment, which holds the canaries.
async fn canary_child(db: &Path) {
    let source = EnvApprovalSource;
    let mut shown = Vec::new();
    let store = writer_with(db, Duration::from_millis(300)).await;
    // An approving ingest: the keys are real (the canaries), the task needs no gate.
    let approved = store
        .ingest(
            &request(
                "R-1",
                r#"{"task_id":"R-1","state":"IN_REVIEW","outputs":[{"uri":"file://r"}]}"#,
            ),
            NOW,
            &source,
        )
        .await;
    shown.push(format!("{approved:?}"));
    if let Ok(applied) = &approved {
        shown.push(format!(
            "{} {:?} {:?}",
            applied.reply, applied.notes_log, applied.events
        ));
    }
    // A refusal of Python's: unknown task, and an invalid result with its event.
    for refused in [
        store
            .ingest(
                &request("T-404", r#"{"task_id":"T-404","state":"IN_PROGRESS"}"#),
                NOW,
                &source,
            )
            .await,
        store
            .ingest(&request("H-1", r#"{"task_id":"H-1"}"#), NOW, &source)
            .await,
    ] {
        let error = refused.expect_err("a refusal");
        shown.push(format!(
            "{error:?} | {error} | {:?} | {:?}",
            error.to_seam_error(),
            error.events()
        ));
    }
    // A busy store.
    let mut holder = connect(db).await;
    sqlx::query("BEGIN IMMEDIATE")
        .execute(&mut holder)
        .await
        .expect("hold the write lock");
    let busy = store.ingest(&heartbeat(), NOW, &source).await;
    shown.push(format!("{busy:?}"));
    sqlx::query("ROLLBACK")
        .execute(&mut holder)
        .await
        .expect("release");
    holder.close().await.expect("close");
    // A refused approval list and the key holder itself.
    shown.push(format!("{source:?} {:?}", source.keys()));
    store.close().await;
    let mut out = std::io::stdout().lock();
    for line in shown {
        use std::io::Write;
        writeln!(out, "{line}").expect("print");
    }
    use std::io::Write;
    writeln!(out, "{PARKED}").expect("announce");
    out.flush().expect("flush");
}

/// Not a test of its own: the entry of the canary child. A normal run has no `GSD_INGEST_CHILD` and returns at once.
#[test]
fn zz_canary_child_entry() {
    let (Ok(scenario), Ok(db)) = (std::env::var(CHILD_ENV), std::env::var(DB_ENV)) else {
        return;
    };
    assert_eq!(scenario, "canary");
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .expect("runtime")
        .block_on(canary_child(Path::new(&db)));
}

#[tokio::test]
async fn the_key_canaries_never_reach_a_reply_an_error_a_debug_or_a_file() {
    let fixture = Fixture::new();
    let (hmac, ed25519) = (random_hex(), random_hex());
    let output = Command::new(std::env::current_exe().expect("test binary path"))
        .args([
            "--exact",
            "zz_canary_child_entry",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(CHILD_ENV, "canary")
        .env(DB_ENV, &fixture.path)
        .env("SWARM_SIGNING_KEY", &hmac)
        .env("SWARM_ED25519_KEY", &ed25519)
        .env("HOME", "/nonexistent-home-for-ingest-tests")
        .env_remove("XDG_CONFIG_HOME")
        .env_remove("SWARM_ED25519_KEY_FILE")
        .env_remove("SWARM_REQUIRE_KEY")
        .stdin(Stdio::null())
        .output()
        .expect("run the canary child");
    let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
    assert!(
        output.status.success(),
        "the child failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        stdout.contains(PARKED),
        "the child did not finish: {stdout}"
    );
    assert!(
        stdout.contains("Ok(IngestApplied"),
        "the approving ingest did not run: it proves nothing without it"
    );
    for (name, canary) in [
        ("SWARM_SIGNING_KEY", &hmac),
        ("SWARM_ED25519_KEY", &ed25519),
    ] {
        assert_no_canary(
            canary,
            &format!("the child's stdout ({name})"),
            stdout.as_bytes(),
        );
        assert_no_canary(
            canary,
            &format!("the child's stderr ({name})"),
            &output.stderr,
        );
        assert_no_canary(
            canary,
            &format!("the store file ({name})"),
            &std::fs::read(&fixture.path).expect("read the store"),
        );
    }
    // The same text must not be reachable from the writer either: the source's own `Debug` is presence flags only.
    let source = FixedApprovalSource::new(vec![
        ("SWARM_SIGNING_KEY".to_string(), hmac.clone()),
        ("SWARM_ED25519_KEY".to_string(), ed25519.clone()),
    ]);
    assert_no_canary(
        &hmac,
        "the Debug of a source",
        format!("{source:?} {:?}", source.keys()).as_bytes(),
    );
    assert_no_canary(
        &ed25519,
        "the Debug of a source",
        format!("{source:?} {:?}", source.keys()).as_bytes(),
    );
}

// ---- the pause points -------------------------------------------------------------------------------------------------------

#[cfg(feature = "fault-injection")]
mod parked {
    use gsd_taskwriter::fault::PausePoint;

    use super::*;

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn an_ingest_holds_the_write_lock_before_its_first_read() {
        let fixture = Fixture::new();
        let store = Arc::new(writer(&fixture.path).await);
        let mut gate = store.store().faults().arm(PausePoint::AfterBegin);
        let task = tokio::spawn({
            let store = Arc::clone(&store);
            async move { store.ingest(&heartbeat(), NOW, &keyed()).await }
        });
        gate.wait_reached().await;
        // Parked right after BEGIN IMMEDIATE, before the snapshot: another writer cannot even start its own write transaction.
        let options = SqliteConnectOptions::new()
            .filename(&fixture.path)
            .busy_timeout(Duration::from_millis(0));
        let mut other = options.connect().await.expect("second connection");
        let attempt = sqlx::query("BEGIN IMMEDIATE").execute(&mut other).await;
        assert!(
            attempt.is_err(),
            "the snapshot is read without the write lock"
        );
        other.close().await.ok();
        gate.release();
        let applied = task
            .await
            .expect("join")
            .expect("the ingest applies once released");
        assert_eq!(applied.reply, "IN_PROGRESS");
        Arc::try_unwrap(store).expect("sole owner").close().await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn a_dropped_ingest_parked_between_ops_leaves_none_applied() {
        let fixture = Fixture::new();
        let before = content(&fixture.path).await;
        let store = Arc::new(writer(&fixture.path).await);
        // Let the claim and the reported state through, park before the fourth op.
        let mut gates: Vec<_> = (0..4)
            .map(|_| store.store().faults().arm(PausePoint::BetweenOps))
            .collect();
        let mut held = gates.pop().expect("a gate");
        let task = tokio::spawn({
            let store = Arc::clone(&store);
            async move { store.ingest(&claim_and_approve(), NOW, &keyed()).await }
        });
        for mut gate in gates {
            gate.wait_reached().await;
            gate.release();
        }
        held.wait_reached().await;
        // Three ops are written inside the open transaction; no other connection can see or change them.
        let mut other = SqliteConnectOptions::new()
            .filename(&fixture.path)
            .busy_timeout(Duration::from_millis(0))
            .connect()
            .await
            .expect("second connection");
        assert!(
            sqlx::query("BEGIN IMMEDIATE")
                .execute(&mut other)
                .await
                .is_err(),
            "the lock is held between the ops"
        );
        other.close().await.ok();
        task.abort();
        let _ = task.await;
        drop(held);
        Arc::try_unwrap(store).expect("sole owner").close().await;
        assert_same_content(
            &before,
            &content(&fixture.path).await,
            "an ingest dropped between two ops",
        );
    }
}

// ---- a killed process (needs the pause points) ------------------------------------------------------------------------------

#[cfg(feature = "fault-injection")]
mod killed {
    use std::io::{BufRead, BufReader};
    use std::os::unix::process::ExitStatusExt;
    use std::process::Child;
    use std::sync::mpsc;

    use gsd_taskwriter::fault::PausePoint;

    use super::*;

    /// Generous: a loaded host takes long to start a 100 MB test binary and open a store.
    const PARK_DEADLINE: Duration = Duration::from_secs(240);
    /// A child that nobody killed ends itself: a failed parent must not leave a process behind.
    const CHILD_LIFETIME: Duration = Duration::from_secs(900);

    fn announce() {
        use std::io::Write;
        let mut out = std::io::stdout().lock();
        // The libtest harness has printed `test <name> ... ` without a newline: the marker ends that line.
        writeln!(out, "{PARKED}").expect("announce");
        out.flush().expect("flush");
    }

    /// The child: runs the claim-and-approve ingest and either parks before the fourth op or announces after the commit.
    async fn child_main(scenario: &str, db: &Path) {
        std::thread::spawn(|| {
            std::thread::sleep(CHILD_LIFETIME);
            std::process::exit(99);
        });
        let store = Arc::new(writer(db).await);
        let source = EnvApprovalSource;
        match scenario {
            "committed" => {
                store
                    .ingest(&heartbeat(), NOW, &source)
                    .await
                    .expect("the child's ingest commits");
                announce();
            }
            "between-ops" => {
                let mut gates: Vec<_> = (0..4)
                    .map(|_| store.store().faults().arm(PausePoint::BetweenOps))
                    .collect();
                let mut held = gates.pop().expect("a gate");
                let task = tokio::spawn({
                    let store = Arc::clone(&store);
                    async move {
                        store
                            .ingest(&claim_and_approve(), NOW, &EnvApprovalSource)
                            .await
                    }
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
            other => panic!("unknown scenario {other}"),
        }
        std::future::pending::<()>().await;
    }

    /// Not a test of its own: the entry of the child processes below.
    #[test]
    fn zz_child_process_entry() {
        let (Ok(scenario), Ok(db)) = (std::env::var(CHILD_ENV), std::env::var(DB_ENV)) else {
            return;
        };
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .expect("runtime")
            .block_on(child_main(&scenario, Path::new(&db)));
    }

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

    fn spawn_child(scenario: &str, db: &Path, canaries: &(String, String)) -> ChildGuard {
        let mut child = Command::new(std::env::current_exe().expect("test binary path"))
            .args([
                "--exact",
                "killed::zz_child_process_entry",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(CHILD_ENV, scenario)
            .env(DB_ENV, db)
            .env("SWARM_SIGNING_KEY", &canaries.0)
            .env("SWARM_ED25519_KEY", &canaries.1)
            .env("HOME", "/nonexistent-home-for-ingest-tests")
            .env_remove("XDG_CONFIG_HOME")
            .env_remove("SWARM_ED25519_KEY_FILE")
            .env_remove("SWARM_REQUIRE_KEY")
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

        /// SIGKILL (`Child::kill` is `kill -9` on Unix), then everything the child printed to stderr.
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

    /// The files of the store at this moment: the database and its journal.
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

    fn check_no_canaries(
        what: &str,
        canaries: &(String, String),
        printed: &[String],
        stderr: &str,
        files: &[(&'static str, Vec<u8>)],
    ) {
        for canary in [&canaries.0, &canaries.1] {
            for (name, bytes) in files {
                assert_no_canary(canary, &format!("{what}: the {name} at the kill"), bytes);
            }
            assert_no_canary(
                canary,
                &format!("{what}: the child's stdout"),
                printed.join("\n").as_bytes(),
            );
            assert_no_canary(
                canary,
                &format!("{what}: the child's stderr"),
                stderr.as_bytes(),
            );
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn a_process_killed_between_two_ops_leaves_none_applied_and_the_ingest_then_applies_once()
    {
        let fixture = Fixture::new();
        let what = "kill -9 between two ops";
        let canaries = (random_hex(), random_hex());
        let before = content(&fixture.path).await;
        let uninterrupted = oracle(&claim_and_approve()).await;

        let mut child = spawn_child("between-ops", &fixture.path, &canaries);
        let printed = child.wait_parked(what);
        let files = store_files(&fixture);
        assert!(
            fixture.sidecar("-journal").exists(),
            "{what}: the killed writer left no rollback journal, so no write was in flight"
        );
        let stderr = child.sigkill(what);
        drop(child);
        check_no_canaries(what, &canaries, &printed, &stderr, &files);

        // Read by the next opener, which recovers the hot journal on its own: neither a claim without its result nor the reverse.
        assert_same_content(&before, &content(&fixture.path).await, what);
        assert_eq!(
            scalar_text(&fixture.path, "PRAGMA integrity_check").await,
            "ok"
        );
        assert_eq!(
            scalar_int(&fixture.path, "SELECT COUNT(*) FROM artifacts").await,
            0
        );

        // The same ingest then applies, once, and equals a run nobody interrupted.
        let store = writer(&fixture.path).await;
        store
            .ingest(&claim_and_approve(), NOW, &keyed())
            .await
            .expect("the ingest applies after the crash");
        store.close().await;
        assert_same_content(
            &content(&fixture.path).await,
            &uninterrupted,
            "after the crash and the retry",
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn a_reply_lost_after_the_commit_then_a_replay_applies_nothing_twice() {
        let fixture = Fixture::new();
        let what = "kill -9 after the commit";
        let canaries = (random_hex(), random_hex());
        let uninterrupted = oracle(&heartbeat()).await;

        let mut child = spawn_child("committed", &fixture.path, &canaries);
        let printed = child.wait_parked(what);
        let files = store_files(&fixture);
        let stderr = child.sigkill(what);
        drop(child);
        check_no_canaries(what, &canaries, &printed, &stderr, &files);

        // The commit is whole after the kill ...
        assert_same_content(&content(&fixture.path).await, &uninterrupted, what);
        assert_eq!(
            scalar_text(&fixture.path, "PRAGMA integrity_check").await,
            "ok"
        );
        // ... and the caller, who never saw the reply, repeats the call: the stored reply answers it and nothing is written again.
        let store = writer(&fixture.path).await;
        let replay = store
            .ingest(&heartbeat(), NOW + 60.0, &keyed())
            .await
            .expect("the replay is answered");
        store.close().await;
        assert!(replay.replayed());
        assert_eq!(replay.reply, "IN_PROGRESS");
        assert_same_content(
            &content(&fixture.path).await,
            &uninterrupted,
            "a replay after the lost reply",
        );
        assert_eq!(
            scalar_int(&fixture.path, "SELECT COUNT(*) FROM artifacts").await,
            1,
            "the artifact row is not doubled"
        );
    }
}
