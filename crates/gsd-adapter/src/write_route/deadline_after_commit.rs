//! AR-02 (audit `adapter-rest.md`, refutation `adapter-rest-write-route.md`): a call deadline that expires after the COMMIT of a
//! transition must not drop the effects manifest, and the reply must say the write happened.
//!
//! The host `orch_status --transition` route runs under `within_deadline` (`strangler_seam.rs`,
//! `RustEngine::orch_status_transition`) with the call's own `Invocation::deadline`. Inside it, `transition_reply_with` commits,
//! closes the store, then writes the effects manifest. An expiry in the window between the COMMIT and `write_manifest` drops the
//! future before the manifest is written: the row is committed, the manifest is missing and the reply is a bare `E-TIMEOUT` with no
//! `write_state`.
//!
//! The window is held open by [`super::arm_post_commit_pause`], which exists only in unit-test builds. The deadline is the call's
//! own, so the binary's 600 s default is not touched. The test drives the real route on a real store built by Python's `TaskStore`.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use gsd_route::{jstr, Reply, SeamError};
use tempfile::TempDir;

use super::arm_post_commit_pause;
use crate::strangler_seam::{CoreBackend, Invocation, RustEngine};

/// The call deadline the test gives the engine: long enough for the work before the COMMIT, far shorter than [`HOLD`].
const DEADLINE: Duration = Duration::from_secs(3);

/// How long the armed pause holds the route after its COMMIT: longer than [`DEADLINE`], so the expiry lands in the window.
const HOLD: Duration = Duration::from_secs(6);

/// Seeds one task through Python's own `TaskStore` (schema and migrations are Python's), in state IN_PROGRESS, so that
/// IN_PROGRESS to IN_REVIEW is a legal transition.
const SEED_SCRIPT: &str = r#"
import sys
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
s.create(task_id="T-1", correlation_id="C-1", capability="code.backend", title="T-1")
s.conn.execute("UPDATE tasks SET state='IN_PROGRESS' WHERE task_id=?", ("T-1",))
s.conn.commit()
s.conn.close()
"#;

/// The repository root: `SWARM_REPO_ROOT`, or two levels above this crate.
fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

/// A scratch repository whose `.swarm/tasks.db` holds one IN_PROGRESS task, built by Python's `TaskStore`.
struct World {
    dir: TempDir,
    root: PathBuf,
    swarm: PathBuf,
}

impl World {
    fn seeded() -> Result<Self, String> {
        let dir = TempDir::new().map_err(|e| e.to_string())?;
        let root = dir.path().join("work");
        let swarm = root.join(".swarm");
        fs::create_dir_all(&swarm).map_err(|e| e.to_string())?;
        // The environment is set on the child only; the test process's own environment is never changed.
        let output = Command::new("python3")
            .arg("-c")
            .arg(SEED_SCRIPT)
            .arg(swarm.join("tasks.db"))
            .current_dir(repo_root())
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("HOME", std::env::var_os("HOME").unwrap_or_default())
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("SWARM_GSD_STATE_FILE", "")
            .env("SWARM_DIR", &swarm)
            .output()
            .map_err(|e| format!("run python3: {e}"))?;
        if !output.status.success() {
            return Err(format!(
                "python seed failed: {}",
                String::from_utf8_lossy(&output.stderr)
            ));
        }
        Ok(Self { dir, root, swarm })
    }

    fn db(&self) -> PathBuf {
        self.swarm.join("tasks.db")
    }
}

/// The transition under test as the selector passes it: the argv of `orch_status --transition`, with `--json` first, as the route accepts it.
fn invocation(world: &World, effects: &Path) -> Invocation {
    Invocation {
        agent: "A01".to_string(),
        script: "orch_status".to_string(),
        argv: [
            "--json",
            "--transition",
            "T-1",
            "IN_REVIEW",
            "--reason",
            "deadline-after-commit",
        ]
        .iter()
        .map(ToString::to_string)
        .collect(),
        root: world.root.clone(),
        env: vec![(
            "SWARM_DIR".to_string(),
            world.swarm.display().to_string(),
        )],
        deadline: DEADLINE,
        effects_path: Some(effects.to_path_buf()),
    }
}

/// The row's state as Python's `TaskStore` reads it.
fn state_of(world: &World, task_id: &str) -> Result<String, String> {
    let output = Command::new("python3")
        .arg("-c")
        .arg("import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('SELECT state FROM tasks WHERE task_id=?',(sys.argv[2],)).fetchone()[0])")
        .arg(world.db())
        .arg(task_id)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .output()
        .map_err(|e| format!("run python3: {e}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).into_owned());
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// The reply as one line for a failure message: `ok`, or the error's code and message.
fn outcome_of(result: &Result<Reply, SeamError>) -> String {
    match result {
        Ok(_) => "ok".to_string(),
        Err(error) => format!("error {:?}: {}", error.code(), error.message()),
    }
}

#[test]
fn transition_manifest_survives_deadline_after_commit() -> Result<(), String> {
    let world = World::seeded()?;
    let effects = world.dir.path().join("effects.json");
    fs::write(&effects, "").map_err(|e| e.to_string())?;
    let call = invocation(&world, &effects);
    let engine = RustEngine::new().map_err(|e| format!("{e:?}"))?;

    // The route's store is the canonical path of the swarm directory; the pause is keyed by it, so only this call pauses.
    let db = fs::canonicalize(world.db()).map_err(|e| e.to_string())?;
    arm_post_commit_pause(db, HOLD);
    let result = engine.orch_status(&call);

    if state_of(&world, "T-1")? != "IN_REVIEW" {
        return Err(format!(
            "the transition did not commit within the deadline ({}); the window after COMMIT was not reached",
            outcome_of(&result)
        ));
    }
    // The commit landed. The reply either completed or says so with write_state "final"; a bare E-TIMEOUT is the defect.
    if let Err(error) = &result {
        let write_state = error
            .details()
            .iter()
            .find(|(key, _)| key == "write_state")
            .map(|(_, value)| value.clone());
        if write_state != Some(jstr("final")) {
            return Err(format!(
                "a deadline after the COMMIT must reply with write_state \"final\", got {write_state:?} ({}); a bare E-TIMEOUT is the defect",
                outcome_of(&result)
            ));
        }
    }
    // The effects manifest must exist for the caller to replay, whatever the reply says.
    let manifest = fs::read_to_string(&effects).map_err(|e| e.to_string())?;
    let value: serde_json::Value = serde_json::from_str(&manifest).map_err(|e| {
        format!("the effects manifest is not written or not JSON ({e}): {manifest:?}")
    })?;
    if value["effects"][0]["type"] != "task.transition" {
        return Err(format!("the effects manifest lacks the task.transition effect: {value}"));
    }
    Ok(())
}
