//! Process-group runner for the omp bridge: runs one `scripts/<name>.py --json` child and maps its
//! exit to a reply or an error.
//!
//! The four lifecycle verbs map onto `omp/src/bridge.ts`:
//! - `init` is `swarmRoot()` (bridge.ts:66-72): the trimmed env root when it holds
//!   `scripts/orch_plan.py`, else the package root (realpath), else `E-DEP`.
//! - `health` re-checks `scripts/orch_plan.py` under the stored root.
//! - `invoke` is `runScript()` (bridge.ts:121-193): abort gate, name gate, script-file gate,
//!   env build, detached spawn, timed stop (group SIGTERM, then group SIGKILL after
//!   `KILL_GRACE_MS`), and the exit verdict from `classify_exit`.
//! - `shutdown` is `killInflight()` (bridge.ts:91-93): every in-flight `invoke` stops with
//!   `StopReason::Abort` and reports the session-shutdown message.
//!
//! The swarm directory is not resolved here. bridge.ts `swarmDir` (decision D-08) is the
//! caller's job. The runner takes the resolved directory and hands it to the child as
//! `SWARM_DIR`.
//!
//! `gsd-adapter`'s `strangler_seam::PythonEngine` drives this module through
//! [`OmpBridge::invoke_raw`] and keeps its own exit-contract check, so its exit-2 error replies
//! come back as data. [`OmpBridge::invoke`] is the omp-facing path: it follows the frozen contract
//! in `tests/omp_contract/freeze.md` and classifies every exit.

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::future::Future;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::pin::Pin;
use std::process::{ExitStatus, Stdio};
use std::time::Duration;

use gsd_codes::ErrorCode;
use nix::sys::signal::{killpg, Signal};
use nix::unistd::Pid;
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;
use tokio::sync::watch;
use tokio::task::JoinError;
use tokio::time::{Instant, Sleep};

use crate::omp_bridge_pins::{
    classify_exit, script_argv, script_path, AbortKind, BridgeError, BridgeReply, StopReason,
};
use crate::omp_constants_mirror::{KILL_GRACE_MS, STRIPPED_ENV};

/// The frozen-contract record ids this module replays.
pub const VECTOR_IDS: &[&str] = &[
    "A-SPAWN-GENERIC-01",
    "A-SPAWN-GENERIC-02",
    "A-SPAWN-GENERIC-03",
    "A-SPAWN-GENERIC-04",
    "A-SPAWN-GENERIC-05",
    "A-SPAWN-GENERIC-06",
    "A-SPAWN-GENERIC-07",
    "A-SPAWN-GENERIC-08",
    "A-SPAWN-GENERIC-09",
    "A-SPAWN-GENERIC-10",
    "A-SPAWN-GENERIC-11",
    "A-SPAWN-GENERIC-12",
    "A-SPAWN-GENERIC-13",
    "A-SPAWN-GENERIC-14",
    "X-GROUP-KILL-TERM-01",
    "X-GROUP-KILL-TERM-02",
    "X-GROUP-KILL-TERM-03",
    "X-GROUP-KILL-TERM-04",
];

/// Deadline used for the escalation timer while it is disarmed. The select guard keeps the timer
/// from being polled while disarmed, so this only has to lie far in the future.
const DISARMED_FOR: Duration = Duration::from_secs(365 * 24 * 60 * 60);

/// The most bytes kept from one child pipe. A child that writes more fails the run with
/// `E-INTERNAL`. The surplus is still read and dropped, so the child never blocks on a full pipe.
const PIPE_CAP_BYTES: usize = 64 * 1024 * 1024;

/// The bridge: the root that holds `scripts/`, and the shutdown broadcast for in-flight calls.
#[derive(Debug)]
pub struct OmpBridge {
    root: PathBuf,
    shutdown: watch::Sender<bool>,
    interpreter: String,
}

/// One `invoke` call.
#[derive(Debug, Clone, Copy)]
pub struct InvokeRequest<'a> {
    /// Script name without `.py`, for example `orch_plan`.
    pub script: &'a str,
    /// Arguments before `--json`, which the runner appends.
    pub args: &'a [String],
    /// The caller's working directory; a relative `SWARM_ED25519_KEY_FILE` resolves against it.
    pub cwd: &'a Path,
    /// The resolved swarm directory, handed to the child as `SWARM_DIR`.
    pub swarm_dir: &'a Path,
    /// The caller's environment as name/value pairs. The child gets it after the runner's edits.
    pub env: &'a [(String, String)],
    /// Deadline in milliseconds. The child's process group is stopped when it passes.
    pub timeout_ms: u64,
}

/// The outcome of one [`OmpBridge::invoke_raw`] child. Nothing is classified: the exit code and
/// both pipes come back as the child left them, with the bridge's stop state beside them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawRun {
    /// The exit code, or `None` when a signal ended the child.
    pub exit_code: Option<i32>,
    /// The child's stdout bytes. Empty when the bridge stopped the child and gave up on the pipe.
    pub stdout: Vec<u8>,
    /// The child's stderr bytes, with the same rule as `stdout`.
    pub stderr: Vec<u8>,
    /// Why the bridge stopped the child: the deadline or an abort. `None` when it exited alone.
    pub stopped: Option<StopReason>,
    /// The caller's abort, when it fired before the exit was observed.
    pub aborted: Option<AbortKind>,
}

/// Resolves the bridge root: the env root (trimmed) when it holds `scripts/orch_plan.py`, else the
/// package root (realpath) when it does, else `E-DEP`.
pub fn init(package_root: &Path, env_root: Option<&str>) -> Result<OmpBridge, BridgeError> {
    if let Some(env) = env_root.map(str::trim).filter(|value| !value.is_empty()) {
        let candidate = lexical_normal(&absolute(Path::new(env))?);
        if has_orch_plan(&candidate) {
            return Ok(OmpBridge::for_root(candidate));
        }
    }
    if has_orch_plan(package_root) {
        let real = fs::canonicalize(package_root).map_err(|error| {
            BridgeError::new(
                ErrorCode::Dep,
                format!("cannot resolve {}: {error}", package_root.display()),
            )
        })?;
        return Ok(OmpBridge::for_root(real));
    }
    Err(scripts_missing())
}

