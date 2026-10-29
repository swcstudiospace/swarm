//! Signing keys never enter the guest, in the host code that builds the guest's requests (phase 14.5, unit U3.4e).
//!
//! What runs today:
//!
//! - the guest request of the `transition` mode, built by the REAL host path (`SandboxEngine::transition_via`, which calls
//!   `SandboxDecider`, which calls `encode_request` on the facts it read inside the write transaction), captured at the
//!   `WriteGuest` seam, has exactly the allowed keys at every level (top level, `request`, `facts`, the `task` row, and the
//!   `approval` facts the encoder can carry) with canary signing keys in the process environment and in the call's environment;
//! - no canary (raw, upper-case, hex, `0x`, space-separated hex, `\x..`, decimal list, Python bytes repr, base64 at every alignment
//!   and in both alphabets, UTF-16 in both byte orders, the decoded key bytes and a 16-digit prefix and suffix window of each key)
//!   reaches the request bytes, the reply, the effects manifest, the Task Store files (database, WAL, SHM) or the `Debug` text of a
//!   refusal;
//! - canary CONTENT in the key file `SWARM_ED25519_KEY_FILE` names and in `$HOME/.config/agent-swarm/signing-key.hex` is never in
//!   anything an APPROVED transition prints, sends to the guest or writes, and that transition is refused before any write;
//! - the non-test code of this crate names no linker for the guest (`HostBridge`, `instantiate_with`, `wasmtime_wasi`,
//!   `add_to_linker`): the guest is started on an empty linker only;
//! - the checks have teeth: an extra key at each level, a canary in each channel and every form of every canary are detected.
//!
//! What does not run yet, and why (each item is a `#[cfg(any())]` module below, ready to enable):
//!
//! - the key-reading path with canaries (an APPROVED transition that reads keys) waits for U3.4d, which makes `gsd-envelope` a
//!   dependency of this crate; nothing here claims to cover it, and the `KeyMaterial` trait check waits for the same reason (an
//!   integration test can only name the dependencies of its own package);
//! - the key sets of the read modes (`list`, `history`) wait for a public seam: the request is built by `read_snapshot` and
//!   `request_json` in `strangler_seam.rs`, `pub(crate)` and private, and `SandboxEngine::answer` hands it to the guest with no hook.
//!
//! The Task Store is built by the real Python `TaskStore` (the schema is Python's). Canaries are made at run time from the
//! standard library's randomly keyed hasher; they are never written to a file or printed, and a failed assertion names the variable
//! and the form that matched, never a value. The repository root is `SWARM_REPO_ROOT` or two levels above this crate. Temporary
//! files follow `TMPDIR`.

use std::collections::hash_map::RandomState;
use std::collections::HashMap;
use std::fs;
use std::hash::{BuildHasher, Hasher};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, PoisonError};
use std::time::Duration;

use gsd_adapter::sandbox_engine::{SandboxEngine, WriteGuest};
use gsd_adapter::strangler_seam::{error_reply, Invocation};
use gsd_failure::ErrorCode;
use gsd_route::transition_args::parse_transition_args;
use gsd_route::transition_plan::{ApprovalFacts, Facts, Request};
use gsd_route::transition_wire::{encode_request, transition_invoke};
use gsd_route::{obj_get, parse_json, Json};
use gsd_sandbox::wasmtime_limits::deadline_ticks_for;
use gsd_sandbox::Limits;
use tempfile::TempDir;

// ---- the allowlists (sorted) ---------------------------------------------------------------------------------------------

/// Top level of a `transition` request (`transition_wire::encode_request`, Design 4 of the plan).
const REQUEST_KEYS: [&str; 5] = ["facts", "mode", "now", "request", "task"];
/// `request`: what the caller asked.
const CALL_KEYS: [&str; 5] = ["actor", "manual", "reason", "task_id", "to"];
/// `facts`: what the host read for the decision.
const FACT_KEYS: [&str; 1] = ["approval"];
/// `facts.approval`: booleans derived from the key policy and gate names. No key, no envelope, no signature.
const APPROVAL_KEYS: [&str; 5] = [
    "dev_key",
    "missing_gates",
    "real_key",
    "require_key",
    "unaccepted_gates",
];
/// `task`: the columns of the `tasks` table (`swarm/taskstore.py` `SCHEMA`).
const TASK_KEYS: [&str; 22] = [
    "acceptance",
    "agent_id",
    "assigned_to",
    "attempt",
    "budget",
    "capability",
    "correlation_id",
    "created_at",
    "dag_depth",
    "depends_on",
    "inputs",
    "max_attempts",
    "notes",
    "outputs",
    "parent_id",
    "priority",
    "rework_loops",
    "risk_class",
    "state",
    "task_id",
    "title",
    "updated_at",
];

// ---- canaries -----------------------------------------------------------------------------------------------------------

const SECRET_VARS: [&str; 3] = [
    "SWARM_SIGNING_KEY",
    "SWARM_ED25519_KEY",
    "SWARM_ED25519_KEY_FILE",
];

/// `bytes` random bytes as lowercase hex, from the randomly keyed hasher of the standard library.
fn random_hex(bytes: usize) -> String {
    let mut out = String::new();
    while out.len() < bytes * 2 {
        let word = RandomState::new().build_hasher().finish();
        out.push_str(&format!("{word:016x}"));
    }
    out.truncate(bytes * 2);
    out
}

/// The labelled byte forms of one secret.
type Forms = Vec<(String, Vec<u8>)>;

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Base64 of `data` in the standard alphabet, without padding.
fn base64(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for chunk in data.chunks(3) {
        let word = chunk.iter().enumerate().fold(0u32, |word, (index, byte)| {
            word | u32::from(*byte) << (16 - 8 * index)
        });
        for position in 0..=chunk.len() {
            out.push(B64[(word >> (18 - 6 * position) & 63) as usize]);
        }
    }
    out
}

