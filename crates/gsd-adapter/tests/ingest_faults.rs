//! Faults of the host `orch_status --ingest` route (phase 14.5, unit U3.5f), through the real `gsd_engine` binary.
//!
//! Stores are built by the real Python `TaskStore`. Each scenario runs on a rollback-journal store and on a WAL store.
//! The binary is `CARGO_BIN_EXE_gsd_engine`. What is proved:
//!
//! - an invalid result is refused before any write (`E-DEP`, `write_state = not_started`) and the store file is unchanged;
//! - two concurrent ingests of one result serialize on the write lock: one applies, the other is a replay, and the task has one
//!   artifact row;
//! - a reply lost after the commit (SIGKILL once the effects manifest exists, while the binary is blocked printing a reply larger
//!   than a pipe) leaves exactly one artifact row, and a replay returns the stored reply without a second row;
//! - a key canary made at run time is in no reply, stderr, manifest or store file.

use std::fs;
use std::future::Future;
use std::io::{Read, Write};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Barrier, LazyLock};
use std::time::{Duration, Instant};

use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Connection, SqliteConnection};
use tempfile::TempDir;

const SEED_SCRIPT: &str = r#"
import sys
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
order = ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS")
for task_id in ("T-1", "T-BIG"):
    extra = {"inputs": ["x" * 400000]} if task_id == "T-BIG" else {}
    s.create(task_id=task_id, correlation_id="C-1", capability="code.backend", notes={"gates": []}, **extra)
    for state in order:
        s.transition(task_id, state)
s.conn.close()
"#;

fn repo_root() -> PathBuf {
    std::env::var_os("SWARM_REPO_ROOT").map_or_else(
        || Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
        PathBuf::from,
    )
}

fn engine() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_gsd_engine"))
}

fn block<F: Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime")
        .block_on(future)
}

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
        "no swarm/taskstore.py under {}: set SWARM_REPO_ROOT",
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
        .env("SWARM_DIR", dir.path().join(".swarm"));
    if wal {
        command.env("SWARM_TASKSTORE_WAL", "1");
    }
    let output = command.output().expect("run python3");
    assert!(
        output.status.success(),
        "python seed failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        !db.with_file_name("tasks.db-wal").exists(),
        "python left a WAL file; the copied database would be incomplete"
    );
    fs::read(&db).expect("read seeded db")
}

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

    fn effects(&self, name: &str) -> PathBuf {
        let path = self.dir.path().join(name);
        fs::write(&path, "").expect("effects file");
        path
    }

    fn result(&self, task: &str) -> PathBuf {
        let path = self.dir.path().join(format!("{task}.json"));
        let body = serde_json::json!({
            "task_id": task,
            "state": "IN_PROGRESS",
            "outputs": [{"kind": "code.backend", "uri": format!("file://{task}"), "version": "1", "digest": ""}]
        });
        fs::write(&path, body.to_string()).expect("result file");
        path
    }

    fn store_files(&self) -> Vec<(String, Vec<u8>)> {
        let mut files = Vec::new();
        for suffix in ["", "-journal", "-wal", "-shm"] {
            let mut name = self.db().file_name().expect("name").to_os_string();
            name.push(suffix);
            if let Ok(bytes) = fs::read(self.db().with_file_name(&name)) {
                files.push((format!("tasks.db{suffix}"), bytes));
            }
        }
        files
    }
}

fn argv_for(result: &Path) -> Vec<String> {
    ["--ingest", &result.display().to_string(), "--json"]
        .into_iter()
        .map(str::to_string)
        .collect()
}

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("raw connection")
}

async fn artifact_count(path: &Path, task_id: &str) -> i64 {
    let mut conn = connect(path).await;
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM artifacts WHERE task_id=?")
        .bind(task_id)
        .fetch_one(&mut conn)
        .await
        .expect("artifact count");
    conn.close().await.expect("close");
    count
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

fn make_canary() -> String {
    let mut bytes = [0u8; 32];
    fs::File::open("/dev/urandom")
        .expect("open /dev/urandom")
        .read_exact(&mut bytes)
        .expect("read /dev/urandom");
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

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
            &format!("{scenario}: effects"),
            &fs::read(path).unwrap_or_default(),
        );
    }
    for (name, bytes) in outputs {
        assert_no_canary(canary, &format!("{scenario}: {name}"), bytes);
    }
}

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