impl OmpBridge {
    /// Binds the bridge to `root` with no `scripts/orch_plan.py` check. The caller owns the root
    /// and its path checks; [`init`] is the omp-facing resolver.
    pub fn for_root(root: PathBuf) -> Self {
        let (shutdown, _subscriber) = watch::channel(false);
        Self {
            root,
            shutdown,
            interpreter: "python3".to_owned(),
        }
    }

    /// Spawns `interpreter` in place of `python3` for every call made through this bridge.
    pub fn with_interpreter(mut self, interpreter: impl Into<String>) -> Self {
        self.interpreter = interpreter.into();
        self
    }

    /// Reports `E-DEP` when `scripts/orch_plan.py` is missing under the stored root.
    pub fn health(&self) -> Result<(), BridgeError> {
        if has_orch_plan(&self.root) {
            Ok(())
        } else {
            Err(scripts_missing())
        }
    }

    /// Stops every in-flight `invoke` with `StopReason::Abort`. Invocations started afterwards are
    /// not affected.
    pub fn shutdown(&self) {
        let _previous = self.shutdown.send_replace(true);
    }

    /// Runs `scripts/<script>.py <args> --json` and maps its outcome.
    ///
    /// `abort` is the caller's cancellation. A future that is already ready refuses the call before
    /// anything is spawned. Callers without an abort pass `std::future::pending()`.
    ///
    /// The child gets the isolated environment of `runScript`: `env` with the task identity
    /// stripped, `SWARM_DIR` set, and nothing inherited.
    pub async fn invoke(
        &self,
        req: InvokeRequest<'_>,
        abort: impl Future<Output = AbortKind>,
    ) -> Result<BridgeReply, BridgeError> {
        let InvokeRequest {
            script,
            args,
            cwd,
            swarm_dir,
            env,
            timeout_ms,
        } = req;
        let mut abort = std::pin::pin!(abort);
        let path = self
            .admit(script, timeout_ms, swarm_dir, &mut abort)
            .await?;
        let environ = child_env(env, swarm_dir, cwd)?;
        let mut command = self.command(&path, args);
        command.env_clear().envs(&environ);
        let run = self
            .drive(script, command, timeout_ms, abort.as_mut())
            .await?;

        let stopped_early = run.stopped.is_some() || run.aborted.is_some();
        let stdout = match String::from_utf8(run.stdout) {
            Ok(text) => text,
            Err(_) if stopped_early => String::new(),
            Err(_) => {
                return Err(BridgeError::new(
                    ErrorCode::Internal,
                    format!("{script} stdout is not valid UTF-8"),
                ))
            }
        };
        let stderr = String::from_utf8_lossy(&run.stderr);
        classify_exit(
            script,
            timeout_ms,
            run.aborted,
            run.stopped,
            run.exit_code,
            &stdout,
            &stderr,
            swarm_dir,
        )
    }

    /// Runs `scripts/<script>.py <args> --json` and returns what the child left behind, with no
    /// verdict. This is the seam path: the bridge owns the process group, the deadline and the
    /// abort, and the caller classifies the exit.
    ///
    /// The child inherits the process environment. `req.env` is an overlay on it: the task identity
    /// is removed from both, and `SWARM_DIR` is set.
    pub async fn invoke_raw(
        &self,
        req: InvokeRequest<'_>,
        abort: impl Future<Output = AbortKind>,
    ) -> Result<RawRun, BridgeError> {
        let InvokeRequest {
            script,
            args,
            cwd,
            swarm_dir,
            env,
            timeout_ms,
        } = req;
        let mut abort = std::pin::pin!(abort);
        let path = self
            .admit(script, timeout_ms, swarm_dir, &mut abort)
            .await?;
        let environ = child_env(env, swarm_dir, cwd)?;
        let mut command = self.command(&path, args);
        for key in STRIPPED_ENV {
            command.env_remove(key);
        }
        command.envs(&environ);
        self.drive(script, command, timeout_ms, abort.as_mut())
            .await
    }

    /// The checks that come before a spawn: an abort already set, a valid script name, and the
    /// script file. Returns the script path.
    async fn admit<A>(
        &self,
        script: &str,
        timeout_ms: u64,
        swarm_dir: &Path,
        abort: &mut Pin<&mut A>,
    ) -> Result<PathBuf, BridgeError>
    where
        A: Future<Output = AbortKind>,
    {
        if let Some(kind) = poll_now(abort).await {
            return Err(aborted_before_start(script, timeout_ms, kind, swarm_dir));
        }
        let path = script_path(&self.root, script)?;
        if !path.exists() {
            return Err(BridgeError::new(
                ErrorCode::Dep,
                format!("script not found: scripts/{script}.py under SWARM_ROOT"),
            ));
        }
        Ok(path)
    }

    /// The interpreter command for `path`: the bridge root as working directory, stdin closed,
    /// stdout and stderr piped, and its own process group. The caller sets the environment.
    fn command(&self, path: &Path, args: &[String]) -> Command {
        let mut command = Command::new(&self.interpreter);
        command
            .args(script_argv(path, args))
            .current_dir(&self.root)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        command
    }