/// The base64 text `data` makes wherever it sits in a larger blob: one fragment per alignment and alphabet, cut where the characters
/// depend on the neighbouring bytes.
fn base64_fragments(data: &[u8]) -> Forms {
    let mut found = Vec::new();
    for offset in 0..3usize {
        let mut padded = vec![0u8; offset];
        padded.extend_from_slice(data);
        let encoded = base64(&padded);
        let head = [0, 2, 3][offset];
        let tail = [0, 1, 1][(offset + data.len()) % 3];
        let standard = encoded[head..encoded.len() - tail].to_vec();
        let url = standard
            .iter()
            .map(|byte| match byte {
                b'+' => b'-',
                b'/' => b'_',
                other => *other,
            })
            .collect();
        found.push((format!("b64.{offset}"), standard));
        found.push((format!("b64url.{offset}"), url));
    }
    found
}

/// The bytes the hex digits of `value` spell, or `None` for any other text or an odd number of digits.
fn hex_bytes(value: &str) -> Option<Vec<u8>> {
    let digits: Option<Vec<u8>> = value
        .chars()
        .map(|digit| digit.to_digit(16).map(|number| number as u8))
        .collect();
    let digits = digits?;
    (digits.len() % 2 == 0).then(|| {
        digits
            .chunks(2)
            .map(|pair| pair[0] << 4 | pair[1])
            .collect()
    })
}

/// The shapes a dump or list of `data` takes in code and logs, labelled `<prefix><shape>`.
fn byte_forms(prefix: &str, data: &[u8]) -> Forms {
    let listed = |separator: &str, show: fn(u8) -> String| -> Vec<u8> {
        data.iter()
            .map(|byte| show(*byte))
            .collect::<Vec<_>>()
            .join(separator)
            .into_bytes()
    };
    let hex: String = data.iter().map(|byte| format!("{byte:02x}")).collect();
    vec![
        (
            format!("{prefix}dec-list"),
            listed(", ", |byte| byte.to_string()),
        ),
        (
            format!("{prefix}dec-list-tight"),
            listed(",", |byte| byte.to_string()),
        ),
        (
            format!("{prefix}x-escapes"),
            listed("", |byte| format!("\\x{byte:02x}")),
        ),
        (format!("{prefix}0x-hex"), format!("0x{hex}").into_bytes()),
        (
            format!("{prefix}0x-list"),
            listed(", ", |byte| format!("0x{byte:02x}")),
        ),
        (
            format!("{prefix}hex-spaced"),
            listed(" ", |byte| format!("{byte:02x}")),
        ),
        (
            format!("{prefix}HEX-spaced"),
            listed(" ", |byte| format!("{byte:02X}")),
        ),
        (format!("{prefix}bytes-repr"), python_bytes_repr(data)),
    ]
}

/// The text between the quotes of Python's `repr(bytes)`: printable ASCII as is, backslash, `\t`, `\n`, `\r` and the quote the
/// repr picks escaped, every other byte as `\xNN`.
fn python_bytes_repr(data: &[u8]) -> Vec<u8> {
    let quote = if data.contains(&b'\'') && !data.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    let mut out = Vec::new();
    for &byte in data {
        match byte {
            b'\\' => out.extend_from_slice(b"\\\\"),
            b'\t' => out.extend_from_slice(b"\\t"),
            b'\n' => out.extend_from_slice(b"\\n"),
            b'\r' => out.extend_from_slice(b"\\r"),
            other if other == quote => out.extend_from_slice(&[b'\\', other]),
            0x20..=0x7e => out.push(byte),
            other => out.extend_from_slice(format!("\\x{other:02x}").as_bytes()),
        }
    }
    out
}

/// A secret of at least this many hex digits is a key, and its prefix and suffix windows are a key id; a shorter one is a marker.
const KEY_WINDOW_MIN_DIGITS: usize = 32;

/// Every byte form of the secret `value` a leak could take, with a label that is safe to print.
fn forms(value: &str) -> Forms {
    let raw = value.as_bytes();
    let hex = |bytes: &[u8]| -> String { bytes.iter().map(|byte| format!("{byte:02x}")).collect() };
    let mut found = vec![
        ("utf8".to_string(), raw.to_vec()),
        ("utf8-upper".to_string(), value.to_uppercase().into_bytes()),
        (
            "utf16le".to_string(),
            value.encode_utf16().flat_map(u16::to_le_bytes).collect(),
        ),
        (
            "utf16be".to_string(),
            value.encode_utf16().flat_map(u16::to_be_bytes).collect(),
        ),
        ("hex-of-text".to_string(), hex(raw).into_bytes()),
        (
            "HEX-of-text".to_string(),
            hex(raw).to_uppercase().into_bytes(),
        ),
    ];
    found.extend(base64_fragments(raw));
    found.extend(byte_forms("text-", raw));
    if let Some(key) = hex_bytes(value) {
        found.push(("key-bytes".to_string(), key.clone()));
        found.extend(
            base64_fragments(&key)
                .into_iter()
                .map(|(label, fragment)| (format!("key-{label}"), fragment)),
        );
        found.extend(byte_forms("key-", &key));
        if value.len() >= KEY_WINDOW_MIN_DIGITS {
            let (head, tail) = (&value[..16], &value[value.len() - 16..]);
            found.extend([
                ("key-prefix16".to_string(), head.as_bytes().to_vec()),
                ("key-suffix16".to_string(), tail.as_bytes().to_vec()),
                ("key-PREFIX16".to_string(), head.to_uppercase().into_bytes()),
                ("key-SUFFIX16".to_string(), tail.to_uppercase().into_bytes()),
                ("key-first8-bytes".to_string(), key[..8].to_vec()),
                ("key-last8-bytes".to_string(), key[key.len() - 8..].to_vec()),
            ]);
        }
    }
    found
}

/// Per-run canary values and the forms to search for. `Debug` is redacted: a failing assertion must not print a value.
struct Canaries {
    env: Vec<(String, String)>,
    needles: Vec<(String, Forms)>,
}

impl std::fmt::Debug for Canaries {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Canaries(<redacted>)")
    }
}

