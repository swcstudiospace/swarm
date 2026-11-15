//! Phase 9 packet R, the adapter side (C9): `invoke_routed` with the real engines on the `orch_status` read route.
//!
//! Each argv of [`ARGVS`] is answered twice on one scratch store. The store is restored byte-for-byte from a Python-built
//! template before each leg, so both legs see identical bytes at identical paths. The Python leg is `invoke_routed` with
//! `GSD_ENGINE=python` and the real `PythonEngine` (`scripts/orch_status.py`); the Rust leg is `invoke_routed` with `rust` and the
//! real `RustEngine`. The exit code and the stdout bytes must be equal, with no mask. The store is either empty or seeded with two
//! CREATED tasks, both built by the real Python `TaskStore`, whose schema the Rust reader uses.
//!
//! The Rust leg is handed a `PythonEngine` whose interpreter cannot start. A routing that reached Python would then fail rather
//! than pass, so the Rust answer is the Rust engine's own.
//!
//! The repository root is `SWARM_REPO_ROOT` or two levels above this crate. Temporary files follow `TMPDIR`.

use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use gsd_adapter::route_table::invoke_routed;
use gsd_adapter::strangler_seam::{Invocation, PythonEngine, RustEngine};
use tempfile::TempDir;

type TestResult = Result<(), String>;

const DEADLINE: Duration = Duration::from_secs(60);

/// The read-route argv of the comparison: the list, a known task, and an unknown task. Each ends in `--json`, as the
/// selector sends it; the bridge appends `--json` only when it is missing.
const ARGVS: &[&[&str]] = &[
    &["--json"],
    &["--history", "T-1", "--json"],
    &["--history", "T-404", "--json"],
];

/// The Python-built empty store: the schema and the migrations are Python's.
const EMPTY_SCRIPT: &str =
    "import sys\nfrom swarm.taskstore import TaskStore\nTaskStore(sys.argv[1]).conn.close()\n";

/// The Python-built seeded store: two CREATED tasks made through `TaskStore.create`.
const SEEDED_SCRIPT: &str = "import sys\nfrom swarm.taskstore import TaskStore\nstore = TaskStore(sys.argv[1])\nfor task_id in ('T-1', 'T-2'):\n    store.create(task_id=task_id, correlation_id='C-1', capability='code.backend', title=task_id)\nstore.conn.close()\n";

fn repo_root() -> Result<PathBuf, String> {
    let root = match std::env::var_os("SWARM_REPO_ROOT") {
        Some(value) => PathBuf::from(value),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    };
    root.canonicalize()
        .map_err(|e| format!("repository root: {e}"))
}

/// Builds `pristine/tasks.db` with `script`, run by Python from the repository root.
fn build_store(repo: &Path, script: &str, pristine: &Path, claude: &Path) -> TestResult {
    let output = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(pristine.join("tasks.db"))
        .current_dir(repo)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("SWARM_PURE_PYTHON", "1")
        .env("SWARM_GSD_STATE_FILE", "")
        .env("SWARM_CLAUDE_STATE_DIR", claude)
        .env_remove("SWARM_TASKSTORE_WAL")
        .output()
        .map_err(|e| format!("run python3: {e}"))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "python failed: {}",
            String::from_utf8_lossy(&output.stderr)
        ))
    }
}