    /// Spawns `command` and drives it to its exit: the deadline, the abort, a session shutdown and
    /// the SIGTERM-then-SIGKILL escalation stop the group. Both pipes drain while the child runs.
    /// No verdict is taken here.
    async fn drive<A>(
        &self,
        script: &str,
        mut command: Command,
        timeout_ms: u64,
        mut abort: Pin<&mut A>,
    ) -> Result<RawRun, BridgeError>
    where
        A: Future<Output = AbortKind>,
    {
        // Subscribe before spawning, so a shutdown during the spawn still stops the child.
        let mut shutdown_rx = self.shutdown.subscribe();
        let mut child = command.spawn().map_err(|error| {
            BridgeError::new(
                ErrorCode::Dep,
                format!("cannot start {}: {error}", self.interpreter),
            )
        })?;
        let pid = child
            .id()
            .and_then(|id| i32::try_from(id).ok())
            .ok_or_else(|| {
                BridgeError::new(
                    ErrorCode::Internal,
                    format!("{} has no process id", self.interpreter),
                )
            })?;
        let mut reap = ReapGuard { pid, armed: true };

        let stdout = child.stdout.take().ok_or_else(|| {
            BridgeError::new(
                ErrorCode::Internal,
                format!("{} stdout is not piped", self.interpreter),
            )
        })?;
        let stderr = child.stderr.take().ok_or_else(|| {
            BridgeError::new(
                ErrorCode::Internal,
                format!("{} stderr is not piped", self.interpreter),
            )
        })?;
        // Both pipes drain while the child runs, so a chatty child cannot fill one and block.
        let mut out_task = tokio::spawn(read_pipe(stdout, PIPE_CAP_BYTES));
        let mut err_task = tokio::spawn(read_pipe(stderr, PIPE_CAP_BYTES));

        let mut wait = std::pin::pin!(child.wait());
        let mut deadline = std::pin::pin!(tokio::time::sleep(Duration::from_millis(timeout_ms)));
        let mut escalation = std::pin::pin!(tokio::time::sleep(DISARMED_FOR));
        let mut stop = Stop {
            pid,
            reason: None,
            kill_armed: false,
        };
        let mut signal_abort: Option<AbortKind> = None;

        let exit = loop {
            let event = tokio::select! {
                biased;
                exit = &mut wait => Event::Exited(exit),
                _ = &mut deadline, if stop.reason.is_none() => Event::Deadline,
                kind = &mut abort, if signal_abort.is_none() => Event::Abort(kind),
                _ = shutdown_rx.changed(), if stop.reason.is_none() => Event::Shutdown,
                _ = &mut escalation, if stop.kill_armed => Event::Escalate,
            };
            match event {
                Event::Exited(exit) => break exit,
                Event::Deadline => stop.request(StopReason::Timeout, escalation.as_mut()),
                Event::Abort(kind) => {
                    signal_abort = Some(kind);
                    stop.request(StopReason::Abort, escalation.as_mut());
                }
                Event::Shutdown => stop.request(StopReason::Abort, escalation.as_mut()),
                Event::Escalate => {
                    signal_group(pid, Signal::SIGKILL);
                    stop.kill_armed = false;
                }
            }
        };
        // The child is reaped: from here on the group id is no longer ours to kill, and the
        // escalation timer is cancelled (it is never polled again).
        reap.armed = false;
        let status = exit.map_err(|error| {
            BridgeError::new(
                ErrorCode::Internal,
                format!("cannot wait for {}: {error}", self.interpreter),
            )
        })?;

        // An abort that fired before the exit was observed still counts, as bridge.ts checks
        // `signal.aborted` after the child exits.
        if signal_abort.is_none() {
            signal_abort = poll_now(&mut abort).await;
        }

        let grace = Duration::from_millis(KILL_GRACE_MS);
        let drained =
            tokio::time::timeout(grace, async { tokio::join!(&mut out_task, &mut err_task) }).await;
        let (stdout_read, stderr_read) = match drained {
            Ok((out, err)) => (
                out.map_err(|error| read_failure(script, error))?,
                err.map_err(|error| read_failure(script, error))?,
            ),
            Err(_elapsed) => {
                // A grandchild outlived the leader and still holds a pipe. The group id stays
                // valid while any member lives, so SIGKILL reaches it and closes the pipes. A
                // stop already decided the verdict, so empty output is classified; otherwise
                // the output is lost.
                signal_group(pid, Signal::SIGKILL);
                out_task.abort();
                err_task.abort();
                if stop.reason.is_none() && signal_abort.is_none() {
                    return Err(BridgeError::new(
                        ErrorCode::Internal,
                        format!("{script} exited, but a child kept its output pipes open"),
                    ));
                }
                (PipeRead::default(), PipeRead::default())
            }
        };
        if let Some(err) = pipe_failure(script, &stdout_read, &stderr_read) {
            return Err(err);
        }
        Ok(RawRun {
            exit_code: status.code(),
            stdout: stdout_read.bytes,
            stderr: stderr_read.bytes,
            stopped: stop.reason,
            aborted: signal_abort,
        })
    }
}

/// One wake-up of the invocation loop.
enum Event {
    Exited(io::Result<ExitStatus>),
    Deadline,
    Abort(AbortKind),
    Shutdown,
    Escalate,
}

/// Stop state of one child. The first stop wins. SIGTERM goes to the process group, and the
/// escalation timer stays armed until the child exits.
struct Stop {
    pid: i32,
    reason: Option<StopReason>,
    kill_armed: bool,
}

impl Stop {
    fn request(&mut self, why: StopReason, escalation: Pin<&mut Sleep>) {
        if self.reason.is_some() {
            return;
        }
        self.reason = Some(why);
        signal_group(self.pid, Signal::SIGTERM);
        escalation.reset(Instant::now() + Duration::from_millis(KILL_GRACE_MS));
        self.kill_armed = true;
    }
}

/// Kills the process group if the invocation ends before the child is reaped, so a dropped future
/// never leaves a live child behind. Disarmed once the child has been reaped.
struct ReapGuard {
    pid: i32,
    armed: bool,
}

impl Drop for ReapGuard {
    fn drop(&mut self) {
        if self.armed {
            signal_group(self.pid, Signal::SIGKILL);
        }
    }
}

/// Sends `signal` to the process group led by `pid`. A group that is already gone is not an error.
fn signal_group(pid: i32, signal: Signal) {
    let _ = killpg(Pid::from_raw(pid), signal);
}

/// What one child pipe held: the bytes kept under the cap, whether any byte was dropped, and
/// whether the read failed before the pipe ended.
#[derive(Default)]
struct PipeRead {
    bytes: Vec<u8>,
    overflowed: bool,
    errored: bool,
}

