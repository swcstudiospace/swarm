//! Strangler seam between the Python swarm scripts and the Rust core (U4.1).
//!
//! One [`CoreBackend`] method exists per frozen entrypoint. Both engines receive the same
//! [`Invocation`] and answer with the same [`Reply`] shape, so the argv, exit-code and JSON
//! contracts of `plan/rust-wasmer-omp-options.md` §1.2 hold whichever engine runs.
//! [`invoke`] enforces the 100 KB `--input` cap and the 0/1/2 exit contract at the seam.
//!
//! The Rust engine serves two `orch_status` routes against the SQLite Task Store (`swarm/taskstore.py`): the read-only listing
//! and history, and the host `--transition` write route (`crate::write_route`). Every other entrypoint refuses with `E-DEP`
//! and `error.details.write_state = "not_started"`, so the Python body answers it, until its route flips.

use std::fs;
use std::future::{pending, Future};
use std::io::{ErrorKind, Write};
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use gsd_failure::{error_code_from_python, ErrorCode};
use gsd_route::transition_args::{parse_transition_args, TransitionArgs};
use gsd_route::{
    check_safe_id, jstr, obj_get, orch_status_reply, parse_json, run_log_counts, to_python_json,
    Fields, Json, ReadArgs, Task,
};
use gsd_route::{parse_orch_status_read, Reply, SeamError};
use gsd_sdk::omp_bridge_pins::{AbortKind, BridgeError, StopReason};
use gsd_sdk::omp_bridge_runner::{InvokeRequest, OmpBridge};
use sqlx::sqlite::{SqliteConnectOptions, SqlitePool, SqlitePoolOptions, SqliteRow};
use sqlx::{Column, Row, TypeInfo, ValueRef};

use crate::write_route::{ingest::ingest_reply, transition_reply};

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
    /// File the call writes its post-commit effects manifest to (`crate::write_route`); the selector creates and owns it.
    /// Only the write routes read it.
    pub effects_path: Option<PathBuf>,
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

/// Canonicalises `path`. When it does not exist, the longest existing ancestor is canonicalised
/// and the missing tail is appended after `.` and `..` are removed lexically, as
/// `Path(base, value).resolve()` does in `swarm/paths.py`.
pub(crate) fn canonical_or(path: &Path) -> PathBuf {
    if let Ok(done) = fs::canonicalize(path) {
        return done;
    }
    let normal = lexical_normal(path);
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    let mut head = normal.clone();
    loop {
        if let Ok(done) = fs::canonicalize(&head) {
            return tail.iter().rev().fold(done, |acc, part| acc.join(part));
        }
        let Some(name) = head.file_name().map(std::ffi::OsStr::to_os_string) else {
            return normal;
        };
        let Some(parent) = head.parent().map(Path::to_path_buf) else {
            return normal;
        };
        tail.push(name);
        head = parent;
    }
}

