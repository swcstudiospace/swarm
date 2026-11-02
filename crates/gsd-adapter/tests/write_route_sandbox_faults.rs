//! The sandbox write path of `orch_status --transition` (phase 14.5, unit U3.3b): the guest is a verified shadow of the host decision
//! (D-P29). It decides inside the write transaction, the host decides alongside, and the guest's answer is used only when it equals the
//! host's exactly; a fault or any difference before the apply step is answered by the host decision; only a spent deadline is
//! `E-TIMEOUT`; and nothing is decided after the commit.
//!
//! Stores are built by the REAL Python `TaskStore` (the schema is Python's) and seeded with task rows of the 351 oracle vectors of
//! `gsd-route` (`tests/vectors/transition.json`, produced by the real `TaskStore.transition`). The oracle of every reply here is the
//! host backend (`RustEngine`), which `tests/write_route.rs` proves byte-identical to Python.
//!
//! What is proved here, with a scripted guest that stands in for the real one (so it can misbehave on purpose):
//!
//! - a hostile reply is answered by the host decision: the stdout, the exit code, the effects manifest and the whole store equal the
//!   host backend's, and the failover is recorded with the code of the failure (the notice line of the binary is built from it). That
//!   holds for every plan the validator rejects (an extra column, a wrong `from`, three audit rows, unchained rows, an illegal final
//!   state, a wrong actor, a plan for another request) and, since the validator checks shape and legality and not values, for every
//!   well-formed one that is not the host's (an approval without facts, a gate task moved by hand, a skipped `max_attempts`, altered
//!   counters, `notes` and `reason`, forged refusals, duplicated effects), for the reply shapes of a hostile parser input (duplicate
//!   keys, `NaN`, an integer above `i64`, `1e999`, a huge `notes`, an unknown effect, extra keys) and for every fault of the sandbox
//!   before the apply step;
//! - the module is prepared before the transaction opens, and a guest that cannot be prepared is never called;
//! - the guest gets the budget less a reserve for apply and commit; only a SPENT deadline (the sandbox's `E-TIMEOUT` after the
//!   guest's budget, a reply that lands late, a budget already spent) ends the call as `E-TIMEOUT` with the transaction rolled back,
//!   the store byte-identical, no manifest and no failover, and an `E-TIMEOUT` while time remains is fuel and fails over;
//! - a refusal the guest gives that the host gives too is the answer, not a fault;
//! - the decision is made once: a commit that fails, an apply that fails and a manifest that is lost all leave the guest called once;
//! - the write lock is held across the guest call: a second writer is locked out while a slow guest runs.
//!
//! With the `sandbox-write` feature (the binary embeds the write guest) the REAL guest is held to the same standard: every one of the
//! 351 transition vectors gives the host's `Applied` or refusal and the same store, with no failover (the shadow never rejects a
//! faithful guest), the route on the sandbox backend prints the bytes the host backend prints, and fuel, memory and limits the sandbox
//! refuses give the documented failovers.

use std::fs;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::sync::{LazyLock, Mutex, PoisonError};
use std::time::{Duration, Instant};