impl Canaries {
    /// Valid-shaped canaries: two different 64-hex keys and a key file path that carries a random marker.
    fn new(base: &Path) -> Self {
        let marker = random_hex(12);
        let signing = random_hex(32);
        let mut ed25519 = random_hex(32);
        while ed25519 == signing {
            ed25519 = random_hex(32);
        }
        let file = base.join(format!("keys-{marker}")).join("ed25519.pem");
        let values = [
            (SECRET_VARS[0], signing),
            (SECRET_VARS[1], ed25519),
            (SECRET_VARS[2], file.display().to_string()),
        ];
        let mut needles: Vec<(String, Forms)> = values
            .iter()
            .map(|(name, value)| ((*name).to_string(), forms(value)))
            .collect();
        needles.push(("KEY_FILE_MARKER".to_string(), forms(&marker)));
        let mut env: Vec<(String, String)> = values
            .into_iter()
            .map(|(name, value)| (name.to_string(), value))
            .collect();
        env.push(("SWARM_ALLOW_INSECURE_DEV_KEY".to_string(), "1".to_string()));
        env.push(("SWARM_REQUIRE_KEY".to_string(), "1".to_string()));
        Self { env, needles }
    }

    /// `VARIABLE/form` of every canary form in `data`. A value is never part of the answer.
    fn leaks(&self, data: &[u8]) -> Vec<String> {
        let mut found = Vec::new();
        for (name, table) in &self.needles {
            for (label, needle) in table {
                if !needle.is_empty() && data.windows(needle.len()).any(|window| window == needle) {
                    found.push(format!("{name}/{label}"));
                }
            }
        }
        found
    }

    /// Fails with the labels of the forms found in `data`, naming `channel`.
    fn require_clean(&self, data: &[u8], channel: &str) {
        let found = self.leaks(data);
        assert!(found.is_empty(), "canary in {channel}: {found:?}");
    }
}

/// Every file under `dir`, as bytes, with its name.
fn files_under(dir: &Path) -> Vec<(String, Vec<u8>)> {
    let mut found = Vec::new();
    let mut pending = vec![dir.to_path_buf()];
    while let Some(next) = pending.pop() {
        for entry in fs::read_dir(&next).expect("read dir").flatten() {
            let path = entry.path();
            if path.is_dir() {
                pending.push(path);
            } else if let Ok(bytes) = fs::read(&path) {
                found.push((entry.file_name().to_string_lossy().into_owned(), bytes));
            }
        }
    }
    found
}

// ---- the shape check ------------------------------------------------------------------------------------------------------

/// The sorted key names of the object `value`, or a marker for anything else.
fn keys(value: &Json) -> Vec<String> {
    match value {
        Json::Obj(fields) => {
            let mut names: Vec<String> = fields.iter().map(|(name, _)| name.clone()).collect();
            names.sort();
            names
        }
        _ => vec!["<not an object>".to_string()],
    }
}

fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
    match value {
        Json::Obj(fields) => obj_get(fields, key).unwrap_or(&Json::Null),
        _ => &Json::Null,
    }
}

/// How the key sets of a `transition` request differ from the allowlists: one line per level that differs. An absent `task` (an
/// unknown task) and an absent `approval` (no APPROVED target) are `null` and carry no keys.
fn shape_drift(request: &Json) -> Vec<String> {
    let task = member(request, "task");
    let facts = member(request, "facts");
    let approval = member(facts, "approval");
    let mut levels: Vec<(&str, &Json, &[&str])> = vec![
        ("request", request, &REQUEST_KEYS),
        ("request.request", member(request, "request"), &CALL_KEYS),
        ("request.facts", facts, &FACT_KEYS),
    ];
    if !matches!(task, Json::Null) {
        levels.push(("request.task", task, &TASK_KEYS));
    }
    if !matches!(approval, Json::Null) {
        levels.push(("request.facts.approval", approval, &APPROVAL_KEYS));
    }
    levels
        .into_iter()
        .filter_map(|(path, value, allowed)| {
            let found = keys(value);
            (found != allowed).then(|| format!("{path}: {found:?} differs from {allowed:?}"))
        })
        .collect()
}

// ---- a store and a guest that records ------------------------------------------------------------------------------------

/// The Python-built store: the schema is Python's, task T-1 (CREATED) is created by `TaskStore.create`, and an optional second
/// argument puts it into another state first (a test fixture, written straight to the scratch store).
const SEED_SCRIPT: &str = "import sys\nfrom swarm.taskstore import TaskStore\nstore = TaskStore(sys.argv[1])\nstore.create(task_id='T-1', correlation_id='C-1', capability='code.backend', title='canary task')\nif len(sys.argv) > 2:\n    store.conn.execute('UPDATE tasks SET state = ? WHERE task_id = ?', (sys.argv[2], 'T-1'))\n    store.conn.commit()\nstore.conn.close()\n";

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

/// A scratch repository: `<dir>/work/.swarm/tasks.db` with one task, and an empty effects file as the selector makes one.
struct World {
    dir: TempDir,
    root: PathBuf,
    swarm: PathBuf,
    effects: PathBuf,
}

