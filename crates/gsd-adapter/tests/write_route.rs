//! The host `orch_status --transition` route (phase 14.5, unit U3.2a), through the real `gsd_engine` binary.
//!
//! Stores are built by the REAL Python `TaskStore` (the schema is Python's) and seeded with the task rows of the 351 oracle vectors of
//! `gsd-route` (`tests/vectors/transition.json`, produced by the real `TaskStore.transition`). The oracle of every reply is Python's own
//! `scripts/orch_status.py` run on a copy of the same store: the route prints what Python prints, byte for byte, with the two values
//! that depend on the clock masked (`updated_at` and `notes.verdicts_since`, Design 6 of the plan), and leaves the same rows behind.
//!
//! What is proved here:
//!
//! - a sample of the vectors (legal and illegal transitions, the gate-task `E-POLICY`, an unknown task, the rework cap, the attempt cap)
//!   gives Python's stdout, exit code and store content; a case the route must leave to Python is refused with `write_state =
//!   "not_started"` and the store file is byte-identical afterwards, as it is after every refusal;
//! - the effects manifest is written after the commit with the contents and order of Design 8, and only then: a refusal writes
//!   none, a dry run writes an empty one;
//! - every argv outside the grammar, the sandbox backend, a missing `effects_path`, a missing or unmigrated store and an unreadable
//!   `latest_correlation` are refused before the first write;
//! - the `git rev-parse` child of the swarm-directory resolution gets no environment but `PATH` and `HOME`.
//!
//! The repository root is `SWARM_REPO_ROOT` or two levels above this crate. Temporary files follow `TMPDIR`.

use std::fs;
use std::future::Future;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::time::{SystemTime, UNIX_EPOCH};

use gsd_route::transition_args::parse_transition_args;
use gsd_route::{obj_get, parse_json, Fields, Json};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{Column, ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};
use tempfile::TempDir;

const VECTORS: &str = include_str!("../../gsd-route/tests/vectors/transition.json");

/// The Python-built empty store: the schema and the migrations are Python's.
const TEMPLATE_SCRIPT: &str =
    "import sys\nfrom swarm.taskstore import TaskStore\nTaskStore(sys.argv[1]).conn.close()\n";

