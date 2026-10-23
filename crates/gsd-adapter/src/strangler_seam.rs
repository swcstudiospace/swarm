//! Strangler seam between the Python swarm scripts and the Rust core (U4.1).
//!
//! One [`CoreBackend`] method exists per frozen entrypoint. Both engines receive the same
//! [`Invocation`] and answer with the same [`Reply`] shape, so the argv, exit-code and JSON
//! contracts of `plan/rust-wasmer-omp-options.md` §1.2 hold whichever engine runs.
//! [`invoke`] enforces the 100 KB `--input` cap and the 0/1/2 exit contract at the seam.
//!
//! The Rust engine serves only the read-only `orch_status` route, against the SQLite Task Store
//! (`swarm/taskstore.py`). Every other entrypoint refuses with `E-DEP` until its route flips.

use std::fs;
use std::io::{ErrorKind, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use gsd_failure::{error_code_from_python, ErrorCode};
use gsd_route::{
    check_safe_id, count_state, empty_status, finish, history_fields, jstr, obj_get, parse_json,
    push_latest, run_log_counts, satisfied_ids, status_list, status_row, to_python_json, Fields,
    Json, LatestRow, ReadArgs, StatusRow, Task,
};
use gsd_route::{parse_orch_status_read, Reply, SeamError};
use sqlx::sqlite::{SqliteConnectOptions, SqlitePool, SqlitePoolOptions, SqliteRow};
use sqlx::{Column, Row, TypeInfo, ValueRef};

/// Largest `--input` payload accepted at the seam, in UTF-8 bytes (`scripts/swarm_tool.py` `MAX_INPUT_BYTES`).
pub const INPUT_CAP_BYTES: usize = 100_000;

/// Stderr characters kept in a usage-error message (`swarm/script_base.py` tail rule).
const STDERR_TAIL_CHARS: usize = 400;

/// One frozen script invocation, identical for both engines.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Invocation {
    /// Agent id the script reports (`A01` for `orch_status`).
    pub agent: String,
    /// Script stem, e.g. `orch_status`; must match `[a-z][a-z0-9_]*`.
    pub script: String,
    /// Flags as passed on the command line, without the trailing `--json`.
    pub argv: Vec<String>,
    /// Repository root; the working directory of the call.
    pub root: PathBuf,
    /// Environment overlay: `SWARM_DIR`, `SWARM_TASK_ID`, `SWARM_CORRELATION_ID`.
    pub env: Vec<(String, String)>,
    /// Deadline after which the call fails with `E-TIMEOUT`.
    pub deadline: Duration,
}

/// The frozen entrypoints, one per [`CoreBackend`] method.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Entrypoint {
    /// `scripts/swarm_run.py`.
    SwarmRun,
    /// `scripts/orch_plan.py`.
    OrchPlan,
    /// `scripts/orch_status.py`.
    OrchStatus,
    /// `scripts/orch_assign.py`.
    OrchAssign,
    /// `scripts/qa_gate.py`.
    QaGate,
    /// `scripts/rev_gate.py`.
    RevGate,
    /// `scripts/sec_gate.py`.
    SecGate,
    /// `scripts/rel_plan.py`.
    RelPlan,
    /// `scripts/swarm_tool.py`.
    SwarmTool,
    /// Any `scripts/<lane>_<verb>.py` passthrough.
    LaneVerb,
}

impl Entrypoint {
    /// Maps a script stem onto its entrypoint; unknown stems are lane-verb passthroughs.
    pub fn from_script(script: &str) -> Self {
        match script {
            "swarm_run" => Entrypoint::SwarmRun,
            "orch_plan" => Entrypoint::OrchPlan,
            "orch_status" => Entrypoint::OrchStatus,
            "orch_assign" => Entrypoint::OrchAssign,
            "qa_gate" => Entrypoint::QaGate,
            "rev_gate" => Entrypoint::RevGate,
            "sec_gate" => Entrypoint::SecGate,
            "rel_plan" => Entrypoint::RelPlan,
            "swarm_tool" => Entrypoint::SwarmTool,
            _ => Entrypoint::LaneVerb,
        }
    }
}

/// The single strangler seam: one method per frozen entrypoint, shared by both engines.
pub trait CoreBackend {
    /// Runs the `swarm_run.py` continuous scheduler.
    fn swarm_run(&self, call: &Invocation) -> Result<Reply, SeamError>;
    /// Runs `orch_plan.py`.
    fn orch_plan(&self, call: &Invocation) -> Result<Reply, SeamError>;
    /// Runs `orch_status.py` (reads, transitions, ingest).
    fn orch_status(&self, call: &Invocation) -> Result<Reply, SeamError>;
    /// Runs `orch_assign.py`.
    fn orch_assign(&self, call: &Invocation) -> Result<Reply, SeamError>;
    /// Runs `qa_gate.py`.
    fn qa_gate(&self, call: &Invocation) -> Result<Reply, SeamError>;
    /// Runs `rev_gate.py`.
    fn rev_gate(&self, call: &Invocation) -> Result<Reply, SeamError>;
    /// Runs `sec_gate.py`.
    fn sec_gate(&self, call: &Invocation) -> Result<Reply, SeamError>;
    /// Runs `rel_plan.py`.
    fn rel_plan(&self, call: &Invocation) -> Result<Reply, SeamError>;
    /// Runs `swarm_tool.py` (registry bridge, 100 KB input cap).
    fn swarm_tool(&self, call: &Invocation) -> Result<Reply, SeamError>;
    /// Runs any `<lane>_<verb>.py` passthrough.
    fn lane_verb(&self, call: &Invocation) -> Result<Reply, SeamError>;
}

