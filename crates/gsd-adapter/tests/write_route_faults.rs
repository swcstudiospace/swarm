//! Faults of the host `orch_status --transition` route (phase 14.5, unit U3.2d), through the real `gsd_engine` binary.
//!
//! Stores are built by the REAL Python `TaskStore` (the schema, the envelope migration and the WAL opt-in `SWARM_TASKSTORE_WAL=1` are
//! Python's own). Every scenario runs on a rollback-journal store and on a WAL store. The binary is `CARGO_BIN_EXE_gsd_engine`, or
//! the file named by `GSD_FAULTS_ENGINE_BIN` (a scratch build of a mutated copy, to show that a test fails when its property breaks).
//! The repository root is `SWARM_REPO_ROOT` or two levels above this crate. Temporary files follow `TMPDIR`.
//!
//! What is proved here:
//!
//! - **race**: 8 binary processes get the SAME transition of the SAME task from the SAME state at the same moment (the test holds the
//!   write lock while they start, then lets go), 20 times. Exactly one answers `status ok` and writes a manifest; the other seven
//!   answer Python's own `E-CONTRACT` refusal (typed, no manifest); the store holds exactly one audit row for the from-state, written
//!   by the process that won, and passes `integrity_check`;
//! - **lock held past the busy timeout** (the route's 30 s, `WriterConfig::new`): the reply is `E-INTERNAL` with
//!   `error.details.write_state = "not_started"`, exit 2, no manifest and a store byte for byte unchanged; the same call applies once
//!   the lock is gone;
//! - **reply lost after the commit**: a task whose row is larger than a pipe makes the binary block on printing its reply, so the
//!   test can SIGKILL it after the effects manifest appeared (the manifest is written after the `COMMIT`) and before a single reply
//!   is complete. The store has exactly one row for the transition, the manifest is whole, the reply is not a JSON document, and an
//!   explicit replay of the same call is refused by the state machine (no self-loop) and changes nothing;
//! - **foreign, corrupt and half-migrated stores**: a store without the audit table or the envelope columns, a database of another
//!   program, a file that is not a database and a damaged `tasks` page answer `E-DEP` with `write_state = "not_started"` and leave the
//!   store file byte for byte unchanged; a damaged `transitions` page, which the first write meets after the state `UPDATE`, answers
//!   `E-INTERNAL` with `write_state = "final"` and rolls the `UPDATE` back;
//! - **key canary**: a random 64-hex value made at run time in `SWARM_SIGNING_KEY` and `SWARM_ED25519_KEY` of the binary's own
//!   environment is in no reply, no stderr, no manifest and no store file of any scenario. It is never printed.

use std::fs;
use std::future::Future;
use std::io::{Read, Write};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Barrier, LazyLock};
use std::time::{Duration, Instant};

use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};
use tempfile::TempDir;

/// Seeds the tasks through Python's own `TaskStore`. `T-BIG` carries a row far larger than a pipe's capacity: the reply of a
/// transition on it cannot be written to a pipe nobody reads, so the binary blocks in the write after its commit.
const SEED_SCRIPT: &str = r#"
import sys
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
for task_id in ("T-1", "T-2", "T-BIG"):
    extra = {"inputs": ["x" * 400000]} if task_id == "T-BIG" else {}
    s.create(task_id=task_id, correlation_id="C-1", capability="code.backend", title=task_id, **extra)
    s.conn.execute("UPDATE tasks SET state='IN_PROGRESS' WHERE task_id=?", (task_id,))
s.conn.commit()
s.conn.close()
"#;

/// The same call for every scenario: IN_PROGRESS to IN_REVIEW is legal, IN_REVIEW to IN_REVIEW is not.
fn call_for(task: &str, reason: &str) -> Vec<String> {
    [
        "--json",
        "--transition",
        task,
        "IN_REVIEW",
        "--reason",
        reason,
    ]
    .iter()
    .map(ToString::to_string)
    .collect()
}

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