impl World {
    /// A scratch repository whose task T-1 is CREATED, or in `state` when one is given.
    fn seeded(state: Option<&str>) -> Self {
        let dir = TempDir::new().expect("tempdir");
        let root = dir.path().join("work");
        let swarm = root.join(".swarm");
        fs::create_dir_all(&swarm).expect("swarm dir");
        let mut seed = std::process::Command::new("python3");
        seed.arg("-c")
            .arg(SEED_SCRIPT)
            .arg(swarm.join("tasks.db"))
            .args(state)
            .current_dir(repo_root())
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("SWARM_PURE_PYTHON", "1")
            .env_remove("SWARM_TASKSTORE_WAL");
        let output = seed.output().expect("run python3");
        assert!(
            output.status.success(),
            "python failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let effects = dir.path().join("effects.json");
        fs::write(&effects, "").expect("effects file");
        Self {
            dir,
            root,
            swarm,
            effects,
        }
    }

    /// The call the selector hands the binary for `argv` (`--json` first), with `extra` pairs in its environment.
    fn call(&self, argv: &[&str], extra: &[(String, String)]) -> Invocation {
        let mut full = vec!["--json".to_string()];
        full.extend(argv.iter().map(|item| (*item).to_string()));
        let mut env = vec![("SWARM_DIR".to_string(), self.swarm.display().to_string())];
        env.extend(extra.iter().cloned());
        Invocation {
            agent: "A01".to_string(),
            script: "orch_status".to_string(),
            argv: full,
            root: self.root.clone(),
            env,
            deadline: Duration::from_secs(60),
            effects_path: Some(self.effects.clone()),
        }
    }
}

/// The write guest of the engine, replaced by one that records every request it is handed and answers like the real guest function.
struct Recording {
    seen: Mutex<Vec<Vec<u8>>>,
    replies: Mutex<Vec<Vec<u8>>>,
}

impl WriteGuest for Recording {
    fn call(&self, request: &[u8], _limits: &Limits) -> Result<Vec<u8>, ErrorCode> {
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(request.to_vec());
        let reply = transition_invoke(request).map_err(|error| error.code())?;
        self.replies
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(reply.clone());
        Ok(reply)
    }
}

/// The production limits (OD-6); the recording guest ignores them, but the decider must pass them.
fn production(budget: Duration) -> Limits {
    Limits {
        fuel: 1_000_000_000,
        memory_bytes: 16 * 1024 * 1024,
        deadline_ticks: Some(deadline_ticks_for(budget)),
    }
}

/// Everything one transition leaves behind, with the canaries in the process environment and in the call's.
struct Run {
    requests: Vec<Vec<u8>>,
    replies: Vec<Vec<u8>>,
    stdout: String,
    refusal_text: String,
    world: World,
}

/// Serializes the runs that put key variables into the process environment: the variables are process-wide, and two tests with
/// different canaries must not see each other's.
static ENV_LOCK: Mutex<()> = Mutex::new(());

/// Every variable a key can come from: `SWARM_*` and the places the conventional key file is looked for.
const KEY_ENV: [&str; 8] = [
    "SWARM_SIGNING_KEY",
    "SWARM_ED25519_KEY",
    "SWARM_ED25519_KEY_FILE",
    "SWARM_ALLOW_INSECURE_DEV_KEY",
    "SWARM_REQUIRE_KEY",
    "SWARM_AGENT_SESSION",
    "XDG_CONFIG_HOME",
    "HOME",
];

/// Runs `orch_status --json --transition <task> <state>` in `world` on the sandbox write route with the recording guest. The
/// canaries are in the call's environment and in the process environment, which holds no other key variable for the run.
fn transition(world: World, task: &str, state: &str, canaries: &Canaries) -> Run {
    let _env = ENV_LOCK.lock().unwrap_or_else(PoisonError::into_inner);
    for name in KEY_ENV {
        std::env::remove_var(name);
    }
    for (name, value) in &canaries.env {
        std::env::set_var(name, value);
    }
    let call = world.call(
        &["--transition", task, state, "--reason", "canary"],
        &canaries.env,
    );
    let args = parse_transition_args(&call.argv).expect("a transition argv");
    let guest = Recording {
        seen: Mutex::new(Vec::new()),
        replies: Mutex::new(Vec::new()),
    };
    let engine = SandboxEngine::new().expect("sandbox engine");
    let result = engine.transition_via(&guest, production, &args, &call);
    let (stdout, refusal_text) = match &result {
        Ok(reply) => (reply.stdout.clone(), String::new()),
        Err(error) => (
            error_reply("A01", "orch_status", error).stdout,
            format!("{error:?}"),
        ),
    };
    Run {
        requests: guest
            .seen
            .into_inner()
            .unwrap_or_else(PoisonError::into_inner),
        replies: guest
            .replies
            .into_inner()
            .unwrap_or_else(PoisonError::into_inner),
        stdout,
        refusal_text,
        world,
    }
}

// ---- the tests ------------------------------------------------------------------------------------------------------------

/// The one test that sets variables in the process environment: it covers both the legal transition and the unknown task, so no
/// other test of this binary reads the environment while it runs.
#[test]
fn no_canary_reaches_the_guest_request_the_reply_the_manifest_the_store_or_a_refusal() {
    let probe = TempDir::new().expect("tempdir");
    let canaries = Canaries::new(probe.path());

    let legal = transition(World::seeded(None), "T-1", "VALIDATED", &canaries);
    assert_eq!(legal.requests.len(), 1, "the guest is asked once");
    assert_eq!(legal.replies.len(), 1);
    let request =
        parse_json(std::str::from_utf8(&legal.requests[0]).expect("UTF-8")).expect("JSON");
    assert_eq!(shape_drift(&request), Vec::<String>::new());
    assert!(
        matches!(member(&request, "task"), Json::Obj(_)),
        "the task row is in the request: the key sets above were checked on a real row"
    );
    assert_eq!(
        member(&request, "mode"),
        &Json::Str("transition".to_string())
    );
    canaries.require_clean(&legal.requests[0], "the guest request");
    canaries.require_clean(&legal.replies[0], "the guest reply");
    canaries.require_clean(legal.stdout.as_bytes(), "the reply on stdout");
    assert!(
        legal.stdout.contains("\"ok\""),
        "the transition committed, so the scan below saw the written store"
    );
    for (name, bytes) in files_under(legal.world.dir.path()) {
        canaries.require_clean(&bytes, &format!("the file {name}"));
    }

    // The refusal path: an unknown task is a typed error. Its reply and its Debug text carry no value either.
    let missing = transition(World::seeded(None), "T-404", "VALIDATED", &canaries);
    canaries.require_clean(missing.stdout.as_bytes(), "the refusal on stdout");
    canaries.require_clean(
        missing.refusal_text.as_bytes(),
        "the Debug text of a refusal",
    );
    assert!(
        missing.stdout.contains("T-404"),
        "the refusal answered for the task asked: the scan above saw a real refusal"
    );
    for request in &missing.requests {
        canaries.require_clean(request, "the guest request of a refusal");
        let parsed = parse_json(std::str::from_utf8(request).expect("UTF-8")).expect("JSON");
        assert_eq!(shape_drift(&parsed), Vec::<String>::new());
    }
    for (name, bytes) in files_under(missing.world.dir.path()) {
        canaries.require_clean(&bytes, &format!("the file {name}"));
    }
}

/// `encode_request`, which the decider calls, can carry approval facts. Whatever the host puts there, its wire has five keys,
/// booleans and gate names, and nothing a key could ride in.
#[test]
fn an_approval_fact_travels_as_five_named_fields_of_booleans_and_gate_names() {
    let probe = TempDir::new().expect("tempdir");
    let canaries = Canaries::new(probe.path());
    let facts = Facts {
        task: None,
        approval: Some(ApprovalFacts {
            require_key: true,
            real_key: true,
            dev_key: false,
            missing_gates: vec!["review".to_string()],
            unaccepted_gates: vec!["security".to_string()],
        }),
    };
    let request = Request {
        task_id: "T-1".to_string(),
        to: "APPROVED".to_string(),
        actor: "A01".to_string(),
        reason: "ok".to_string(),
        manual: true,
    };
    let wire = encode_request(&facts, &request, 1.5);
    let parsed = parse_json(&wire).expect("JSON");
    assert_eq!(shape_drift(&parsed), Vec::<String>::new());
    let approval = member(member(&parsed, "facts"), "approval");
    for flag in ["require_key", "real_key", "dev_key"] {
        assert!(
            matches!(member(approval, flag), Json::Bool(_)),
            "{flag} is a boolean"
        );
    }
    for gates in ["missing_gates", "unaccepted_gates"] {
        let Json::Arr(items) = member(approval, gates) else {
            panic!("{gates} is a list");
        };
        assert!(items.iter().all(|item| matches!(item, Json::Str(_))));
    }
    canaries.require_clean(wire.as_bytes(), "the encoded request");
}

// ---- teeth ----------------------------------------------------------------------------------------------------------------

/// A request as the host builds it for a task that exists, for the mutation tests below.
fn sample_request() -> Json {
    let task = TASK_KEYS
        .iter()
        .map(|name| ((*name).to_string(), Json::Null))
        .collect();
    let facts = Facts {
        task: Some(task),
        approval: Some(ApprovalFacts {
            require_key: false,
            real_key: true,
            dev_key: false,
            missing_gates: Vec::new(),
            unaccepted_gates: Vec::new(),
        }),
    };
    let request = Request {
        task_id: "T-1".to_string(),
        to: "VALIDATED".to_string(),
        actor: "A01".to_string(),
        reason: "r".to_string(),
        manual: true,
    };
    parse_json(&encode_request(&facts, &request, 1.5)).expect("JSON")
}

/// `request` with `key` added to the object found by walking `path`.
fn with_extra_key(request: &Json, path: &[&str], key: &str) -> Json {
    match (request, path.split_first()) {
        (Json::Obj(fields), None) => {
            let mut fields = fields.clone();
            fields.push((key.to_string(), Json::Str("x".to_string())));
            Json::Obj(fields)
        }
        (Json::Obj(fields), Some((head, rest))) => Json::Obj(
            fields
                .iter()
                .map(|(name, value)| {
                    let value = if name == head {
                        with_extra_key(value, rest, key)
                    } else {
                        value.clone()
                    };
                    (name.clone(), value)
                })
                .collect(),
        ),
        _ => request.clone(),
    }
}

#[test]
fn the_shape_check_accepts_the_allowed_shape() {
    assert_eq!(shape_drift(&sample_request()), Vec::<String>::new());
}

#[test]
fn an_extra_key_at_any_level_fails_the_shape_check() {
    let request = sample_request();
    let levels: [(&[&str], &str); 5] = [
        (&[], "request"),
        (&["request"], "request.request"),
        (&["facts"], "request.facts"),
        (&["task"], "request.task"),
        (&["facts", "approval"], "request.facts.approval"),
    ];
    for (path, level) in levels {
        let mutated = with_extra_key(&request, path, "sig");
        let drift = shape_drift(&mutated);
        assert_eq!(drift.len(), 1, "{level}: {drift:?}");
        assert!(drift[0].starts_with(level), "{level}: {drift:?}");
    }
}

#[test]
fn a_missing_key_fails_the_shape_check_too() {
    let Json::Obj(mut fields) = sample_request() else {
        panic!("a request is an object");
    };
    fields.retain(|(name, _)| name != "now");
    let drift = shape_drift(&Json::Obj(fields));
    assert_eq!(drift.len(), 1);
    assert!(drift[0].starts_with("request:"), "{drift:?}");
}

#[test]
fn every_form_of_every_canary_is_found_and_clean_data_is_not_flagged() {
    let probe = TempDir::new().expect("tempdir");
    let canaries = Canaries::new(probe.path());
    for (name, table) in &canaries.needles {
        for (label, needle) in table {
            let mut data = b"\x00 before ".to_vec();
            data.extend_from_slice(needle);
            data.extend_from_slice(b" after \xff");
            let found = canaries.leaks(&data);
            assert!(
                found.contains(&format!("{name}/{label}")),
                "{name}/{label} not found"
            );
        }
    }
    assert!(canaries
        .leaks(br#"{"status": "ok", "key": "abc"}"#)
        .is_empty());
    let failure = std::panic::catch_unwind(|| {
        let leaked = Canaries::new(Path::new("/x"));
        let value = leaked.env[0].1.clone();
        leaked.require_clean(value.as_bytes(), "a channel");
    });
    let payload = failure.expect_err("a canary in a channel fails the check");
    let message = payload
        .downcast_ref::<String>()
        .cloned()
        .unwrap_or_default();
    assert!(message.contains("SWARM_SIGNING_KEY/utf8"), "{message}");
    assert!(message.contains("a channel"), "{message}");
}

#[test]
fn base64_fragments_match_wherever_the_secret_sits_in_a_larger_blob() {
    let secret: Vec<u8> = (0..32u8)
        .map(|index| index.wrapping_mul(37).wrapping_add(11))
        .collect();
    for prefix in 0..6usize {
        for suffix in 0..6usize {
            let mut blob = vec![0xA5u8; prefix];
            blob.extend_from_slice(&secret);
            blob.resize(blob.len() + suffix, 0x5A);
            let encoded = base64(&blob);
            let standard = base64_fragments(&secret)
                .into_iter()
                .filter(|(label, _)| label.starts_with("b64."))
                .any(|(_, fragment)| {
                    encoded
                        .windows(fragment.len())
                        .any(|window| window == fragment)
                });
            assert!(standard, "prefix {prefix}, suffix {suffix}");
        }
    }
    assert_eq!(base64(b"Man"), b"TWFu".to_vec());
    assert_eq!(base64(b"Ma"), b"TWE".to_vec());
    assert_eq!(base64(b"M"), b"TQ".to_vec());
}

/// A canary planted in the request the host sends is found by the same scan the real run uses.
#[test]
fn a_canary_leaked_into_a_request_is_found() {
    let probe = TempDir::new().expect("tempdir");
    let canaries = Canaries::new(probe.path());
    let leaked = canaries.env[1].1.clone();
    let facts = Facts {
        task: None,
        approval: None,
    };
    let request = Request {
        task_id: "T-1".to_string(),
        to: "VALIDATED".to_string(),
        actor: "A01".to_string(),
        reason: leaked,
        manual: true,
    };
    let wire = encode_request(&facts, &request, 1.5);
    // The raw form is found, and only forms of that one variable: the prefix and suffix windows and the bytes repr of a hex text
    // overlap the raw form on purpose, so the number of forms that match is not fixed.
    let found = canaries.leaks(wire.as_bytes());
    assert!(found.contains(&"SWARM_ED25519_KEY/utf8".to_string()));
    assert!(found
        .iter()
        .all(|hit| hit.starts_with("SWARM_ED25519_KEY/")));
}

// ---- the widened forms ----------------------------------------------------------------------------------------------------

#[test]
fn the_byte_forms_have_the_shapes_a_dump_takes() {
    let table: HashMap<String, Vec<u8>> = forms("0cff").into_iter().collect();
    assert_eq!(table["key-dec-list"], b"12, 255");
    assert_eq!(table["key-dec-list-tight"], b"12,255");
    assert_eq!(table["key-x-escapes"], br"\x0c\xff");
    assert_eq!(table["key-0x-hex"], b"0x0cff");
    assert_eq!(table["key-0x-list"], b"0x0c, 0xff");
    assert_eq!(table["key-hex-spaced"], b"0c ff");
    assert_eq!(table["key-HEX-spaced"], b"0C FF");
    assert_eq!(table["key-bytes-repr"], br"\x0c\xff");
    assert_eq!(table["text-dec-list"], b"48, 99, 102, 102");
    assert_eq!(table["utf16le"], b"0\x00c\x00f\x00f\x00");
    assert_eq!(table["utf16be"], b"\x000\x00c\x00f\x00f");
}

#[test]
fn the_bytes_repr_form_is_what_python_prints_between_the_quotes() {
    assert_eq!(python_bytes_repr(b"a'b\x00\xff\\\n"), br#"a'b\x00\xff\\\n"#);
    assert_eq!(python_bytes_repr(b"'\""), br#"\'""#);
    assert_eq!(python_bytes_repr(b"\t\r\x7f ~"), br"\t\r\x7f ~");
}

#[test]
fn a_key_has_prefix_and_suffix_windows_and_a_marker_or_path_has_none() {
    let key = random_hex(32);
    let table: HashMap<String, Vec<u8>> = forms(&key).into_iter().collect();
    assert_eq!(table["key-prefix16"], &key.as_bytes()[..16]);
    assert_eq!(table["key-suffix16"], &key.as_bytes()[48..]);
    assert_eq!(table["key-PREFIX16"], key[..16].to_uppercase().as_bytes());
    assert_eq!(table["key-SUFFIX16"], key[48..].to_uppercase().as_bytes());
    let bytes = hex_bytes(&key).expect("hex");
    assert_eq!(table["key-first8-bytes"], bytes[..8]);
    assert_eq!(table["key-last8-bytes"], bytes[24..]);
    let windowed = |label: &str| {
        label.contains("prefix") || label.contains("suffix") || label.contains("8-bytes")
    };
    assert!(!forms(&random_hex(12))
        .iter()
        .any(|(label, _)| windowed(label)));
    assert!(forms("/some/where/ed25519.pem")
        .iter()
        .all(|(label, _)| !label.starts_with("key-")));
}

// ---- the pinned linker ----------------------------------------------------------------------------------------------------

/// What a guest could be linked to besides the empty linker: the bridge of the host crate and WASI.
const LINKER_NAMES: [&str; 4] = [
    "HostBridge",
    "instantiate_with",
    "wasmtime_wasi",
    "add_to_linker",
];

/// `source` without its test code and its comment lines. An item guarded by `#[cfg(test)]` ends at its first line when that line
/// ends with `;`, otherwise at the next line that starts with `}` (rustfmt puts the closing brace of a module in column 0).
fn non_test_code(source: &str) -> String {
    let mut kept = String::new();
    let mut lines = source.lines();
    while let Some(line) = lines.next() {
        let trimmed = line.trim();
        if trimmed == "#[cfg(test)]" {
            if let Some(first) = lines.next() {
                if !first.trim_end().ends_with(';') {
                    for item in lines.by_ref() {
                        if item.starts_with('}') {
                            break;
                        }
                    }
                }
            }
        } else if !trimmed.starts_with("//") {
            kept.push_str(line);
            kept.push('\n');
        }
    }
    kept
}

#[test]
fn the_non_test_code_of_this_crate_names_no_linker_for_the_guest() {
    let sources = files_under(&repo_root().join("crates/gsd-adapter/src"));
    let rust: Vec<(&String, String)> = sources
        .iter()
        .filter(|(name, _)| name.ends_with(".rs"))
        .map(|(name, bytes)| (name, non_test_code(&String::from_utf8_lossy(bytes))))
        .collect();
    let mut found = Vec::new();
    for (name, code) in &rust {
        for marker in [
            "#[test]",
            "#[cfg(test)]",
            "mod tests",
            "mod read_route_faults",
        ] {
            assert!(
                !code.contains(marker),
                "{name}: test code is still there ({marker})"
            );
        }
        found.extend(
            LINKER_NAMES
                .into_iter()
                .filter(|hit| code.contains(hit))
                .map(|hit| format!("{name}: {hit}")),
        );
    }
    assert_eq!(found, Vec::<String>::new());
    // Not vacuous: the engine that starts the guest and the binary were read, and what was kept is their production code.
    let engine = rust.iter().find(|(name, _)| *name == "sandbox_engine.rs");
    assert!(engine.is_some_and(|(_, code)| code.contains("pub struct SandboxEngine")));
    assert!(rust.iter().any(|(name, _)| *name == "gsd_engine.rs"));
    assert!(rust.len() >= 6, "{} sources", rust.len());
}

#[test]
fn the_linker_check_flags_each_name_in_code_and_ignores_tests_and_comments() {
    let flagged = |source: &str| -> Vec<&'static str> {
        let code = non_test_code(source);
        LINKER_NAMES
            .into_iter()
            .filter(|name| code.contains(name))
            .collect()
    };
    for name in LINKER_NAMES {
        let in_code = format!("use gsd_sandbox::{name};\nfn run() {{}}\n");
        assert_eq!(flagged(&in_code), vec![name], "{name} in code");
        let in_tests = format!(
            "fn run() {{}}\n\n#[cfg(test)]\nmod tests {{\n    use gsd_sandbox::{name};\n\n    #[test]\n    fn t() {{}}\n}}\n"
        );
        assert!(flagged(&in_tests).is_empty(), "{name} in a test module");
        let in_comment = format!("/// Never {name}.\n// {name}\nfn run() {{}}\n");
        assert!(flagged(&in_comment).is_empty(), "{name} in a comment");
        let after_tests = format!(
            "#[cfg(test)]\nmod tests {{\n    fn t() {{}}\n}}\n\nfn late() {{\n    {name}();\n}}\n"
        );
        assert_eq!(flagged(&after_tests), vec![name], "{name} after a module");
        let after_single =
            format!("#[cfg(test)]\nuse super::run;\nfn late() {{\n    {name}();\n}}\n");
        assert_eq!(flagged(&after_single), vec![name], "{name} after a use");
    }
}

// ---- key files ------------------------------------------------------------------------------------------------------------

/// The two places a key file is read from, planted with canary CONTENT: the file `SWARM_ED25519_KEY_FILE` names and the
/// conventional `$HOME/.config/agent-swarm/signing-key.hex`.
struct KeyFiles {
    content: String,
    named: PathBuf,
    conventional: PathBuf,
    home: PathBuf,
}

impl KeyFiles {
    fn plant(base: &Path) -> Self {
        let home = base.join("home");
        let files = Self {
            content: random_hex(32),
            named: base.join("keys").join("ed25519.pem"),
            conventional: home.join(".config/agent-swarm/signing-key.hex"),
            home,
        };
        for path in [&files.named, &files.conventional] {
            fs::create_dir_all(path.parent().expect("a parent")).expect("key directory");
            fs::write(path, format!("{}\n", files.content)).expect("key file");
        }
        files
    }

    /// The canaries of a run given the key file by `via`: `file-variable` sets only `SWARM_ED25519_KEY_FILE`, `conventional`
    /// sets no key variable at all and only a `HOME` that holds the conventional file. They scan for the CONTENT.
    fn canaries(&self, via: &str) -> Canaries {
        let env = if via == "file-variable" {
            vec![(SECRET_VARS[2].to_string(), self.named.display().to_string())]
        } else {
            vec![("HOME".to_string(), self.home.display().to_string())]
        };
        Canaries {
            env,
            needles: vec![("KEY_FILE_CONTENT".to_string(), forms(&self.content))],
        }
    }
}

/// An APPROVED transition was refused before any write with a typed E-DEP, nothing it printed, sent to the guest or wrote holds the
/// planted content, and the planted files are as they were.
fn check_refused_without_leak(run: &Run, files: &KeyFiles, canaries: &Canaries, via: &str) {
    let reply = parse_json(&run.stdout).expect("a JSON reply");
    let error = member(&reply, "error");
    assert_eq!(
        member(error, "code"),
        &Json::Str("E-DEP".to_string()),
        "{via}: the refusal is a typed E-DEP"
    );
    assert_eq!(
        member(member(error, "details"), "write_state"),
        &Json::Str("not_started".to_string()),
        "{via}: nothing was written"
    );
    canaries.require_clean(run.stdout.as_bytes(), &format!("stdout ({via})"));
    canaries.require_clean(
        run.refusal_text.as_bytes(),
        &format!("the Debug text of the refusal ({via})"),
    );
    for request in &run.requests {
        canaries.require_clean(request, &format!("a guest request ({via})"));
    }
    for (name, bytes) in files_under(run.world.dir.path()) {
        canaries.require_clean(&bytes, &format!("the file {name} ({via})"));
    }
    for path in [&files.named, &files.conventional] {
        assert_eq!(
            fs::read_to_string(path).expect("planted file"),
            format!("{}\n", files.content),
            "{via}: a planted key file changed"
        );
    }
}

#[test]
fn key_file_content_never_reaches_an_approval_that_is_refused() {
    let probe = TempDir::new().expect("tempdir");
    let files = KeyFiles::plant(probe.path());
    for via in ["file-variable", "conventional"] {
        let canaries = files.canaries(via);
        let run = transition(
            World::seeded(Some("IN_REVIEW")),
            "T-1",
            "APPROVED",
            &canaries,
        );
        check_refused_without_leak(&run, &files, &canaries, via);
    }
}

#[test]
fn the_key_file_check_flags_content_in_every_channel_and_an_untyped_or_changed_run() {
    let probe = TempDir::new().expect("tempdir");
    let files = KeyFiles::plant(probe.path());
    let canaries = files.canaries("file-variable");
    let mut run = transition(
        World::seeded(Some("IN_REVIEW")),
        "T-1",
        "APPROVED",
        &canaries,
    );
    let fails = |run: &Run| {
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            check_refused_without_leak(run, &files, &canaries, "scratch");
        }))
        .is_err()
    };
    let form = |label: &str| -> String {
        let found = canaries.needles[0].1.iter().find(|(name, _)| name == label);
        String::from_utf8(found.expect("a form").1.clone()).expect("text form")
    };
    assert!(!fails(&run), "the real refusal is clean");