/// Returns true when `script` matches the allowlist `^[a-z][a-z0-9_]*$`.
pub fn is_valid_stem(script: &str) -> bool {
    let mut chars = script.chars();
    match chars.next() {
        Some(first) if first.is_ascii_lowercase() => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// Rejects an `--input` payload above [`INPUT_CAP_BYTES`], in both `--input V` and `--input=V` forms.
pub fn check_input_cap(argv: &[String]) -> Result<(), SeamError> {
    let mut index = 0;
    while index < argv.len() {
        let token = argv[index].as_str();
        let value: Option<&str> = if token == "--input" {
            index += 1;
            argv.get(index).map(|v| v.as_str())
        } else {
            token.strip_prefix("--input=")
        };
        if let Some(payload) = value {
            if payload.len() > INPUT_CAP_BYTES {
                return Err(SeamError::new(
                    ErrorCode::Input,
                    format!("--input exceeds {INPUT_CAP_BYTES} bytes"),
                ));
            }
        }
        index += 1;
    }
    Ok(())
}

/// Enforces the 0/1/2 exit contract on one engine answer.
///
/// Exit 0 needs a JSON object; exit 1 needs a JSON object with `status` = `fail`; exit 2 needs a JSON
/// object whose `error.code` is one of the seven shared codes, or empty stdout (a usage error,
/// `E-INPUT`). Anything else is `E-INTERNAL`.
pub fn enforce_exit_contract(
    exit_code: i32,
    stdout: &str,
    stderr_tail: &str,
) -> Result<Reply, SeamError> {
    let accepted = match exit_code {
        0 => parse_object(stdout).is_some(),
        1 => match parse_object(stdout) {
            Some(fields) => {
                matches!(obj_get(&fields, "status"), Some(Json::Str(status)) if status.as_str() == "fail")
            }
            None => false,
        },
        2 => {
            if stdout.trim().is_empty() {
                return Err(SeamError::new(
                    ErrorCode::Input,
                    format!("usage error: {}", tail(stderr_tail, STDERR_TAIL_CHARS)),
                ));
            }
            match parse_object(stdout) {
                Some(fields) => error_code_of(&fields).is_some(),
                None => false,
            }
        }
        _ => false,
    };
    if accepted {
        return Ok(Reply {
            exit_code,
            stdout: stdout.to_string(),
        });
    }
    Err(SeamError::new(
        ErrorCode::Internal,
        format!(
            "exit {exit_code} violates the 0/1/2 contract; stdout tail: {}; stderr tail: {}",
            tail(stdout, STDERR_TAIL_CHARS),
            tail(stderr_tail, STDERR_TAIL_CHARS)
        ),
    ))
}

fn parse_object(stdout: &str) -> Option<Vec<(String, Json)>> {
    match parse_json(stdout.trim()).ok()? {
        Json::Obj(fields) => Some(fields),
        _ => None,
    }
}

fn error_code_of(fields: &[(String, Json)]) -> Option<ErrorCode> {
    match obj_get(fields, "error") {
        Some(Json::Obj(error)) => match obj_get(error, "code") {
            Some(Json::Str(code)) => error_code_from_python(code.as_str()),
            _ => None,
        },
        _ => None,
    }
}

fn tail(text: &str, limit: usize) -> String {
    let count = text.chars().count();
    text.chars().skip(count.saturating_sub(limit)).collect()
}

/// Renders a failed call as the `status: error` object the Python scripts print, exit code 2.
pub fn error_reply(agent: &str, script: &str, err: &SeamError) -> Reply {
    let body = Json::Obj(vec![
        ("agent".to_string(), jstr(agent)),
        ("script".to_string(), jstr(script)),
        ("status".to_string(), jstr("error")),
        ("error".to_string(), err.to_json()),
    ]);
    Reply {
        exit_code: 2,
        stdout: format!("{}\n", to_python_json(&body)),
    }
}

/// Runs one invocation through `backend` with every seam rule enforced.
///
/// The script stem, the `--input` cap and the exit contract are checked here for either engine.
/// Failures never escape: they come back as an `exit 2` error reply.
pub fn invoke<B: CoreBackend + ?Sized>(backend: &B, call: &Invocation) -> Reply {
    match invoke_checked(backend, call) {
        Ok(reply) => reply,
        Err(err) => error_reply(&call.agent, &call.script, &err),
    }
}

fn invoke_checked<B: CoreBackend + ?Sized>(
    backend: &B,
    call: &Invocation,
) -> Result<Reply, SeamError> {
    if !is_valid_stem(&call.script) {
        return Err(SeamError::new(
            ErrorCode::Input,
            "script name must match [a-z][a-z0-9_]*",
        ));
    }
    check_input_cap(&call.argv)?;
    let reply = match Entrypoint::from_script(&call.script) {
        Entrypoint::SwarmRun => backend.swarm_run(call),
        Entrypoint::OrchPlan => backend.orch_plan(call),
        Entrypoint::OrchStatus => backend.orch_status(call),
        Entrypoint::OrchAssign => backend.orch_assign(call),
        Entrypoint::QaGate => backend.qa_gate(call),
        Entrypoint::RevGate => backend.rev_gate(call),
        Entrypoint::SecGate => backend.sec_gate(call),
        Entrypoint::RelPlan => backend.rel_plan(call),
        Entrypoint::SwarmTool => backend.swarm_tool(call),
        Entrypoint::LaneVerb => backend.lane_verb(call),
    }?;
    enforce_exit_contract(reply.exit_code, &reply.stdout, "")
}

/// Canonicalises `path`, falling back to the path as given when it does not exist.
fn canonical_or(path: &Path) -> PathBuf {
    fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

/// Returns the value of `key` in `env` when it is set and non-empty.
pub fn env_value<'a>(env: &'a [(String, String)], key: &str) -> Option<&'a str> {
    env.iter()
        .find(|(name, _)| name.as_str() == key)
        .map(|(_, value)| value.as_str())
        .filter(|v| !v.is_empty())
}

/// Resolves the Swarm state directory: env `SWARM_DIR` → `<git toplevel of root>/.swarm` → `<root>/.swarm`.
pub fn resolve_swarm_dir(root: &Path, env_swarm_dir: Option<&str>) -> PathBuf {
    if let Some(value) = env_swarm_dir.filter(|v| !v.is_empty()) {
        let cwd = std::env::current_dir().unwrap_or_else(|_| root.to_path_buf());
        return canonical_or(&cwd.join(value));
    }
    let base = canonical_or(root);
    match git_toplevel(&base) {
        Some(top) => canonical_or(Path::new(&top)).join(".swarm"),
        None => base.join(".swarm"),
    }
}

fn git_toplevel(base: &Path) -> Option<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(base)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let top = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if top.is_empty() {
        None
    } else {
        Some(top)
    }
}