use gsd_adapter::sandbox_engine::{
    failover_notice, SandboxDecider, SandboxEngine, WriteGuest, WRITE_REPLY_CAP_BYTES,
};
use gsd_adapter::strangler_seam::{error_reply, CoreBackend, Invocation, RustEngine};
use gsd_failure::ErrorCode;
use gsd_route::transition_args::parse_transition_args;
use gsd_route::transition_plan::{
    AuditRow, Facts, PlanEffect, Request, TransitionError, TransitionPlan, WriteState,
};
use gsd_route::transition_wire::{decode_reply, encode_reply, transition_invoke, Decision};
use gsd_route::{jstr, obj_get, parse_json, Fields, Json};
use gsd_sandbox::wasmtime_limits::deadline_ticks_for;
use gsd_sandbox::Limits;
use gsd_taskwriter::{
    open_for_write, Applied, ApplyCause, ApplyError, Decider, ErrorKind, TaskStoreWriter,
    WriterConfig,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{Column, ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};
use tempfile::TempDir;

mod signed_store;

const VECTORS: &str = include_str!("../../gsd-route/tests/vectors/transition.json");

/// The Python-built empty store: the schema and the migrations are Python's.
const TEMPLATE_SCRIPT: &str =
    "import sys\nfrom swarm.taskstore import TaskStore\nTaskStore(sys.argv[1]).conn.close()\n";

static TEMPLATE: LazyLock<Vec<u8>> = LazyLock::new(|| {
    let dir = TempDir::new().expect("tempdir");
    let db = dir.path().join("tasks.db");
    let output = std::process::Command::new("python3")
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

fn row_of(value: &Json) -> Fields {
    match value {
        Json::Obj(fields) => fields.clone(),
        other => panic!("not an object: {other:?}"),
    }
}

fn set(row: &mut Fields, column: &str, value: Json) {
    match row.iter_mut().find(|(name, _)| name == column) {
        Some(entry) => entry.1 = value,
        None => row.push((column.to_string(), value)),
    }
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
    let mut row = row_of(member(case, "task"));
    set(&mut row, "task_id", jstr(task_id));
    row
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

async fn exec(path: &Path, sql: &str) {
    let mut conn = connect(path).await;
    sqlx::query(sql).execute(&mut conn).await.expect(sql);
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

type Content = Vec<(&'static str, Vec<Vec<Cell>>)>;

/// Every row of the four tables and `sqlite_sequence`, values typed as stored. With `mask_clock`, `tasks.updated_at` and
/// `transitions.ts` are masked, and so is the number after `verdicts_since` in a text (Design 6: the only clock-dependent values).
async fn content(path: &Path, mask_clock: bool) -> Content {
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
                let clock = mask_clock
                    && matches!(
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
                        "TEXT" => {
                            let value = row.get::<String, _>(index);
                            Cell::Text(if mask_clock { mask(&value) } else { value })
                        }
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

/// Replaces the number after `"updated_at": ` and `"verdicts_since": ` (also inside the escaped `notes` text of a reply) with `<clock>`.
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
    _dir: TempDir,
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
            _dir: dir,
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

    /// The call the selector hands the binary for `argv` (`--json` first, as the harness sends it).
    fn call(&self, argv: &[&str], deadline: Duration) -> Invocation {
        let mut full = vec!["--json".to_string()];
        full.extend(argv.iter().map(|item| (*item).to_string()));
        Invocation {
            agent: "A01".to_string(),
            script: "orch_status".to_string(),
            argv: full,
            root: self.root.clone(),
            env: vec![("SWARM_DIR".to_string(), self.swarm.display().to_string())],
            deadline,
            effects_path: Some(self.effects.clone()),
        }
    }
}

// ---- a scripted guest -------------------------------------------------------------------------------------------------

/// What a scripted guest does with a request.
enum Script {
    /// A faithful guest (the real decision function on the request) whose decision `rewrite` changes before it is encoded.
    Rewrite(fn(Decision) -> Decision),
    /// A faithful guest whose reply TEXT is changed by this function (which must change something, see [`replaced`]): for the
    /// shapes no `Decision` can have (duplicate keys, `NaN`, an unknown effect, an extra key).
    Text(fn(&str) -> String),
    /// These bytes, whatever the request says.
    Bytes(Vec<u8>),
    /// The call faults with this code before any reply (a trap, fuel, memory, a pin mismatch, a spent deadline).
    Fault(ErrorCode),
    /// Takes this long, then faults with this code: a guest the sandbox stops late.
    SlowFault(Duration, ErrorCode),
    /// Waits until this instant (the call has run that long by the decider's clock), then faults with this code. The wait does not
    /// depend on how long the host took to reach the call, so a loaded host cannot move the fault out of the window a case aims at.
    FaultAt(Instant, ErrorCode),
    /// Waits until this instant, then answers faithfully (as [`Script::FaultAt`] waits before it faults).
    ReplyAt(Instant),
}

struct Scripted {
    script: Script,
    prepare: Result<(), ErrorCode>,
    prepares: AtomicUsize,
    calls: AtomicUsize,
    limits: Mutex<Vec<Limits>>,
}

impl Scripted {
    fn new(script: Script) -> Self {
        Self {
            script,
            prepare: Ok(()),
            prepares: AtomicUsize::new(0),
            calls: AtomicUsize::new(0),
            limits: Mutex::new(Vec::new()),
        }
    }

    /// The same guest, whose `prepare` fails with `code`.
    fn failing_to_prepare(mut self, code: ErrorCode) -> Self {
        self.prepare = Err(code);
        self
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }

    fn prepares(&self) -> usize {
        self.prepares.load(Ordering::SeqCst)
    }
}

/// The reply the real guest function gives for `request`.
/// Whether `request` is a transition request (its `mode` is the transition mode).
fn is_transition(request: &[u8]) -> bool {
    let Ok(text) = std::str::from_utf8(request) else {
        return false;
    };
    matches!(
        gsd_route::parse_json(text),
        Ok(gsd_route::Json::Obj(fields))
            if matches!(gsd_route::obj_get(&fields, "mode"), Some(gsd_route::Json::Str(mode)) if mode == "transition")
    )
}

fn faithful(request: &[u8]) -> Decision {
    let bytes = transition_invoke(request).expect("the guest function reads the request");
    decode_reply(&bytes).expect("the guest function's reply decodes")
}

/// `text` with its first `needle` replaced by `with`. A needle that is not there is a broken test, not a no-op.
fn replaced(text: &str, needle: &str, with: &str) -> String {
    assert!(text.contains(needle), "{needle} is not in {text}");
    text.replacen(needle, with, 1)
}

impl WriteGuest for Scripted {
    fn prepare(&self) -> Result<(), ErrorCode> {
        self.prepares.fetch_add(1, Ordering::SeqCst);
        self.prepare
    }

    fn call(&self, request: &[u8], limits: &Limits) -> Result<Vec<u8>, ErrorCode> {
        // The second batch asks the same guest for the APPROVED facts before the decision (phase 14.5, pin P2). Those requests are
        // answered by their own mode, as the real guest answers them, and are not counted: the script only ever rewrites, and counts,
        // the transition decision.
        if !is_transition(request) {
            return gsd_route::core_invoke(request).map_err(|error| error.code());
        }
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.limits
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(*limits);
        match &self.script {
            Script::Rewrite(rewrite) => Ok(encode_reply(&rewrite(faithful(request))).into_bytes()),
            Script::Text(edit) => Ok(edit(&encode_reply(&faithful(request))).into_bytes()),
            Script::Bytes(bytes) => Ok(bytes.clone()),
            Script::Fault(code) => Err(*code),
            Script::SlowFault(delay, code) => {
                std::thread::sleep(*delay);
                Err(*code)
            }
            Script::FaultAt(instant, code) => {
                std::thread::sleep(instant.saturating_duration_since(Instant::now()));
                Err(*code)
            }
            Script::ReplyAt(instant) => {
                std::thread::sleep(instant.saturating_duration_since(Instant::now()));
                Ok(encode_reply(&faithful(request)).into_bytes())
            }
        }
    }
}

/// The production limits (OD-6), as the engine gives its real guest; a scripted guest ignores them but the decider must pass them.
fn production(budget: Duration) -> Limits {
    Limits {
        fuel: 1_000_000_000,
        memory_bytes: 16 * 1024 * 1024,
        deadline_ticks: Some(deadline_ticks_for(budget)),
    }
}

fn plan_of(decision: Decision) -> TransitionPlan {
    decision.expect("the host decision is a plan for this seed")
}

/// A decision rewritten by `change` on its plan.
fn rewrite_plan(decision: Decision, change: impl FnOnce(&mut TransitionPlan)) -> Decision {
    let mut plan = plan_of(decision);
    change(&mut plan);
    Ok(plan)
}

// ---- running the two backends -------------------------------------------------------------------------------------------

const DEADLINE: Duration = Duration::from_secs(60);

/// Everything one call leaves behind.
#[derive(Debug, PartialEq)]
struct Observed {
    stdout: String,
    exit_code: i32,
    manifest: String,
    store: Content,
}

fn observe(world: &World, result: Result<gsd_route::Reply, gsd_route::SeamError>) -> Observed {
    let reply = match result {
        Ok(reply) => reply,
        Err(error) => error_reply("A01", "orch_status", &error),
    };
    Observed {
        stdout: mask(&reply.stdout),
        exit_code: reply.exit_code,
        manifest: world.manifest(),
        store: block(content(&world.db(), true)),
    }
}

/// The host backend's call on a world of its own.
fn run_host(rows: &[Fields], argv: &[&str]) -> Observed {
    let world = World::new(rows);
    let call = world.call(argv, DEADLINE);
    let result = RustEngine::new().expect("host engine").orch_status(&call);
    observe(&world, result)
}

/// The sandbox write route with `guest`, on a world of its own: what it left behind, the engine's failover record, and the
/// world itself for byte comparisons.
fn run_sandbox(
    rows: &[Fields],
    argv: &[&str],
    guest: &dyn WriteGuest,
    deadline: Duration,
) -> (Observed, Option<ErrorCode>, World) {
    let world = World::new(rows);
    let call = world.call(argv, deadline);
    let args = parse_transition_args(&call.argv).expect("a transition argv");
    let engine = SandboxEngine::new().expect("sandbox engine");
    let result = engine.transition_via(guest, production, &args, &call);
    let failover = engine.failover();
    (observe(&world, result), failover, world)
}

// ---- 1. the hostile-guest matrix ------------------------------------------------------------------------------------------

/// CREATED → VALIDATED: one audit row, the columns `state` only.
fn legal() -> (Vec<Fields>, Vec<&'static str>) {
    (
        vec![row_in("CREATED", "T-1")],
        vec!["--transition", "T-1", "VALIDATED", "--reason", "because"],
    )
}

/// IN_REVIEW → CHANGES_REQUESTED with loops to spare: columns `state`, `rework_loops`, `notes`; one audit row.
fn single_hop() -> (Vec<Fields>, Vec<&'static str>) {
    (
        vec![row_in("IN_REVIEW", "T-1")],
        vec![
            "--transition",
            "T-1",
            "CHANGES_REQUESTED",
            "--reason",
            "rework",
        ],
    )
}

/// The same request on a task at the rework cap: two audit rows (the auto-escalation).
fn two_hop() -> (Vec<Fields>, Vec<&'static str>) {
    let mut row = row_in("IN_REVIEW", "T-1");
    set(&mut row, "rework_loops", Json::Int(2));
    (
        vec![row],
        vec![
            "--transition",
            "T-1",
            "CHANGES_REQUESTED",
            "--reason",
            "rework",
        ],
    )
}

type Seed = fn() -> (Vec<Fields>, Vec<&'static str>);

/// A plan that must not be applied: each case changes what the host decision made, and the call must be answered by the host.
#[test]
fn a_hostile_reply_is_answered_by_the_host_decision_with_the_host_backends_bytes() {
    let cases: Vec<(&str, Seed, Script, ErrorCode)> = vec![
        (
            "an extra column",
            single_hop,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| plan.columns.push(("title".into(), jstr("pwned"))))
            }),
            ErrorCode::Contract,
        ),
        (
            "an extra column that is the clock",
            single_hop,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| {
                    plan.columns.push(("updated_at".into(), Json::Float(0.0)));
                })
            }),
            ErrorCode::Contract,
        ),
        (
            "a wrong from",
            single_hop,
            Script::Rewrite(|d| rewrite_plan(d, |plan| plan.from = "PLANNED".into())),
            ErrorCode::Contract,
        ),
        (
            "three audit rows",
            two_hop,
            Script::Rewrite(|d| rewrite_plan(d, |plan| plan.audit.push(plan.audit[1].clone()))),
            ErrorCode::Contract,
        ),
        (
            "unchained audit rows",
            two_hop,
            Script::Rewrite(|d| rewrite_plan(d, |plan| plan.audit[1].from = "CREATED".into())),
            ErrorCode::Contract,
        ),
        (
            "no audit row",
            single_hop,
            Script::Rewrite(|d| rewrite_plan(d, |plan| plan.audit.clear())),
            ErrorCode::Contract,
        ),
        (
            "an illegal final state",
            legal,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| {
                    plan.to = "DONE".into();
                    plan.columns = vec![("state".into(), jstr("DONE"))];
                    plan.audit[0].to = "DONE".into();
                })
            }),
            ErrorCode::Contract,
        ),
        (
            "an audit row by another actor",
            legal,
            Script::Rewrite(|d| rewrite_plan(d, |plan| plan.audit[0].actor = "A99".into())),
            ErrorCode::Contract,
        ),
        (
            "a legal plan for another target than the one asked",
            legal,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| {
                    plan.to = "CANCELLED".into();
                    plan.columns = vec![("state".into(), jstr("CANCELLED"))];
                    plan.audit[0].to = "CANCELLED".into();
                })
            }),
            ErrorCode::Contract,
        ),
        (
            "a whole plan decided for a task in another state",
            single_hop,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| {
                    plan.from = "PLANNED".into();
                    plan.to = "CLAIMED".into();
                    plan.columns = vec![("state".into(), jstr("CLAIMED"))];
                    plan.audit = vec![AuditRow {
                        from: "PLANNED".into(),
                        to: "CLAIMED".into(),
                        actor: "A01".into(),
                        reason: "rework".into(),
                    }];
                })
            }),
            ErrorCode::Contract,
        ),
        (
            "a refusal the decision cannot make (E-INTERNAL as an answer)",
            legal,
            Script::Rewrite(|_| {
                Err(TransitionError {
                    code: ErrorCode::Internal,
                    message: "E-INTERNAL: boom".into(),
                    details: Vec::new(),
                    write_state: WriteState::Final,
                })
            }),
            ErrorCode::Contract,
        ),
        (
            "a deadline refusal that names no task",
            legal,
            Script::Rewrite(|_| {
                Err(TransitionError {
                    code: ErrorCode::Timeout,
                    message: "E-TIMEOUT: guest call exceeded 1 s".into(),
                    details: Vec::new(),
                    write_state: WriteState::Final,
                })
            }),
            ErrorCode::Contract,
        ),
        (
            "a not_started marker that is not E-DEP",
            legal,
            Script::Rewrite(|_| {
                Err(TransitionError {
                    code: ErrorCode::Contract,
                    message: "E-CONTRACT: nothing".into(),
                    details: Vec::new(),
                    write_state: WriteState::NotStarted,
                })
            }),
            ErrorCode::Contract,
        ),
        (
            "garbage",
            legal,
            Script::Bytes(b"this is not json".to_vec()),
            ErrorCode::Dep,
        ),
        (
            "bytes that are not UTF-8",
            legal,
            Script::Bytes(vec![0xff, 0xfe, 0x00]),
            ErrorCode::Contract,
        ),
        (
            "a truncated reply",
            legal,
            Script::Bytes(b"{\"plan\": {\"from\": \"CREATED\"".to_vec()),
            ErrorCode::Dep,
        ),
        (
            "a reply of the wrong shape",
            legal,
            Script::Bytes(b"{\"plan\": 1}".to_vec()),
            ErrorCode::Contract,
        ),
        (
            "an oversize reply",
            legal,
            Script::Bytes(vec![b' '; WRITE_REPLY_CAP_BYTES + 1]),
            ErrorCode::Capacity,
        ),
        // The fault rows are scripted: the code is the one the sandbox gives for the fault. `fault_of` maps what a guest reports to it
        // (unit test `a_guest_cannot_claim_the_deadline_in_an_error_of_its_own`, which covers the trap and the guest's own codes);
        // the REAL guest's classification of fuel exhausted (with and without a deadline), memory exhausted and limits refused at
        // instantiation is held by `real_guest::the_real_guest_under_starved_limits_fails_over`, and a pin mismatch of the real
        // engine by the unit test `a_pin_mismatch_on_a_write_is_answered_by_the_host_and_recorded`.
        (
            "a trap, or fuel exhausted without a deadline (E-INTERNAL)",
            single_hop,
            Script::Fault(ErrorCode::Internal),
            ErrorCode::Internal,
        ),
        (
            "fuel exhausted while time remains: the sandbox's E-TIMEOUT before the budget is spent",
            single_hop,
            Script::Fault(ErrorCode::Timeout),
            ErrorCode::Internal,
        ),
        (
            "memory refused (E-CAPACITY)",
            legal,
            Script::Fault(ErrorCode::Capacity),
            ErrorCode::Capacity,
        ),
        (
            "a pin mismatch or an instantiation failure (E-DEP)",
            two_hop,
            Script::Fault(ErrorCode::Dep),
            ErrorCode::Dep,
        ),
        (
            "limits refused at instantiation, or a request the guest rejects (E-INPUT)",
            legal,
            Script::Fault(ErrorCode::Input),
            ErrorCode::Input,
        ),
    ];
    for (label, seed, script, code) in cases {
        let (rows, argv) = seed();
        let guest = Scripted::new(script);
        let (sandbox, failover, _) = run_sandbox(&rows, &argv, &guest, DEADLINE);
        let host = run_host(&rows, &argv);
        assert_eq!(
            sandbox, host,
            "{label}: the call must be answered as the host backend answers it"
        );
        assert_eq!(
            host.exit_code, 0,
            "{label}: the host commits: {}",
            host.stdout
        );
        assert_eq!(failover, Some(code), "{label}: failover code");
        assert_eq!(guest.calls(), 1, "{label}: the guest is asked once");
    }
    // The notice the binary prints for the code of these cases, as a literal: the wire code only, one fixed line.
    assert_eq!(
        failover_notice(ErrorCode::Contract),
        "gsd_engine: sandbox fault E-CONTRACT; answered by host Rust"
    );
}