    let stdout = run.stdout.clone();
    run.stdout = format!("{stdout} {}", form("utf8"));
    assert!(fails(&run), "content in stdout");
    run.stdout = stdout.clone();
    run.refusal_text = form("key-dec-list");
    assert!(fails(&run), "content in the Debug text");
    run.refusal_text = String::new();
    run.requests.push(form("key-hex-spaced").into_bytes());
    assert!(fails(&run), "content in a guest request");
    run.requests.pop();
    let leaked = run.world.dir.path().join("leak.bin");
    fs::write(&leaked, form("utf16be")).expect("leak file");
    assert!(fails(&run), "content in a file");
    fs::remove_file(&leaked).expect("remove");
    run.stdout = r#"{"status": "ok"}"#.to_string();
    assert!(fails(&run), "an answer that is not a typed refusal");
    run.stdout = stdout;
    assert!(!fails(&run), "restored");
    fs::write(&files.named, "changed").expect("change a planted file");
    assert!(fails(&run), "a planted key file that changed");
}

// ---- waiting for U3.4d ----------------------------------------------------------------------------------------------------

/// Enabling condition: `gsd-envelope = { path = "../gsd-envelope" }` in the `[dependencies]` of `gsd-adapter` (U3.4d wires the host
/// route to the envelope crate). Remove the `#[cfg(any())]` then. The check is the standard stable trick: the method named by
/// `<KeyMaterial as AmbiguousIfImpl<_>>::some_item` is ambiguous, so the line does not compile, exactly when `KeyMaterial` has the
/// trait. A `KeyMaterial` that is `Clone` would let a caller copy a secret out of the process; one that is `Serialize` would let the
/// host put it into a request.
#[cfg(any())]
mod key_material_is_not_clone_or_serialize {
    use gsd_envelope::KeyMaterial;