/// Lane-verb and gate passthrough to the Python scripts, with the frozen argv and env contract.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PythonEngine {
    python: String,
}

impl PythonEngine {
    /// Uses the given interpreter (normally `python3`).
    pub fn new(python: impl Into<String>) -> Self {
        Self {
            python: python.into(),
        }
    }

    fn run_script(&self, call: &Invocation) -> Result<Reply, SeamError> {
        let script_path = call
            .root
            .join("scripts")
            .join(format!("{}.py", call.script));
        if !script_path.is_file() {
            return Err(SeamError::new(
                ErrorCode::Dep,
                format!("script not found: {}", call.script),
            ));
        }
        let swarm_dir = resolve_swarm_dir(&call.root, env_value(&call.env, "SWARM_DIR"));
        let mut command = Command::new(&self.python);
        command
            .arg(&script_path)
            .args(&call.argv)
            .arg("--json")
            .current_dir(&call.root)
            .envs(
                call.env
                    .iter()
                    .map(|(key, value)| (key.as_str(), value.as_str())),
            )
            .env("SWARM_DIR", swarm_dir.as_os_str())
            .env_remove("SWARM_TASK_ID")
            .env_remove("SWARM_CORRELATION_ID")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn().map_err(|e| {
            SeamError::new(ErrorCode::Dep, format!("cannot start {}: {e}", self.python))
        })?;
        let stdout_reader = child.stdout.take().map(read_all);
        let stderr_reader = child.stderr.take().map(read_all);
        let started = Instant::now();
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) => {
                    if started.elapsed() >= call.deadline {
                        let _ = child.kill();
                        let _ = child.wait();
                        return Err(SeamError::new(
                            ErrorCode::Timeout,
                            format!("{} exceeded {} s", call.script, call.deadline.as_secs()),
                        ));
                    }
                    thread::sleep(Duration::from_millis(5));
                }
                Err(e) => {
                    return Err(SeamError::new(
                        ErrorCode::Internal,
                        format!("wait failed: {e}"),
                    ))
                }
            }
        };
        let Some(exit_code) = status.code() else {
            return Err(SeamError::new(
                ErrorCode::Internal,
                "script terminated by a signal",
            ));
        };
        let stdout = String::from_utf8(join_bytes(stdout_reader))
            .map_err(|_| SeamError::new(ErrorCode::Internal, "stdout is not UTF-8"))?;
        let stderr = String::from_utf8_lossy(&join_bytes(stderr_reader)).into_owned();
        enforce_exit_contract(exit_code, &stdout, &stderr)
    }
}

fn read_all<R: Read + Send + 'static>(mut source: R) -> JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = source.read_to_end(&mut buffer);
        buffer
    })
}

fn join_bytes(reader: Option<JoinHandle<Vec<u8>>>) -> Vec<u8> {
    reader
        .and_then(|handle| handle.join().ok())
        .unwrap_or_default()
}

impl CoreBackend for PythonEngine {
    fn swarm_run(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_script(call)
    }

    fn orch_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_script(call)
    }

    fn orch_status(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_script(call)
    }

    fn orch_assign(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_script(call)
    }

    fn qa_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_script(call)
    }

    fn rev_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_script(call)
    }

    fn sec_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_script(call)
    }

    fn rel_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_script(call)
    }

    fn swarm_tool(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_script(call)
    }

    fn lane_verb(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_script(call)
    }
}

/// The Rust engine. Serves the read-only `orch_status` route; other entrypoints refuse with `E-DEP`.
pub struct RustEngine {
    runtime: tokio::runtime::Runtime,
}

impl RustEngine {
    /// Builds the engine's single-threaded tokio runtime.
    pub fn new() -> Result<Self, SeamError> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| SeamError::new(ErrorCode::Internal, format!("tokio runtime: {e}")))?;
        Ok(Self { runtime })
    }

    fn orch_status_read(&self, call: &Invocation) -> Result<Reply, SeamError> {
        let args = match parse_orch_status_read(&call.argv) {
            Some(args) if args.json => args,
            _ => return Err(no_route(&call.script)),
        };
        let deadline = call.deadline;
        self.runtime.block_on(async {
            match tokio::time::timeout(deadline, read_orch_status(&args, call)).await {
                Ok(result) => result,
                Err(_) => Err(SeamError::new(
                    ErrorCode::Timeout,
                    format!("orch_status exceeded {} s", deadline.as_secs()),
                )),
            }
        })
    }
}

fn no_route(script: &str) -> SeamError {
    SeamError::new(
        ErrorCode::Dep,
        format!("the rust engine has no route for {script}; use GSD_ENGINE=python"),
    )
}