/// IN_REVIEW → APPROVED with no verdict row: the host refuses with Python's own `E-POLICY` (no key in this process, else no passing
/// gate), so a guest that commits it is a forgery. Before U3.4d the host had no approval facts and the refusal was `not_started`.
fn approving() -> (Vec<Fields>, Vec<&'static str>) {
    (
        vec![row_in("IN_REVIEW", "T-1")],
        vec!["--transition", "T-1", "APPROVED", "--reason", "because"],
    )
}

/// A gate task (its notes say so) moved from IN_PROGRESS to IN_REVIEW by hand: the host refuses with `E-POLICY`.
fn gate_by_hand() -> (Vec<Fields>, Vec<&'static str>) {
    let mut row = row_in("IN_PROGRESS", "T-1");
    set(
        &mut row,
        "notes",
        jstr("{\"gate\": \"review\", \"gate_for\": [\"T1\"]}"),
    );
    (
        vec![row],
        vec!["--transition", "T-1", "IN_REVIEW", "--reason", "because"],
    )
}

/// PLANNED → CLAIMED with attempts to spare (0 of 3): the host counts attempt 1.
fn claim() -> (Vec<Fields>, Vec<&'static str>) {
    (
        vec![row_in("PLANNED", "T-1")],
        vec!["--transition", "T-1", "CLAIMED", "--reason", "because"],
    )
}

/// PLANNED → CLAIMED with every attempt used (3 of 3): the host refuses with `E-TIMEOUT` `max_attempts exceeded`.
fn claim_at_the_cap() -> (Vec<Fields>, Vec<&'static str>) {
    let mut row = row_in("PLANNED", "T-1");
    set(&mut row, "attempt", Json::Int(3));
    (
        vec![row],
        vec!["--transition", "T-1", "CLAIMED", "--reason", "because"],
    )
}

/// IN_PROGRESS → FAILED, a legal move the host commits.
fn failing() -> (Vec<Fields>, Vec<&'static str>) {
    (
        vec![row_in("IN_PROGRESS", "T-1")],
        vec!["--transition", "T-1", "FAILED", "--reason", "because"],
    )
}

/// What the host decides for a plain move: the column `state`, one audit row by `A01` with the reason `because`, one live refresh.
fn plain_plan(from: &str, to: &str) -> TransitionPlan {
    TransitionPlan {
        from: from.into(),
        to: to.into(),
        columns: vec![("state".into(), jstr(to))],
        audit: vec![AuditRow {
            from: from.into(),
            to: to.into(),
            actor: "A01".into(),
            reason: "because".into(),
        }],
        effects: vec![PlanEffect::LiveRefresh],
    }
}

/// A refusal in the shape of Python's own, naming the task.
fn forged_refusal(code: ErrorCode) -> Decision {
    Err(TransitionError {
        code,
        message: format!("{}: forged", code.as_str()),
        details: vec![("task_id".into(), jstr("T-1"))],
        write_state: WriteState::Final,
    })
}