    trait AmbiguousIfClone<A> {
        fn some_item() {}
    }
    impl<T: ?Sized> AmbiguousIfClone<()> for T {}
    impl<T: ?Sized + Clone> AmbiguousIfClone<u8> for T {}

    trait AmbiguousIfSerialize<A> {
        fn some_item() {}
    }
    impl<T: ?Sized> AmbiguousIfSerialize<()> for T {}
    impl<T: ?Sized + serde::Serialize> AmbiguousIfSerialize<u8> for T {}

    trait AmbiguousIfDeserialize<A> {
        fn some_item() {}
    }
    impl<T: ?Sized> AmbiguousIfDeserialize<()> for T {}
    impl<T: ?Sized + for<'de> serde::Deserialize<'de>> AmbiguousIfDeserialize<u8> for T {}

    const _: fn() = || {
        let _ = <KeyMaterial as AmbiguousIfClone<_>>::some_item;
        let _ = <KeyMaterial as AmbiguousIfSerialize<_>>::some_item;
        let _ = <KeyMaterial as AmbiguousIfDeserialize<_>>::some_item;
    };
}

/// Enabling condition: a public seam for the read request. `read_snapshot` and `read_args` in `strangler_seam.rs` are `pub(crate)`
/// and `request_json` is private; once `read_snapshot` is `pub` (or `SandboxEngine::answer` takes a recording guest the way
/// `transition_via` does), remove the `#[cfg(any())]` and replace `snapshot_for` by the seam. The allowlists are the key sets of
/// `request_json` and of the rows it embeds (`SELECT *` of `tasks`, `transitions` and `verdicts`).
#[cfg(any())]
mod read_requests_have_exactly_the_allowed_keys {
    use super::*;