fn copy_tree(from: &Path, to: &Path) -> TestResult {
    fs::create_dir_all(to).map_err(|e| e.to_string())?;
    for entry in fs::read_dir(from).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let target = to.join(entry.file_name());
        if entry.file_type().map_err(|e| e.to_string())?.is_dir() {
            copy_tree(&entry.path(), &target)?;
        } else {
            fs::copy(entry.path(), &target).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// One scratch store. Every leg runs on `swarm`, which is restored from `pristine` before it starts.
struct Scratch {
    _dir: TempDir,
    repo: PathBuf,
    pristine: PathBuf,
    swarm: PathBuf,
    claude: PathBuf,
}

impl Scratch {
    fn new(script: &str) -> Result<Self, String> {
        let dir = TempDir::new().map_err(|e| e.to_string())?;
        let repo = repo_root()?;
        let pristine = dir.path().join("pristine");
        let swarm = dir.path().join("work").join(".swarm");
        let claude = dir.path().join("claude");
        fs::create_dir_all(&pristine).map_err(|e| e.to_string())?;
        build_store(&repo, script, &pristine, &claude)?;
        Ok(Self {
            _dir: dir,
            repo,
            pristine,
            swarm,
            claude,
        })
    }

    /// Puts the store back to the Python-built bytes.
    fn restore(&self) -> TestResult {
        match fs::remove_dir_all(&self.swarm) {
            Ok(()) => {}
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(error) => return Err(error.to_string()),
        }
        copy_tree(&self.pristine, &self.swarm)
    }

    fn call(&self, argv: &[&str]) -> Invocation {
        Invocation {
            agent: "A01".to_string(),
            script: "orch_status".to_string(),
            argv: argv.iter().map(|s| s.to_string()).collect(),
            root: self.repo.clone(),
            // The overlay pins the Python child to Python and keeps its state out of the checkout. The Rust engine
            // reads only SWARM_DIR, the task and correlation ids and the argv.
            env: vec![
                ("SWARM_DIR".to_string(), self.swarm.display().to_string()),
                ("GSD_ENGINE".to_string(), "python".to_string()),
                ("SWARM_PURE_PYTHON".to_string(), "1".to_string()),
                ("SWARM_GSD_STATE_FILE".to_string(), String::new()),
                (
                    "SWARM_CLAUDE_STATE_DIR".to_string(),
                    self.claude.display().to_string(),
                ),
            ],
            deadline: DEADLINE,
            effects_path: None,
        }
    }
}

struct Engines {
    python: PythonEngine,
    unreachable: PythonEngine,
    rust: RustEngine,
}

impl Engines {
    fn new() -> Result<Self, String> {
        Ok(Self {
            python: PythonEngine::new("python3"),
            unreachable: PythonEngine::new("/nonexistent-agent-swarm-python3"),
            rust: RustEngine::new().map_err(|e| e.message().to_string())?,
        })
    }
}

/// Answers every argv of [`ARGVS`] through both selections on `store` and returns the Python exit code of each argv, in order.
/// Any difference in exit code or stdout bytes is an error that names the argv and both replies.
fn compare(store: &Scratch, engines: &Engines) -> Result<Vec<i32>, String> {
    let mut codes = Vec::new();
    for argv in ARGVS.iter().copied() {
        let call = store.call(argv);
        store.restore()?;
        let python = invoke_routed(Some("python"), &engines.python, &engines.rust, &call);
        store.restore()?;
        let rust = invoke_routed(Some("rust"), &engines.unreachable, &engines.rust, &call);
        if python.exit_code != rust.exit_code || python.stdout.as_bytes() != rust.stdout.as_bytes()
        {
            return Err(format!(
                "{argv:?}: python exit {} stdout {:?}; rust exit {} stdout {:?}",
                python.exit_code, python.stdout, rust.exit_code, rust.stdout
            ));
        }
        serde_json::from_str::<serde_json::Value>(&python.stdout)
            .map_err(|e| format!("{argv:?}: stdout is not one JSON object: {e}"))?;
        codes.push(python.exit_code);
    }
    Ok(codes)
}

#[test]
fn read_route_matches_python_bytes_on_an_empty_store() -> TestResult {
    let store = Scratch::new(EMPTY_SCRIPT)?;
    let codes = compare(&store, &Engines::new()?)?;
    if codes[0] != 0 {
        return Err(format!(
            "the list on an empty store exits {}, not 0",
            codes[0]
        ));
    }
    Ok(())
}

#[test]
fn read_route_matches_python_bytes_on_a_seeded_store() -> TestResult {
    let store = Scratch::new(SEEDED_SCRIPT)?;
    let codes = compare(&store, &Engines::new()?)?;
    if codes[0] != 0 {
        return Err(format!(
            "the list on a seeded store exits {}, not 0",
            codes[0]
        ));
    }
    if codes[1] != 0 {
        return Err(format!(
            "the history of seeded task T-1 exits {}, not 0",
            codes[1]
        ));
    }
    Ok(())
}