impl CoreBackend for RustEngine {
    fn swarm_run(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn orch_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn orch_status(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.orch_status_read(call)
    }

    fn orch_assign(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn qa_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn rev_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn sec_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn rel_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn swarm_tool(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn lane_verb(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }
}

fn io_error(error: std::io::Error) -> SeamError {
    if error.kind() == ErrorKind::InvalidData {
        SeamError::new(ErrorCode::Input, error.to_string())
    } else {
        SeamError::new(ErrorCode::Dep, error.to_string())
    }
}

fn store_err(error: sqlx::Error) -> SeamError {
    SeamError::new(ErrorCode::Internal, format!("task store: {error}"))
}

/// Creates the Swarm state directory and its `.gitignore` (`*`) if absent, as `swarm_dir(create=True)` does.
fn ensure_store_dir(swarm: &Path) -> Result<(), SeamError> {
    fs::create_dir_all(swarm).map_err(io_error)?;
    match fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(swarm.join(".gitignore"))
    {
        Ok(mut file) => file.write_all(b"*").map_err(io_error),
        Err(error) if error.kind() == ErrorKind::AlreadyExists => Ok(()),
        Err(error) => Err(io_error(error)),
    }
}

/// `CREATE TABLE IF NOT EXISTS` statements mirroring `swarm/taskstore.py::_SCHEMA`.
const SCHEMA_STATEMENTS: [&str; 4] = [
    "CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL, parent_id TEXT,
  dag_depth INTEGER DEFAULT 0, capability TEXT NOT NULL, agent_id TEXT,
  title TEXT, inputs TEXT DEFAULT '[]', outputs TEXT DEFAULT '[]',
  acceptance TEXT DEFAULT '[]', budget TEXT DEFAULT '{}', risk_class TEXT DEFAULT 'low',
  priority TEXT DEFAULT 'P2', state TEXT NOT NULL, attempt INTEGER DEFAULT 0,
  max_attempts INTEGER DEFAULT 3, rework_loops INTEGER DEFAULT 0,
  depends_on TEXT DEFAULT '[]', assigned_to TEXT, notes TEXT,
  created_at REAL, updated_at REAL
)",
    "CREATE TABLE IF NOT EXISTS transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, from_state TEXT, to_state TEXT,
  actor TEXT, reason TEXT, ts REAL
)",
    "CREATE TABLE IF NOT EXISTS verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, gate TEXT, verdict TEXT,
  agent_id TEXT, findings TEXT, expires_at REAL, ts REAL, envelope_json TEXT, sig TEXT
)",
    "CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, uri TEXT,
  version TEXT, digest TEXT, producer TEXT, ts REAL
)",
];

/// Creates the Swarm state directory, then opens `tasks.db` under it with the schema and the verdict envelope columns, like `TaskStore.__init__`.
async fn open_store(swarm: &Path) -> Result<SqlitePool, SeamError> {
    ensure_store_dir(swarm)?;
    let options = SqliteConnectOptions::new()
        .filename(swarm.join("tasks.db"))
        .create_if_missing(true)
        .busy_timeout(Duration::from_secs(30));
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect_with(options)
        .await
        .map_err(store_err)?;
    for statement in SCHEMA_STATEMENTS {
        sqlx::query(statement)
            .execute(&pool)
            .await
            .map_err(store_err)?;
    }
    migrate_verdicts(&pool).await?;
    Ok(pool)
}

/// Adds the signed-envelope columns to a v1 `verdicts` table, idempotently (`TaskStore._migrate_verdicts`).
async fn migrate_verdicts(pool: &SqlitePool) -> Result<(), SeamError> {
    let columns = sqlx::query("PRAGMA table_info(verdicts)")
        .fetch_all(pool)
        .await
        .map_err(store_err)?;
    let mut names: Vec<String> = Vec::new();
    for row in &columns {
        names.push(row.try_get::<String, _>("name").map_err(store_err)?);
    }
    for (column, ddl) in [
        (
            "envelope_json",
            "ALTER TABLE verdicts ADD COLUMN envelope_json TEXT",
        ),
        ("sig", "ALTER TABLE verdicts ADD COLUMN sig TEXT"),
    ] {
        if names.iter().any(|name| name.as_str() == column) {
            continue;
        }
        if let Err(error) = sqlx::query(ddl).execute(pool).await {
            if !error.to_string().contains("duplicate column") {
                return Err(store_err(error));
            }
        }
    }
    Ok(())
}

/// Converts one SQLite row into ordered fields, typed by the stored value (Python `dict(row)`).
fn row_fields(row: &SqliteRow) -> Result<Fields, SeamError> {
    let mut fields = Vec::with_capacity(row.columns().len());
    for (index, column) in row.columns().iter().enumerate() {
        let value = {
            let raw = row.try_get_raw(index).map_err(store_err)?;
            if raw.is_null() {
                Json::Null
            } else {
                let type_name = raw.type_info().name().to_string();
                match type_name.as_str() {
                    "INTEGER" | "BOOLEAN" => {
                        Json::Int(row.try_get::<i64, _>(index).map_err(store_err)?)
                    }
                    "REAL" | "NUMERIC" => {
                        Json::Float(row.try_get::<f64, _>(index).map_err(store_err)?)
                    }
                    "TEXT" => Json::Str(row.try_get::<String, _>(index).map_err(store_err)?),
                    other => {
                        return Err(SeamError::new(
                            ErrorCode::Internal,
                            format!("unsupported column type {other}"),
                        ));
                    }
                }
            }
        };
        fields.push((column.name().to_string(), value));
    }
    Ok(fields)
}

async fn fetch_task(pool: &SqlitePool, task_id: &str) -> Result<Option<Task>, SeamError> {
    let rows = sqlx::query("SELECT * FROM tasks WHERE task_id=?")
        .bind(task_id)
        .fetch_all(pool)
        .await
        .map_err(store_err)?;
    match rows.first() {
        Some(row) => Ok(Some(Task::from_row(row_fields(row)?)?)),
        None => Ok(None),
    }
}

/// `TaskStore.list(correlation_id=…)`: an empty or absent id lists every task.
async fn fetch_tasks(
    pool: &SqlitePool,
    correlation_id: Option<&str>,
) -> Result<Vec<Task>, SeamError> {
    let rows = match correlation_id.filter(|c| !c.is_empty()) {
        Some(correlation) => {
            sqlx::query("SELECT * FROM tasks WHERE correlation_id=? ORDER BY dag_depth, created_at")
                .bind(correlation)
                .fetch_all(pool)
                .await
        }
        None => {
            sqlx::query("SELECT * FROM tasks ORDER BY dag_depth, created_at")
                .fetch_all(pool)
                .await
        }
    }
    .map_err(store_err)?;
    let mut tasks = Vec::with_capacity(rows.len());
    for row in &rows {
        tasks.push(Task::from_row(row_fields(row)?)?);
    }
    Ok(tasks)
}