    const READ_KEYS: [&str; 10] = [
        "agent",
        "correlation",
        "db_path",
        "events",
        "history",
        "mode",
        "script",
        "swarm_dir",
        "tasks",
        "verdicts",
    ];
    const HISTORY_KEYS: [&str; 3] = ["task", "task_id", "transitions"];
    const VERDICT_ENTRY_KEYS: [&str; 2] = ["rows", "task_id"];
    /// `verdicts` columns: `envelope_json` and `sig` are the signed envelope and its signature (public), never a key.
    const VERDICT_ROW_KEYS: [&str; 10] = [
        "agent_id",
        "envelope_json",
        "expires_at",
        "findings",
        "gate",
        "id",
        "sig",
        "task_id",
        "ts",
        "verdict",
    ];
    const TRANSITION_ROW_KEYS: [&str; 7] = [
        "actor",
        "from_state",
        "id",
        "reason",
        "task_id",
        "to_state",
        "ts",
    ];

    fn snapshot_for(world: &World, argv: &[&str], canaries: &Canaries) -> Json {
        let call = world.call(argv, &canaries.env);
        let args = gsd_adapter::strangler_seam::read_args(&call).expect("read args");
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime")
            .block_on(gsd_adapter::strangler_seam::read_snapshot(&args, &call))
            .expect("snapshot")
    }