/// Removes `.` components and resolves each `..` against the components before it.
fn lexical_normal(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
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
    // A child that needs nothing from the caller's environment gets nothing: no signing key or other secret rides along.
    let mut git = Command::new("git");
    git.env_clear();
    for name in ["PATH", "HOME"] {
        if let Some(value) = std::env::var_os(name) {
            git.env(name, value);
        }
    }
    let output = git
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
///
/// Each call runs through the sdk runner, which owns the child's process group and its deadline.
/// This seam classifies the outcome with [`enforce_exit_contract`].
#[derive(Debug)]
pub struct PythonEngine {
    python: String,
    runtime: Result<tokio::runtime::Runtime, String>,
}

impl PythonEngine {
    /// Uses the given interpreter (normally `python3`).
    pub fn new(python: impl Into<String>) -> Self {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| e.to_string());
        Self {
            python: python.into(),
            runtime,
        }
    }

    fn run_through_sdk(&self, call: &Invocation) -> Result<Reply, SeamError> {
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
        let runtime = self
            .runtime
            .as_ref()
            .map_err(|e| SeamError::new(ErrorCode::Internal, format!("tokio runtime: {e}")))?;
        let swarm_dir = resolve_swarm_dir(&call.root, env_value(&call.env, "SWARM_DIR"));
        let bridge = OmpBridge::for_root(call.root.clone()).with_interpreter(self.python.as_str());
        let req = InvokeRequest {
            script: &call.script,
            args: &call.argv,
            cwd: &call.root,
            swarm_dir: &swarm_dir,
            env: &call.env,
            timeout_ms: u64::try_from(call.deadline.as_millis()).unwrap_or(u64::MAX),
        };
        // The seam has no abort source, so only the deadline stops the call.
        let run = runtime
            .block_on(bridge.invoke_raw(req, pending::<AbortKind>()))
            .map_err(bridge_error)?;
        match run.stopped {
            Some(StopReason::Timeout) => {
                return Err(SeamError::new(
                    ErrorCode::Timeout,
                    format!("{} exceeded {} s", call.script, call.deadline.as_secs()),
                ));
            }
            Some(StopReason::Abort) => {
                return Err(SeamError::new(
                    ErrorCode::Internal,
                    format!("{} cancelled", call.script),
                ));
            }
            None => {}
        }
        let Some(exit_code) = run.exit_code else {
            return Err(SeamError::new(
                ErrorCode::Internal,
                "script terminated by a signal",
            ));
        };
        let stdout = String::from_utf8(run.stdout)
            .map_err(|_| SeamError::new(ErrorCode::Internal, "stdout is not UTF-8"))?;
        let stderr = String::from_utf8_lossy(&run.stderr);
        enforce_exit_contract(exit_code, &stdout, &stderr)
    }
}

/// Carries a runner error into the seam with its wire code. The runner message already starts
/// with `CODE: `, so it is not prefixed a second time.
fn bridge_error(error: BridgeError) -> SeamError {
    SeamError::unprefixed(error.code, error.message)
}

impl CoreBackend for PythonEngine {
    fn swarm_run(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_through_sdk(call)
    }

    fn orch_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_through_sdk(call)
    }

    fn orch_status(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_through_sdk(call)
    }

    fn orch_assign(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_through_sdk(call)
    }

    fn qa_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_through_sdk(call)
    }

    fn rev_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_through_sdk(call)
    }

    fn sec_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_through_sdk(call)
    }

    fn rel_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_through_sdk(call)
    }

    fn swarm_tool(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_through_sdk(call)
    }

    fn lane_verb(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.run_through_sdk(call)
    }
}

/// The Rust engine. Serves the read-only `orch_status` route; other entrypoints refuse with `E-DEP`.
pub struct RustEngine {
    runtime: tokio::runtime::Runtime,
}

impl RustEngine {
    /// Builds the engine's single-threaded tokio runtime.
    pub fn new() -> Result<Self, SeamError> {
        Ok(Self {
            runtime: single_thread_runtime()?,
        })
    }

    fn orch_status_read(&self, call: &Invocation) -> Result<Reply, SeamError> {
        let args = read_args(call)?;
        self.runtime.block_on(within_deadline(
            call.deadline,
            &call.script,
            host_orch_status(&args, call),
        ))
    }

    /// The host `--transition` route: the caller has checked that the argv is inside its grammar.
    fn orch_status_transition(
        &self,
        args: &TransitionArgs,
        call: &Invocation,
    ) -> Result<Reply, SeamError> {
        self.runtime.block_on(within_deadline(
            call.deadline,
            &call.script,
            transition_reply(args, call, &gsd_taskwriter::HostDecider),
        ))
    }

    /// The host `--ingest` route: the caller has checked that the argv is inside its grammar.
    fn orch_status_ingest(
        &self,
        args: &gsd_route::ingest_args::IngestArgs,
        call: &Invocation,
    ) -> Result<Reply, SeamError> {
        self.runtime.block_on(within_deadline(
            call.deadline,
            &call.script,
            ingest_reply(args, call),
        ))
    }
}

/// Marks a refusal that happened before the first write: `error.details.write_state = "not_started"`, the reply on which a caller
/// may let the Python body answer. The details the error already has stay first.
pub(crate) fn before_any_write(error: SeamError) -> SeamError {
    let mut details = error.details().to_vec();
    details.push(("write_state".to_string(), jstr("not_started")));
    error.with_details(details)
}