/// Well-formed replies that the plan validator passes today (or that decode) and that are NOT what the host decides: every one is a
/// forgery, not applied and not printed. The call is answered by the host decision with the host backend's stdout, exit code, effects
/// manifest and whole store, and the failover is recorded under the code `E-CONTRACT` (a reply that cannot be read at all keeps the
/// code of the read: `E-DEP` for what the parser cannot carry).
#[test]
fn a_well_formed_reply_that_is_not_the_host_decision_is_answered_by_the_host() {
    let cases: Vec<(&str, Seed, Script, i32, ErrorCode)> = vec![
        (
            "an APPROVED target without approval facts",
            approving,
            Script::Rewrite(|_| Ok(plain_plan("IN_REVIEW", "APPROVED"))),
            2,
            ErrorCode::Contract,
        ),
        (
            "a gate task moved by hand",
            gate_by_hand,
            Script::Rewrite(|_| Ok(plain_plan("IN_PROGRESS", "IN_REVIEW"))),
            2,
            ErrorCode::Contract,
        ),
        (
            "max_attempts skipped on a CLAIMED move",
            claim_at_the_cap,
            Script::Rewrite(|_| {
                let mut plan = plain_plan("PLANNED", "CLAIMED");
                plan.columns.push(("attempt".into(), Json::Int(4)));
                Ok(plan)
            }),
            2,
            ErrorCode::Contract,
        ),
        (
            "attempt altered on a CLAIMED move",
            claim,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| set(&mut plan.columns, "attempt", Json::Int(9)))
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "attempt added to a move that counts none",
            legal,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| {
                    plan.columns.push(("attempt".into(), Json::Int(5)))
                })
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "rework_loops altered",
            single_hop,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| {
                    set(&mut plan.columns, "rework_loops", Json::Int(0))
                })
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "rework_loops added to a move that counts none",
            legal,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| {
                    plan.columns.push(("rework_loops".into(), Json::Int(7)));
                })
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "notes replaced",
            single_hop,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| {
                    set(
                        &mut plan.columns,
                        "notes",
                        Json::Obj(vec![("x".into(), Json::Int(1))]),
                    );
                })
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "notes added to a move that changes none",
            legal,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| {
                    plan.columns.push(("notes".into(), Json::Obj(Vec::new())))
                })
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "a reason altered",
            legal,
            Script::Rewrite(|d| rewrite_plan(d, |plan| plan.audit[0].reason = "forged".into())),
            0,
            ErrorCode::Contract,
        ),
        (
            "a reason with a NUL",
            legal,
            Script::Rewrite(|d| {
                rewrite_plan(d, |plan| plan.audit[0].reason = "because\u{0}".into())
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "an escalation forced below the rework cap",
            single_hop,
            Script::Rewrite(|_| {
                let audit = |from: &str, to: &str, reason: &str| AuditRow {
                    from: from.into(),
                    to: to.into(),
                    actor: "A01".into(),
                    reason: reason.into(),
                };
                Ok(TransitionPlan {
                    from: "IN_REVIEW".into(),
                    to: "ESCALATED".into(),
                    columns: vec![("state".into(), jstr("ESCALATED"))],
                    audit: vec![
                        audit("IN_REVIEW", "CHANGES_REQUESTED", "rework cap reached"),
                        audit(
                            "CHANGES_REQUESTED",
                            "ESCALATED",
                            "rework loops exhausted (2); rework",
                        ),
                    ],
                    effects: vec![PlanEffect::LiveRefresh],
                })
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "a forged E-POLICY refusal of a legal move",
            legal,
            Script::Rewrite(|_| forged_refusal(ErrorCode::Policy)),
            0,
            ErrorCode::Contract,
        ),
        (
            "a forged E-CONTRACT refusal of a legal move",
            legal,
            Script::Rewrite(|_| forged_refusal(ErrorCode::Contract)),
            0,
            ErrorCode::Contract,
        ),
        (
            "a forged E-INPUT refusal of a legal move",
            legal,
            Script::Rewrite(|_| forged_refusal(ErrorCode::Input)),
            0,
            ErrorCode::Contract,
        ),
        (
            "an E-TIMEOUT refusal that merely carries a task_id",
            legal,
            Script::Rewrite(|_| forged_refusal(ErrorCode::Timeout)),
            0,
            ErrorCode::Contract,
        ),
        (
            "the host's own refusal with another message",
            gate_by_hand,
            Script::Rewrite(|d| {
                d.map_err(|mut refusal| {
                    refusal.message.push_str(" (forged)");
                    refusal
                })
            }),
            2,
            ErrorCode::Contract,
        ),
        (
            "the host's own not_started marker with another message",
            approving,
            Script::Rewrite(|d| {
                d.map_err(|mut refusal| {
                    refusal.message.push_str(" (forged)");
                    refusal
                })
            }),
            2,
            ErrorCode::Contract,
        ),
        (
            "a refusal where the host commits (not_started marker)",
            legal,
            Script::Rewrite(|_| {
                Err(TransitionError {
                    code: ErrorCode::Dep,
                    message: "E-DEP: not started: the guest cannot decide this".into(),
                    details: Vec::new(),
                    write_state: WriteState::NotStarted,
                })
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "a plan where the host refuses",
            claim_at_the_cap,
            Script::Rewrite(|_| Ok(plain_plan("PLANNED", "CLAIMED"))),
            2,
            ErrorCode::Contract,
        ),
        (
            "duplicated effects",
            legal,
            Script::Rewrite(|d| rewrite_plan(d, |plan| plan.effects.push(PlanEffect::LiveRefresh))),
            0,
            ErrorCode::Contract,
        ),
        (
            "dropped effects",
            legal,
            Script::Rewrite(|d| rewrite_plan(d, |plan| plan.effects.clear())),
            0,
            ErrorCode::Contract,
        ),
        (
            "a stale from for the right task, with a chain that holds together",
            failing,
            Script::Rewrite(|_| Ok(plain_plan("CLAIMED", "FAILED"))),
            0,
            ErrorCode::Contract,
        ),
        (
            "duplicate keys, the last of which is not the host's",
            legal,
            Script::Text(|text| {
                replaced(
                    text,
                    "{\"plan\": {\"from\": \"CREATED\"",
                    "{\"plan\": {\"from\": \"CREATED\", \"from\": \"PLANNED\"",
                )
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "NaN in a column",
            legal,
            Script::Text(|text| {
                replaced(
                    text,
                    "{\"state\": \"VALIDATED\"}",
                    "{\"state\": \"VALIDATED\", \"attempt\": NaN}",
                )
            }),
            0,
            ErrorCode::Dep,
        ),
        (
            "Infinity in a column",
            legal,
            Script::Text(|text| {
                replaced(
                    text,
                    "{\"state\": \"VALIDATED\"}",
                    "{\"state\": \"VALIDATED\", \"attempt\": Infinity}",
                )
            }),
            0,
            ErrorCode::Dep,
        ),
        (
            "-Infinity in a column",
            legal,
            Script::Text(|text| {
                replaced(
                    text,
                    "{\"state\": \"VALIDATED\"}",
                    "{\"state\": \"VALIDATED\", \"attempt\": -Infinity}",
                )
            }),
            0,
            ErrorCode::Dep,
        ),
        (
            "an integer above i64 in a column",
            legal,
            Script::Text(|text| {
                replaced(
                    text,
                    "{\"state\": \"VALIDATED\"}",
                    "{\"state\": \"VALIDATED\", \"attempt\": 9223372036854775808}",
                )
            }),
            0,
            ErrorCode::Dep,
        ),
        (
            "1e999 in notes (it decodes, and differs from the host's plan)",
            legal,
            Script::Text(|text| {
                replaced(
                    text,
                    "{\"state\": \"VALIDATED\"}",
                    "{\"state\": \"VALIDATED\", \"notes\": {\"x\": 1e999}}",
                )
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "a valid but huge notes below the reply cap",
            legal,
            Script::Text(|text| {
                let notes = format!(
                    "{{\"state\": \"VALIDATED\", \"notes\": {{\"pad\": \"{}\"}}}}",
                    "x".repeat(900_000)
                );
                replaced(text, "{\"state\": \"VALIDATED\"}", &notes)
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "an unknown effect kind",
            legal,
            Script::Text(|text| replaced(text, "\"live_refresh\"", "\"rm_rf\"")),
            0,
            ErrorCode::Contract,
        ),
        (
            "an extra key on an effect",
            legal,
            Script::Text(|text| {
                replaced(
                    text,
                    "{\"kind\": \"live_refresh\"}",
                    "{\"kind\": \"live_refresh\", \"run\": \"x\"}",
                )
            }),
            0,
            ErrorCode::Contract,
        ),
        (
            "an extra key on the plan",
            legal,
            Script::Text(|text| replaced(text, "{\"plan\": {", "{\"plan\": {\"extra\": 1, ")),
            0,
            ErrorCode::Contract,
        ),
        (
            "an error next to the plan",
            legal,
            Script::Text(|text| replaced(text, "{\"plan\": ", "{\"error\": null, \"plan\": ")),
            0,
            ErrorCode::Contract,
        ),
        (
            "an extra ts key in an audit row",
            legal,
            Script::Text(|text| {
                replaced(
                    text,
                    "\"reason\": \"because\"}",
                    "\"reason\": \"because\", \"ts\": 1.0}",
                )
            }),
            0,
            ErrorCode::Contract,
        ),
    ];
    for (label, seed, script, host_exit, code) in cases {
        let (rows, argv) = seed();
        let guest = Scripted::new(script);
        let (sandbox, failover, _) = run_sandbox(&rows, &argv, &guest, DEADLINE);
        let host = run_host(&rows, &argv);
        assert_eq!(
            sandbox, host,
            "{label}: the call must be answered as the host backend answers it"
        );
        assert_eq!(host.exit_code, host_exit, "{label}: {}", host.stdout);
        assert_eq!(failover, Some(code), "{label}: failover code");
        assert_eq!(guest.calls(), 1, "{label}: the guest is asked once");
    }
}

/// A guest that gives a good plan is the answer, with no failover and the same bytes as the host's.
#[test]
fn a_faithful_guest_is_the_answer_and_records_no_failover() {
    for seed in [legal as Seed, single_hop, two_hop] {
        let (rows, argv) = seed();
        let guest = Scripted::new(Script::Rewrite(|decision| decision));
        let (sandbox, failover, _) = run_sandbox(&rows, &argv, &guest, DEADLINE);
        assert_eq!(sandbox, run_host(&rows, &argv), "{argv:?}");
        assert_eq!(sandbox.exit_code, 0, "{}", sandbox.stdout);
        assert_eq!(failover, None);
        assert_eq!(guest.calls(), 1);
    }
}

/// The seed row and request of the vector `id`, whose request is `--transition <task> <to>` with the default reason.
fn vector_seed(id: &str) -> (Vec<Fields>, Vec<String>) {
    let doc = parse_json(VECTORS).expect("vectors are JSON");
    let Json::Arr(items) = member(&doc, "cases") else {
        panic!("cases is not a list");
    };
    let case = items
        .iter()
        .find(|case| text(member(case, "id")) == id)
        .unwrap_or_else(|| panic!("no vector {id}"));
    let request = member(case, "request");
    (
        vec![row_of(member(case, "task"))],
        vec![
            "--transition".to_string(),
            text(member(request, "task_id")).to_string(),
            text(member(request, "to")).to_string(),
        ],
    )
}

/// The guest's own refusal, one Python would give (an illegal transition, the manual-transition `E-POLICY`, an unknown task), is the
/// answer and not a fault: the host backend's bytes, no failover, the guest asked once, and nothing written.
#[test]
fn a_refusal_python_would_give_is_the_answer_and_not_a_fault() {
    let (gate_rows, gate_argv) = vector_seed("gate-to-in-review-manual");
    let gate_argv: Vec<&str> = gate_argv.iter().map(String::as_str).collect();
    let cases: Vec<(&str, Vec<Fields>, Vec<&str>, &str)> = vec![
        (
            "an illegal transition",
            vec![row_in("DONE", "T-1")],
            vec!["--transition", "T-1", "CREATED"],
            "E-CONTRACT",
        ),
        (
            "a gate task moved by hand",
            gate_rows,
            gate_argv,
            "E-POLICY",
        ),
        (
            "an unknown task",
            Vec::new(),
            vec!["--transition", "T-none", "VALIDATED"],
            "E-INPUT",
        ),
    ];
    for (label, rows, argv, code) in cases {
        let guest = Scripted::new(Script::Rewrite(|decision| decision));
        let (sandbox, failover, world) = run_sandbox(&rows, &argv, &guest, DEADLINE);
        assert_eq!(sandbox, run_host(&rows, &argv), "{label}");
        assert_eq!(sandbox.exit_code, 2, "{label}: {}", sandbox.stdout);
        assert!(
            sandbox.stdout.contains(&format!("\"code\": \"{code}\"")),
            "{label}: {}",
            sandbox.stdout
        );
        assert_eq!(failover, None, "{label}");
        assert_eq!(guest.calls(), 1, "{label}");
        assert_eq!(sandbox.manifest, "", "{label}");
        assert!(
            !sandbox.stdout.contains("write_state"),
            "{label}: Python's own refusal carries no write_state: {}",
            sandbox.stdout
        );
        assert_eq!(world.db_bytes(), World::new(&rows).db_bytes(), "{label}");
    }
}

/// The reply of a guest that is faithful but for its duplicate keys: the parser keeps the last, so a last that is the host's is the
/// answer and a last that is not is a forgery (held in the hostile cases below).
#[test]
fn duplicate_keys_whose_last_value_is_the_host_decision_are_the_answer() {
    let (rows, argv) = legal();
    let guest = Scripted::new(Script::Text(|text| {
        replaced(
            text,
            "{\"plan\": {\"from\": \"CREATED\"",
            "{\"plan\": {\"from\": \"PLANNED\", \"from\": \"CREATED\"",
        )
    }));
    let (sandbox, failover, _) = run_sandbox(&rows, &argv, &guest, DEADLINE);
    assert_eq!(sandbox, run_host(&rows, &argv));
    assert_eq!(sandbox.exit_code, 0, "{}", sandbox.stdout);
    assert_eq!(failover, None);
    assert_eq!(guest.calls(), 1);
}

/// A not_started answer from the guest is the answer when the host makes it too (a target that is not a state name): the host's
/// bytes, no failover, nothing written. A guest that says it cannot decide where the host would commit is a forgery, held with the
/// hostile cases below.
///
/// This test's premise is reversed twice. D-P29: it used to give the guest a not_started marker of its own for a legal transition and
/// expect that marker to be the answer, which the verified shadow now refuses to take on the guest's word. U3.4d: its not_started
/// decision was a legal APPROVED without approval facts; the host reads those facts itself now (a legal APPROVED without a key is
/// Python's own `E-POLICY`), so the decision the host and the guest share is a target that is not a state name, which Python answers
/// with a traceback.
#[test]
fn a_not_started_answer_the_host_gives_too_writes_nothing_and_is_not_a_fault() {
    let rows = vec![row_in("IN_REVIEW", "T-1")];
    let argv = vec!["--transition", "T-1", "BOGUS", "--reason", "because"];
    let guest = Scripted::new(Script::Rewrite(|decision| decision));
    let (sandbox, failover, world) = run_sandbox(&rows, &argv, &guest, DEADLINE);
    assert_eq!(sandbox, run_host(&rows, &argv));
    assert_eq!(sandbox.exit_code, 2, "{}", sandbox.stdout);
    assert!(
        sandbox.stdout.contains("\"write_state\": \"not_started\""),
        "{}",
        sandbox.stdout
    );
    assert_eq!(failover, None);
    assert_eq!(guest.calls(), 1);
    assert_eq!(world.db_bytes(), World::new(&rows).db_bytes());
    assert_eq!(sandbox.manifest, "");
}

/// The writer's transition of `T-1` to VALIDATED in `world`, decided by a decider over `guest` for a call that started at `started`
/// and has `deadline` to spend: the writer's outcome and the decider's failover record.
fn decide_through_writer(
    world: &World,
    guest: &dyn WriteGuest,
    started: Instant,
    deadline: Duration,
) -> (Result<Applied, ApplyError>, Option<ErrorCode>) {
    let decider = SandboxDecider::new(guest, started, deadline);
    let outcome = block(async {
        let writer = writer(&world.db(), Duration::from_secs(10)).await;
        let outcome = writer
            .transition(&request("T-1", "VALIDATED"), NOW, &decider)
            .await;
        writer.close().await;
        outcome
    });
    (outcome, decider.failover())
}

/// The instant a call that has run for `age` started at.
fn ago(age: Duration) -> Instant {
    Instant::now()
        .checked_sub(age)
        .expect("the clock has run that long")
}

/// The limits the decider hands the guest are the production ones, with an epoch deadline made from the budget left less the reserve
/// for apply and commit: a tenth of the call deadline, at most two seconds. The clock of the decider has not started (an instant in
/// the future counts no time), so the budget is exact.
#[test]
fn the_guest_gets_fuel_memory_and_a_deadline_made_from_the_budget_less_a_reserve() {
    let secs = Duration::from_secs;
    // (the call deadline, the budget the guest's epoch deadline must be made from)
    let cases = [
        (secs(5), Duration::from_millis(4_500)),
        (secs(15), Duration::from_millis(13_500)),
        (secs(20), secs(18)),
        (secs(30), secs(28)),
        (secs(600), secs(598)),
    ];
    for (deadline, budget) in cases {
        let guest = Scripted::new(Script::Rewrite(|decision| decision));
        let world = World::new(&[row_in("CREATED", "T-1")]);
        let not_started = Instant::now() + secs(3_600);
        let (outcome, failover) = decide_through_writer(&world, &guest, not_started, deadline);
        outcome.expect("a faithful guest commits");
        assert_eq!(failover, None, "{deadline:?}");
        let seen = guest.limits.lock().unwrap_or_else(PoisonError::into_inner);
        assert_eq!(seen.len(), 1, "{deadline:?}");
        assert_eq!(seen[0].fuel, 1_000_000_000, "{deadline:?}");
        assert_eq!(seen[0].memory_bytes, 16 * 1024 * 1024, "{deadline:?}");
        assert_eq!(
            seen[0].deadline_ticks,
            Some(deadline_ticks_for(budget)),
            "{deadline:?}: the budget is the deadline less the reserve"
        );
    }
}

/// Time left only inside the reserve is not the guest's: the call has no budget for it, so it is `E-TIMEOUT` and the guest is not
/// called. A call with budget left before the reserve calls it, with the budget that is left.
#[test]
fn a_budget_inside_the_reserve_is_timeout_and_one_before_it_calls_the_guest() {
    let secs = Duration::from_secs;
    let world = World::new(&[row_in("CREATED", "T-1")]);
    let before = world.db_bytes();
    // 10 s with a reserve of 1 s: the guest's budget ends at 9 s, and 9.5 s have been spent.
    let guest = Scripted::new(Script::Rewrite(|decision| decision));
    let (outcome, failover) =
        decide_through_writer(&world, &guest, ago(Duration::from_millis(9_500)), secs(10));
    let error = outcome.expect_err("the guest has no budget left");
    assert_eq!(error.code(), ErrorCode::Timeout);
    assert_eq!(guest.calls(), 0);
    assert_eq!(failover, None);
    assert_eq!(world.db_bytes(), before);

    // 5 s have been spent: 4 s of the guest's 9 s are left, and the epoch deadline is made from them.
    let guest = Scripted::new(Script::Rewrite(|decision| decision));
    let (outcome, failover) = decide_through_writer(&world, &guest, ago(secs(5)), secs(10));
    outcome.expect("a guest with budget left commits");
    assert_eq!(failover, None);
    assert_eq!(guest.calls(), 1);
    let seen = guest.limits.lock().unwrap_or_else(PoisonError::into_inner);
    let ticks = seen[0].deadline_ticks.expect("a deadline");
    assert!(
        (deadline_ticks_for(Duration::from_millis(3_000))..=deadline_ticks_for(secs(4)))
            .contains(&ticks),
        "{ticks}"
    );
}

/// The deadline is spent when the sandbox stops the guest after the guest's budget, or the guest returns after the call deadline: the
/// call is `E-TIMEOUT` with the transaction rolled back, no failover, and no `task_id`. The first case is the sandbox's own `E-TIMEOUT`
/// landing inside the reserve (after the guest's budget, before the call deadline), which only the host clock tells from fuel.
#[test]
fn a_guest_that_spends_the_deadline_ends_the_call_as_timeout_with_no_failover() {
    let secs = Duration::from_secs;
    // Each case backdates its call by `age`, which leaves the guest `18 s - age` of its own budget when it is called: three seconds, so
    // a host that needs seconds to open the store (load 40 to 150 here) still reaches the call with budget left. The guest then waits
    // until the offset the case aims at, counted from the start of the call, whatever the host's delay was. The guest's budget is 18 s
    // and the call deadline 20 s: 19 s is inside the reserve, 20.5 s is after the deadline.
    let age = secs(15);
    let cases = [
        ("stopped inside the reserve", 19.0, ErrorCode::Timeout),
        ("stopped after the call deadline", 20.5, ErrorCode::Timeout),
        ("failed after the call deadline", 20.5, ErrorCode::Dep),
    ];
    for (label, offset, code) in cases {
        let started = ago(age);
        let (rows, _) = legal();
        let world = World::new(&rows);
        let before = world.db_bytes();
        let guest = Scripted::new(Script::FaultAt(
            started + Duration::from_secs_f64(offset),
            code,
        ));
        let (outcome, failover) = decide_through_writer(&world, &guest, started, secs(20));
        let error = outcome.expect_err("the deadline is spent");
        assert_eq!(error.code(), ErrorCode::Timeout, "{label}");
        assert_eq!(error.cause(), &ApplyCause::Refused, "{label}");
        assert!(error.details().is_empty(), "{label}: {:?}", error.details());
        assert_eq!(failover, None, "{label}: a timeout never fails over");
        assert_eq!(
            world.db_bytes(),
            before,
            "{label}: the store is byte-identical"
        );
        assert_eq!(guest.calls(), 1, "{label}: the guest is asked once");
    }
}

/// The sandbox's `E-TIMEOUT` while the budget is not spent can only be the fuel (the epoch deadline cannot trap before the budget it
/// was made from): a fault of the guest, answered by the host decision (the transaction commits it) under `E-INTERNAL`.
#[test]
fn an_e_timeout_before_the_budget_is_spent_is_fuel_and_the_host_answers() {
    let secs = Duration::from_secs;
    let world = World::new(&[row_in("CREATED", "T-1")]);
    let guest = Scripted::new(Script::SlowFault(
        Duration::from_millis(100),
        ErrorCode::Timeout,
    ));
    let (outcome, failover) = decide_through_writer(&world, &guest, ago(secs(5)), secs(20));
    let applied = outcome.expect("the host decision commits");
    assert_eq!(applied.audit.len(), 1);
    assert_eq!(failover, Some(ErrorCode::Internal));
    assert_eq!(guest.calls(), 1);
}

/// A reply that lands inside the reserve (after the guest's budget, before the call deadline) is still the answer: apply and commit
/// have the rest of the reserve.
#[test]
fn a_reply_inside_the_reserve_is_used() {
    // 15 s of a 20 s call are gone when the guest is called (three seconds of its own budget left, which a loaded host cannot spend
    // before the call); it replies at 19 s, after its budget (18 s) and before the call deadline.
    let secs = Duration::from_secs;
    let world = World::new(&[row_in("CREATED", "T-1")]);
    let started = ago(secs(15));
    let guest = Scripted::new(Script::ReplyAt(started + secs(19)));
    let (outcome, failover) = decide_through_writer(&world, &guest, started, secs(20));
    outcome.expect("a faithful reply inside the reserve commits");
    assert_eq!(failover, None);
    assert_eq!(guest.calls(), 1);
}

// ---- 2. the decider on its own: a spent budget, no decision after a failure, the lock ------------------------------------------

async fn writer(path: &Path, busy: Duration) -> TaskStoreWriter {
    let mut config = WriterConfig::new(path);
    config.busy_timeout = busy;
    TaskStoreWriter::new(open_for_write(&config).await.expect("open_for_write"))
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

const NOW: f64 = 1_700_000_000.25;

/// A budget already spent when the decider is asked: `E-TIMEOUT` before the guest is called, the transaction rolled back.
#[test]
fn a_budget_that_is_spent_before_the_guest_is_called_is_timeout_and_the_guest_is_not_called() {
    let world = World::new(&[row_in("CREATED", "T-1")]);
    let before = world.db_bytes();
    let guest = Scripted::new(Script::Rewrite(|decision| decision));
    let started = Instant::now()
        .checked_sub(Duration::from_secs(10))
        .expect("the clock has run for ten seconds");
    let decider = SandboxDecider::new(&guest, started, Duration::from_secs(5));
    let error = block(async {
        let writer = writer(&world.db(), Duration::from_secs(5)).await;
        let outcome = writer
            .transition(&request("T-1", "VALIDATED"), NOW, &decider)
            .await;
        writer.close().await;
        outcome
    })
    .expect_err("a spent budget is refused");
    assert_eq!(error.code(), ErrorCode::Timeout);
    assert_eq!(error.cause(), &ApplyCause::Refused);
    assert!(error.details().is_empty(), "{:?}", error.details());
    assert_eq!(guest.calls(), 0);
    assert_eq!(decider.failover(), None);
    assert_eq!(world.db_bytes(), before);
}

/// A guest that answers, but only after the deadline: the reply is not used, the call is `E-TIMEOUT`, the transaction is rolled back
/// and the host does not decide in its place.
#[test]
fn a_reply_that_lands_after_the_deadline_is_timeout_and_rolls_back() {
    // 6 s of a 10 s call are gone when the guest is called (three seconds of its own budget left, which a loaded host cannot spend
    // before the call); it replies at 10.4 s, after the call deadline.
    let world = World::new(&[row_in("CREATED", "T-1")]);
    let before = world.db_bytes();
    let started = ago(Duration::from_secs(6));
    let guest = Scripted::new(Script::ReplyAt(started + Duration::from_millis(10_400)));
    let error = block(async {
        let writer = writer(&world.db(), Duration::from_secs(10)).await;
        let decider = SandboxDecider::new(&guest, started, Duration::from_secs(10));
        let outcome = writer
            .transition(&request("T-1", "VALIDATED"), NOW, &decider)
            .await;
        writer.close().await;
        assert_eq!(decider.failover(), None);
        outcome
    })
    .expect_err("a late reply is not used");
    assert_eq!(error.code(), ErrorCode::Timeout);
    assert_eq!(error.cause(), &ApplyCause::Refused);
    assert_eq!(guest.calls(), 1);
    assert_eq!(world.db_bytes(), before);
}

/// Counts the decisions the writer asks for; the wrapped decider does the work.
struct Counting<'a> {
    inner: &'a dyn Decider,
    calls: AtomicUsize,
}

impl Decider for Counting<'_> {
    fn decide(
        &self,
        facts: &Facts,
        request: &Request,
        now: f64,
    ) -> Result<TransitionPlan, TransitionError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.inner.decide(facts, request, now)
    }
}

/// A commit that fails after the guest faulted and the host decided: the call ends with the store's error, and nothing is decided
/// again (no second guest call, no second host decision), so the failover cannot be reached after the commit.
#[test]
fn a_failed_commit_decides_nothing_again() {
    let world = World::new(&[row_in("CREATED", "T-1")]);
    let before = world.db_bytes();
    let guest = Scripted::new(Script::Fault(ErrorCode::Internal));
    let sandbox = SandboxDecider::new(&guest, Instant::now(), DEADLINE);
    let counting = Counting {
        inner: &sandbox,
        calls: AtomicUsize::new(0),
    };
    let error = block(async {
        // A busy timeout long enough for a loaded host to open the store, short enough to end the test soon after the COMMIT waits.
        let writer = writer(&world.db(), Duration::from_secs(4)).await;
        // A reader holds a SHARED lock on the store: the writer can take its RESERVED lock and write, but its COMMIT needs the
        // exclusive lock and finds the store busy.
        let mut reader = connect(&world.db()).await;
        sqlx::query("BEGIN")
            .execute(&mut reader)
            .await
            .expect("begin");
        sqlx::query("SELECT COUNT(*) FROM tasks")
            .fetch_all(&mut reader)
            .await
            .expect("read");
        let outcome = writer
            .transition(&request("T-1", "VALIDATED"), NOW, &counting)
            .await;
        sqlx::query("ROLLBACK")
            .execute(&mut reader)
            .await
            .expect("rollback");
        reader.close().await.expect("close");
        writer.close().await;
        outcome
    })
    .expect_err("the commit cannot take the lock");
    assert!(matches!(error.cause(), ApplyCause::Store(_)), "{error:?}");
    assert_eq!(counting.calls.load(Ordering::SeqCst), 1, "decisions asked");
    assert_eq!(guest.calls(), 1, "guest calls");
    assert_eq!(sandbox.failover(), Some(ErrorCode::Internal));
    assert_eq!(world.db_bytes(), before, "the failed commit left nothing");
}

/// An apply that fails after the decision (a trigger refuses the write), and a manifest that cannot be written after the commit:
/// neither decides again, at the route.
#[test]
fn an_apply_that_fails_and_a_manifest_that_is_lost_leave_the_guest_called_once() {
    // The apply fails: the host decided after the guest's fault, then the store refused the write.
    let rows = [row_in("CREATED", "T-1")];
    let world = World::new(&rows);
    block(exec(
        &world.db(),
        "CREATE TRIGGER refuse_update BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT, 'refused'); END",
    ));
    let before = world.db_bytes();
    let call = world.call(&["--transition", "T-1", "VALIDATED"], DEADLINE);
    let args = parse_transition_args(&call.argv).expect("a transition argv");
    let guest = Scripted::new(Script::Fault(ErrorCode::Internal));
    let engine = SandboxEngine::new().expect("sandbox engine");
    let refusal = engine
        .transition_via(&guest, production, &args, &call)
        .expect_err("the store refuses the write");
    assert_eq!(guest.calls(), 1, "the apply failure did not decide again");
    assert_eq!(engine.failover(), Some(ErrorCode::Internal));
    assert_eq!(refusal.code(), ErrorCode::Internal);
    assert_eq!(
        world.db_bytes(),
        before,
        "the refused apply was rolled back"
    );

    // The commit succeeds and the manifest cannot be written: the reply is still the committed one, the guest was asked once.
    let world = World::new(&rows);
    let mut call = world.call(&["--transition", "T-1", "VALIDATED"], DEADLINE);
    call.effects_path = Some(world.root.join("no-such-dir").join("effects.json"));
    let guest = Scripted::new(Script::Fault(ErrorCode::Capacity));
    let engine = SandboxEngine::new().expect("sandbox engine");
    let reply = engine
        .transition_via(&guest, production, &args, &call)
        .expect("a committed transition is answered");
    assert_eq!(reply.exit_code, 0, "{}", reply.stdout);
    assert_eq!(guest.calls(), 1, "a lost manifest decided nothing again");
    assert_eq!(engine.failover(), Some(ErrorCode::Capacity));
}

/// A guest that blocks until released, to hold the write transaction open.
struct Gate {
    entered: Mutex<mpsc::Sender<()>>,
    release: Mutex<mpsc::Receiver<()>>,
}

impl WriteGuest for Gate {
    fn call(&self, request: &[u8], _limits: &Limits) -> Result<Vec<u8>, ErrorCode> {
        self.entered
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .send(())
            .expect("the test listens");
        self.release
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .recv()
            .expect("the test releases the guest");
        Ok(encode_reply(&faithful(request)).into_bytes())
    }
}

/// The write lock is taken before the guest is called and held until the commit: a second writer is locked out while a slow guest
/// runs, and gets in after the commit.
#[test]
fn the_write_lock_is_held_across_the_guest_call() {
    let world = World::new(&[row_in("CREATED", "T-1")]);
    let path = world.db();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let gate = Gate {
        entered: Mutex::new(entered_tx),
        release: Mutex::new(release_rx),
    };
    let first = std::thread::scope(|scope| {
        let holder = scope.spawn(|| {
            block(async {
                let writer = writer(&path, Duration::from_secs(10)).await;
                let decider = SandboxDecider::new(&gate, Instant::now(), DEADLINE);
                let outcome = writer
                    .transition(&request("T-1", "VALIDATED"), NOW, &decider)
                    .await;
                writer.close().await;
                outcome
            })
        });
        entered_rx
            .recv_timeout(Duration::from_secs(60))
            .expect("the guest is called inside the transaction");
        // The guest is running: a contender cannot begin a write transaction.
        let contender = block(async {
            let contender = writer(&path, Duration::from_secs(3)).await;
            let blocked = contender.store().begin().await;
            contender.close().await;
            blocked
        });
        let locked_out = contender.expect_err("a second writer is locked out");
        assert_eq!(locked_out.kind(), ErrorKind::Busy, "{locked_out:?}");
        release_tx.send(()).expect("release the guest");
        holder.join().expect("the first writer ends")
    });
    first.expect("the first transition commits");
    // After the commit the lock is free again.
    let after = block(async {
        let contender = writer(&path, Duration::from_secs(5)).await;
        let begun = contender.store().begin().await.map(drop);
        contender.close().await;
        begun
    });
    assert!(after.is_ok(), "{after:?}");
}

/// A guest that notes, at each step the decider takes with it, whether a second writer could open a write transaction.
struct Probe {
    path: PathBuf,
    seen: Mutex<Vec<(&'static str, bool)>>,
}

impl Probe {
    fn note(&self, step: &'static str) {
        // Asked from a thread of its own: the call itself runs inside the engine's runtime.
        let free = std::thread::scope(|scope| {
            scope
                .spawn(|| {
                    block(async {
                        let contender = writer(&self.path, Duration::from_millis(300)).await;
                        let free = contender.store().begin().await.map(drop).is_ok();
                        contender.close().await;
                        free
                    })
                })
                .join()
                .expect("the probe thread ends")
        });
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push((step, free));
    }
}

impl WriteGuest for Probe {
    fn prepare(&self) -> Result<(), ErrorCode> {
        self.note("prepare");
        Ok(())
    }

    fn call(&self, request: &[u8], _limits: &Limits) -> Result<Vec<u8>, ErrorCode> {
        self.note("call");
        Ok(encode_reply(&faithful(request)).into_bytes())
    }
}

/// The module is made ready (for the engine: pin check and compile) before the writer's transaction opens, never under the write lock:
/// the guest is prepared once, with the lock free, and called once, with the lock held.
#[test]
fn the_guest_is_prepared_before_the_write_transaction_opens() {
    let (rows, argv) = legal();
    let world = World::new(&rows);
    let call = world.call(&argv, DEADLINE);
    let args = parse_transition_args(&call.argv).expect("a transition argv");
    let probe = Probe {
        path: world.db(),
        seen: Mutex::new(Vec::new()),
    };
    let engine = SandboxEngine::new().expect("sandbox engine");
    let reply = engine
        .transition_via(&probe, production, &args, &call)
        .expect("a legal transition commits");
    assert_eq!(reply.exit_code, 0, "{}", reply.stdout);
    assert_eq!(
        *probe.seen.lock().unwrap_or_else(PoisonError::into_inner),
        vec![("prepare", true), ("call", false)],
        "prepared with the write lock free, called with it held"
    );
    assert_eq!(engine.failover(), None);
}

/// A guest that cannot be prepared (a pin mismatch, a compile failure) is remembered: the guest is prepared once, never called, and the
/// host decision answers with the host backend's bytes, under the sandbox's code for the failure. An `E-TIMEOUT` is not a code a load
/// has and is recorded as `E-INTERNAL`.
#[test]
fn a_guest_that_cannot_be_prepared_is_not_called_and_the_host_answers() {
    let cases = [
        (ErrorCode::Dep, ErrorCode::Dep),
        (ErrorCode::Capacity, ErrorCode::Capacity),
        (ErrorCode::Internal, ErrorCode::Internal),
        (ErrorCode::Timeout, ErrorCode::Internal),
    ];
    for (loaded_as, recorded) in cases {
        let (rows, argv) = single_hop();
        let guest =
            Scripted::new(Script::Rewrite(|decision| decision)).failing_to_prepare(loaded_as);
        let (sandbox, failover, _) = run_sandbox(&rows, &argv, &guest, DEADLINE);
        assert_eq!(sandbox, run_host(&rows, &argv), "{loaded_as:?}");
        assert_eq!(sandbox.exit_code, 0, "{}", sandbox.stdout);
        assert_eq!(failover, Some(recorded), "{loaded_as:?}");
        assert_eq!(guest.prepares(), 1, "{loaded_as:?}: prepared once");
        assert_eq!(guest.calls(), 0, "{loaded_as:?}: never called");
    }
}

// ---- 3. the build without the write guest ---------------------------------------------------------------------------------

/// Without the `sandbox-write` feature the embedded guest has no write mode: a write on the sandbox backend is refused before it
/// starts, naming the feature, and the store is untouched.
#[cfg(not(feature = "sandbox-write"))]
#[test]
fn without_the_feature_a_write_on_the_sandbox_backend_is_refused_before_any_write() {
    let rows = [row_in("CREATED", "T-1")];
    let world = World::new(&rows);
    let before = world.db_bytes();
    let call = world.call(&["--transition", "T-1", "VALIDATED"], DEADLINE);
    let engine = SandboxEngine::new().expect("sandbox engine");
    let refusal = engine
        .orch_status(&call)
        .expect_err("no write guest in this build");
    assert_eq!(refusal.code(), ErrorCode::Dep);
    assert!(
        refusal.message().contains("sandbox-write"),
        "{}",
        refusal.message()
    );
    assert!(
        refusal
            .details()
            .iter()
            .any(|(name, value)| name == "write_state" && *value == jstr("not_started")),
        "{:?}",
        refusal.details()
    );
    assert_eq!(engine.failover(), None);
    assert_eq!(world.db_bytes(), before);
    assert_eq!(world.manifest(), "");
}

// ---- 4. the real write guest ------------------------------------------------------------------------------------------------

#[cfg(feature = "sandbox-write")]
mod real_guest {
    use super::*;
    use gsd_route::transition_plan::ApprovalFacts;
    use gsd_taskwriter::{HostDecider, WriteState};
    use std::sync::Arc;

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

    fn names(value: &Json) -> Vec<String> {
        items(value)
            .iter()
            .map(|name| text(name).to_string())
            .collect()
    }

    fn number(value: &Json) -> f64 {
        match value {
            Json::Float(number) => *number,
            Json::Int(number) => *number as f64,
            other => panic!("not a number: {other:?}"),
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

    /// The writer does not read gate facts yet, so a vector supplies them here, ahead of whichever decider runs.
    struct WithApproval<'a> {
        approval: Option<ApprovalFacts>,
        inner: &'a dyn Decider,
    }

    impl Decider for WithApproval<'_> {
        fn decide(
            &self,
            facts: &Facts,
            request: &Request,
            now: f64,
        ) -> Result<TransitionPlan, TransitionError> {
            let mut facts = facts.clone();
            if self.approval.is_some() {
                facts.approval.clone_from(&self.approval);
            }
            self.inner.decide(&facts, request, now)
        }
    }

    /// How a vector ended.
    #[derive(Debug, PartialEq)]
    enum Ended {
        Applied,
        Refused,
        NotStarted,
    }

    async fn run_case(
        case: Json,
        dir: PathBuf,
        engine: Arc<SandboxEngine>,
    ) -> (String, Ended, Option<ErrorCode>) {
        let id = text(member(&case, "id")).to_string();
        let now = number(member(&case, "now"));
        let request = request_of(&case);
        let approval = approval_of(member(&case, "approval"));
        let rows: Vec<Fields> = match member(&case, "task") {
            Json::Null => Vec::new(),
            row => vec![row_of(row)],
        };
        let sandbox_path = dir.join(format!("{id}.sandbox.db"));
        let host_path = dir.join(format!("{id}.host.db"));
        for path in [&sandbox_path, &host_path] {
            fs::write(path, &*TEMPLATE).expect("copy template");
            for row in &rows {
                insert_task(path, row).await;
            }
        }
        let before = fs::read(&sandbox_path).expect("read store");

        let guest_decider = SandboxDecider::new(&*engine, Instant::now(), DEADLINE);
        let sandbox_writer = writer(&sandbox_path, Duration::from_secs(30)).await;
        let sandbox: Result<Applied, ApplyError> = sandbox_writer
            .transition(
                &request,
                now,
                &WithApproval {
                    approval: approval.clone(),
                    inner: &guest_decider,
                },
            )
            .await;
        sandbox_writer.close().await;

        let host_writer = writer(&host_path, Duration::from_secs(30)).await;
        let host = host_writer
            .transition(
                &request,
                now,
                &WithApproval {
                    approval,
                    inner: &HostDecider,
                },
            )
            .await;
        host_writer.close().await;

        assert_eq!(sandbox, host, "{id}: the guest and the host decide alike");
        assert_eq!(
            content(&sandbox_path, false).await,
            content(&host_path, false).await,
            "{id}: the stores are equal"
        );
        let ended = match &sandbox {
            Ok(_) => Ended::Applied,
            Err(error) if error.write_state() == WriteState::NotStarted => Ended::NotStarted,
            Err(_) => Ended::Refused,
        };
        if ended != Ended::Applied {
            assert_eq!(
                fs::read(&sandbox_path).expect("read store"),
                before,
                "{id}: a refusal leaves the store byte-identical"
            );
        }
        (id, ended, guest_decider.failover())
    }

    /// Every transition vector (351, from the real `TaskStore.transition`) through the REAL write guest: the same `Applied` (row, audit
    /// rows, effects) or refusal as the host decision, the same store, and no failover: the guest served every one.
    #[test]
    fn every_vector_through_the_real_write_guest_gives_the_host_answer() {
        let engine = Arc::new(SandboxEngine::new().expect("sandbox engine"));
        let doc = parse_json(VECTORS).expect("vector file parses");
        let cases = items(member(&doc, "cases")).to_vec();
        assert_eq!(cases.len(), 351);
        let dir = TempDir::new().expect("tempdir");
        LazyLock::force(&TEMPLATE);
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .enable_all()
            .build()
            .expect("runtime");
        let results = runtime.block_on(async {
            let mut running = tokio::task::JoinSet::new();
            for case in cases {
                running.spawn(run_case(
                    case,
                    dir.path().to_path_buf(),
                    Arc::clone(&engine),
                ));
            }
            let mut results = Vec::new();
            while let Some(done) = running.join_next().await {
                results.push(done.expect("a case panicked"));
            }
            results
        });
        drop(runtime);
        let failed_over: Vec<_> = results
            .iter()
            .filter(|(_, _, failover)| failover.is_some())
            .map(|(id, _, failover)| (id.as_str(), *failover))
            .collect();
        assert!(
            failed_over.is_empty(),
            "the guest faulted on {failed_over:?}"
        );
        let tally = |wanted: Ended| {
            results
                .iter()
                .filter(|(_, ended, _)| *ended == wanted)
                .count()
        };
        assert_eq!(
            (
                tally(Ended::Applied),
                tally(Ended::Refused),
                tally(Ended::NotStarted)
            ),
            (121, 196, 34)
        );
    }

    /// The route on the sandbox backend, in process, prints what the host backend prints and leaves what it leaves, on the seeds of
    /// the matrix above, and does not fail over.
    #[test]
    fn the_route_on_the_real_guest_matches_the_host_backend() {
        let engine = SandboxEngine::new().expect("sandbox engine");
        for seed in [legal as Seed, single_hop, two_hop] {
            let (rows, argv) = seed();
            let world = World::new(&rows);
            let call = world.call(&argv, DEADLINE);
            let sandbox = observe(&world, engine.orch_status(&call));
            assert_eq!(engine.failover(), None, "{argv:?}");
            assert_eq!(sandbox, run_host(&rows, &argv), "{argv:?}");
            assert_eq!(sandbox.exit_code, 0, "{}", sandbox.stdout);
            assert!(
                sandbox.manifest.contains("live_refresh"),
                "{}",
                sandbox.manifest
            );
        }
        // An illegal transition and an unknown task are Python's own refusals, answered by the guest.
        for (rows, argv) in [
            (
                vec![row_in("DONE", "T-1")],
                vec!["--transition", "T-1", "CREATED"],
            ),
            (Vec::new(), vec!["--transition", "T-none", "VALIDATED"]),
        ] {
            let world = World::new(&rows);
            let call = world.call(&argv, DEADLINE);
            let sandbox = observe(&world, engine.orch_status(&call));
            assert_eq!(engine.failover(), None, "{argv:?}");
            assert_eq!(sandbox, run_host(&rows, &argv), "{argv:?}");
            assert_eq!(sandbox.exit_code, 2);
        }
    }

    /// What the `gsd_engine` binary printed for one call.
    struct Ran {
        code: i32,
        stdout: String,
        stderr: String,
    }

    /// The binary on `backend`, called as the selector calls it: the request on stdin, an empty environment but `PATH`, `HOME` and
    /// `GSD_RUST_BACKEND`.
    fn binary(world: &World, argv: &[&str], backend: &str) -> Ran {
        let call = world.call(argv, DEADLINE);
        let request = serde_json::json!({
            "agent": "A01",
            "script": "orch_status",
            "argv": call.argv,
            "root": world.root.display().to_string(),
            "env": {"SWARM_DIR": world.swarm.display().to_string()},
            "effects_path": world.effects.display().to_string(),
        });
        let mut command = std::process::Command::new(env!("CARGO_BIN_EXE_gsd_engine"));
        command
            .current_dir(&world.root)
            .env_clear()
            .env("GSD_RUST_BACKEND", backend)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
        for name in ["PATH", "HOME"] {
            if let Some(value) = std::env::var_os(name) {
                command.env(name, value);
            }
        }
        let mut child = command.spawn().expect("spawn gsd_engine");
        std::io::Write::write_all(
            &mut child.stdin.take().expect("stdin"),
            request.to_string().as_bytes(),
        )
        .expect("write request");
        let output = child.wait_with_output().expect("wait");
        Ran {
            code: output.status.code().expect("exit code"),
            stdout: String::from_utf8(output.stdout).expect("utf8 stdout"),
            stderr: String::from_utf8(output.stderr).expect("utf8 stderr"),
        }
    }

    /// The binary on the sandbox backend serves a write from the guest: the bytes on stdout, the exit code, the effects manifest and
    /// the store equal the host backend's on a twin store, and stderr is silent because nothing failed over.
    #[test]
    fn the_binary_on_the_sandbox_backend_prints_the_host_backends_bytes() {
        let (gate_rows, gate_argv) = vector_seed("gate-to-in-review-manual");
        let gate_argv: Vec<&str> = gate_argv.iter().map(String::as_str).collect();
        let (legal_rows, legal_argv) = legal();
        let (hop_rows, hop_argv) = two_hop();
        let cases: Vec<(&str, Vec<Fields>, Vec<&str>)> = vec![
            ("a legal transition", legal_rows, legal_argv),
            ("the auto-escalation", hop_rows, hop_argv),
            (
                "an illegal transition",
                vec![row_in("DONE", "T-1")],
                vec!["--transition", "T-1", "CREATED"],
            ),
            ("a gate task moved by hand", gate_rows, gate_argv),
            (
                "an unknown task",
                Vec::new(),
                vec!["--transition", "T-none", "VALIDATED"],
            ),
        ];
        for (label, rows, argv) in cases {
            let (sandbox_world, host_world) = (World::new(&rows), World::new(&rows));
            let sandbox = binary(&sandbox_world, &argv, "sandbox");
            let host = binary(&host_world, &argv, "host");
            assert_eq!(sandbox.code, host.code, "{label}: {}", sandbox.stdout);
            assert_eq!(mask(&sandbox.stdout), mask(&host.stdout), "{label}");
            assert_eq!(sandbox.stderr, "", "{label}: nothing failed over");
            assert_eq!(host.stderr, "", "{label}");
            assert_eq!(sandbox_world.manifest(), host_world.manifest(), "{label}");
            assert_eq!(
                block(content(&sandbox_world.db(), true)),
                block(content(&host_world.db(), true)),
                "{label}"
            );
        }
    }

    fn starved_without_deadline(_budget: Duration) -> Limits {
        Limits {
            fuel: 1_000,
            memory_bytes: 16 * 1024 * 1024,
            deadline_ticks: None,
        }
    }

    fn starved_with_deadline(budget: Duration) -> Limits {
        Limits {
            fuel: 1_000,
            memory_bytes: 16 * 1024 * 1024,
            deadline_ticks: Some(deadline_ticks_for(budget)),
        }
    }

    /// A cap of one page is below what the module needs to be instantiated at all.
    fn one_page(_budget: Duration) -> Limits {
        Limits {
            fuel: 1_000_000_000,
            memory_bytes: 64 * 1024,
            deadline_ticks: None,
        }
    }

    /// A cap the module instantiates under and a request with a 3 MiB `notes` outgrows.
    fn four_mib(_budget: Duration) -> Limits {
        Limits {
            fuel: 1_000_000_000,
            memory_bytes: 4 * 1024 * 1024,
            deadline_ticks: None,
        }
    }

    fn no_fuel(_budget: Duration) -> Limits {
        Limits {
            fuel: 0,
            memory_bytes: 16 * 1024 * 1024,
            deadline_ticks: None,
        }
    }

    /// CREATED → VALIDATED on a task whose `notes` are 3 MiB of valid JSON.
    fn big_notes() -> (Vec<Fields>, Vec<&'static str>) {
        let (mut rows, argv) = legal();
        let notes = format!("{{\"pad\": \"{}\"}}", "x".repeat(3 * 1024 * 1024));
        set(&mut rows[0], "notes", jstr(&notes));
        (rows, argv)
    }

    /// The per-call limits of a guest, made from the budget left.
    type MakeLimits = fn(Duration) -> Limits;

    /// The real guest under limits it cannot meet: every one is a fault before the apply step, so the host decides and the call is
    /// answered with the host backend's stdout, exit code, manifest and store, with the failover recorded under the sandbox's code
    /// for the fault. Fuel exhausted under a deadline is a fault too while the budget is not spent: the sandbox maps it to
    /// `E-TIMEOUT`, and the decider reads that as fuel because the host clock says the deadline is far off (D-P29, Design 5).
    ///
    /// This is where the sandbox's real classification is held (the matrix of the scripted guest takes the codes as given): fuel with
    /// and without a deadline, memory that cannot be instantiated, memory outgrown by a request, and limits the sandbox refuses.
    ///
    /// This test's premise is reversed by D-P29: it used to assert that the guest out of fuel under a deadline is the deadline spent,
    /// a final `E-TIMEOUT` with nothing written, which production limits (always with a deadline) turned every fuel exhaustion into.
    #[test]
    fn the_real_guest_under_starved_limits_fails_over() {
        let engine = SandboxEngine::new().expect("sandbox engine");
        let cases: [(&str, Seed, MakeLimits, ErrorCode); 5] = [
            (
                "fuel without a deadline",
                single_hop,
                starved_without_deadline,
                ErrorCode::Internal,
            ),
            (
                "fuel under a deadline with time left",
                single_hop,
                starved_with_deadline,
                ErrorCode::Internal,
            ),
            (
                "memory of one page: the module cannot be instantiated",
                single_hop,
                one_page,
                ErrorCode::Dep,
            ),
            (
                "memory outgrown by the request",
                big_notes,
                four_mib,
                ErrorCode::Capacity,
            ),
            (
                "limits the sandbox refuses at instantiation",
                single_hop,
                no_fuel,
                ErrorCode::Input,
            ),
        ];
        for (label, seed, make, wanted) in cases {
            let (rows, argv) = seed();
            let host = run_host(&rows, &argv);
            let world = World::new(&rows);
            let call = world.call(&argv, DEADLINE);
            let args = parse_transition_args(&call.argv).expect("a transition argv");
            let sandbox = observe(&world, engine.transition_via(&engine, make, &args, &call));
            assert_eq!(
                engine.failover(),
                Some(wanted),
                "{label}: the failover code of the fault"
            );
            assert_eq!(
                sandbox, host,
                "{label}: answered as the host backend answers"
            );
            assert_eq!(sandbox.exit_code, 0, "{label}: {}", sandbox.stdout);
        }
    }

    /// A transition to APPROVED on the real guest (unit U3.4d): the host reads the key policy and the Python-signed verdict rows inside
    /// its own transaction and hands the guest booleans and gate names; the guest's decision is the host's, with no failover, for an
    /// approval and for each of Python's policy refusals. (The writer takes its keys from a fixed environment here: a process-wide key
    /// variable would race the other tests of this binary.)
    #[test]
    fn the_real_guest_serves_an_approved_transition_with_no_failover() {
        use gsd_taskwriter::{FixedApprovalSource, WriteState as RefusalState};

        let key: String = (0..4)
            .map(|_| {
                use std::collections::hash_map::RandomState;
                use std::hash::{BuildHasher, Hasher};
                format!("{:016x}", RandomState::new().build_hasher().finish())
            })
            .collect();
        let sign = vec![("SWARM_SIGNING_KEY".to_string(), key.clone())];
        let engine = SandboxEngine::new().expect("sandbox engine");
        // label, store spec, the environment of the run, the refusal text Python gives (None when it approves)
        type Case<'a> = (&'a str, &'a str, Vec<(String, String)>, Option<&'a str>);
        let cases: [Case; 4] = [
            (
                "a signed approval",
                r#"{"verdicts": [["review", "pass"], ["quality", "pass"]]}"#,
                sign.clone(),
                None,
            ),
            (
                "a failing gate",
                r#"{"verdicts": [["review", "pass"], ["quality", "fail"]]}"#,
                sign.clone(),
                Some("lacks passing gates ['quality']"),
            ),
            (
                "an unaccepted gate task",
                r#"{"verdicts": [["review", "pass"], ["quality", "pass"]], "issuer": "FAILED"}"#,
                sign.clone(),
                Some("has unaccepted gates ['quality', 'review']"),
            ),
            (
                "no key",
                r#"{"verdicts": [["review", "pass"], ["quality", "pass"]]}"#,
                Vec::new(),
                Some("fail-closed: no signing key configured"),
            ),
        ];
        for (label, spec, run_env, refusal) in cases {
            let dir = TempDir::new().expect("tempdir");
            let scratch = dir.path().join("scratch");
            fs::create_dir(&scratch).expect("scratch");
            let host_db = dir.path().join("host.db");
            let sandbox_db = dir.path().join("sandbox.db");
            signed_store::seed_into(&repo_root(), &host_db, &scratch, spec, &sign);
            fs::copy(&host_db, &sandbox_db).expect("copy the signed store");
            let source = std::sync::Arc::new(FixedApprovalSource::new(run_env.clone()));
            let decide = |path: PathBuf, decider: &dyn Decider| {
                block(async {
                    let store = open_for_write(&WriterConfig::new(&path))
                        .await
                        .expect("open_for_write");
                    let writer = TaskStoreWriter::with_approval(store, source.clone());
                    let outcome = writer
                        .transition(&request("T-1", "APPROVED"), NOW, decider)
                        .await;
                    writer.close().await;
                    outcome
                })
            };
            let host = decide(host_db.clone(), &gsd_taskwriter::HostDecider);
            let shadow = SandboxDecider::new(&engine, Instant::now(), DEADLINE);
            let served = decide(sandbox_db.clone(), &shadow);
            assert_eq!(
                shadow.failover(),
                None,
                "{label}: the guest served, no failover"
            );
            match (&host, &served, refusal) {
                (Ok(host), Ok(served), None) => {
                    assert_eq!(host.task, served.task, "{label}: the committed row");
                    assert_eq!(
                        obj_get(&served.task, "state"),
                        Some(&jstr("APPROVED")),
                        "{label}"
                    );
                }
                (Err(host), Err(served), Some(text)) => {
                    assert_eq!(
                        (host.code(), host.message()),
                        (served.code(), served.message()),
                        "{label}"
                    );
                    assert_eq!(served.code(), ErrorCode::Policy, "{label}");
                    assert_eq!(served.write_state(), RefusalState::Final, "{label}");
                    assert!(
                        served.message().contains(text),
                        "{label}: {}",
                        served.message()
                    );
                }
                other => panic!("{label}: the guest and the host disagree: {other:?}"),
            }
            assert_eq!(
                block(content(&sandbox_db, true)),
                block(content(&host_db, true)),
                "{label}: the stores are the same"
            );
            let spent = format!("{host:?}{served:?}");
            assert!(
                !spent.contains(&key),
                "{label}: the key is in a Debug output"
            );
        }
    }
}