/// `TaskStore._latest_rows(task)`: highest-id verdict row per gate, at or after the last rework cut-off.
async fn latest_rows(pool: &SqlitePool, task: &Task) -> Result<Vec<LatestRow>, SeamError> {
    let since = task.verdict_since()?;
    let rows = sqlx::query("SELECT * FROM verdicts WHERE task_id=? AND ts>=? ORDER BY id")
        .bind(task.task_id.as_str())
        .bind(since)
        .fetch_all(pool)
        .await
        .map_err(store_err)?;
    let mut latest: Vec<LatestRow> = Vec::new();
    for row in &rows {
        push_latest(&mut latest, row_fields(row)?)?;
    }
    Ok(latest)
}

/// Counts run-log records for `correlation_id` in `events.jsonl` (`runlog.read_events`).
fn count_events(swarm: &Path, correlation_id: Option<&str>) -> Result<i64, SeamError> {
    let path = swarm.join("events.jsonl");
    if !path.exists() {
        return Ok(0);
    }
    let text = fs::read_to_string(&path).map_err(io_error)?;
    let wanted = correlation_id.filter(|c| !c.is_empty());
    let mut count = 0i64;
    for line in text.lines() {
        if line.trim().is_empty() {
            continue;
        }
        if run_log_counts(line, wanted)? {
            count += 1;
        }
    }
    Ok(count)
}

/// The latest correlation id from `latest_correlation`, trimmed; absent when the file is absent.
fn latest_correlation(swarm: &Path) -> Result<Option<String>, SeamError> {
    let path = swarm.join("latest_correlation");
    if !path.exists() {
        return Ok(None);
    }
    let text = fs::read_to_string(&path).map_err(io_error)?;
    Ok(Some(text.trim().to_string()))
}

/// The `orch_status` read path: task list (no `--history`) or one task's history.
async fn read_orch_status(args: &ReadArgs, call: &Invocation) -> Result<Reply, SeamError> {
    let task_id = match &args.task_id {
        Some(value) => Some(value.clone()),
        None => env_value(&call.env, "SWARM_TASK_ID").map(str::to_string),
    };
    if let Some(id) = &task_id {
        check_safe_id(id, "task id")?;
    }
    let correlation_flag = match &args.correlation_id {
        Some(value) => Some(value.clone()),
        None => env_value(&call.env, "SWARM_CORRELATION_ID").map(str::to_string),
    };
    if let Some(id) = &correlation_flag {
        check_safe_id(id, "correlation id")?;
    }
    let root = canonical_or(&call.root.join(args.root.as_deref().unwrap_or(".")));
    let swarm = resolve_swarm_dir(&root, env_value(&call.env, "SWARM_DIR"));
    let pool = open_store(&swarm).await?;
    let correlation = match correlation_flag {
        Some(value) => Some(value),
        None => latest_correlation(&swarm)?,
    };
    match args.history.as_deref().filter(|h| !h.is_empty()) {
        Some(history) => read_history(&pool, call, history).await,
        None => list_status(&pool, call, &swarm, correlation).await,
    }
}

/// `orch_status --history TASK`: transitions, latest verdicts and missing gate reasons of one task.
async fn read_history(
    pool: &SqlitePool,
    call: &Invocation,
    task_id: &str,
) -> Result<Reply, SeamError> {
    let task = fetch_task(pool, task_id)
        .await?
        .ok_or_else(|| SeamError::new(ErrorCode::Input, format!("unknown task {task_id}")))?;
    let transitions = sqlx::query("SELECT * FROM transitions WHERE task_id=? ORDER BY id")
        .bind(task_id)
        .fetch_all(pool)
        .await
        .map_err(store_err)?;
    let mut history: Vec<Json> = Vec::with_capacity(transitions.len());
    for row in &transitions {
        history.push(Json::Obj(row_fields(row)?));
    }
    let latest = latest_rows(pool, &task).await?;
    let fields = history_fields(task_id, history, latest, &task)?;
    Ok(finish(fields, &call.agent, &call.script))
}