/// Marks a failure after the Task Store commit: `error.details.write_state = "final"`. The verdict is recorded, so the binary's
/// answer is printed as it is and Python never runs the call again. The details the error already has stay first.
pub(crate) fn after_commit(error: SeamError) -> SeamError {
    let mut details = error.details().to_vec();
    details.push(("write_state".to_string(), jstr("final")));
    error.with_details(details)
}

/// Marks an outcome the binary cannot prove either way, such as a commit whose landing is unknown:
/// `error.details.write_state = "unknown"`. The selector answers it with `E-INTERNAL` and never runs the call again.
pub(crate) fn outcome_unknown(error: SeamError) -> SeamError {
    let mut details = error.details().to_vec();
    details.push(("write_state".to_string(), jstr("unknown")));
    error.with_details(details)
}

/// The `E-DEP` refusal for a script or argv this engine does not serve; nothing was written.
pub(crate) fn no_route(script: &str) -> SeamError {
    before_any_write(SeamError::new(
        ErrorCode::Dep,
        format!("the rust engine has no route for {script}; use GSD_ENGINE=python"),
    ))
}

/// Builds the single-threaded tokio runtime the engines block on.
pub(crate) fn single_thread_runtime() -> Result<tokio::runtime::Runtime, SeamError> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| SeamError::new(ErrorCode::Internal, format!("tokio runtime: {e}")))
}

/// Reads the `orch_status --json` flags of `call`; any other argv is refused with `E-DEP`.
pub(crate) fn read_args(call: &Invocation) -> Result<ReadArgs, SeamError> {
    match parse_orch_status_read(&call.argv) {
        Some(args) if args.json => Ok(args),
        _ => Err(no_route(&call.script)),
    }
}

/// Runs `future` under the call deadline; expiry is `E-TIMEOUT`.
pub(crate) async fn within_deadline<T>(
    deadline: Duration,
    script: &str,
    future: impl Future<Output = Result<T, SeamError>>,
) -> Result<T, SeamError> {
    match tokio::time::timeout(deadline, future).await {
        Ok(result) => result,
        Err(_) => Err(SeamError::new(
            ErrorCode::Timeout,
            format!("{script} exceeded {} s", deadline.as_secs()),
        )),
    }
}

/// The host Rust answer to a read: the snapshot shaped by the pure route logic.
async fn host_orch_status(args: &ReadArgs, call: &Invocation) -> Result<Reply, SeamError> {
    let request = read_snapshot(args, call).await?;
    orch_status_reply(&request)
}

impl CoreBackend for RustEngine {
    fn swarm_run(&self, call: &Invocation) -> Result<Reply, SeamError> {
        Err(no_route(&call.script))
    }