/// Reads a pipe to its end, keeping at most `cap` bytes. Bytes past the cap are read and dropped,
/// so the child never blocks on a full pipe. A read error ends the read; the bytes read so far
/// are kept and `errored` is set, which the caller reports as `E-INTERNAL`.
async fn read_pipe<R>(mut pipe: R, cap: usize) -> PipeRead
where
    R: AsyncRead + Unpin + Send + 'static,
{
    let mut captured = PipeRead::default();
    let mut chunk = [0u8; 8192];
    loop {
        match pipe.read(&mut chunk).await {
            Ok(0) => break,
            Ok(count) => {
                captured.overflowed |= append_capped(&mut captured.bytes, &chunk[..count], cap);
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(_) => {
                captured.errored = true;
                break;
            }
        }
    }
    captured
}

/// Appends `chunk` to `kept` while `kept` holds fewer than `cap` bytes. Returns whether any byte
/// of `chunk` was dropped.
fn append_capped(kept: &mut Vec<u8>, chunk: &[u8], cap: usize) -> bool {
    let room = cap.saturating_sub(kept.len());
    let taken = room.min(chunk.len());
    kept.extend_from_slice(&chunk[..taken]);
    taken < chunk.len()
}

/// The failure a child's pipes force, if any: a read error or an overflow past the cap is
/// `E-INTERNAL`. Both pipes are checked, stdout first.
fn pipe_failure(script: &str, stdout: &PipeRead, stderr: &PipeRead) -> Option<BridgeError> {
    if stdout.errored || stderr.errored {
        return Some(BridgeError::new(
            ErrorCode::Internal,
            format!("{script} output could not be read"),
        ));
    }
    if stdout.overflowed || stderr.overflowed {
        return Some(BridgeError::new(
            ErrorCode::Internal,
            format!("{script} wrote more than {PIPE_CAP_BYTES} bytes of output"),
        ));
    }
    None
}

fn read_failure(script: &str, error: JoinError) -> BridgeError {
    BridgeError::new(
        ErrorCode::Internal,
        format!("cannot read {script} output: {error}"),
    )
}

/// Polls `future` once without waiting. Returns its value when it is already ready.
async fn poll_now<F>(future: &mut F) -> Option<F::Output>
where
    F: Future + Unpin,
{
    tokio::time::timeout(Duration::ZERO, future).await.ok()
}

/// The refusal for an abort that is already set before the call starts. Nothing is spawned.
fn aborted_before_start(
    script: &str,
    timeout_ms: u64,
    kind: AbortKind,
    swarm_dir: &Path,
) -> BridgeError {
    // classify_exit owns the abort messages, and with an abort kind it always returns the error.
    classify_exit(
        script,
        timeout_ms,
        Some(kind),
        None,
        None,
        "",
        "",
        swarm_dir,
    )
    .err()
    .unwrap_or_else(|| {
        BridgeError::new(
            ErrorCode::Internal,
            format!("{script} cancelled before start"),
        )
    })
}

/// The child's environment, as `runScript` builds it: the caller's environment with the task
/// identity stripped and `SWARM_DIR` set. A relative `SWARM_ED25519_KEY_FILE` resolves against
/// `cwd`.
fn child_env(
    base: &[(String, String)],
    swarm_dir: &Path,
    cwd: &Path,
) -> Result<BTreeMap<OsString, OsString>, BridgeError> {
    let mut env: BTreeMap<OsString, OsString> = base
        .iter()
        .map(|(key, value)| (OsString::from(key.as_str()), OsString::from(value.as_str())))
        .collect();
    for key in STRIPPED_ENV {
        env.remove(OsStr::new(key));
    }
    env.insert(
        OsString::from("SWARM_DIR"),
        swarm_dir.as_os_str().to_owned(),
    );
    let key_name = OsStr::new("SWARM_ED25519_KEY_FILE");
    let relative_key = env
        .get(key_name)
        .filter(|value| !value.is_empty() && !Path::new(value).is_absolute())
        .cloned();
    if let Some(key) = relative_key {
        let resolved = lexical_normal(&absolute(cwd)?.join(&key));
        env.insert(key_name.to_os_string(), resolved.into_os_string());
    }
    Ok(env)
}

/// Makes `path` absolute against the working directory, as Node's `path.resolve` does.
fn absolute(path: &Path) -> Result<PathBuf, BridgeError> {
    std::path::absolute(path).map_err(|error| {
        BridgeError::new(
            ErrorCode::Internal,
            format!("cannot resolve {}: {error}", path.display()),
        )
    })
}

/// Removes `.` and `..` lexically, as Node's `path.resolve` does. Symlinks are not resolved.
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

fn has_orch_plan(root: &Path) -> bool {
    root.join("scripts").join("orch_plan.py").exists()
}

fn scripts_missing() -> BridgeError {
    BridgeError::new(
        ErrorCode::Dep,
        "agent-swarm scripts not found (no scripts/orch_plan.py); set SWARM_ROOT",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use nix::errno::Errno;
    use nix::sys::signal::kill;
    use serde_json::json;
    use std::future::{pending, ready};
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Reports its own path, cwd, environment facts and stdin as one JSON object.
    const PROBE: &str = r#"import json, os, sys
print(json.dumps({
    "status": "ok",
    "argv": sys.argv[1:],
    "script": os.path.abspath(__file__),
    "cwd": os.getcwd(),
    "swarm_dir": os.environ.get("SWARM_DIR"),
    "task_id_set": "SWARM_TASK_ID" in os.environ,
    "correlation_set": "SWARM_CORRELATION_ID" in os.environ,
    "stdin": sys.stdin.read(),
    "own_group": os.getpgid(0) == os.getpid(),
    "key_file": os.environ.get("SWARM_ED25519_KEY_FILE"),
}))
"#;

    const FAIL_VERDICT: &str = r#"import json, sys
print(json.dumps({"status": "fail", "summary": "gate red"}))
sys.exit(1)
"#;

    const REJECT: &str = r#"import json, sys
print(json.dumps({"error": {"code": "E-CONTRACT", "message": "schema rejected"}}))
sys.exit(2)
"#;

    const USAGE: &str = r#"import sys
sys.stderr.write("usage: probe --task-id\n")
sys.exit(2)
"#;

    const NON_UTF8: &str = r#"import sys
sys.stdout.buffer.write(b"\xff\n")
"#;

    const SLEEP_FOREVER: &str = r#"import time
time.sleep(60)
"#;

    /// Starts a grandchild in the same group, records its pid in `argv[1]`, then sleeps.
    const GRANDCHILD: &str = r#"import subprocess, sys, time
child = subprocess.Popen(["sleep", "60"])
with open(sys.argv[1], "w") as handle:
    handle.write(str(child.pid))
time.sleep(60)
"#;

    /// Ignores SIGTERM, records its pid in `argv[1]`, then sleeps forever.
    const STUBBORN: &str = r#"import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(sys.argv[1], "w") as handle:
    handle.write(str(os.getpid()))
while True:
    time.sleep(1)
"#;

    /// Exits at once. It starts `stubborn.py` in the same group with the inherited stdout and
    /// stderr, so that grandchild holds both pipes after the leader is reaped.
    const LEAVES_GRANDCHILD: &str = r#"import os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
subprocess.Popen([sys.executable, os.path.join(here, "stubborn.py"), sys.argv[1]])
"#;

    /// Writes a marker file at `argv[1]`, so a test can show the script ran.
    const MARKER: &str = r#"import sys
open(sys.argv[1], "w").close()
"#;

    static NEXT_ROOT: AtomicUsize = AtomicUsize::new(0);

    /// A throwaway root with a `scripts/` directory. It is removed on drop, including on panic.
    struct TempRoot {
        path: PathBuf,
    }

    impl TempRoot {
        /// A root that passes `init`: it holds an empty `scripts/orch_plan.py`.
        fn new(name: &str) -> Self {
            let root = Self::bare(name);
            root.script("orch_plan", "");
            root
        }

        /// A root with an empty `scripts/` directory and no `orch_plan.py`.
        fn bare(name: &str) -> Self {
            let id = NEXT_ROOT.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir()
                .join(format!("gsd-sdk-runner-{}-{name}-{id}", std::process::id()));
            fs::create_dir_all(path.join("scripts")).expect("create test root");
            Self { path }
        }

        fn script(&self, name: &str, body: &str) {
            fs::write(self.path.join("scripts").join(format!("{name}.py")), body)
                .expect("write fake script");
        }

        fn canonical(&self) -> PathBuf {
            fs::canonicalize(&self.path).expect("canonical test root")
        }
    }

    impl Drop for TempRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.path);
        }
    }

    fn text(path: &Path) -> String {
        path.to_string_lossy().into_owned()
    }

    fn base_env() -> Vec<(String, String)> {
        std::env::vars().collect()
    }

    async fn run(
        bridge: &OmpBridge,
        root: &TempRoot,
        script: &str,
        args: &[String],
        env: &[(String, String)],
        timeout_ms: u64,
        abort: impl Future<Output = AbortKind>,
    ) -> Result<BridgeReply, BridgeError> {
        let swarm_dir = root.path.join(".swarm");
        let req = InvokeRequest {
            script,
            args,
            cwd: &root.path,
            swarm_dir: &swarm_dir,
            env,
            timeout_ms,
        };
        bridge.invoke(req, abort).await
    }

    /// Whether `pid` has exited or is a zombie, which is also dead. Polls for up to `limit`.
    async fn wait_until_gone(pid: i32, limit: Duration) -> bool {
        let started = std::time::Instant::now();
        loop {
            if process_gone(pid) {
                return true;
            }
            if started.elapsed() >= limit {
                return false;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }

    fn process_gone(pid: i32) -> bool {
        matches!(kill(Pid::from_raw(pid), None), Err(Errno::ESRCH)) || is_zombie(pid)
    }

    fn is_zombie(pid: i32) -> bool {
        fs::read_to_string(format!("/proc/{pid}/stat")).is_ok_and(|stat| {
            stat.rsplit_once(')')
                .and_then(|(_, rest)| rest.split_whitespace().next())
                == Some("Z")
        })
    }

    fn read_pid(path: &Path) -> i32 {
        fs::read_to_string(path)
            .expect("pid file written")
            .trim()
            .parse()
            .expect("pid file holds a pid")
    }

    /// Polls until `path` exists or `limit` passes. Returns whether it appeared.
    async fn wait_for_file(path: &Path, limit: Duration) -> bool {
        let started = std::time::Instant::now();
        while !path.exists() {
            if started.elapsed() >= limit {
                return false;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        true
    }

    #[tokio::test]
    async fn init_prefers_a_valid_env_root() {
        let package = TempRoot::new("init-package");
        let env_root = TempRoot::new("init-env");
        env_root.script("probe", PROBE);
        let padded = format!("  {}  ", text(&env_root.path));
        let bridge = init(&package.path, Some(padded.as_str())).expect("valid env root wins");
        bridge.health().expect("env root holds orch_plan.py");

        let reply = run(
            &bridge,
            &env_root,
            "probe",
            &[],
            &base_env(),
            5_000,
            pending::<AbortKind>(),
        )
        .await
        .expect("probe runs from the env root");
        let script = fs::canonicalize(reply.json["script"].as_str().expect("script path"))
            .expect("reported script exists");
        assert_eq!(
            script,
            env_root.canonical().join("scripts").join("probe.py")
        );
    }

    #[tokio::test]
    async fn init_falls_back_to_package_root_when_env_root_is_invalid() {
        let package = TempRoot::new("fallback-package");
        package.script("probe", PROBE);
        for env_root in [Some("/gsd-sdk-no-such-root"), Some("   ")] {
            let bridge = init(&package.path, env_root).expect("package root is valid");
            let reply = run(
                &bridge,
                &package,
                "probe",
                &[],
                &base_env(),
                5_000,
                pending::<AbortKind>(),
            )
            .await
            .expect("probe runs from the package root");
            let script = fs::canonicalize(reply.json["script"].as_str().expect("script path"))
                .expect("reported script exists");
            assert_eq!(script, package.canonical().join("scripts").join("probe.py"));
        }
    }

    #[test]
    fn init_without_any_root_is_dep_error() {
        let package = TempRoot::bare("init-none");
        let err = init(&package.path, Some("/gsd-sdk-no-such-root"))
            .expect_err("no root holds orch_plan.py");
        assert_eq!(err.code, ErrorCode::Dep);
        assert_eq!(
            err.message,
            "E-DEP: agent-swarm scripts not found (no scripts/orch_plan.py); set SWARM_ROOT"
        );
    }

    #[test]
    fn health_reports_dep_once_orch_plan_is_removed() {
        let root = TempRoot::new("health");
        let bridge = init(&root.path, None).expect("init");
        bridge.health().expect("healthy while orch_plan.py exists");
        fs::remove_file(root.path.join("scripts").join("orch_plan.py")).expect("remove plan");
        let err = bridge.health().expect_err("unhealthy without orch_plan.py");
        assert_eq!(err.code, ErrorCode::Dep);
        assert_eq!(
            err.message,
            "E-DEP: agent-swarm scripts not found (no scripts/orch_plan.py); set SWARM_ROOT"
        );
    }

    #[tokio::test]
    async fn invoke_runs_the_script_with_the_contract_argv_env_and_group() {
        let root = TempRoot::new("contract");
        root.script("probe", PROBE);
        let bridge = init(&root.path, None).expect("init");
        let mut env = base_env();
        env.push(("SWARM_TASK_ID".to_owned(), "T-1".to_owned()));
        env.push(("SWARM_CORRELATION_ID".to_owned(), "C-1".to_owned()));
        env.push((
            "SWARM_ED25519_KEY_FILE".to_owned(),
            "keys/signer.pem".to_owned(),
        ));
        let swarm_dir = root.path.join("state").join(".swarm");
        let args = vec!["--task-id=T-1".to_owned(), "plain".to_owned()];
        let req = InvokeRequest {
            script: "probe",
            args: &args,
            cwd: &root.path,
            swarm_dir: &swarm_dir,
            env: &env,
            timeout_ms: 5_000,
        };

        let reply = bridge
            .invoke(req, pending::<AbortKind>())
            .await
            .expect("probe succeeds");

        let json = &reply.json;
        assert_eq!(reply.exit_code, 0);
        assert_eq!(json["argv"], json!(["--task-id=T-1", "plain", "--json"]));
        assert_eq!(json["cwd"], json!(text(&root.canonical())));
        assert_eq!(
            json["script"],
            json!(text(&root.canonical().join("scripts").join("probe.py")))
        );
        assert_eq!(json["swarm_dir"], json!(text(&swarm_dir)));
        assert_eq!(json["task_id_set"], json!(false));
        assert_eq!(json["correlation_set"], json!(false));
        assert_eq!(json["stdin"], json!(""));
        assert_eq!(json["own_group"], json!(true));
        assert_eq!(
            json["key_file"],
            json!(text(&root.path.join("keys").join("signer.pem")))
        );
    }

    #[test]
    fn child_env_sets_swarm_dir_and_strips_task_identity() {
        let base = vec![
            ("PATH".to_owned(), "/usr/bin".to_owned()),
            ("SWARM_TASK_ID".to_owned(), "T-1".to_owned()),
            ("SWARM_CORRELATION_ID".to_owned(), "C-1".to_owned()),
            ("SWARM_DIR".to_owned(), "/stale".to_owned()),
        ];
        let env =
            child_env(&base, Path::new("/work/.swarm"), Path::new("/work")).expect("env builds");
        assert_eq!(
            env.get(OsStr::new("PATH")),
            Some(&OsString::from("/usr/bin"))
        );
        assert_eq!(
            env.get(OsStr::new("SWARM_DIR")),
            Some(&OsString::from("/work/.swarm"))
        );
        assert!(!env.contains_key(OsStr::new("SWARM_TASK_ID")));
        assert!(!env.contains_key(OsStr::new("SWARM_CORRELATION_ID")));
    }

    #[test]
    fn child_env_resolves_only_a_relative_key_file_against_cwd() {
        let resolved = |value: &str| -> OsString {
            let base = vec![("SWARM_ED25519_KEY_FILE".to_owned(), value.to_owned())];
            let mut env = child_env(&base, Path::new("/work/.swarm"), Path::new("/work"))
                .expect("env builds");
            env.remove(OsStr::new("SWARM_ED25519_KEY_FILE"))
                .expect("key stays set")
        };
        assert_eq!(
            resolved("keys/signer.pem"),
            OsString::from("/work/keys/signer.pem")
        );
        assert_eq!(
            resolved("a/../keys/signer.pem"),
            OsString::from("/work/keys/signer.pem")
        );
        assert_eq!(
            resolved("/etc/signer.pem"),
            OsString::from("/etc/signer.pem")
        );
        assert_eq!(resolved(""), OsString::new());
    }

    #[tokio::test]
    async fn invalid_script_name_is_input_error() {
        let root = TempRoot::new("bad-name");
        let bridge = init(&root.path, None).expect("init");
        let err = run(
            &bridge,
            &root,
            "Bad-Name",
            &[],
            &base_env(),
            5_000,
            pending::<AbortKind>(),
        )
        .await
        .expect_err("name gate refuses");
        assert_eq!(err.code, ErrorCode::Input);
    }

    #[tokio::test]
    async fn missing_script_is_dep_error() {
        let root = TempRoot::new("missing");
        let bridge = init(&root.path, None).expect("init");
        let err = run(
            &bridge,
            &root,
            "missing",
            &[],
            &base_env(),
            5_000,
            pending::<AbortKind>(),
        )
        .await
        .expect_err("script file is absent");
        assert_eq!(
            err.message,
            "E-DEP: script not found: scripts/missing.py under SWARM_ROOT"
        );
    }

    #[tokio::test]
    async fn exit_one_with_fail_status_is_returned_as_data() {
        let root = TempRoot::new("exit-one");
        root.script("gate", FAIL_VERDICT);
        let bridge = init(&root.path, None).expect("init");
        let reply = run(
            &bridge,
            &root,
            "gate",
            &[],
            &base_env(),
            5_000,
            pending::<AbortKind>(),
        )
        .await
        .expect("exit 1 with status fail is a result");
        assert_eq!(reply.exit_code, 1);
        assert_eq!(reply.json["status"], json!("fail"));
    }

    #[tokio::test]
    async fn exit_two_with_error_object_maps_to_its_code() {
        let root = TempRoot::new("exit-two");
        root.script("reject", REJECT);
        let bridge = init(&root.path, None).expect("init");
        let err = run(
            &bridge,
            &root,
            "reject",
            &[],
            &base_env(),
            5_000,
            pending::<AbortKind>(),
        )
        .await
        .expect_err("exit 2 with an error object fails");
        assert_eq!(err.code, ErrorCode::Contract);
        assert!(err.message.contains("schema rejected"), "{}", err.message);
    }

    #[tokio::test]
    async fn exit_two_without_stdout_reports_captured_stderr() {
        let root = TempRoot::new("usage");
        root.script("probe", USAGE);
        let bridge = init(&root.path, None).expect("init");
        let err = run(
            &bridge,
            &root,
            "probe",
            &[],
            &base_env(),
            5_000,
            pending::<AbortKind>(),
        )
        .await
        .expect_err("usage error");
        assert_eq!(err.code, ErrorCode::Input);
        assert!(
            err.message.contains("usage: probe --task-id"),
            "{}",
            err.message
        );
    }

    #[tokio::test]
    async fn interpreter_that_cannot_start_is_dep_error() {
        let root = TempRoot::new("no-python");
        root.script("probe", PROBE);
        let bridge = init(&root.path, None).expect("init");
        let env = vec![("PATH".to_owned(), "/nonexistent-swarm-path".to_owned())];
        let err = run(
            &bridge,
            &root,
            "probe",
            &[],
            &env,
            5_000,
            pending::<AbortKind>(),
        )
        .await
        .expect_err("python3 is not on the child's PATH");
        assert_eq!(err.code, ErrorCode::Dep);
        assert!(
            err.message.contains("cannot start python3"),
            "{}",
            err.message
        );
    }

    #[tokio::test]
    async fn non_utf8_stdout_is_internal_error() {
        let root = TempRoot::new("non-utf8");
        root.script("noisy", NON_UTF8);
        let bridge = init(&root.path, None).expect("init");
        let err = run(
            &bridge,
            &root,
            "noisy",
            &[],
            &base_env(),
            5_000,
            pending::<AbortKind>(),
        )
        .await
        .expect_err("stdout must be UTF-8");
        assert_eq!(err.code, ErrorCode::Internal);
        assert!(
            err.message.contains("stdout is not valid UTF-8"),
            "{}",
            err.message
        );
    }

    #[tokio::test]
    async fn timeout_kills_the_child_and_reports_e_timeout() {
        let root = TempRoot::new("timeout");
        root.script("sleeper", SLEEP_FOREVER);
        let bridge = init(&root.path, None).expect("init");
        let started = std::time::Instant::now();
        let err = run(
            &bridge,
            &root,
            "sleeper",
            &[],
            &base_env(),
            200,
            pending::<AbortKind>(),
        )
        .await
        .expect_err("deadline passes");
        assert_eq!(err.code, ErrorCode::Timeout);
        assert!(
            err.message.contains("exceeded 200 ms and was killed"),
            "{}",
            err.message
        );
        assert!(started.elapsed() < Duration::from_secs(10));
    }

    #[tokio::test]
    async fn timeout_stops_the_whole_process_group() {
        let root = TempRoot::new("group-kill");
        root.script("grandchild", GRANDCHILD);
        let bridge = init(&root.path, None).expect("init");
        let pid_file = root.path.join("grandchild.pid");
        let args = vec![text(&pid_file)];
        let env = base_env();
        // The 5 s deadline leaves the child's startup ample room. The ready wait only proves the
        // grandchild existed before the stop; it does not trigger the stop.
        let (result, ready) = tokio::join!(
            run(
                &bridge,
                &root,
                "grandchild",
                &args,
                &env,
                5_000,
                pending::<AbortKind>()
            ),
            wait_for_file(&pid_file, Duration::from_secs(10)),
        );
        assert!(ready, "grandchild never wrote its pid file");
        let err = result.expect_err("deadline passes");
        assert_eq!(err.code, ErrorCode::Timeout);
        let grandchild = read_pid(&pid_file);
        assert!(
            wait_until_gone(grandchild, Duration::from_secs(2)).await,
            "grandchild {grandchild} survived the group stop"
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn drain_timeout_sigkills_a_grandchild_that_ignores_sigterm() {
        let root = TempRoot::new("drain-kill");
        root.script("leader", LEAVES_GRANDCHILD);
        root.script("stubborn", STUBBORN);
        let bridge = init(&root.path, None).expect("init");
        let pid_file = root.path.join("grandchild.pid");
        let args = vec![text(&pid_file)];
        let env = base_env();
        // The leader exits at once, so the run waits out the pipe drain. The ready wait only
        // proves the grandchild wrote its pid while it still held the pipes.
        let (result, ready) = tokio::join!(
            run(
                &bridge,
                &root,
                "leader",
                &args,
                &env,
                60_000,
                pending::<AbortKind>()
            ),
            wait_for_file(&pid_file, Duration::from_secs(10)),
        );
        assert!(ready, "grandchild never wrote its pid file");
        let err = result.expect_err("a child kept the output pipes open");
        assert_eq!(err.code, ErrorCode::Internal);
        let grandchild = read_pid(&pid_file);
        let gone = wait_until_gone(grandchild, Duration::from_secs(2)).await;
        if !gone {
            // Do not leave the stubborn child running behind a failed assertion.
            let _ = kill(Pid::from_raw(grandchild), Signal::SIGKILL);
        }
        assert!(gone, "grandchild {grandchild} survived the drain timeout");
    }

    #[tokio::test]
    async fn child_that_ignores_sigterm_is_killed_after_the_grace_period() {
        let root = TempRoot::new("sigterm-ignored");
        root.script("stubborn", STUBBORN);
        let bridge = init(&root.path, None).expect("init");
        let pid_file = root.path.join("stubborn.pid");
        let args = vec![text(&pid_file)];
        let env = base_env();
        let started = std::time::Instant::now();
        // The pid file is written after SIGTERM is ignored, so its presence means the trap is set.
        let (result, ready) = tokio::join!(
            run(
                &bridge,
                &root,
                "stubborn",
                &args,
                &env,
                5_000,
                pending::<AbortKind>()
            ),
            wait_for_file(&pid_file, Duration::from_secs(10)),
        );
        let elapsed = started.elapsed();
        assert!(ready, "stubborn child never wrote its pid file");
        let err = result.expect_err("deadline passes");
        assert_eq!(err.code, ErrorCode::Timeout);
        // The stop comes at the 5 s deadline, and SIGKILL only after the grace period.
        assert!(
            elapsed >= Duration::from_millis(5_000 + KILL_GRACE_MS),
            "SIGKILL must wait out the grace period, returned after {elapsed:?}"
        );
        assert!(
            elapsed < Duration::from_millis(5_000 + KILL_GRACE_MS + 2_000),
            "returned after {elapsed:?}"
        );
        let stubborn = read_pid(&pid_file);
        assert!(
            wait_until_gone(stubborn, Duration::from_secs(1)).await,
            "child {stubborn} survived SIGKILL"
        );
    }

    #[tokio::test]
    async fn cancelled_abort_stops_the_child_and_reports_partial_state() {
        let root = TempRoot::new("abort-cancelled");
        root.script("sleeper", SLEEP_FOREVER);
        let bridge = init(&root.path, None).expect("init");
        let abort = async {
            tokio::time::sleep(Duration::from_millis(200)).await;
            AbortKind::Cancelled
        };
        let err = run(&bridge, &root, "sleeper", &[], &base_env(), 60_000, abort)
            .await
            .expect_err("caller cancelled");
        assert_eq!(err.code, ErrorCode::Internal);
        assert!(err.message.contains("partially applied"), "{}", err.message);
    }

    #[tokio::test]
    async fn deadline_abort_reports_e_timeout() {
        let root = TempRoot::new("abort-deadline");
        root.script("sleeper", SLEEP_FOREVER);
        let bridge = init(&root.path, None).expect("init");
        let abort = async {
            tokio::time::sleep(Duration::from_millis(200)).await;
            AbortKind::Deadline
        };
        let err = run(&bridge, &root, "sleeper", &[], &base_env(), 60_000, abort)
            .await
            .expect_err("deadline passed");
        assert_eq!(err.code, ErrorCode::Timeout);
        assert!(err.message.contains("deadline exceeded"), "{}", err.message);
    }

    #[tokio::test]
    async fn abort_that_is_ready_before_start_spawns_nothing() {
        let root = TempRoot::new("abort-first");
        root.script("marker", MARKER);
        let bridge = init(&root.path, None).expect("init");
        let marker = root.path.join("spawned.marker");
        let args = vec![text(&marker)];
        let err = run(
            &bridge,
            &root,
            "marker",
            &args,
            &base_env(),
            5_000,
            ready(AbortKind::Cancelled),
        )
        .await
        .expect_err("already aborted");
        assert_eq!(err.code, ErrorCode::Internal);
        assert!(
            !marker.exists(),
            "an already-aborted call must not run the script"
        );
    }

    #[tokio::test]
    async fn shutdown_stops_in_flight_invocations() {
        let root = TempRoot::new("shutdown");
        root.script("sleeper", SLEEP_FOREVER);
        let bridge = init(&root.path, None).expect("init");
        let env = base_env();
        let (result, ()) = tokio::join!(
            run(
                &bridge,
                &root,
                "sleeper",
                &[],
                &env,
                60_000,
                pending::<AbortKind>()
            ),
            async {
                tokio::time::sleep(Duration::from_millis(300)).await;
                bridge.shutdown();
            }
        );
        let err = result.expect_err("shutdown stops the child");
        assert_eq!(err.code, ErrorCode::Internal);
        assert!(
            err.message.contains("omp session shutdown"),
            "{}",
            err.message
        );

        // A call that starts after the shutdown is not affected: killInflight stops only the
        // calls in flight at that moment.
        root.script("probe", PROBE);
        let reply = run(
            &bridge,
            &root,
            "probe",
            &[],
            &env,
            5_000,
            pending::<AbortKind>(),
        )
        .await
        .expect("a call started after shutdown runs");
        assert_eq!(reply.exit_code, 0);
    }

    #[test]
    fn append_capped_keeps_up_to_the_cap_and_reports_what_it_drops() {
        let mut kept = Vec::new();
        assert!(!append_capped(&mut kept, b"abc", 5));
        assert!(append_capped(&mut kept, b"defgh", 5));
        assert_eq!(kept, b"abcde");
        assert!(append_capped(&mut kept, b"x", 5));
        assert_eq!(kept, b"abcde");
        let mut exact = Vec::new();
        assert!(!append_capped(&mut exact, b"abcd", 4));
        assert_eq!(exact, b"abcd");
    }

    #[tokio::test]
    async fn read_pipe_keeps_the_cap_and_drains_the_rest() {
        let over = read_pipe(&b"0123456789"[..], 4).await;
        assert_eq!(over.bytes, b"0123");
        assert!(over.overflowed);
        let within = read_pipe(&b"0123"[..], 4).await;
        assert_eq!(within.bytes, b"0123");
        assert!(!within.overflowed);
    }

    /// Yields `good` on the first read, then fails every later read with a non-EINTR error.
    struct FailAfter {
        good: Option<Vec<u8>>,
    }

    impl AsyncRead for FailAfter {
        fn poll_read(
            mut self: Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
            buf: &mut tokio::io::ReadBuf<'_>,
        ) -> std::task::Poll<io::Result<()>> {
            match self.good.take() {
                Some(bytes) => {
                    buf.put_slice(&bytes);
                    std::task::Poll::Ready(Ok(()))
                }
                None => std::task::Poll::Ready(Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    "read failed",
                ))),
            }
        }
    }

    #[tokio::test]
    async fn pipe_read_error_is_internal() {
        let failed = read_pipe(
            FailAfter {
                good: Some(b"partial".to_vec()),
            },
            1024,
        )
        .await;
        assert!(failed.errored, "a read error must be recorded");
        assert_eq!(
            failed.bytes, b"partial",
            "bytes read before the error are kept"
        );
        let clean = read_pipe(&b"done"[..], 1024).await;
        assert!(!clean.errored);
        let err = pipe_failure("probe", &failed, &clean).expect("a failed pipe is an error");
        assert_eq!(err.code, ErrorCode::Internal);
        assert!(err.message.contains("could not be read"), "{}", err.message);
        assert!(pipe_failure("probe", &clean, &clean).is_none());
    }

    /// Records its own pid in `argv[1]` (written whole, then renamed into place), then sleeps.
    const RECORD_PID: &str = r#"import os, sys, time
tmp = sys.argv[1] + ".tmp"
with open(tmp, "w") as handle:
    handle.write(str(os.getpid()))
os.replace(tmp, sys.argv[1])
time.sleep(60)
"#;

    /// Whether `/proc/<pid>` is gone. A zombie still has an entry, so this means reaped.
    async fn wait_for_reap(pid: i32, limit: Duration) -> bool {
        let started = std::time::Instant::now();
        while Path::new(&format!("/proc/{pid}")).exists() {
            if started.elapsed() >= limit {
                return false;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        true
    }

    #[tokio::test]
    async fn dropping_invoke_reaps_leader() {
        let root = TempRoot::new("drop-reap");
        root.script("recorder", RECORD_PID);
        let bridge = init(&root.path, None).expect("init");
        let pid_file = root.path.join("leader.pid");
        let env = base_env();
        let args = [text(&pid_file)];
        let mut invoke = Box::pin(run(
            &bridge,
            &root,
            "recorder",
            &args,
            &env,
            60_000,
            pending::<AbortKind>(),
        ));
        let written = tokio::select! {
            _ = &mut invoke => panic!("the leader exited before the invoke was dropped"),
            written = wait_for_file(&pid_file, Duration::from_secs(10)) => written,
        };
        assert!(written, "leader never wrote its pid file");
        let leader = read_pid(&pid_file);
        drop(invoke);
        assert!(
            wait_for_reap(leader, Duration::from_secs(5)).await,
            "leader {leader} is still in the process table after its invoke was dropped"
        );
    }
}