/// `orch_status` with no transition or ingest: the task table for one correlation, or all tasks.
async fn list_status(
    pool: &SqlitePool,
    call: &Invocation,
    swarm: &Path,
    correlation: Option<String>,
) -> Result<Reply, SeamError> {
    let tasks = fetch_tasks(pool, correlation.as_deref()).await?;
    let db_path = swarm.join("tasks.db").display().to_string();
    let swarm_text = swarm.display().to_string();
    if tasks.is_empty() {
        let fields = empty_status(correlation.as_deref(), &db_path, &swarm_text);
        return Ok(finish(fields, &call.agent, &call.script));
    }

    let satisfied = satisfied_ids(&tasks);
    let mut counts: Vec<(String, i64)> = Vec::new();
    let mut rows: Vec<StatusRow> = Vec::with_capacity(tasks.len());
    for task in &tasks {
        count_state(&mut counts, &task.state);
        let latest = latest_rows(pool, task).await?;
        rows.push(status_row(task, &latest, &satisfied)?);
    }
    let events = count_events(swarm, correlation.as_deref())?;
    let fields = status_list(
        correlation.as_deref(),
        &rows,
        &counts,
        events,
        &db_path,
        &swarm_text,
    );
    Ok(finish(fields, &call.agent, &call.script))
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_route::py_float;
    use std::future::Future;

    type TestResult = Result<(), String>;

    fn block<F: Future>(future: F) -> Result<F::Output, String> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| e.to_string())?;
        Ok(runtime.block_on(future))
    }

    fn message(error: SeamError) -> String {
        error.message().to_string()
    }

    fn call(root: &Path, swarm: &Path, argv: &[&str]) -> Invocation {
        Invocation {
            agent: "A01".to_string(),
            script: "orch_status".to_string(),
            argv: argv.iter().map(|s| s.to_string()).collect(),
            root: root.to_path_buf(),
            env: vec![("SWARM_DIR".to_string(), swarm.display().to_string())],
            deadline: Duration::from_secs(30),
        }
    }

    async fn insert_task(
        pool: &SqlitePool,
        id: &str,
        state: &str,
        depth: i64,
        created: f64,
        risk: &str,
        deps: &str,
    ) -> Result<(), sqlx::Error> {
        sqlx::query(
            "INSERT INTO tasks (task_id, correlation_id, dag_depth, capability, agent_id, title, risk_class, state, depends_on, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(id)
        .bind("C1")
        .bind(depth)
        .bind("cap")
        .bind("A05")
        .bind("title")
        .bind(risk)
        .bind(state)
        .bind(deps)
        .bind("{}")
        .bind(created)
        .bind(created)
        .execute(pool)
        .await?;
        Ok(())
    }

    /// Fixture: T-A DONE (low, one unsigned review pass), T-B PLANNED (medium, depends on T-A),
    /// T-C IN_REVIEW (low, no verdicts), T-D ESCALATED (low, depends on T-C). Two of three C1 run-log lines match.
    async fn seed(swarm: &Path) -> Result<(), String> {
        let pool = open_store(swarm).await.map_err(message)?;
        insert_task(&pool, "T-A", "DONE", 0, 1.0, "low", "[]")
            .await
            .map_err(|e| e.to_string())?;
        insert_task(&pool, "T-B", "PLANNED", 1, 2.0, "medium", "[\"T-A\"]")
            .await
            .map_err(|e| e.to_string())?;
        insert_task(&pool, "T-C", "IN_REVIEW", 1, 3.0, "low", "[]")
            .await
            .map_err(|e| e.to_string())?;
        insert_task(&pool, "T-D", "ESCALATED", 2, 4.0, "low", "[\"T-C\"]")
            .await
            .map_err(|e| e.to_string())?;
        sqlx::query(
            "INSERT INTO transitions (task_id, from_state, to_state, actor, reason, ts) VALUES ('T-A', NULL, 'CREATED', 'system', 'created', 1.0)",
        )
        .execute(&pool)
        .await
        .map_err(|e| e.to_string())?;
        sqlx::query(
            "INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts) VALUES ('T-A', 'review', 'pass', 'A09', '[]', 9.0, 2.0)",
        )
        .execute(&pool)
        .await
        .map_err(|e| e.to_string())?;
        pool.close().await;
        fs::write(
            swarm.join("events.jsonl"),
            "{\"type\":\"a\",\"correlation_id\":\"C1\"}\n{\"type\":\"b\",\"correlation_id\":\"C2\"}\n{\"type\":\"c\",\"correlation_id\":\"C1\"}\n",
        )
        .map_err(|e| e.to_string())?;
        Ok(())
    }

    fn run(engine: &RustEngine, root: &Path, swarm: &Path, argv: &[&str]) -> Result<Reply, String> {
        engine
            .orch_status(&call(root, swarm, argv))
            .map_err(message)
    }

    #[test]
    fn python_json_layout_matches_json_dumps_indent_2() -> TestResult {
        let value = Json::Obj(vec![
            ("a".to_string(), Json::Arr(vec![Json::Int(1), jstr("é")])),
            ("b".to_string(), Json::Obj(Vec::new())),
            ("c".to_string(), Json::Arr(Vec::new())),
        ]);
        let expected = "{\n  \"a\": [\n    1,\n    \"\\u00e9\"\n  ],\n  \"b\": {},\n  \"c\": []\n}";
        assert_eq!(to_python_json(&value), expected);
        Ok(())
    }

    #[test]
    fn python_float_repr_matches_float_repr() -> TestResult {
        assert_eq!(py_float(1.0), "1.0");
        assert_eq!(py_float(1781234567.5), "1781234567.5");
        assert_eq!(py_float(1e16), "1e+16");
        assert_eq!(py_float(1.5e-5), "1.5e-05");
        assert_eq!(py_float(0.0001), "0.0001");
        assert_eq!(py_float(-0.0), "-0.0");
        Ok(())
    }

    #[test]
    fn parser_keeps_key_order_and_int_float_split() -> TestResult {
        let parsed = parse_json("{\"b\": 1, \"a\": 2.0, \"c\": [true, null]}").map_err(message)?;
        let expected = Json::Obj(vec![
            ("b".to_string(), Json::Int(1)),
            ("a".to_string(), Json::Float(2.0)),
            (
                "c".to_string(),
                Json::Arr(vec![Json::Bool(true), Json::Null]),
            ),
        ]);
        assert_eq!(parsed, expected);
        assert!(parse_json("{\"a\": 1} trailing").is_err());
        Ok(())
    }

    #[test]
    fn exit_contract_accepts_0_1_2_and_rejects_the_rest() -> TestResult {
        assert!(enforce_exit_contract(0, "{}\n", "").is_ok());
        assert!(
            enforce_exit_contract(0, "[1]", "").err().map(|e| e.code())
                == Some(ErrorCode::Internal)
        );
        assert!(enforce_exit_contract(1, "{\"status\":\"fail\"}\n", "").is_ok());
        assert!(
            enforce_exit_contract(1, "{\"status\":\"ok\"}", "")
                .err()
                .map(|e| e.code())
                == Some(ErrorCode::Internal)
        );
        assert!(enforce_exit_contract(
            2,
            "{\"error\":{\"code\":\"E-TIMEOUT\",\"message\":\"m\"}}",
            ""
        )
        .is_ok());
        assert!(
            enforce_exit_contract(2, "{\"error\":{\"code\":\"E-BOGUS\"}}", "")
                .err()
                .map(|e| e.code())
                == Some(ErrorCode::Internal)
        );
        let usage = enforce_exit_contract(2, "", "usage: swarm x").err();
        assert_eq!(usage.as_ref().map(|e| e.code()), Some(ErrorCode::Input));
        assert!(usage
            .map(|e| e.message().contains("usage: swarm x"))
            .unwrap_or(false));
        assert_eq!(
            enforce_exit_contract(3, "{}", "").err().map(|e| e.code()),
            Some(ErrorCode::Internal)
        );
        Ok(())
    }

    #[test]
    fn input_cap_is_exact_at_the_boundary() -> TestResult {
        let at_cap = "x".repeat(INPUT_CAP_BYTES);
        let over = "x".repeat(INPUT_CAP_BYTES + 1);
        assert!(check_input_cap(&["--input".to_string(), at_cap.clone()]).is_ok());
        assert!(check_input_cap(&[format!("--input={at_cap}")]).is_ok());
        assert_eq!(
            check_input_cap(&["--input".to_string(), over.clone()])
                .err()
                .map(|e| e.code()),
            Some(ErrorCode::Input)
        );
        assert_eq!(
            check_input_cap(&[format!("--input={over}")])
                .err()
                .map(|e| e.code()),
            Some(ErrorCode::Input)
        );
        Ok(())
    }

    #[test]
    fn script_stem_allowlist() -> TestResult {
        assert!(is_valid_stem("orch_status"));
        assert!(is_valid_stem("qa_gate2"));
        assert!(!is_valid_stem("Orch"));
        assert!(!is_valid_stem("../x"));
        assert!(!is_valid_stem("orch-status"));
        assert!(!is_valid_stem(""));
        Ok(())
    }

    struct Canned;

    fn canned(call: &Invocation) -> Result<Reply, SeamError> {
        if call.script == "bad" {
            return Ok(Reply {
                exit_code: 0,
                stdout: "[1, 2]".to_string(),
            });
        }
        Ok(Reply {
            exit_code: 0,
            stdout: "{\"status\":\"ok\"}\n".to_string(),
        })
    }

    impl CoreBackend for Canned {
        fn swarm_run(&self, call: &Invocation) -> Result<Reply, SeamError> {
            canned(call)
        }
        fn orch_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
            canned(call)
        }
        fn orch_status(&self, call: &Invocation) -> Result<Reply, SeamError> {
            canned(call)
        }
        fn orch_assign(&self, call: &Invocation) -> Result<Reply, SeamError> {
            canned(call)
        }
        fn qa_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
            canned(call)
        }
        fn rev_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
            canned(call)
        }
        fn sec_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
            canned(call)
        }
        fn rel_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
            canned(call)
        }
        fn swarm_tool(&self, call: &Invocation) -> Result<Reply, SeamError> {
            canned(call)
        }
        fn lane_verb(&self, call: &Invocation) -> Result<Reply, SeamError> {
            canned(call)
        }
    }

    #[test]
    fn invoke_routes_by_entrypoint_and_enforces_the_contract() -> TestResult {
        let root = PathBuf::from(".");
        let mut call_value = call(&root, &root, &["--json"]);
        let reply = invoke(&Canned, &call_value);
        assert_eq!(reply.exit_code, 0);
        call_value.script = "bad".to_string();
        let broken = invoke(&Canned, &call_value);
        assert_eq!(broken.exit_code, 2);
        let parsed: serde_json::Value =
            serde_json::from_str(&broken.stdout).map_err(|e| e.to_string())?;
        assert_eq!(parsed["error"]["code"].as_str(), Some("E-INTERNAL"));
        assert_eq!(parsed["agent"].as_str(), Some("A01"));
        call_value.script = "../escape".to_string();
        let rejected = invoke(&Canned, &call_value);
        let parsed: serde_json::Value =
            serde_json::from_str(&rejected.stdout).map_err(|e| e.to_string())?;
        assert_eq!(parsed["error"]["code"].as_str(), Some("E-INPUT"));
        Ok(())
    }

    #[test]
    fn dry_run_flag_does_not_change_read_output() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let swarm = dir.path().join(".swarm");
        block(seed(&swarm))??;
        let engine = RustEngine::new().map_err(message)?;
        let plain = run(
            &engine,
            dir.path(),
            &swarm,
            &["--correlation-id", "C1", "--json"],
        )?;
        let dry = run(
            &engine,
            dir.path(),
            &swarm,
            &["--dry-run", "--correlation-id", "C1", "--json"],
        )?;
        assert_eq!(plain, dry);
        assert_eq!(plain.exit_code, 0);
        Ok(())
    }

    #[test]
    fn list_reads_the_task_store_with_the_python_shape() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let swarm = dir.path().join(".swarm");
        block(seed(&swarm))??;
        let engine = RustEngine::new().map_err(message)?;
        let reply = run(
            &engine,
            dir.path(),
            &swarm,
            &["--correlation-id", "C1", "--json"],
        )?;
        let value: serde_json::Value =
            serde_json::from_str(&reply.stdout).map_err(|e| e.to_string())?;
        assert_eq!(value["status"].as_str(), Some("ok"));
        assert_eq!(value["correlation_id"].as_str(), Some("C1"));
        let ids: Vec<&str> = value["tasks"]
            .as_array()
            .map(|rows| rows.iter().filter_map(|r| r["task_id"].as_str()).collect())
            .unwrap_or_default();
        assert_eq!(ids, vec!["T-A", "T-B", "T-C", "T-D"]);
        assert_eq!(
            value["tasks"][0]["verdicts"]["review"].as_str(),
            Some("pass")
        );
        assert_eq!(
            value["tasks"][0]["gates_required"][0].as_str(),
            Some("review")
        );
        assert_eq!(value["tasks"][1]["ready"].as_bool(), Some(true));
        assert_eq!(
            value["tasks"][1]["gates_required"]
                .as_array()
                .map(|g| g.len()),
            Some(2)
        );
        assert_eq!(
            value["tasks"][2]["missing_gates"][0].as_str(),
            Some("review")
        );
        assert_eq!(value["tasks"][2]["ready"].as_bool(), Some(false));
        assert_eq!(value["tasks"][3]["depends_on"][0].as_str(), Some("T-C"));
        assert_eq!(value["counts"]["DONE"].as_i64(), Some(1));
        assert_eq!(value["escalated"][0].as_str(), Some("T-D"));
        assert_eq!(value["complete"].as_bool(), Some(false));
        assert_eq!(value["events"].as_i64(), Some(2));
        assert_eq!(value["agent"].as_str(), Some("A01"));
        assert_eq!(value["script"].as_str(), Some("orch_status"));
        let summary = value["summary"].as_str().unwrap_or_default();
        assert!(summary.contains("counts: {'DONE': 1, 'PLANNED': 1, 'IN_REVIEW': 1, 'ESCALATED': 1}   escalated: ['T-D']   complete: False"));
        assert!(summary.contains("review=pass"));
        Ok(())
    }

    #[test]
    fn history_reports_transitions_verdicts_and_missing_reasons() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let swarm = dir.path().join(".swarm");
        block(seed(&swarm))??;
        let engine = RustEngine::new().map_err(message)?;
        let reply = run(&engine, dir.path(), &swarm, &["--history", "T-A", "--json"])?;
        let value: serde_json::Value =
            serde_json::from_str(&reply.stdout).map_err(|e| e.to_string())?;
        assert_eq!(value["task_id"].as_str(), Some("T-A"));
        assert_eq!(value["history"][0]["to_state"].as_str(), Some("CREATED"));
        assert_eq!(value["history"][0]["from_state"].is_null(), true);
        assert_eq!(
            value["verdicts"]["review"]["verdict"].as_str(),
            Some("pass")
        );
        assert_eq!(
            value["verdicts"]["review"]["findings"]
                .as_array()
                .map(|f| f.len()),
            Some(0)
        );
        assert_eq!(
            value["missing_gate_reasons"]["review"].as_str(),
            Some("unsigned")
        );
        assert_eq!(value["summary"].as_str(), Some("history for T-A"));
        let plan = run(&engine, dir.path(), &swarm, &["--history", "T-B", "--json"])?;
        let value: serde_json::Value =
            serde_json::from_str(&plan.stdout).map_err(|e| e.to_string())?;
        assert_eq!(
            value["missing_gate_reasons"]["quality"].as_str(),
            Some("absent")
        );
        Ok(())
    }

    #[test]
    fn history_of_unknown_task_is_e_input_with_exit_two() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let swarm = dir.path().join(".swarm");
        block(seed(&swarm))??;
        let engine = RustEngine::new().map_err(message)?;
        let error = engine
            .orch_status(&call(dir.path(), &swarm, &["--history", "nope", "--json"]))
            .err();
        assert_eq!(error.as_ref().map(|e| e.code()), Some(ErrorCode::Input));
        let reply = invoke(
            &engine,
            &call(dir.path(), &swarm, &["--history", "nope", "--json"]),
        );
        assert_eq!(reply.exit_code, 2);
        let value: serde_json::Value =
            serde_json::from_str(&reply.stdout).map_err(|e| e.to_string())?;
        assert_eq!(value["error"]["code"].as_str(), Some("E-INPUT"));
        assert_eq!(
            value["error"]["message"].as_str(),
            Some("E-INPUT: unknown task nope")
        );
        assert_eq!(value["status"].as_str(), Some("error"));
        Ok(())
    }

    #[test]
    fn empty_store_answers_no_tasks_with_the_python_keys() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let swarm = dir.path().join(".swarm");
        let engine = RustEngine::new().map_err(message)?;
        let reply = run(
            &engine,
            dir.path(),
            &swarm,
            &["--correlation-id", "C9", "--json"],
        )?;
        let value: serde_json::Value =
            serde_json::from_str(&reply.stdout).map_err(|e| e.to_string())?;
        assert_eq!(value["correlation_id"].as_str(), Some("C9"));
        assert_eq!(value["tasks"].as_array().map(|t| t.len()), Some(0));
        assert_eq!(
            value["summary"].as_str(),
            Some("no tasks (run orch_plan.py first)")
        );
        assert!(swarm.join("tasks.db").is_file());
        assert!(reply.stdout.find("\"status\"") < reply.stdout.find("\"agent\""));
        Ok(())
    }

    #[test]
    fn signed_verdict_rows_are_refused_not_guessed() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let swarm = dir.path().join(".swarm");
        block(seed(&swarm))??;
        block(async {
            let pool = open_store(&swarm).await.map_err(message)?;
            sqlx::query(
                "UPDATE verdicts SET envelope_json = '{\"sig\":\"x\"}' WHERE task_id = 'T-A'",
            )
            .execute(&pool)
            .await
            .map_err(|e| e.to_string())?;
            Ok::<(), String>(())
        })??;
        let engine = RustEngine::new().map_err(message)?;
        let error = engine
            .orch_status(&call(dir.path(), &swarm, &["--history", "T-A", "--json"]))
            .err();
        assert_eq!(error.as_ref().map(|e| e.code()), Some(ErrorCode::Dep));
        Ok(())
    }

    #[test]
    fn unsupported_flags_fall_outside_the_read_route() -> TestResult {
        let argv = |items: &[&str]| items.iter().map(|s| s.to_string()).collect::<Vec<String>>();
        assert!(parse_orch_status_read(&argv(&["--json"])).is_some());
        assert!(parse_orch_status_read(&argv(&["--transition", "T", "DONE", "--json"])).is_none());
        assert!(parse_orch_status_read(&argv(&["--ingest=x.json", "--json"])).is_none());
        assert!(parse_orch_status_read(&argv(&["--json", "--bogus"])).is_none());
        assert!(parse_orch_status_read(&argv(&["--history"])).is_none());
        let parsed = parse_orch_status_read(&argv(&["--history=T-A", "--json"]));
        assert_eq!(parsed.and_then(|a| a.history), Some("T-A".to_string()));
        Ok(())
    }

    #[test]
    fn rust_engine_refuses_every_other_entrypoint() -> TestResult {
        let engine = RustEngine::new().map_err(message)?;
        let root = PathBuf::from(".");
        let mut other = call(&root, &root, &["--json"]);
        other.script = "swarm_run".to_string();
        let error = engine.swarm_run(&other).err();
        assert_eq!(error.as_ref().map(|e| e.code()), Some(ErrorCode::Dep));
        Ok(())
    }

    #[test]
    fn invalid_task_id_fails_before_any_store_access() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let swarm = dir.path().join(".swarm");
        let engine = RustEngine::new().map_err(message)?;
        let error = engine
            .orch_status(&call(dir.path(), &swarm, &["--task-id", "../x", "--json"]))
            .err();
        assert_eq!(error.as_ref().map(|e| e.code()), Some(ErrorCode::Input));
        assert!(!swarm.exists());
        Ok(())
    }
}