fn binary_command(world: &World, canary: &str) -> Command {
    let mut command = Command::new(engine());
    command
        .current_dir(&world.root)
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("GSD_RUST_BACKEND", "host");
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

fn invalid_result_writes_nothing(wal: bool) {
    let world = World::new(wal);
    let canary = make_canary();
    let scenario = format!("{} invalid result", world.mode());
    let before = world.db_bytes();
    let bad = world.dir.path().join("bad.json");
    fs::write(&bad, "not json").expect("bad result");
    let effects = world.effects("effects.json");
    let run = run_binary(&world, &argv_for(&bad), &effects, &canary);
    assert_eq!(
        run.code,
        2,
        "{scenario}: {}",
        run.stdout.chars().take(200).collect::<String>()
    );
    assert_eq!(run.json()["error"]["code"], "E-CONTRACT", "{scenario}");
    assert!(
        run.json()["error"]["message"]
            .as_str()
            .unwrap_or("")
            .contains("no task_id"),
        "{scenario}: {}",
        run.stdout.chars().take(200).collect::<String>()
    );
    assert_eq!(
        world.db_bytes(),
        before,
        "{scenario}: the store file changed"
    );
    let manifest = fs::read_to_string(&effects).unwrap_or_default();
    assert!(
        manifest.contains("task.result.rejected"),
        "{scenario}: the refusal did not record the rejection event"
    );
    assert_eq!(
        world.db_bytes(),
        before,
        "{scenario}: the store file changed"
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

fn one_apply_then_replay(wal: bool) {
    let world = World::new(wal);
    let canary = make_canary();
    let scenario = format!("{} replay", world.mode());
    let result = world.result("T-1");
    let argv = argv_for(&result);
    let first = run_binary(&world, &argv, &world.effects("first.json"), &canary);
    assert_eq!(
        first.code,
        0,
        "{scenario}: {}",
        first.stdout.chars().take(300).collect::<String>()
    );
    let applied = block(artifact_count(&world.db(), "T-1"));
    assert_eq!(
        applied, 1,
        "{scenario}: the first ingest did not write one artifact"
    );
    let second = run_binary(&world, &argv, &world.effects("second.json"), &canary);
    assert_eq!(
        second.code,
        0,
        "{scenario}: {}",
        second.stdout.chars().take(300).collect::<String>()
    );
    assert_eq!(
        second.json()["summary"],
        first.json()["summary"],
        "{scenario}: the replay reply differs"
    );
    assert_eq!(
        block(artifact_count(&world.db(), "T-1")),
        1,
        "{scenario}: the replay wrote another artifact"
    );
    assert_eq!(block(integrity(&world.db())), "ok", "{scenario}");
    assert_clean(
        &canary,
        &world,
        &[],
        &[
            ("first", first.stdout.as_bytes()),
            ("second", second.stdout.as_bytes()),
        ],
        &scenario,
    );
}

fn race_two(wal: bool) {
    let world = World::new(wal);
    let canary = make_canary();
    let scenario = format!("{} race", world.mode());
    let result = world.result("T-1");
    let argv = argv_for(&result);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("runtime");
    let mut holder = runtime.block_on(connect(&world.db()));
    runtime
        .block_on(sqlx::query("BEGIN IMMEDIATE").execute(&mut holder))
        .expect("take the write lock");
    let barrier = Barrier::new(3);
    let mut slots: [Option<Run>; 2] = [None, None];
    std::thread::scope(|scope| {
        let world = &world;
        let canary = &canary;
        let argv = &argv;
        let barrier = &barrier;
        let mut handles = Vec::new();
        for (index, slot) in slots.iter_mut().enumerate() {
            handles.push(scope.spawn(move || {
                let effects = world.effects(&format!("race-{index}.json"));
                let mut child = binary_command(world, canary).spawn().expect("spawn");
                barrier.wait();
                send_request(&mut child, &request_of(world, argv, &effects));
                let output = child.wait_with_output().expect("wait");
                *slot = Some(Run {
                    code: output.status.code().expect("exit code"),
                    stdout: String::from_utf8(output.stdout).expect("utf8"),
                    stderr: String::from_utf8(output.stderr).expect("utf8"),
                });
            }));
        }
        barrier.wait();
        std::thread::sleep(Duration::from_millis(50));
        runtime
            .block_on(sqlx::query("ROLLBACK").execute(&mut holder))
            .expect("release the lock");
        for handle in handles {
            handle.join().expect("contender");
        }
    });
    let runs: Vec<Run> = slots.into_iter().flatten().collect();
    assert!(
        runs.iter().any(|run| run.code == 0),
        "{scenario}: neither ingest applied"
    );
    assert_eq!(
        block(artifact_count(&world.db(), "T-1")),
        1,
        "{scenario}: the race wrote more than one artifact"
    );
    assert_eq!(block(integrity(&world.db())), "ok", "{scenario}");
    for run in &runs {
        assert_clean(
            &canary,
            &world,
            &[],
            &[("stdout", run.stdout.as_bytes())],
            &scenario,
        );
    }
}

fn lose_the_reply(
    world: &World,
    effects: &Path,
    argv: &[String],
    canary: &str,
) -> (Vec<u8>, Vec<u8>) {
    let mut child = binary_command(world, canary).spawn().expect("spawn");
    send_request(&mut child, &request_of(world, argv, effects));
    let deadline = Instant::now() + Duration::from_secs(60);
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
        "the binary printed its whole reply: the kill would not be before the print"
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

fn reply_lost_then_replay(wal: bool) {
    let world = World::new(wal);
    let canary = make_canary();
    let scenario = format!("{} reply lost", world.mode());
    let result = world.result("T-BIG");
    let argv = argv_for(&result);
    let effects = world.effects("effects.json");
    let (printed, stderr) = lose_the_reply(&world, &effects, &argv, &canary);
    assert!(
        serde_json::from_slice::<serde_json::Value>(&printed).is_err(),
        "{scenario}: the lost reply is a whole JSON document"
    );
    let manifest = fs::read_to_string(&effects).expect("manifest");
    serde_json::from_str::<serde_json::Value>(&manifest)
        .unwrap_or_else(|error| panic!("{scenario}: manifest is not JSON: {error}"));
    assert_eq!(
        block(artifact_count(&world.db(), "T-BIG")),
        1,
        "{scenario}: the commit did not leave one artifact"
    );
    let replay = run_binary(&world, &argv, &world.effects("replay.json"), &canary);
    assert_eq!(
        replay.code,
        0,
        "{scenario}: {}",
        replay.stdout.chars().take(300).collect::<String>()
    );
    assert!(
        replay.json()["summary"]
            .as_str()
            .unwrap_or("")
            .contains("T-BIG"),
        "{scenario}: the replay did not return the stored reply"
    );
    assert_eq!(
        block(artifact_count(&world.db(), "T-BIG")),
        1,
        "{scenario}: the replay wrote another artifact"
    );
    assert_eq!(block(integrity(&world.db())), "ok", "{scenario}");
    assert_clean(
        &canary,
        &world,
        &[effects.as_path()],
        &[
            ("printed", &printed),
            ("stderr", &stderr),
            ("replay", replay.stdout.as_bytes()),
        ],
        &scenario,
    );
}

#[test]
fn an_invalid_result_writes_nothing_on_a_rollback_journal_store() {
    invalid_result_writes_nothing(false);
}

#[test]
fn an_invalid_result_writes_nothing_on_a_wal_store() {
    invalid_result_writes_nothing(true);
}

#[test]
fn a_replay_returns_the_stored_reply_and_does_not_double_the_artifact_on_a_rollback_journal_store()
{
    one_apply_then_replay(false);
}

#[test]
fn a_replay_returns_the_stored_reply_and_does_not_double_the_artifact_on_a_wal_store() {
    one_apply_then_replay(true);
}

#[test]
fn two_concurrent_ingests_of_one_result_leave_one_artifact_on_a_rollback_journal_store() {
    race_two(false);
}

#[test]
fn two_concurrent_ingests_of_one_result_leave_one_artifact_on_a_wal_store() {
    race_two(true);
}

#[test]
fn a_reply_lost_after_the_commit_replays_once_on_a_rollback_journal_store() {
    reply_lost_then_replay(false);
}

#[test]
fn a_reply_lost_after_the_commit_replays_once_on_a_wal_store() {
    reply_lost_then_replay(true);
}