fn engine() -> PathBuf {
    std::env::var_os("GSD_FAULTS_ENGINE_BIN").map_or_else(
        || PathBuf::from(env!("CARGO_BIN_EXE_gsd_engine")),
        PathBuf::from,
    )
}

fn block<F: Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime")
        .block_on(future)
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
    let dir = TempDir::new().expect("tempdir");
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
    fs::read(&db).expect("read seeded db")
}

/// A scratch repository: `<dir>/work/.swarm/tasks.db` seeded from the Python template.
struct World {
    dir: TempDir,
    root: PathBuf,
    swarm: PathBuf,
    wal: bool,
}

impl World {
    fn new(wal: bool) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let root = dir.path().join("work");
        let swarm = root.join(".swarm");
        fs::create_dir_all(&swarm).expect("swarm dir");
        fs::write(swarm.join("tasks.db"), template(wal)).expect("copy template");
        Self {
            dir,
            root,
            swarm,
            wal,
        }
    }

    fn mode(&self) -> &'static str {
        if self.wal {
            "wal"
        } else {
            "rollback-journal"
        }
    }

    fn db(&self) -> PathBuf {
        self.swarm.join("tasks.db")
    }

    fn db_bytes(&self) -> Vec<u8> {
        fs::read(self.db()).expect("read store")
    }

    /// A fresh, empty effects file, as the selector creates it before it starts the binary.
    fn effects(&self, name: &str) -> PathBuf {
        let path = self.dir.path().join(name);
        fs::write(&path, "").expect("effects file");
        path
    }

    /// Every file of the store: the database and its journal, WAL and shared-memory file when they exist.
    fn store_files(&self) -> Vec<(String, Vec<u8>)> {
        let mut files = Vec::new();
        for suffix in ["", "-journal", "-wal", "-shm"] {
            let mut name = self.db().file_name().expect("name").to_os_string();
            name.push(suffix);
            if let Ok(bytes) = fs::read(self.db().with_file_name(name)) {
                files.push((format!("tasks.db{suffix}"), bytes));
            }
        }
        files
    }
}

// ---- raw SQL access, independent of the route under test -------------------------------------------------------------------

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