static TEMPLATE: LazyLock<Vec<u8>> = LazyLock::new(|| {
    let dir = TempDir::new().expect("tempdir");
    let db = dir.path().join("tasks.db");
    let output = Command::new("python3")
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

// ---- vector access ----------------------------------------------------------------------------------------------------

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

fn row_of(value: &Json) -> Fields {
    match value {
        Json::Obj(fields) => fields.clone(),
        other => panic!("not an object: {other:?}"),
    }
}

/// One vector the route can serve: its seed row (absent for an unknown task) and the argv that makes the call.
struct Case {
    id: String,
    group: String,
    row: Option<Fields>,
    argv: Vec<String>,
    to: String,
    rust_not_started: bool,
    outcome: String,
}

fn cases() -> Vec<Case> {
    let doc = parse_json(VECTORS).expect("vectors are JSON");
    let Json::Arr(items) = member(&doc, "cases") else {
        panic!("cases is not a list");
    };
    items
        .iter()
        .filter_map(|case| {
            let request = member(case, "request");
            if text(member(request, "actor")) != "A01" || !flag(member(request, "manual")) {
                return None;
            }
            let mut argv = vec![
                "--json".to_string(),
                "--transition".to_string(),
                text(member(request, "task_id")).to_string(),
                text(member(request, "to")).to_string(),
            ];
            let reason = text(member(request, "reason"));
            if !reason.is_empty() {
                argv.extend(["--reason".to_string(), reason.to_string()]);
            }
            // The vectors the route's grammar does not serve (an empty state name) are the argv-mirror tests' business.
            parse_transition_args(&argv)?;
            Some(Case {
                id: text(member(case, "id")).to_string(),
                group: text(member(case, "group")).to_string(),
                row: match member(case, "task") {
                    Json::Null => None,
                    task => Some(row_of(task)),
                },
                argv,
                to: text(member(request, "to")).to_string(),
                rust_not_started: flag(member(case, "rust_not_started")),
                outcome: text(member(member(case, "expect"), "outcome")).to_string(),
            })
        })
        .collect()
}

// ---- stores ----------------------------------------------------------------------------------------------------------

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("raw connection")
}

/// Inserts one `tasks` row with the values exactly as given.
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

/// One SQLite value with its storage class; a REAL compares by bits and a masked clock value compares equal to any other.
#[derive(Debug, Clone, PartialEq)]
enum Cell {
    Null,
    Int(i64),
    Real(u64),
    Text(String),
    Blob(Vec<u8>),
    Clock,
}

/// Every row of the four tables and `sqlite_sequence`, values typed as stored; `tasks.updated_at` and `transitions.ts` are masked,
/// and so is the number after `verdicts_since` in a text.
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
            for (index, column) in row.columns().iter().enumerate() {
                let raw = row.try_get_raw(index).expect("raw value");
                let clock = matches!(
                    (table, column.name()),
                    ("tasks", "updated_at") | ("transitions", "ts")
                );
                line.push(if raw.is_null() {
                    Cell::Null
                } else if clock {
                    Cell::Clock
                } else {
                    match raw.type_info().name() {
                        "INTEGER" => Cell::Int(row.get(index)),
                        "REAL" => Cell::Real(row.get::<f64, _>(index).to_bits()),
                        "TEXT" => Cell::Text(mask(&row.get::<String, _>(index))),
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

/// Replaces the number after `"updated_at": ` and `"verdicts_since": ` (also inside the escaped `notes` text of a reply) with `<clock>`:
/// the only two values of a reply that depend on the clock (Design 6).
fn mask(text: &str) -> String {
    let mut out = text.to_string();
    for key in [
        "\"updated_at\": ",
        "\"verdicts_since\": ",
        "\\\"verdicts_since\\\": ",
    ] {
        let mut from = 0;
        while let Some(found) = out[from..].find(key) {
            let start = from + found + key.len();
            let length = out[start..]
                .find(|c: char| !(c.is_ascii_digit() || matches!(c, '.' | 'e' | 'E' | '+' | '-')))
                .unwrap_or(out.len() - start);
            out.replace_range(start..start + length, "<clock>");
            from = start + "<clock>".len();
        }
    }
    out
}

/// A scratch repository: `<dir>/work/.swarm/tasks.db` seeded from the Python template, and an empty `effects.json` the way the
/// selector creates one.
struct World {
    dir: TempDir,
    root: PathBuf,
    swarm: PathBuf,
    effects: PathBuf,
}

impl World {
    fn new(rows: &[Fields]) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let root = dir.path().join("work");
        let swarm = root.join(".swarm");
        fs::create_dir_all(&swarm).expect("swarm dir");
        fs::write(swarm.join("tasks.db"), &*TEMPLATE).expect("copy template");
        for row in rows {
            block(insert_task(&swarm.join("tasks.db"), row));
        }
        let effects = dir.path().join("effects.json");
        fs::write(&effects, "").expect("effects file");
        Self {
            dir,
            root,
            swarm,
            effects,
        }
    }

    fn db(&self) -> PathBuf {
        self.swarm.join("tasks.db")
    }

    fn db_bytes(&self) -> Vec<u8> {
        fs::read(self.db()).expect("read store")
    }

    fn manifest(&self) -> String {
        fs::read_to_string(&self.effects).expect("read manifest")
    }
}

// ---- running the two engines ---------------------------------------------------------------------------------------------

struct Run {
    code: i32,
    stdout: String,
    stderr: String,
}

impl Run {
    fn json(&self) -> serde_json::Value {
        serde_json::from_str(&self.stdout)
            .unwrap_or_else(|e| panic!("stdout is not JSON ({e}): {}", self.stdout))
    }

    fn write_state(&self) -> Option<String> {
        self.json()["error"]["details"]["write_state"]
            .as_str()
            .map(str::to_string)
    }

    fn error_code(&self) -> Option<String> {
        self.json()["error"]["code"].as_str().map(str::to_string)
    }
}

/// What the selector sends the binary, and what it leaves in the environment: `PATH` and `HOME` only.
struct Call<'a> {
    world: &'a World,
    argv: &'a [&'a str],
    effects_path: bool,
    env: Vec<(&'a str, String)>,
    swarm_dir: bool,
    extra_process_env: Vec<(&'a str, String)>,
}

impl<'a> Call<'a> {
    fn new(world: &'a World, argv: &'a [&'a str]) -> Self {
        Self {
            world,
            argv,
            effects_path: true,
            env: Vec::new(),
            swarm_dir: true,
            extra_process_env: Vec::new(),
        }
    }

    fn run(&self) -> Run {
        let mut env = serde_json::Map::new();
        if self.swarm_dir {
            env.insert(
                "SWARM_DIR".to_string(),
                self.world.swarm.display().to_string().into(),
            );
        }
        for (name, value) in &self.env {
            env.insert((*name).to_string(), value.clone().into());
        }
        let mut request = serde_json::json!({
            "agent": "A01",
            "script": "orch_status",
            "argv": self.argv,
            "root": self.world.root.display().to_string(),
            "env": env,
        });
        if self.effects_path {
            request["effects_path"] = self.world.effects.display().to_string().into();
        }
        let mut command = Command::new(env!("CARGO_BIN_EXE_gsd_engine"));
        command
            .current_dir(&self.world.root)
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for name in ["PATH", "HOME"] {
            if let Some(value) = std::env::var_os(name) {
                command.env(name, value);
            }
        }
        for (name, value) in &self.extra_process_env {
            command.env(name, value);
        }
        let mut child = command.spawn().expect("spawn gsd_engine");
        child
            .stdin
            .take()
            .expect("stdin")
            .write_all(request.to_string().as_bytes())
            .expect("write request");
        let output = child.wait_with_output().expect("wait");
        Run {
            code: output.status.code().expect("exit code"),
            stdout: String::from_utf8(output.stdout).expect("utf8 stdout"),
            stderr: String::from_utf8(output.stderr).expect("utf8 stderr"),
        }
    }
}

fn rust(world: &World, argv: &[&str]) -> Run {
    Call::new(world, argv).run()
}

/// Python's `scripts/orch_status.py` on the world's store: the oracle.
fn python(world: &World, argv: &[String]) -> Run {
    let output = Command::new("python3")
        .arg(repo_root().join("scripts/orch_status.py"))
        .args(argv)
        .current_dir(&world.root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", std::env::var_os("HOME").unwrap_or_default())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("SWARM_PURE_PYTHON", "1")
        .env("SWARM_DIR", &world.swarm)
        .output()
        .expect("run python3");
    Run {
        code: output.status.code().expect("exit code"),
        stdout: String::from_utf8(output.stdout).expect("utf8 stdout"),
        stderr: String::from_utf8(output.stderr).expect("utf8 stderr"),
    }
}

fn argv_strings(argv: &[&str]) -> Vec<String> {
    argv.iter().map(|item| (*item).to_string()).collect()
}

/// A seed row for `state`, taken from the matrix vector of that state (every column as Python's `create` writes it).
fn row_in(state: &str, task_id: &str) -> Fields {
    let doc = parse_json(VECTORS).expect("vectors are JSON");
    let Json::Arr(items) = member(&doc, "cases") else {
        panic!("cases is not a list");
    };
    let case = items
        .iter()
        .find(|case| text(member(case, "id")) == format!("matrix-{state}-{state}"))
        .unwrap_or_else(|| panic!("no matrix vector for {state}"));
    row_of(member(case, "task"))
        .into_iter()
        .map(|(name, value)| match name.as_str() {
            "task_id" => (name, Json::Str(task_id.to_string())),
            _ => (name, value),
        })
        .collect()
}

fn set(row: &mut Fields, column: &str, value: Json) {
    match row.iter_mut().find(|(name, _)| name == column) {
        Some(entry) => entry.1 = value,
        None => row.push((column.to_string(), value)),
    }
}

fn rows_of(world: &World, table: &str) -> usize {
    block(async {
        let mut conn = connect(&world.db()).await;
        let count: i64 = sqlx::query_scalar(&format!("SELECT COUNT(*) FROM {table}"))
            .fetch_one(&mut conn)
            .await
            .expect("count");
        conn.close().await.expect("close");
        usize::try_from(count).expect("count")
    })
}

// ---- parity with Python ---------------------------------------------------------------------------------------------------

/// Compares one vector on both engines. The route either refuses before the first write (and leaves the store byte-identical, and
/// Python answers) or prints what Python prints and leaves what Python leaves.
fn compare_with_python(case: &Case) -> Result<(), String> {
    let rows: Vec<Fields> = case.row.iter().cloned().collect();
    let (rust_world, python_world) = (World::new(&rows), World::new(&rows));
    let before = rust_world.db_bytes();
    let argv: Vec<&str> = case.argv.iter().map(String::as_str).collect();
    let ours = rust(&rust_world, &argv);
    let id = &case.id;
    if ours.code == 2 && ours.write_state().as_deref() == Some("not_started") {
        // A legal APPROVED needs the approval facts the host cannot read yet (unit U3.4d): not started, whatever the vector says.
        if !case.rust_not_started && case.outcome != "exception" && case.to != "APPROVED" {
            return Err(format!(
                "{id}: refused as not_started although the vector is served: {}",
                ours.stdout
            ));
        }
        if rust_world.db_bytes() != before {
            return Err(format!(
                "{id}: a not_started refusal changed the store file"
            ));
        }
        if !rust_world.manifest().is_empty() {
            return Err(format!("{id}: a refusal wrote a manifest"));
        }
        return Ok(());
    }
    if case.rust_not_started || case.outcome == "exception" {
        return Err(format!(
            "{id}: served although Python cannot be matched: {}",
            ours.stdout
        ));
    }
    let theirs = python(&python_world, &case.argv);
    if (ours.code, mask(&ours.stdout)) != (theirs.code, mask(&theirs.stdout)) {
        return Err(format!(
            "{id}: reply differs\n rust   {} {}\n python {} {}\n python stderr: {}",
            ours.code,
            mask(&ours.stdout),
            theirs.code,
            mask(&theirs.stdout),
            theirs.stderr
        ));
    }
    if block(content(&rust_world.db())) != block(content(&python_world.db())) {
        return Err(format!("{id}: the stores differ after the call"));
    }
    if ours.code == 2 && rust_world.db_bytes() != before {
        return Err(format!("{id}: a refusal changed the store file"));
    }
    if ours.code == 2 && !rust_world.manifest().is_empty() {
        return Err(format!("{id}: a refusal wrote a manifest"));
    }
    if ours.code == 0 && rust_world.manifest().is_empty() {
        return Err(format!("{id}: a committed transition wrote no manifest"));
    }
    Ok(())
}

#[test]
fn a_sample_of_the_vectors_replies_what_python_prints_and_leaves_the_same_rows() {
    // Every Nth vector of each group: the whole matrix is walked at a stride, the small groups nearly whole.
    let strides = [
        ("matrix", 7),
        ("rework", 3),
        ("claim", 3),
        ("approval", 3),
        ("gate", 2),
        ("state-name", 2),
        ("notes", 2),
        ("unknown-task", 2),
        ("row", 2),
        ("rust-refuses", 2),
    ];
    let all = cases();
    let mut sample = Vec::new();
    for (group, stride) in strides {
        sample.extend(
            all.iter()
                .filter(|case| case.group == group)
                .step_by(stride),
        );
    }
    assert!(sample.len() >= 60, "sample of {}", sample.len());
    // The sample must reach every kind of answer: ok, a Python error, an exception Python cannot be matched on.
    for outcome in ["ok", "error", "exception"] {
        assert!(
            sample.iter().any(|case| case.outcome == outcome),
            "no {outcome} vector in the sample"
        );
    }
    LazyLock::force(&TEMPLATE);
    let failures: Vec<String> = std::thread::scope(|scope| {
        let handles: Vec<_> = sample
            .chunks(sample.len().div_ceil(8))
            .map(|chunk| {
                scope.spawn(move || {
                    chunk
                        .iter()
                        .filter_map(|case| compare_with_python(case).err())
                        .collect::<Vec<_>>()
                })
            })
            .collect();
        handles
            .into_iter()
            .flat_map(|handle| handle.join().expect("worker"))
            .collect()
    });
    assert!(
        failures.is_empty(),
        "{} failures:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn a_transition_the_vectors_do_not_cover_matches_python_too() {
    // Ids and reasons with quotes, unicode and spaces, and a task id that is not a safe id: Python's JSON escaping and `unknown task`.
    let mut row = row_in("CREATED", "T-edge");
    set(&mut row, "title", Json::Str("t".to_string()));
    for argv in [
        vec![
            "--json",
            "--transition",
            "T-edge",
            "VALIDATED",
            "--reason",
            "say \"hi\" \\ é ü \u{1f600}",
        ],
        vec![
            "--json",
            "--transition",
            "T-edge",
            "CANCELLED",
            "--reason=line one\nline two",
        ],
        vec!["--json", "--transition", "no such task é", "DONE"],
        vec![
            "--json",
            "--transition",
            "T-edge",
            "DONE",
            "--task-id",
            "T-ctx",
            "--correlation-id",
            "C-1",
        ],
    ] {
        let (ours_world, theirs_world) = (World::new(&[row.clone()]), World::new(&[row.clone()]));
        let ours = rust(&ours_world, &argv);
        let theirs = python(&theirs_world, &argv_strings(&argv));
        assert_eq!(
            (ours.code, mask(&ours.stdout)),
            (theirs.code, mask(&theirs.stdout)),
            "{argv:?}"
        );
        assert_eq!(
            block(content(&ours_world.db())),
            block(content(&theirs_world.db())),
            "{argv:?}"
        );
    }
}

#[test]
fn the_reply_carries_the_clock_of_the_call() {
    let world = World::new(&[row_in("CREATED", "T-clock")]);
    let before = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_secs_f64();
    let run = rust(&world, &["--json", "--transition", "T-clock", "VALIDATED"]);
    let after = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_secs_f64();
    let updated = run.json()["task"]["updated_at"]
        .as_f64()
        .expect("updated_at");
    assert!(
        (before..=after).contains(&updated),
        "{before} <= {updated} <= {after}"
    );
}

// ---- the effects manifest ---------------------------------------------------------------------------------------------------

#[test]
fn a_committed_transition_writes_the_manifest_in_the_order_of_design_8() {
    let world = World::new(&[row_in("CREATED", "T-fx")]);
    let run = rust(
        &world,
        &[
            "--json",
            "--transition",
            "T-fx",
            "VALIDATED",
            "--reason",
            "go ahead",
        ],
    );
    assert_eq!(run.code, 0, "{}", run.stdout);
    let task = "T-fx";
    let expected = format!(
        "{{\"effects\": [{{\"type\": \"task.transition\", \"payload\": {{\"task_id\": \"{task}\", \"state\": \"VALIDATED\", \"reason\": \"go ahead\"}}, \"task_id\": \"{task}\"}}], \"post\": [{{\"kind\": \"live_refresh\"}}, {{\"kind\": \"lease_settle\", \"task_id\": \"{task}\"}}, {{\"kind\": \"inline_transition\", \"task_id\": \"{task}\", \"from\": \"CREATED\", \"reason\": \"go ahead\"}}]}}"
    );
    assert_eq!(world.manifest(), expected);
    // The manifest is private to the caller, and no temporary file is left beside it.
    let mode = fs::metadata(&world.effects)
        .expect("metadata")
        .permissions()
        .mode()
        & 0o777;
    assert_eq!(mode, 0o600);
    let names: Vec<_> = fs::read_dir(world.dir.path())
        .expect("read dir")
        .map(|entry| {
            entry
                .expect("entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    assert!(
        names
            .iter()
            .all(|name| name == "effects.json" || name == "work"),
        "{names:?}"
    );
    assert_eq!(rows_of(&world, "transitions"), 1);
}

#[test]
fn an_empty_reason_is_manual_for_the_audit_and_the_inline_event_but_empty_in_the_payload() {
    let world = World::new(&[row_in("CREATED", "T-fx")]);
    let run = rust(&world, &["--json", "--transition", "T-fx", "VALIDATED"]);
    assert_eq!(run.code, 0);
    let manifest: serde_json::Value =
        serde_json::from_str(&world.manifest()).expect("manifest is JSON");
    assert_eq!(manifest["effects"][0]["payload"]["reason"], "");
    assert_eq!(manifest["post"][2]["reason"], "manual");
    let audit: Vec<String> = block(async {
        let mut conn = connect(&world.db()).await;
        let reasons = sqlx::query_scalar("SELECT reason FROM transitions")
            .fetch_all(&mut conn)
            .await
            .expect("reasons");
        conn.close().await.expect("close");
        reasons
    });
    assert_eq!(audit, ["manual"]);
}

#[test]
fn an_auto_escalation_names_the_state_before_the_call_and_the_state_after_it() {
    let mut row = row_in("IN_REVIEW", "T-esc");
    set(&mut row, "rework_loops", Json::Int(2));
    let world = World::new(&[row]);
    let run = rust(
        &world,
        &[
            "--json",
            "--transition",
            "T-esc",
            "CHANGES_REQUESTED",
            "--reason",
            "again",
        ],
    );
    assert_eq!(run.code, 0, "{}", run.stdout);
    assert_eq!(run.json()["task"]["state"], "ESCALATED");
    assert_eq!(run.json()["summary"], "T-esc → ESCALATED");
    let manifest: serde_json::Value =
        serde_json::from_str(&world.manifest()).expect("manifest is JSON");
    assert_eq!(manifest["post"][2]["from"], "IN_REVIEW");
    assert_eq!(manifest["effects"][0]["payload"]["state"], "ESCALATED");
    assert_eq!(manifest["effects"][0]["payload"]["reason"], "again");
    assert_eq!(rows_of(&world, "transitions"), 2);
}

#[test]
fn a_refusal_writes_no_manifest_and_leaves_the_store_file_byte_identical() {
    let mut gate = row_in("IN_PROGRESS", "T-gate");
    set(
        &mut gate,
        "notes",
        Json::Str("{\"gate\": \"review\"}".to_string()),
    );
    let world = World::new(&[row_in("CREATED", "T-ill"), gate]);
    let before = world.db_bytes();
    for (argv, code, message) in [
        (
            ["--json", "--transition", "T-ill", "DONE"],
            "E-CONTRACT",
            "E-CONTRACT: illegal transition CREATED \u{2192} DONE for T-ill",
        ),
        (
            ["--json", "--transition", "T-gate", "IN_REVIEW"],
            "E-POLICY",
            "E-POLICY: T-gate is a gate task: only a task.result ingest may move it to IN_REVIEW (it checks that the gate script ran during this lease and that the agent's verdicts match the recorded rows)",
        ),
        (
            ["--json", "--transition", "T-none", "DONE"],
            "E-INPUT",
            "E-INPUT: unknown task T-none",
        ),
    ] {
        let run = rust(&world, &argv);
        assert_eq!(run.code, 2, "{argv:?}: {}", run.stdout);
        assert_eq!(run.error_code().as_deref(), Some(code));
        assert_eq!(run.json()["error"]["message"], message, "{}", run.stdout);
        // Python's own refusal: it carries no write_state, so its bytes are Python's.
        assert_eq!(run.write_state(), None);
        assert_eq!(world.db_bytes(), before, "{argv:?}");
        assert_eq!(world.manifest(), "", "{argv:?}");
    }
}

#[test]
fn a_dry_run_writes_nothing_but_an_empty_manifest_and_matches_python_even_for_an_unknown_task() {
    let world = World::new(&[row_in("CREATED", "T-dry")]);
    let before = world.db_bytes();
    for task in ["T-dry", "T-unknown"] {
        let argv = ["--json", "--dry-run", "--transition", task, "DONE"];
        let ours = rust(&world, &argv);
        let theirs = python(
            &World::new(&[row_in("CREATED", "T-dry")]),
            &argv_strings(&argv),
        );
        assert_eq!(
            (ours.code, &ours.stdout),
            (theirs.code, &theirs.stdout),
            "{task}"
        );
        assert_eq!(ours.code, 0);
        assert_eq!(world.db_bytes(), before);
        assert_eq!(world.manifest(), "{\"effects\": [], \"post\": []}");
    }
}

// ---- refusals before the first write ----------------------------------------------------------------------------------------

/// A legal IN_REVIEW → APPROVED and the argv shapes the route does not serve, none of which may touch the store.
#[test]
fn what_rust_cannot_prove_is_refused_before_the_first_write() {
    let world = World::new(&[row_in("IN_REVIEW", "T-rev"), row_in("CREATED", "T-new")]);
    let before = world.db_bytes();
    let cases: [(&[&str], &str); 11] = [
        (&["--json", "--transition", "T-rev", "APPROVED"], "APPROVED"),
        (
            &["--json", "--transition", "T-new", "NOT_A_STATE"],
            "state name",
        ),
        (&["--transition", "T-new", "VALIDATED"], "no --json"),
        (&["--json", "--trans", "T-new", "VALIDATED"], "abbreviation"),
        (&["--json", "--tran", "T-new", "VALIDATED"], "abbreviation"),
        (&["--json", "--transition", "T-new"], "one argument"),
        (
            &["--json", "--transition", "T-new", "VALIDATED", "EXTRA"],
            "three arguments",
        ),
        (
            &[
                "--json",
                "--transition",
                "T-new",
                "VALIDATED",
                "--ingest",
                "r.json",
            ],
            "ingest",
        ),
        (
            &["--json", "--transition", "T-new", "VALIDATED", "--advisory"],
            "advisory",
        ),
        (
            &[
                "--json",
                "--transition",
                "T-new",
                "VALIDATED",
                "--frobnicate",
            ],
            "unknown flag",
        ),
        (
            &["--json", "--transition", "T-new", "VALIDATED", "--reason"],
            "reason without a value",
        ),
    ];
    for (argv, label) in cases {
        let run = rust(&world, argv);
        assert_eq!(run.code, 2, "{label}: {}", run.stdout);
        assert_eq!(
            run.error_code().as_deref(),
            Some("E-DEP"),
            "{label}: {}",
            run.stdout
        );
        assert_eq!(run.write_state().as_deref(), Some("not_started"), "{label}");
        assert_eq!(world.db_bytes(), before, "{label}");
        assert_eq!(world.manifest(), "", "{label}");
    }
}

/// Without the `sandbox-write` feature the sandbox backend has no write guest: the write is refused before it starts, and the message
/// names the missing feature. With the feature the guest serves it (tests/write_route_sandbox_faults.rs).
#[cfg(not(feature = "sandbox-write"))]
#[test]
fn the_sandbox_backend_does_not_serve_a_write_until_its_own_unit() {
    let world = World::new(&[row_in("CREATED", "T-sb")]);
    let before = world.db_bytes();
    let mut call = Call::new(&world, &["--json", "--transition", "T-sb", "VALIDATED"]);
    call.extra_process_env
        .push(("GSD_RUST_BACKEND", "sandbox".to_string()));
    let run = call.run();
    assert_eq!(run.code, 2, "{}", run.stdout);
    assert_eq!(run.error_code().as_deref(), Some("E-DEP"));
    assert_eq!(run.write_state().as_deref(), Some("not_started"));
    assert!(
        run.json()["error"]["message"]
            .as_str()
            .is_some_and(|message| message.contains("sandbox-write")),
        "{}",
        run.stdout
    );
    assert_eq!(world.db_bytes(), before);
    assert_eq!(world.manifest(), "");
}

#[test]
fn a_transition_without_an_effects_path_is_refused_but_a_dry_run_needs_none() {
    let world = World::new(&[row_in("CREATED", "T-ep")]);
    let before = world.db_bytes();
    let mut call = Call::new(&world, &["--json", "--transition", "T-ep", "VALIDATED"]);
    call.effects_path = false;
    let run = call.run();
    assert_eq!(run.error_code().as_deref(), Some("E-DEP"), "{}", run.stdout);
    assert_eq!(run.write_state().as_deref(), Some("not_started"));
    assert_eq!(world.db_bytes(), before);

    let mut dry = Call::new(
        &world,
        &["--json", "--dry-run", "--transition", "T-ep", "VALIDATED"],
    );
    dry.effects_path = false;
    assert_eq!(dry.run().code, 0);
}

#[test]
fn a_missing_or_unmigrated_store_is_left_to_python_to_create_and_migrate() {
    let world = World::new(&[]);
    // No tasks.db at all: Python would create it; Rust must not.
    fs::remove_file(world.db()).expect("remove store");
    let run = rust(&world, &["--json", "--transition", "T-x", "VALIDATED"]);
    assert_eq!(
        run.write_state().as_deref(),
        Some("not_started"),
        "{}",
        run.stdout
    );
    assert!(!world.db().exists(), "the route created a store");
    // An empty SQLite file has no schema: the route runs no DDL.
    fs::write(world.db(), "").expect("empty store");
    let run = rust(
        &world,
        &["--json", "--dry-run", "--transition", "T-x", "VALIDATED"],
    );
    assert_eq!(
        run.write_state().as_deref(),
        Some("not_started"),
        "{}",
        run.stdout
    );
    assert_eq!(fs::read(world.db()).expect("read"), Vec::<u8>::new());
}

#[test]
fn a_latest_correlation_python_cannot_read_is_left_to_python() {
    let world = World::new(&[row_in("CREATED", "T-lc")]);
    fs::write(world.swarm.join("latest_correlation"), [0xff, 0xfe]).expect("write");
    let before = world.db_bytes();
    let run = rust(&world, &["--json", "--transition", "T-lc", "VALIDATED"]);
    assert_eq!(
        run.write_state().as_deref(),
        Some("not_started"),
        "{}",
        run.stdout
    );
    assert_eq!(world.db_bytes(), before);
    // Naming the correlation id skips the read, as Python's `ctx.correlation_id or latest_correlation(...)` does.
    let run = rust(
        &world,
        &[
            "--json",
            "--transition",
            "T-lc",
            "VALIDATED",
            "--correlation-id",
            "C-1",
        ],
    );
    assert_eq!(run.code, 0, "{}", run.stdout);
    // A readable file is read and changes nothing.
    let readable = World::new(&[row_in("CREATED", "T-lc")]);
    fs::write(readable.swarm.join("latest_correlation"), "C-9\n").expect("write");
    assert_eq!(
        rust(&readable, &["--json", "--transition", "T-lc", "VALIDATED"]).code,
        0
    );
}

#[test]
fn an_invalid_id_is_pythons_own_e_input_before_anything_is_read() {
    let argv = [
        "--json",
        "--transition",
        "T-id",
        "VALIDATED",
        "--task-id",
        "bad..id",
    ];
    let (ours_world, theirs_world) = (
        World::new(&[row_in("CREATED", "T-id")]),
        World::new(&[row_in("CREATED", "T-id")]),
    );
    let before = ours_world.db_bytes();
    let ours = rust(&ours_world, &argv);
    let theirs = python(&theirs_world, &argv_strings(&argv));
    assert_eq!((ours.code, &ours.stdout), (theirs.code, &theirs.stdout));
    assert_eq!(ours.error_code().as_deref(), Some("E-INPUT"));
    assert_eq!(ours.write_state(), None);
    assert_eq!(ours_world.db_bytes(), before);
    assert_eq!(ours_world.manifest(), "");

    // The ids may also come from the environment the selector forwards (`SWARM_TASK_ID`, `SWARM_CORRELATION_ID`).
    for (name, what) in [
        ("SWARM_TASK_ID", "task id"),
        ("SWARM_CORRELATION_ID", "correlation id"),
    ] {
        let mut call = Call::new(
            &ours_world,
            &["--json", "--transition", "T-id", "VALIDATED"],
        );
        call.env.push((name, "a b".to_string()));
        let run = call.run();
        assert_eq!(
            run.error_code().as_deref(),
            Some("E-INPUT"),
            "{name}: {}",
            run.stdout
        );
        assert!(run.json()["error"]["message"]
            .as_str()
            .expect("message")
            .contains(&format!("invalid {what}")));
        assert_eq!(ours_world.db_bytes(), before, "{name}");
    }
}

// ---- the environment of the git child ---------------------------------------------------------------------------------------

#[test]
fn the_git_child_gets_path_and_home_and_nothing_else() {
    let world = World::new(&[row_in("CREATED", "T-git")]);
    // A fake `git` first on PATH records the variables it was started with and reports "not a repository".
    let bin = world.dir.path().join("bin");
    fs::create_dir_all(&bin).expect("bin");
    let marker = world.dir.path().join("git-env.txt");
    let script = format!(
        "#!/bin/sh\nenv | sed 's/=.*//' | sort > {}\nexit 1\n",
        marker.display()
    );
    fs::write(bin.join("git"), script).expect("fake git");
    fs::set_permissions(bin.join("git"), fs::Permissions::from_mode(0o755)).expect("chmod");
    let path = format!(
        "{}:{}",
        bin.display(),
        std::env::var("PATH").unwrap_or_default()
    );

    let mut call = Call::new(&world, &["--json", "--transition", "T-git", "VALIDATED"]);
    call.swarm_dir = false; // the swarm directory is resolved through git, so git is started
    for (name, value) in [
        ("PATH", path.as_str()),
        ("SWARM_SIGNING_KEY", "canary-hmac-7c1f"),
        ("SWARM_ED25519_KEY", "canary-ed-91ab"),
        ("SWARM_ED25519_KEY_FILE", "/canary/key"),
        ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
        ("SWARM_REQUIRE_KEY", "1"),
        ("GIT_DIR", "/canary/git-dir"),
    ] {
        call.extra_process_env.push((name, value.to_string()));
    }
    let run = call.run();
    assert_eq!(run.code, 0, "{} {}", run.stdout, run.stderr);
    let seen = fs::read_to_string(&marker).expect("the fake git was not started");
    let names: Vec<&str> = seen
        .lines()
        .filter(|name| !matches!(*name, "PWD" | "SHLVL" | "OLDPWD" | "_"))
        .collect();
    assert_eq!(names, ["HOME", "PATH"], "git was started with {seen:?}");
    assert!(!run.stdout.contains("canary") && !run.stderr.contains("canary"));
}