    #[test]
    fn list_and_history_requests_carry_only_allowed_keys() {
        let probe = TempDir::new().expect("tempdir");
        let canaries = Canaries::new(probe.path());
        let world = World::seeded(None);
        for (argv, mode) in [(vec![], "list"), (vec!["--history", "T-1"], "history")] {
            let request = snapshot_for(&world, &argv, &canaries);
            assert_eq!(keys(&request), READ_KEYS, "{mode}");
            let Json::Arr(tasks) = member(&request, "tasks") else {
                panic!("tasks is a list");
            };
            for task in tasks {
                assert_eq!(keys(task), TASK_KEYS, "{mode} task row");
            }
            let Json::Arr(verdicts) = member(&request, "verdicts") else {
                panic!("verdicts is a list");
            };
            for entry in verdicts {
                assert_eq!(keys(entry), VERDICT_ENTRY_KEYS, "{mode} verdict entry");
                let Json::Arr(rows) = member(entry, "rows") else {
                    panic!("rows is a list");
                };
                for row in rows {
                    assert_eq!(keys(row), VERDICT_ROW_KEYS, "{mode} verdict row");
                }
            }
            if mode == "history" {
                assert_eq!(keys(member(&request, "history")), HISTORY_KEYS);
                let Json::Arr(rows) = member(member(&request, "history"), "transitions") else {
                    panic!("transitions is a list");
                };
                for row in rows {
                    assert_eq!(keys(row), TRANSITION_ROW_KEYS);
                }
            }
            canaries.require_clean(
                gsd_route::to_python_json(&request).as_bytes(),
                "a read request",
            );
        }
    }
}