async fn checkpoint(path: &Path) {
    exec(path, "PRAGMA wal_checkpoint(TRUNCATE)").await;
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

async fn integrity(path: &Path) -> String {
    let mut conn = connect(path).await;
    let value: String = sqlx::query_scalar("PRAGMA integrity_check")
        .fetch_one(&mut conn)
        .await
        .expect("integrity_check");
    conn.close().await.expect("close");
    value
}

/// The `transitions` rows of `task_id` as `from>to|reason`, in insertion order.
async fn audit_of(path: &Path, task_id: &str) -> Vec<String> {
    let mut conn = connect(path).await;
    let rows = sqlx::query(
        "SELECT coalesce(from_state, 'NULL') || '>' || to_state || '|' || reason FROM transitions WHERE task_id=? ORDER BY id",
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

/// Every row of every table of the store, values typed as stored: the whole content (read through SQLite, so a WAL is included).
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

/// Compares two store contents without printing a value (a row of this store is larger than a pipe).
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

// ---- the key canary ---------------------------------------------------------------------------------------------------------

/// A random 64-hex value from the kernel, made at run time. It is never written to a file or printed.
fn make_canary() -> String {
    let mut bytes = [0u8; 32];
    fs::File::open("/dev/urandom")
        .expect("open /dev/urandom")
        .read_exact(&mut bytes)
        .expect("read /dev/urandom");
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Fails (without printing the value) when `canary`, as text or as the 32 bytes it spells, is inside `bytes`.
fn assert_no_canary(canary: &str, what: &str, bytes: &[u8]) {
    let raw: Vec<u8> = (0..canary.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&canary[at..at + 2], 16).expect("hex"))
        .collect();
    let has = |needle: &[u8]| bytes.windows(needle.len()).any(|window| window == needle);
    assert!(
        !has(canary.as_bytes()) && !has(&raw),
        "the key canary is in {what}"
    );
}

/// The canary is in no store file, no effects file and none of the captured outputs of a scenario.
fn assert_clean(
    canary: &str,
    world: &World,
    effects: &[&Path],
    outputs: &[(&str, &[u8])],
    scenario: &str,
) {
    for (name, bytes) in world.store_files() {
        assert_no_canary(canary, &format!("{scenario}: {name}"), &bytes);
    }
    for path in effects {
        assert_no_canary(
            canary,
            &format!("{scenario}: the effects file"),
            &fs::read(path).unwrap_or_default(),
        );
    }
    for (name, bytes) in outputs {
        assert_no_canary(canary, &format!("{scenario}: {name}"), bytes);
    }
}

// ---- running the binary -----------------------------------------------------------------------------------------------------

/// What the selector sends the binary (`effects_path` included) as the stdin object.
fn request_of(world: &World, argv: &[String], effects: &Path) -> String {
    serde_json::json!({
        "agent": "A01",
        "script": "orch_status",
        "argv": argv,
        "root": world.root.display().to_string(),
        "env": {"SWARM_DIR": world.swarm.display().to_string()},
        "effects_path": effects.display().to_string(),
    })
    .to_string()
}

/// The binary's command: an empty environment but `PATH` and `HOME`, plus the canary in both key variables (the selector never
/// passes them; the route must not print or store them even if it inherited them).
fn binary_command(world: &World, canary: &str) -> Command {
    let mut command = Command::new(engine());
    command
        .current_dir(&world.root)
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for name in ["PATH", "HOME"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command
        .env("SWARM_SIGNING_KEY", canary)
        .env("SWARM_ED25519_KEY", canary);
    command
}

struct Run {
    code: i32,
    stdout: String,
    stderr: String,
}

impl Run {
    fn json(&self) -> serde_json::Value {
        serde_json::from_str(&self.stdout).unwrap_or_else(|error| {
            panic!(
                "stdout is not JSON ({error}): {}",
                self.stdout.chars().take(300).collect::<String>()
            )
        })
    }

    fn error_code(&self) -> Option<String> {
        self.json()["error"]["code"].as_str().map(str::to_string)
    }

    fn write_state(&self) -> Option<String> {
        self.json()["error"]["details"]["write_state"]
            .as_str()
            .map(str::to_string)
    }

    fn message(&self) -> String {
        self.json()["error"]["message"]
            .as_str()
            .unwrap_or_default()
            .to_string()
    }
}

fn send_request(child: &mut Child, request: &str) {
    child
        .stdin
        .take()
        .expect("stdin")
        .write_all(request.as_bytes())
        .expect("write request");
}

fn run_binary(world: &World, argv: &[String], effects: &Path, canary: &str) -> Run {
    let mut child = binary_command(world, canary)
        .spawn()
        .expect("spawn gsd_engine");
    send_request(&mut child, &request_of(world, argv, effects));
    let output = child.wait_with_output().expect("wait");
    Run {
        code: output.status.code().expect("exit code"),
        stdout: String::from_utf8(output.stdout).expect("utf8 stdout"),
        stderr: String::from_utf8(output.stderr).expect("utf8 stderr"),
    }
}

/// A manifest that is whole: `effects` with the one `task.transition` event and `post` with its three steps in order.
fn assert_whole_manifest(path: &Path, task: &str, what: &str) {
    let text = fs::read_to_string(path).expect("read manifest");
    let manifest: serde_json::Value = serde_json::from_str(&text)
        .unwrap_or_else(|error| panic!("{what}: the manifest is not JSON: {error}"));
    assert_eq!(manifest["effects"][0]["type"], "task.transition", "{what}");
    assert_eq!(manifest["effects"][0]["payload"]["task_id"], task, "{what}");
    let post: Vec<&str> = manifest["post"]
        .as_array()
        .expect("post list")
        .iter()
        .map(|step| step["kind"].as_str().expect("kind"))
        .collect();
    assert_eq!(
        post,
        ["live_refresh", "lease_settle", "inline_transition"],
        "{what}"
    );
}

// ---- race -------------------------------------------------------------------------------------------------------------------

const CONTENDERS: usize = 8;

/// 8 binaries on one store. The test holds the write lock (`BEGIN IMMEDIATE`) while they start and while their requests are sent, and
/// lets go a little after the last request, so they all meet the lock at the same moment. Returns each process's run and its effects
/// file.
fn race_round(world: &World, canary: &str) -> Vec<(Run, PathBuf)> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime");
    let mut holder = runtime.block_on(connect(&world.db()));
    runtime
        .block_on(sqlx::query("BEGIN IMMEDIATE").execute(&mut holder))
        .expect("take the write lock");
    // The contenders and this thread: every process is started before any request is sent.
    let barrier = Barrier::new(CONTENDERS + 1);
    let mut runs: Vec<Option<(Run, PathBuf)>> = (0..CONTENDERS).map(|_| None).collect();
    std::thread::scope(|scope| {
        let handles: Vec<_> = runs
            .iter_mut()
            .enumerate()
            .map(|(index, slot)| {
                let barrier = &barrier;
                scope.spawn(move || {
                    let effects = world.effects(&format!("effects-{index}.json"));
                    let argv = call_for("T-1", &format!("p{index}"));
                    let mut child = binary_command(world, canary)
                        .spawn()
                        .expect("spawn gsd_engine");
                    barrier.wait();
                    send_request(&mut child, &request_of(world, &argv, &effects));
                    let output = child.wait_with_output().expect("wait");
                    *slot = Some((
                        Run {
                            code: output.status.code().expect("exit code"),
                            stdout: String::from_utf8(output.stdout).expect("utf8 stdout"),
                            stderr: String::from_utf8(output.stderr).expect("utf8 stderr"),
                        },
                        effects,
                    ));
                })
            })
            .collect();
        barrier.wait();
        // The requests are on their way and the contenders are about to wait for the lock: let go of it.
        std::thread::sleep(Duration::from_millis(600));
        runtime
            .block_on(sqlx::query("ROLLBACK").execute(&mut holder))
            .expect("release the write lock");
        for handle in handles {
            handle.join().expect("contender thread");
        }
    });
    runtime.block_on(holder.close()).expect("close");
    runs.into_iter().map(|run| run.expect("ran")).collect()
}

#[test]
fn eight_binaries_racing_for_one_transition_have_one_winner_and_never_a_half_state() {
    for wal in [false, true] {
        for round in 0..20 {
            let world = World::new(wal);
            let canary = make_canary();
            let scenario = format!("{} round {round}", world.mode());
            let runs = race_round(&world, &canary);

            let winners: Vec<usize> = runs
                .iter()
                .enumerate()
                .filter(|(_, (run, _))| run.code == 0)
                .map(|(index, _)| index)
                .collect();
            assert_eq!(winners.len(), 1, "{scenario}: winners");
            let winner = winners[0];
            let (win, win_effects) = &runs[winner];
            assert_eq!(win.json()["status"], "ok", "{scenario}");
            assert_whole_manifest(win_effects, "T-1", &scenario);

            for (index, (run, effects)) in runs.iter().enumerate() {
                if index == winner {
                    continue;
                }
                // Python's own refusal of a transition the state machine does not allow: typed, and nothing written.
                assert_eq!(
                    run.code,
                    2,
                    "{scenario} contender {index}: {}",
                    run.stdout.chars().take(300).collect::<String>()
                );
                assert_eq!(
                    run.error_code().as_deref(),
                    Some("E-CONTRACT"),
                    "{scenario} contender {index}"
                );
                assert!(
                    run.message()
                        .contains("illegal transition IN_REVIEW → IN_REVIEW for T-1")
                        || run.message().contains("concurrent transition on T-1"),
                    "{scenario} contender {index}: {}",
                    run.message()
                );
                assert_eq!(
                    fs::metadata(effects).expect("effects file").len(),
                    0,
                    "{scenario} contender {index}: a refusal wrote a manifest"
                );
            }

            block(async {
                assert_eq!(
                    audit_of(&world.db(), "T-1").await,
                    [
                        "NULL>CREATED|created".to_string(),
                        format!("IN_PROGRESS>IN_REVIEW|p{winner}")
                    ],
                    "{scenario}: one audit row for the from-state, by the winner"
                );
                assert_eq!(
                    state_of(&world.db(), "T-1").await,
                    "IN_REVIEW",
                    "{scenario}"
                );
                assert_eq!(
                    state_of(&world.db(), "T-2").await,
                    "IN_PROGRESS",
                    "{scenario}: another task moved"
                );
                assert_eq!(integrity(&world.db()).await, "ok", "{scenario}");
            });
            let outputs: Vec<(String, Vec<u8>)> = runs
                .iter()
                .enumerate()
                .flat_map(|(index, (run, _))| {
                    [
                        (
                            format!("stdout of {index}"),
                            run.stdout.clone().into_bytes(),
                        ),
                        (
                            format!("stderr of {index}"),
                            run.stderr.clone().into_bytes(),
                        ),
                    ]
                })
                .collect();
            let outputs: Vec<(&str, &[u8])> = outputs
                .iter()
                .map(|(name, bytes)| (name.as_str(), bytes.as_slice()))
                .collect();
            let effect_paths: Vec<&Path> = runs.iter().map(|(_, path)| path.as_path()).collect();
            assert_clean(&canary, &world, &effect_paths, &outputs, &scenario);
        }
    }
}

// ---- lock held past the busy timeout ------------------------------------------------------------------------------------------

/// The route waits for the write lock as long as `WriterConfig::new` says (30 s, like Python's `sqlite3.connect(timeout=30)`), so
/// this scenario takes a little over 30 s of wall time whatever the load: the lock is held until the binary has answered, so the
/// outcome does not depend on timing, only on the lock.
fn lock_held_past_the_timeout(wal: bool) {
    let world = World::new(wal);
    let canary = make_canary();
    let scenario = format!("{} lock held", world.mode());
    let before_bytes = world.db_bytes();
    let before = block(content(&world.db()));
    let effects = world.effects("effects.json");
    let argv = call_for("T-1", "late");

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime");
    let mut holder = runtime.block_on(connect(&world.db()));
    runtime
        .block_on(sqlx::query("BEGIN IMMEDIATE").execute(&mut holder))
        .expect("take the write lock");
    // The holder connection is idle and keeps the lock while the binary waits for it and gives up.
    let outcome = run_binary(&world, &argv, &effects, &canary);
    assert_eq!(
        world.db_bytes(),
        before_bytes,
        "{scenario}: file bytes while the lock is held"
    );
    runtime
        .block_on(sqlx::query("ROLLBACK").execute(&mut holder))
        .expect("release the write lock");
    runtime.block_on(holder.close()).expect("close");

    assert_eq!(
        outcome.code,
        2,
        "{scenario}: {}",
        outcome.stdout.chars().take(300).collect::<String>()
    );
    assert_eq!(
        outcome.error_code().as_deref(),
        Some("E-INTERNAL"),
        "{scenario}"
    );
    assert_eq!(
        outcome.write_state().as_deref(),
        Some("not_started"),
        "{scenario}"
    );
    assert!(
        outcome.message().contains("BEGIN IMMEDIATE"),
        "{scenario}: {}",
        outcome.message()
    );
    assert_eq!(
        fs::metadata(&effects).expect("effects").len(),
        0,
        "{scenario}: a manifest for a write that did not start"
    );
    assert_same_content(&block(content(&world.db())), &before, &scenario);
    assert_eq!(world.db_bytes(), before_bytes, "{scenario}: file bytes");
    assert_clean(
        &canary,
        &world,
        &[effects.as_path()],
        &[
            ("stdout", outcome.stdout.as_bytes()),
            ("stderr", outcome.stderr.as_bytes()),
        ],
        &scenario,
    );

    // The lock was the only reason: the same call applies once it is gone.
    let again = run_binary(&world, &argv, &effects, &canary);
    assert_eq!(
        again.code,
        0,
        "{scenario}: {}",
        again.stdout.chars().take(300).collect::<String>()
    );
    assert_whole_manifest(&effects, "T-1", &scenario);
    block(async {
        assert_eq!(
            audit_of(&world.db(), "T-1").await,
            ["NULL>CREATED|created", "IN_PROGRESS>IN_REVIEW|late"],
            "{scenario}"
        );
        assert_eq!(integrity(&world.db()).await, "ok", "{scenario}");
    });
}

#[test]
fn a_lock_held_past_the_busy_timeout_is_e_internal_not_started_on_a_rollback_journal_store() {
    lock_held_past_the_timeout(false);
}

#[test]
fn a_lock_held_past_the_busy_timeout_is_e_internal_not_started_on_a_wal_store() {
    lock_held_past_the_timeout(true);
}

// ---- reply lost after the commit -----------------------------------------------------------------------------------------------

/// Starts the binary on `T-BIG` (a reply larger than a pipe), waits for the manifest to appear, and SIGKILLs it while it is blocked
/// printing: after the `COMMIT`, before the reply is whole. Returns what it had printed and its stderr.
fn lose_the_reply(
    world: &World,
    effects: &Path,
    argv: &[String],
    canary: &str,
) -> (Vec<u8>, Vec<u8>) {
    let mut child = binary_command(world, canary)
        .spawn()
        .expect("spawn gsd_engine");
    send_request(&mut child, &request_of(world, argv, effects));
    let deadline = Instant::now() + Duration::from_secs(300);
    loop {
        if fs::metadata(effects).map(|meta| meta.len()).unwrap_or(0) > 0 {
            break;
        }
        if let Some(status) = child.try_wait().expect("try_wait") {
            panic!("the binary ended ({status}) before it wrote a manifest");
        }
        if Instant::now() > deadline {
            child.kill().ok();
            child.wait().ok();
            panic!("the binary wrote no manifest within the deadline");
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(
        child.try_wait().expect("try_wait").is_none(),
        "the binary printed its whole reply and ended: the reply fits in a pipe, so the kill would not be before the print"
    );
    child.kill().expect("SIGKILL");
    let status = child.wait().expect("wait");
    assert_eq!(
        status.signal(),
        Some(9),
        "not killed by SIGKILL: {status:?}"
    );
    let (mut stdout, mut stderr) = (Vec::new(), Vec::new());
    child
        .stdout
        .take()
        .expect("stdout")
        .read_to_end(&mut stdout)
        .expect("drain stdout");
    child
        .stderr
        .take()
        .expect("stderr")
        .read_to_end(&mut stderr)
        .expect("drain stderr");
    (stdout, stderr)
}

fn reply_lost_after_commit(wal: bool) {
    let world = World::new(wal);
    let canary = make_canary();
    let scenario = format!("{} reply lost", world.mode());
    let argv = call_for("T-BIG", "lost");
    let effects = world.effects("effects.json");

    let (printed, stderr) = lose_the_reply(&world, &effects, &argv, &canary);

    // The reply never completed. The binary may or may not have started to print (the kill lands in the window between the
    // manifest and the end of the print either way), and a reply larger than a pipe can never be whole in one that nobody reads.
    assert!(
        serde_json::from_slice::<serde_json::Value>(&printed).is_err(),
        "{scenario}: the lost reply is a whole JSON document"
    );
    // The commit happened, and the manifest that follows it is whole.
    assert_whole_manifest(&effects, "T-BIG", &scenario);
    let committed = block(async {
        assert_eq!(
            audit_of(&world.db(), "T-BIG").await,
            ["NULL>CREATED|created", "IN_PROGRESS>IN_REVIEW|lost"],
            "{scenario}: exactly one row for the transition"
        );
        assert_eq!(
            state_of(&world.db(), "T-BIG").await,
            "IN_REVIEW",
            "{scenario}"
        );
        assert_eq!(
            state_of(&world.db(), "T-1").await,
            "IN_PROGRESS",
            "{scenario}: another task moved"
        );
        assert_eq!(integrity(&world.db()).await, "ok", "{scenario}");
        content(&world.db()).await
    });
    assert_clean(
        &canary,
        &world,
        &[effects.as_path()],
        &[("the printed prefix", &printed), ("stderr", &stderr)],
        &scenario,
    );

    // An explicit replay of the same call: the state machine refuses it (IN_REVIEW has no self-loop); nothing is doubled.
    let bytes_after_kill = (!wal).then(|| world.db_bytes());
    let replay_effects = world.effects("replay-effects.json");
    let replay = run_binary(&world, &argv, &replay_effects, &canary);
    assert_eq!(
        replay.code,
        2,
        "{scenario}: {}",
        replay.stdout.chars().take(300).collect::<String>()
    );
    assert_eq!(
        replay.error_code().as_deref(),
        Some("E-CONTRACT"),
        "{scenario}"
    );
    assert!(
        replay
            .message()
            .contains("illegal transition IN_REVIEW → IN_REVIEW for T-BIG"),
        "{scenario}: {}",
        replay.message()
    );
    assert_eq!(
        replay.write_state(),
        None,
        "{scenario}: Python's own refusal carries no write_state"
    );
    assert_eq!(
        fs::metadata(&replay_effects).expect("effects").len(),
        0,
        "{scenario}: a refused replay wrote a manifest"
    );
    assert_same_content(
        &block(content(&world.db())),
        &committed,
        &format!("{scenario}: the replay changed the store"),
    );
    if let Some(bytes) = bytes_after_kill {
        assert_eq!(
            world.db_bytes(),
            bytes,
            "{scenario}: the replay changed the store file"
        );
    }
    assert_clean(
        &canary,
        &world,
        &[replay_effects.as_path()],
        &[
            ("the replay's stdout", replay.stdout.as_bytes()),
            ("the replay's stderr", replay.stderr.as_bytes()),
        ],
        &scenario,
    );
}

#[test]
fn a_reply_lost_after_the_commit_leaves_one_row_and_the_replay_is_refused_on_a_rollback_journal_store(
) {
    reply_lost_after_commit(false);
}

#[test]
fn a_reply_lost_after_the_commit_leaves_one_row_and_the_replay_is_refused_on_a_wal_store() {
    reply_lost_after_commit(true);
}

// ---- foreign, corrupt and half-migrated stores -----------------------------------------------------------------------------------

/// Drops or alters something Python's `TaskStore` would create or migrate.
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

/// The reply of a store the route must leave to Python: `E-DEP`, `write_state` not started, exit 2, no manifest, the file as it was.
fn assert_left_to_python(world: &World, scenario: &str) {
    let before = world.db_bytes();
    let canary = make_canary();
    let effects = world.effects("effects.json");
    let run = run_binary(world, &call_for("T-1", "x"), &effects, &canary);
    assert_eq!(
        run.code,
        2,
        "{scenario}: {}",
        run.stdout.chars().take(300).collect::<String>()
    );
    assert_eq!(run.error_code().as_deref(), Some("E-DEP"), "{scenario}");
    assert_eq!(
        run.write_state().as_deref(),
        Some("not_started"),
        "{scenario}"
    );
    assert_eq!(
        fs::metadata(&effects).expect("effects").len(),
        0,
        "{scenario}: a manifest for a write that did not start"
    );
    assert_eq!(
        world.db_bytes(),
        before,
        "{scenario}: the route changed the store file"
    );
    assert_clean(
        &canary,
        world,
        &[effects.as_path()],
        &[
            ("stdout", run.stdout.as_bytes()),
            ("stderr", run.stderr.as_bytes()),
        ],
        scenario,
    );
}

#[test]
fn a_store_without_the_audit_table_or_the_envelope_columns_is_left_to_python_and_stays_byte_identical(
) {
    for wal in [false, true] {
        for (name, sql) in BROKEN_BY_SQL {
            let world = World::new(wal);
            block(async {
                exec(&world.db(), sql).await;
                checkpoint(&world.db()).await;
            });
            assert_left_to_python(&world, &format!("{} ({name})", world.mode()));
        }
    }
}

#[test]
fn a_database_of_another_program_a_file_that_is_no_database_and_a_damaged_tasks_page_are_left_to_python(
) {
    for wal in [false, true] {
        let good = template(wal);
        let mut clobbered = good.to_vec();
        clobbered[..16].copy_from_slice(b"NOT A SQLITE 3.0");
        let by_bytes: [(&str, Vec<u8>); 5] = [
            (
                "not a database",
                b"this is not a database file\n".repeat(200),
            ),
            ("an empty file", Vec::new()),
            ("only the first page", good[..4096].to_vec()),
            ("half the file", good[..good.len() / 2].to_vec()),
            ("a clobbered header", clobbered),
        ];
        for (name, bytes) in by_bytes {
            let world = World::new(wal);
            fs::write(world.db(), bytes).expect("write broken store");
            assert_left_to_python(&world, &format!("{} ({name})", world.mode()));
        }

        // A database that has other tables and a `tasks` table of its own shape.
        let world = World::new(wal);
        fs::remove_file(world.db()).expect("remove store");
        block(async {
            let mut conn = SqliteConnectOptions::new()
                .filename(world.db())
                .create_if_missing(true)
                .connect()
                .await
                .expect("create foreign db");
            for sql in [
                "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)",
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)",
            ] {
                sqlx::query(sql).execute(&mut conn).await.expect(sql);
            }
            conn.close().await.expect("close");
        });
        assert_left_to_python(
            &world,
            &format!("{} (a database of another program)", world.mode()),
        );

        // The schema reads (page 1 is fine) but the page of the `tasks` table is damaged: the first read of the row ends it.
        let world = World::new(wal);
        damage_root_page(&world, "tasks");
        assert_left_to_python(&world, &format!("{} (damaged tasks page)", world.mode()));
    }
}

/// Overwrites the root page of `table` with 0xff, in the main database file (the WAL is folded in first).
fn damage_root_page(world: &World, table: &str) {
    block(checkpoint(&world.db()));
    let page_size =
        usize::try_from(block(scalar_int(&world.db(), "PRAGMA page_size"))).expect("page size");
    let root = usize::try_from(block(scalar_int(
        &world.db(),
        &format!("SELECT rootpage FROM sqlite_master WHERE name='{table}'"),
    )))
    .expect("root page");
    let mut bytes = world.db_bytes();
    bytes[(root - 1) * page_size..root * page_size].fill(0xff);
    fs::write(world.db(), bytes).expect("damage the page");
}

/// A damaged `transitions` page is met by the audit INSERT, after the state UPDATE: `E-INTERNAL`, `write_state` final, and the UPDATE
/// is rolled back, so the store file is what it was and no state changed without its audit row.
#[test]
fn a_damaged_audit_page_is_final_and_rolls_the_state_update_back() {
    for wal in [false, true] {
        let world = World::new(wal);
        damage_root_page(&world, "transitions");
        let scenario = format!("{} (damaged transitions page)", world.mode());
        let before = world.db_bytes();
        let canary = make_canary();
        let effects = world.effects("effects.json");

        let run = run_binary(&world, &call_for("T-1", "x"), &effects, &canary);
        assert_eq!(
            run.code,
            2,
            "{scenario}: {}",
            run.stdout.chars().take(300).collect::<String>()
        );
        assert_eq!(
            run.error_code().as_deref(),
            Some("E-INTERNAL"),
            "{scenario}"
        );
        assert_eq!(run.write_state().as_deref(), Some("final"), "{scenario}");
        assert!(
            run.message().contains("audit insert"),
            "{scenario}: {}",
            run.message()
        );
        assert_eq!(
            fs::metadata(&effects).expect("effects").len(),
            0,
            "{scenario}: a manifest for a write that never committed"
        );
        assert_eq!(
            world.db_bytes(),
            before,
            "{scenario}: the state UPDATE was not rolled back"
        );
        assert_clean(
            &canary,
            &world,
            &[effects.as_path()],
            &[
                ("stdout", run.stdout.as_bytes()),
                ("stderr", run.stderr.as_bytes()),
            ],
            &scenario,
        );
    }
}

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