    fn orch_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.runtime
            .block_on(crate::write_route::plan::answer(call))
    }

    fn orch_status(&self, call: &Invocation) -> Result<Reply, SeamError> {
        if let Some(args) = gsd_route::ingest_args::parse_ingest_args(&call.argv) {
            return self.orch_status_ingest(&args, call);
        }
        match parse_transition_args(&call.argv) {
            Some(args) => self.orch_status_transition(&args, call),
            None => self.orch_status_read(call),
        }
    }

    fn orch_assign(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.runtime
            .block_on(crate::write_route::assign::answer(call))
    }

    fn qa_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.runtime.block_on(crate::gates::qa_gate::answer(call))
    }

    fn rev_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.runtime.block_on(crate::gates::rev_gate::answer(call))
    }

    fn sec_gate(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.runtime.block_on(crate::gates::sec_gate::answer(call))
    }

    fn rel_plan(&self, call: &Invocation) -> Result<Reply, SeamError> {
        self.runtime.block_on(crate::gates::rel_plan::answer(call))
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
pub(crate) async fn open_store(swarm: &Path) -> Result<SqlitePool, SeamError> {
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

/// `TaskStore._fetch(task_id)`: the task row in column order, or none.
async fn fetch_task(pool: &SqlitePool, task_id: &str) -> Result<Option<Fields>, SeamError> {
    let rows = sqlx::query("SELECT * FROM tasks WHERE task_id=?")
        .bind(task_id)
        .fetch_all(pool)
        .await
        .map_err(store_err)?;
    match rows.first() {
        Some(row) => Ok(Some(row_fields(row)?)),
        None => Ok(None),
    }
}

/// `TaskStore.list(correlation_id=…)`: an empty or absent id lists every task, in SQL order.
async fn fetch_tasks(
    pool: &SqlitePool,
    correlation_id: Option<&str>,
) -> Result<Vec<Fields>, SeamError> {
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
        tasks.push(row_fields(row)?);
    }
    Ok(tasks)
}

/// One `verdicts` entry of the request: the verdict rows `TaskStore._latest_rows(task)` reads, at or
/// after the last rework cut-off and in id order. The latest row per gate is chosen by the route.
async fn verdict_entry(pool: &SqlitePool, task: &Task) -> Result<Json, SeamError> {
    let since = task.verdict_since()?;
    let rows = sqlx::query("SELECT * FROM verdicts WHERE task_id=? AND ts>=? ORDER BY id")
        .bind(task.task_id.as_str())
        .bind(since)
        .fetch_all(pool)
        .await
        .map_err(store_err)?;
    let mut verdict_rows: Vec<Json> = Vec::with_capacity(rows.len());
    for row in &rows {
        verdict_rows.push(Json::Obj(row_fields(row)?));
    }
    Ok(Json::Obj(vec![
        ("task_id".to_string(), jstr(&task.task_id)),
        ("rows".to_string(), Json::Arr(verdict_rows)),
    ]))
}

/// Counts run-log records for `correlation_id` in `events.jsonl` (`runlog.read_events`).
///
/// A last non-blank line that does not parse as JSON is a torn trailing record (a writer died
/// mid-line) and is skipped. A last line that parses but is not an object still fails, and an
/// unparseable line before the last one still fails.
fn count_events(swarm: &Path, correlation_id: Option<&str>) -> Result<i64, SeamError> {
    let path = swarm.join("events.jsonl");
    if !path.exists() {
        return Ok(0);
    }
    let text = fs::read_to_string(&path).map_err(io_error)?;
    let wanted = correlation_id.filter(|c| !c.is_empty());
    let mut lines: Vec<&str> = text.lines().filter(|line| !line.trim().is_empty()).collect();
    let torn_tail = lines.last().is_some_and(|last| parse_json(last).is_err());
    if torn_tail {
        lines.pop();
    }
    let mut count = 0i64;
    for line in lines {
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

/// The `orch_status` snapshot: the task rows of the task list (no `--history`) or of one task's
/// history, as the request object `request_json` builds. Host Rust and the sandbox answer from it.
pub(crate) async fn read_snapshot(args: &ReadArgs, call: &Invocation) -> Result<Json, SeamError> {
    let task_id = match &args.task_id {
        Some(value) => Some(value.clone()),
        None => env_value(&call.env, "SWARM_TASK_ID").map(str::to_string),
    };
    if let Some(id) = &task_id {
        check_safe_id(id, "task id")?;
    }
    if let Some(history) = args.history.as_deref().filter(|h| !h.is_empty()) {
        check_safe_id(history, "task id")?;
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
        Some(history) => history_snapshot(&pool, call, &swarm, history).await,
        None => list_snapshot(&pool, call, &swarm, correlation).await,
    }
}

/// The `orch_status` request fields that differ by mode; everything else comes from the invocation.
struct OrchStatusRequest {
    mode: &'static str,
    correlation: Option<String>,
    events: i64,
    tasks: Vec<Json>,
    verdicts: Vec<Json>,
    history: Json,
}

/// Builds the `orch_status` request object in the key order of the wire contract.
fn request_json(agent: &str, script: &str, swarm: &Path, request: OrchStatusRequest) -> Json {
    Json::Obj(vec![
        ("mode".to_string(), jstr(request.mode)),
        ("agent".to_string(), jstr(agent)),
        ("script".to_string(), jstr(script)),
        (
            "correlation".to_string(),
            request.correlation.as_deref().map_or(Json::Null, jstr),
        ),
        (
            "db_path".to_string(),
            jstr(&swarm.join("tasks.db").display().to_string()),
        ),
        ("swarm_dir".to_string(), jstr(&swarm.display().to_string())),
        ("events".to_string(), Json::Int(request.events)),
        ("tasks".to_string(), Json::Arr(request.tasks)),
        ("verdicts".to_string(), Json::Arr(request.verdicts)),
        ("history".to_string(), request.history),
    ])
}

/// `orch_status --history TASK`: the task row, its transitions and latest verdicts are read here,
/// into the request object `request_json` builds.
async fn history_snapshot(
    pool: &SqlitePool,
    call: &Invocation,
    swarm: &Path,
    task_id: &str,
) -> Result<Json, SeamError> {
    let task = fetch_task(pool, task_id).await?;
    let (task_json, transitions, verdicts) = match task {
        Some(row) => {
            let parsed = Task::from_row(row.clone())?;
            let rows = sqlx::query("SELECT * FROM transitions WHERE task_id=? ORDER BY id")
                .bind(task_id)
                .fetch_all(pool)
                .await
                .map_err(store_err)?;
            let mut transitions: Vec<Json> = Vec::with_capacity(rows.len());
            for transition in &rows {
                transitions.push(Json::Obj(row_fields(transition)?));
            }
            let verdict = verdict_entry(pool, &parsed).await?;
            (Json::Obj(row), transitions, vec![verdict])
        }
        None => (Json::Null, Vec::new(), Vec::new()),
    };
    let history = Json::Obj(vec![
        ("task_id".to_string(), jstr(task_id)),
        ("task".to_string(), task_json),
        ("transitions".to_string(), Json::Arr(transitions)),
    ]);
    let request = request_json(
        &call.agent,
        &call.script,
        swarm,
        OrchStatusRequest {
            mode: "history",
            correlation: None,
            events: 0,
            tasks: Vec::new(),
            verdicts,
            history,
        },
    );
    Ok(request)
}

/// `orch_status` with no `--history`: the task rows for one correlation, or all tasks, with their
/// verdicts, into the request object `request_json` builds.
async fn list_snapshot(
    pool: &SqlitePool,
    call: &Invocation,
    swarm: &Path,
    correlation: Option<String>,
) -> Result<Json, SeamError> {
    let rows = fetch_tasks(pool, correlation.as_deref()).await?;
    let mut verdicts: Vec<Json> = Vec::with_capacity(rows.len());
    for row in &rows {
        let task = Task::from_row(row.clone())?;
        verdicts.push(verdict_entry(pool, &task).await?);
    }
    // An empty table never reads the run log, so a malformed log cannot fail an empty answer.
    let events = if rows.is_empty() {
        0
    } else {
        count_events(swarm, correlation.as_deref())?
    };
    let request = request_json(
        &call.agent,
        &call.script,
        swarm,
        OrchStatusRequest {
            mode: "list",
            correlation,
            events,
            tasks: rows.into_iter().map(Json::Obj).collect(),
            verdicts,
            history: Json::Null,
        },
    );
    Ok(request)
}

/// The snapshot of an empty Task Store in list mode: the input of the sandbox health probe.
pub(crate) fn empty_snapshot() -> Json {
    request_json(
        "A01",
        "orch_status",
        Path::new(".swarm"),
        OrchStatusRequest {
            mode: "list",
            correlation: None,
            events: 0,
            tasks: Vec::new(),
            verdicts: Vec::new(),
            history: Json::Null,
        },
    )
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
            effects_path: None,
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
        assert!(value["history"][0]["from_state"].is_null());
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
            Some("E-INPUT: unknown task for history.task_id")
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

    #[test]
    fn invalid_history_id_fails_before_any_store_access() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let swarm = dir.path().join(".swarm");
        let engine = RustEngine::new().map_err(message)?;
        let error = engine
            .orch_status(&call(dir.path(), &swarm, &["--history", "../x", "--json"]))
            .err();
        assert_eq!(error.as_ref().map(|e| e.code()), Some(ErrorCode::Input));
        assert!(!swarm.exists());
        Ok(())
    }

    #[test]
    fn count_events_skips_a_torn_trailing_line() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        fs::write(
            dir.path().join("events.jsonl"),
            "{\"correlation_id\":\"C1\"}\n{\"correlation_id\":\"C1",
        )
        .map_err(|e| e.to_string())?;
        let count = count_events(dir.path(), Some("C1")).map_err(message)?;
        assert_eq!(count, 1);
        Ok(())
    }

    #[test]
    fn count_events_still_refuses_a_parsed_non_object_line() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        fs::write(
            dir.path().join("events.jsonl"),
            "{\"correlation_id\":\"C1\"}\n42",
        )
        .map_err(|e| e.to_string())?;
        let error = count_events(dir.path(), Some("C1")).err();
        assert_eq!(error.as_ref().map(|e| e.code()), Some(ErrorCode::Internal));
        Ok(())
    }

    #[test]
    fn missing_relative_swarm_dir_with_dotdot_is_normalised() -> TestResult {
        let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
        let base = fs::canonicalize(&cwd).map_err(|e| e.to_string())?;
        let resolved = resolve_swarm_dir(&cwd, Some("f32-no-such-dir/../f32-state"));
        assert_eq!(resolved, base.join("f32-state"));
        Ok(())
    }

    /// Writes `scripts/<name>.py` under `root`.
    fn write_script(root: &Path, name: &str, body: &str) -> TestResult {
        let dir = root.join("scripts");
        fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
        fs::write(dir.join(format!("{name}.py")), body).map_err(|e| e.to_string())
    }

    fn python_call(
        root: &Path,
        script: &str,
        argv: &[&str],
        env: Vec<(String, String)>,
        deadline: Duration,
    ) -> Invocation {
        Invocation {
            agent: "A01".to_string(),
            script: script.to_string(),
            argv: argv.iter().map(|s| s.to_string()).collect(),
            root: root.to_path_buf(),
            env,
            deadline,
            effects_path: None,
        }
    }

    /// Exit 0, 1 and 2 replies pass through the seam with their stdout bytes unchanged.
    #[test]
    fn python_engine_keeps_the_exit_0_1_2_reply_bytes() -> TestResult {
        const EXIT_ZERO: &str = r#"import sys
sys.stdout.write('{"status": "ok", "n": 1}\n')
"#;
        const EXIT_ONE: &str = r#"import sys
sys.stdout.write('{"status": "fail", "summary": "gate red"}\n')
sys.exit(1)
"#;
        const EXIT_TWO: &str = r#"import sys
sys.stdout.write('{"error": {"code": "E-CONTRACT", "message": "schema rejected"}}\n')
sys.exit(2)
"#;
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        let engine = PythonEngine::new("python3");
        let cases = [
            (
                "exit_zero",
                EXIT_ZERO,
                0,
                r#"{"status": "ok", "n": 1}
"#,
            ),
            (
                "exit_one",
                EXIT_ONE,
                1,
                r#"{"status": "fail", "summary": "gate red"}
"#,
            ),
            (
                "exit_two",
                EXIT_TWO,
                2,
                r#"{"error": {"code": "E-CONTRACT", "message": "schema rejected"}}
"#,
            ),
        ];
        for (script, body, exit_code, stdout) in cases {
            write_script(dir.path(), script, body)?;
            let call = python_call(dir.path(), script, &[], Vec::new(), Duration::from_secs(30));
            let reply = invoke(&engine, &call);
            if reply.exit_code != exit_code || reply.stdout != stdout {
                return Err(format!(
                    "{script}: exit {} stdout {:?}",
                    reply.exit_code, reply.stdout
                ));
            }
        }
        Ok(())
    }

    /// A usage error (exit 2, empty stdout) is an `E-INPUT` failure at the seam.
    #[test]
    fn python_engine_usage_error_is_e_input() -> TestResult {
        const USAGE: &str = r#"import sys
sys.stderr.write("usage: probe --task-id\n")
sys.exit(2)
"#;
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        write_script(dir.path(), "usage", USAGE)?;
        let engine = PythonEngine::new("python3");
        let call = python_call(
            dir.path(),
            "usage",
            &[],
            Vec::new(),
            Duration::from_secs(30),
        );
        let err = engine.lane_verb(&call).err().ok_or("usage must fail")?;
        if err.code() != ErrorCode::Input {
            return Err(err.message().to_string());
        }
        Ok(())
    }

    /// Whether `pid` has exited or is a zombie, which is also dead. Polls for up to `limit`.
    #[cfg(target_os = "linux")]
    fn gone_within(pid: u32, limit: Duration) -> bool {
        let started = std::time::Instant::now();
        loop {
            if process_gone(pid) {
                return true;
            }
            if started.elapsed() >= limit {
                return false;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }

    #[cfg(target_os = "linux")]
    fn process_gone(pid: u32) -> bool {
        match fs::read_to_string(format!("/proc/{pid}/stat")) {
            Err(_) => true,
            Ok(stat) => {
                stat.rsplit_once(')')
                    .and_then(|(_, rest)| rest.split_whitespace().next())
                    == Some("Z")
            }
        }
    }

    /// Starts a grandchild in the same process group, records its pid in `argv[1]`, then sleeps.
    #[cfg(target_os = "linux")]
    const GRANDCHILD: &str = r#"import subprocess, sys, time
child = subprocess.Popen(["sleep", "60"])
with open(sys.argv[1], "w") as handle:
    handle.write(str(child.pid))
time.sleep(60)
"#;

    /// The deadline kills the whole process group, not only the python leader.
    #[cfg(target_os = "linux")]
    #[test]
    fn python_engine_timeout_stops_the_whole_process_group() -> TestResult {
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        write_script(dir.path(), "grandchild", GRANDCHILD)?;
        let pid_file = dir.path().join("grandchild.pid");
        let pid_arg = pid_file.display().to_string();
        let engine = PythonEngine::new("python3");
        let call = python_call(
            dir.path(),
            "grandchild",
            &[pid_arg.as_str()],
            Vec::new(),
            Duration::from_secs(5),
        );
        let err = engine
            .lane_verb(&call)
            .err()
            .ok_or("the deadline must stop the call")?;
        if err.code() != ErrorCode::Timeout {
            return Err(err.message().to_string());
        }
        let pid: u32 = fs::read_to_string(&pid_file)
            .map_err(|e| e.to_string())?
            .trim()
            .parse()
            .map_err(|e: std::num::ParseIntError| e.to_string())?;
        if !gone_within(pid, Duration::from_secs(3)) {
            return Err(format!("grandchild {pid} survived the group stop"));
        }
        Ok(())
    }

    /// The child inherits the parent's PATH and SWARM_DIR, and never sees the task identity.
    #[test]
    fn python_engine_child_inherits_path_and_loses_task_identity() -> TestResult {
        const ENV_PROBE: &str = r#"import json, os
print(json.dumps({
    "path": os.environ.get("PATH"),
    "swarm_dir": os.environ.get("SWARM_DIR"),
    "task_id_set": "SWARM_TASK_ID" in os.environ,
    "correlation_set": "SWARM_CORRELATION_ID" in os.environ,
}))
"#;
        let dir = tempfile::tempdir().map_err(|e| e.to_string())?;
        write_script(dir.path(), "env_probe", ENV_PROBE)?;
        let swarm_arg = dir.path().join(".swarm").display().to_string();
        let env = vec![
            ("SWARM_DIR".to_string(), swarm_arg.clone()),
            ("SWARM_TASK_ID".to_string(), "T-1".to_string()),
            ("SWARM_CORRELATION_ID".to_string(), "C-1".to_string()),
        ];
        let engine = PythonEngine::new("python3");
        let call = python_call(dir.path(), "env_probe", &[], env, Duration::from_secs(30));
        let reply = engine.lane_verb(&call).map_err(message)?;
        let seen: serde_json::Value =
            serde_json::from_str(&reply.stdout).map_err(|e| e.to_string())?;

        let inherited_path = std::env::var("PATH").ok();
        if seen["path"].as_str() != inherited_path.as_deref() {
            return Err(format!("child PATH {} is not inherited", seen["path"]));
        }
        let expected_swarm = resolve_swarm_dir(dir.path(), Some(swarm_arg.as_str()));
        if seen["swarm_dir"].as_str() != expected_swarm.to_str() {
            return Err(format!("child SWARM_DIR {} is wrong", seen["swarm_dir"]));
        }
        if seen["task_id_set"].as_bool() != Some(false)
            || seen["correlation_set"].as_bool() != Some(false)
        {
            return Err("the child still sees the task identity".to_string());
        }
        Ok(())
    }
}
