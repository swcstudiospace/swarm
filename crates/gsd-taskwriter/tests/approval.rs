//! A transition to APPROVED in the writer (phase 14.5, unit U3.4d): the host reads the key policy and the signed verdict rows inside
//! its own transaction and the pure decision does the rest.
//!
//! Stores are built by the REAL Python `TaskStore`, and every verdict row is signed by the REAL `swarm.gates.make_verdict` under keys made
//! when the test runs (`RandomState`, never a file, never printed): Python signs, this crate verifies. What is proved:
//!
//! - each key source (Ed25519 only, HMAC only, both, the dev-key opt-in) approves what Python approves, and the facts the decider is
//!   handed are real (`approval` non-null, the key policy booleans, the missing and unaccepted gate names);
//! - the refusals carry Python's `E-POLICY` text (a failing, missing or unaccepted gate, no key, `SWARM_REQUIRE_KEY=1` without a key) and
//!   leave the store byte-identical;
//! - a key-file reference, an unreadable shape and a verdict row whose envelope Python reads but this crate cannot represent refuse BEFORE
//!   any write as not started, and a call to any other target never asks for a key;
//! - the keys are read once per call and BEFORE the transaction; the verdict rows are read INSIDE it: a row committed by another connection
//!   while this writer waits for the lock is seen;
//! - no key, signature or envelope is in any error text, `Debug` output or fact.

use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{mpsc, Arc, Mutex, PoisonError};
use std::time::Duration;

use gsd_envelope::Declined;
use gsd_route::transition_plan::{Facts, Request, TransitionError, TransitionPlan};
use gsd_route::{obj_get, Json};
use gsd_taskwriter::{
    open_for_write, Applied, ApplyError, ApprovalKeys, ApprovalSource, Decider, EnvApprovalSource,
    FixedApprovalSource, HostDecider, TaskStoreWriter, WriteState, WriterConfig,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Connection, Row, SqliteConnection};

/// Builds the store at `argv[2]` from the JSON scenario in `argv[1]` and prints `{"late": <a verdict row, not inserted>}`. Run from the
/// repository root with the SIGNING keys in its environment. `scratch` takes the run-log events of the signing (a dev-key audit).
const SEED: &str = r#"
import json, sys, time
from swarm.gates import make_verdict
from swarm.taskstore import TaskStore

spec = json.loads(sys.argv[1])
now = time.time()
store = TaskStore(sys.argv[2])
store.create(task_id='T-1', correlation_id='C-1', capability='code.backend', risk_class=spec.get('risk', 'medium'), notes=spec.get('notes') or {})
store.conn.execute("UPDATE tasks SET state='IN_REVIEW' WHERE task_id='T-1'")
if spec.get('issuer'):
    store.create(task_id='T-g', correlation_id='C-1', capability='qa.gate')
    store.conn.execute("UPDATE tasks SET state=? WHERE task_id='T-g'", (spec['issuer'],))
store.conn.commit()

def sign(gate, verdict, extra=None):
    extra = dict(extra or {})
    extra['issued_at'] = now
    return make_verdict(gate=gate, task_id='T-1', agent_id='A09', verdict=verdict, correlation_id='C-1', extra=extra,
                        root=spec['scratch'], audit=False)

issued = {'gate_task': 'T-g'} if spec.get('issuer') else None
for gate, verdict in spec['verdicts']:
    store.record_verdict('T-1', sign(gate, verdict, issued))
late = None
if spec.get('late'):
    gate, verdict = spec['late']
    envelope = sign(gate, verdict)
    late = {'gate': gate, 'verdict': verdict, 'findings': '[]', 'expires_at': now + 86400, 'ts': now,
            'envelope_json': json.dumps(envelope), 'sig': envelope['sig']}
if spec.get('surrogate'):
    first = store.conn.execute('SELECT id, envelope_json FROM verdicts ORDER BY id LIMIT 1').fetchone()
    text = first['envelope_json'].replace('"findings": []', '"findings": ["\\ud800"]', 1)
    assert text != first['envelope_json']
    store.conn.execute('UPDATE verdicts SET envelope_json=? WHERE id=?', (text, first['id']))
if spec.get('odd_risk'):
    store.conn.execute("UPDATE tasks SET risk_class='odd' WHERE task_id='T-1'")
for statement in spec.get('sql') or []:
    store.conn.execute(statement)
store.conn.commit()
store.conn.close()
print(json.dumps({'late': late}))
"#;

const NOW: f64 = 1_700_000_000.0;

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

/// `bytes` random bytes as lowercase hex from the randomly keyed hasher of the standard library.
fn random_hex(bytes: usize) -> String {
    let mut out = String::new();
    while out.len() < bytes * 2 {
        let word = RandomState::new().build_hasher().finish();
        out.push_str(&format!("{word:016x}"));
    }
    out.truncate(bytes * 2);
    out
}

/// The keys of one test: two secrets that are never the same and never printed.
struct Secrets {
    hmac: String,
    ed25519: String,
}

impl Secrets {
    fn new() -> Self {
        let hmac = random_hex(32);
        let mut ed25519 = random_hex(32);
        while ed25519 == hmac {
            ed25519 = random_hex(32);
        }
        Self { hmac, ed25519 }
    }

    /// The environment of a key profile: `hmac`, `ed25519`, `both`, `dev` (the opt-in), `require`, `none`.
    fn profile(&self, name: &str) -> Vec<(String, String)> {
        let pair = |key: &str, value: &str| (key.to_string(), value.to_string());
        match name {
            "hmac" => vec![pair("SWARM_SIGNING_KEY", &self.hmac)],
            "ed25519" => vec![pair("SWARM_ED25519_KEY", &self.ed25519)],
            "both" => vec![
                pair("SWARM_SIGNING_KEY", &self.hmac),
                pair("SWARM_ED25519_KEY", &self.ed25519),
            ],
            "dev" => vec![pair("SWARM_ALLOW_INSECURE_DEV_KEY", "1")],
            "require" => vec![pair("SWARM_REQUIRE_KEY", "1")],
            "none" => Vec::new(),
            other => panic!("no key profile {other}"),
        }
    }

    /// Every form a leak of either secret could take in text: the raw text, upper case and the text as hex.
    fn leaks_in(&self, text: &str) -> Vec<&'static str> {
        let hex =
            |value: &str| -> String { value.bytes().map(|byte| format!("{byte:02x}")).collect() };
        let mut found = Vec::new();
        for (label, secret) in [("hmac", &self.hmac), ("ed25519", &self.ed25519)] {
            // The whole secret in three spellings, and an 8-character window at either end of it (a prefix or suffix is a leak too).
            let forms = [
                secret.clone(),
                secret.to_uppercase(),
                hex(secret),
                secret[..8].to_string(),
                secret[secret.len() - 8..].to_string(),
            ];
            if forms.iter().any(|form| text.contains(form.as_str())) {
                found.push(if label == "hmac" { "hmac" } else { "ed25519" });
            }
        }
        found
    }
}

/// A store built by Python, and the late row of its scenario.
struct Store {
    dir: tempfile::TempDir,
    db: PathBuf,
    late: Option<LateRow>,
}

struct LateRow {
    gate: String,
    verdict: String,
    findings: String,
    expires_at: f64,
    ts: f64,
    envelope_json: String,
    sig: String,
}

fn json_text(value: &Json, key: &str) -> String {
    match value {
        Json::Obj(fields) => match obj_get(fields, key) {
            Some(Json::Str(text)) => text.clone(),
            other => panic!("{key} is not text: {other:?}"),
        },
        other => panic!("not an object: {other:?}"),
    }
}

fn json_number(value: &Json, key: &str) -> f64 {
    match value {
        Json::Obj(fields) => match obj_get(fields, key) {
            Some(Json::Float(number)) => *number,
            Some(Json::Int(number)) => *number as f64,
            other => panic!("{key} is not a number: {other:?}"),
        },
        other => panic!("not an object: {other:?}"),
    }
}

/// Builds a store from `spec` (JSON) with the rows signed under `sign`.
fn store(spec: &str, sign: &[(String, String)]) -> Store {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("tasks.db");
    let scratch = dir.path().join("scratch");
    std::fs::create_dir(&scratch).expect("scratch");
    let spec = spec.replacen(
        '{',
        &format!("{{\"scratch\": \"{}\", ", scratch.display()),
        1,
    );
    let mut command = Command::new("python3");
    command
        .arg("-c")
        .arg(SEED)
        .arg(&spec)
        .arg(&db)
        .current_dir(repo_root())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", dir.path())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("SWARM_PURE_PYTHON", "1")
        .env("SWARM_DIR", &scratch)
        .env("SWARM_GSD_STATE_FILE", "")
        .env("SWARM_CLAUDE_STATE_DIR", dir.path().join("claude-state"));
    for (name, value) in sign {
        command.env(name, value);
    }
    let output = command.output().expect("run python3");
    assert!(
        output.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let printed =
        gsd_route::parse_json(String::from_utf8_lossy(&output.stdout).trim()).expect("seed output");
    let late = match &printed {
        Json::Obj(fields) => match obj_get(fields, "late") {
            Some(row @ Json::Obj(_)) => Some(LateRow {
                gate: json_text(row, "gate"),
                verdict: json_text(row, "verdict"),
                findings: json_text(row, "findings"),
                expires_at: json_number(row, "expires_at"),
                ts: json_number(row, "ts"),
                envelope_json: json_text(row, "envelope_json"),
                sig: json_text(row, "sig"),
            }),
            _ => None,
        },
        other => panic!("seed output is not an object: {other:?}"),
    };
    Store { dir, db, late }
}

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("raw connection")
}

/// The bytes of the store file and of the files beside it: what "byte-identical" compares.
fn store_bytes(path: &Path) -> Vec<(String, Vec<u8>)> {
    let mut found = Vec::new();
    let parent = path.parent().expect("a parent");
    for entry in std::fs::read_dir(parent).expect("read dir").flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with("tasks.db") {
            found.push((name, std::fs::read(entry.path()).expect("read")));
        }
    }
    found.sort();
    found
}

async fn state_of(path: &Path, task: &str) -> String {
    let mut conn = connect(path).await;
    let row = sqlx::query("SELECT state FROM tasks WHERE task_id=?")
        .bind(task)
        .fetch_one(&mut conn)
        .await
        .expect("state");
    let state: String = row.get(0);
    conn.close().await.expect("close");
    state
}

async fn audit_count(path: &Path) -> i64 {
    let mut conn = connect(path).await;
    let row = sqlx::query("SELECT count(*) FROM transitions WHERE task_id='T-1'")
        .fetch_one(&mut conn)
        .await
        .expect("count");
    let count: i64 = row.get(0);
    conn.close().await.expect("close");
    count
}

fn request(to: &str) -> Request {
    Request {
        task_id: "T-1".to_string(),
        to: to.to_string(),
        actor: "A01".to_string(),
        reason: "approved".to_string(),
        manual: true,
    }
}

async fn writer_with(path: &Path, source: Arc<dyn ApprovalSource>) -> TaskStoreWriter {
    TaskStoreWriter::with_approval(
        open_for_write(&WriterConfig::new(path))
            .await
            .expect("open_for_write"),
        source,
    )
}

/// The host decision with a record of the facts it was handed and how many times it was called.
#[derive(Default)]
struct Spy {
    facts: Mutex<Vec<Facts>>,
    calls: AtomicUsize,
}

impl Decider for Spy {
    fn decide(
        &self,
        facts: &Facts,
        request: &Request,
        now: f64,
    ) -> Result<TransitionPlan, TransitionError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.facts
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(facts.clone());
        HostDecider.decide(facts, request, now)
    }
}

impl Spy {
    fn approval(&self) -> Option<gsd_route::transition_plan::ApprovalFacts> {
        let seen = self.facts.lock().unwrap_or_else(PoisonError::into_inner);
        assert_eq!(seen.len(), 1, "the decider is called exactly once");
        seen[0].approval.clone()
    }
}

fn names(items: &[&str]) -> Vec<String> {
    items.iter().map(|name| (*name).to_string()).collect()
}

fn rendered(error: &ApplyError) -> String {
    format!("{error} | {error:?} | {}", error.message())
}

const PASS: &str = r#"{"verdicts": [["review", "pass"], ["quality", "pass"]]}"#;

// ---- the key sources -----------------------------------------------------------------------------------------------------

/// Python signs under `sign`, the writer verifies under `call`; `approves` says whether Python approves that pairing.
async fn run_pairing(sign: &str, call: &str, approves: bool) {
    let secrets = Secrets::new();
    let seeded = store(PASS, &secrets.profile(sign));
    let source = Arc::new(FixedApprovalSource::new(secrets.profile(call)));
    let writer = writer_with(&seeded.db, source).await;
    let spy = Spy::default();
    let outcome = writer.transition(&request("APPROVED"), NOW, &spy).await;
    writer.close().await;
    let facts = spy.approval().expect("the writer read the approval facts");
    let real = matches!(call, "hmac" | "ed25519" | "both");
    assert_eq!(
        (facts.require_key, facts.real_key, facts.dev_key),
        (false, real, call == "dev"),
        "{sign}/{call}: the key policy the writer read"
    );
    if approves {
        let applied = outcome.unwrap_or_else(|error| panic!("{sign}/{call}: {}", rendered(&error)));
        assert!(facts.missing_gates.is_empty() && facts.unaccepted_gates.is_empty());
        assert_eq!(
            obj_get(&applied.task, "state"),
            Some(&Json::Str("APPROVED".to_string()))
        );
        assert_eq!(state_of(&seeded.db, "T-1").await, "APPROVED");
        assert_eq!(audit_count(&seeded.db).await, 2);
    } else {
        let error = outcome.expect_err(&format!("{sign}/{call} must be refused"));
        assert_eq!(error.code(), gsd_codes::ErrorCode::Policy);
        assert!(
            error
                .message()
                .contains("lacks passing gates ['review', 'quality']"),
            "{sign}/{call}: {}",
            error.message()
        );
        assert_eq!(facts.missing_gates, names(&["review", "quality"]));
        assert_eq!(state_of(&seeded.db, "T-1").await, "IN_REVIEW");
    }
    let leaks = secrets.leaks_in(&format!("{facts:?}"));
    assert!(
        leaks.is_empty(),
        "{sign}/{call}: a secret is in the facts: {leaks:?}"
    );
}

#[tokio::test]
async fn an_hmac_key_approves_hmac_signed_rows() {
    run_pairing("hmac", "hmac", true).await;
}

#[tokio::test]
async fn an_ed25519_key_approves_ed25519_signed_rows() {
    run_pairing("ed25519", "ed25519", true).await;
}

#[tokio::test]
async fn both_keys_approve_rows_signed_by_either() {
    run_pairing("both", "both", true).await;
    run_pairing("hmac", "both", true).await;
    run_pairing("ed25519", "both", true).await;
}

#[tokio::test]
async fn the_dev_key_opt_in_approves_dev_signed_rows_and_only_those() {
    run_pairing("dev", "dev", true).await;
    run_pairing("hmac", "dev", false).await;
}

#[tokio::test]
async fn rows_signed_by_another_key_do_not_verify() {
    run_pairing("hmac", "ed25519", false).await;
    run_pairing("ed25519", "hmac", false).await;
}

// ---- the refusals ----------------------------------------------------------------------------------------------------------

async fn refused_with(
    spec: &str,
    sign: &str,
    call: &str,
    text: &str,
    facts_missing: &[&str],
    facts_unaccepted: &[&str],
) {
    let secrets = Secrets::new();
    let seeded = store(spec, &secrets.profile(sign));
    let before = store_bytes(&seeded.db);
    let writer = writer_with(
        &seeded.db,
        Arc::new(FixedApprovalSource::new(secrets.profile(call))),
    )
    .await;
    let spy = Spy::default();
    let error = writer
        .transition(&request("APPROVED"), NOW, &spy)
        .await
        .expect_err("the call is refused");
    writer.close().await;
    assert_eq!(
        error.code(),
        gsd_codes::ErrorCode::Policy,
        "{}",
        error.message()
    );
    assert_eq!(
        error.write_state(),
        WriteState::Final,
        "Python's own refusal"
    );
    assert!(
        error.message().contains(text),
        "{} lacks {text}",
        error.message()
    );
    let facts = spy.approval().expect("facts");
    assert_eq!(facts.missing_gates, names(facts_missing));
    assert_eq!(facts.unaccepted_gates, names(facts_unaccepted));
    assert_eq!(
        store_bytes(&seeded.db),
        before,
        "a refusal leaves the store byte-identical"
    );
    let leaks = secrets.leaks_in(&rendered(&error));
    assert!(leaks.is_empty(), "a secret is in the refusal: {leaks:?}");
}

#[tokio::test]
async fn a_failing_gate_is_a_policy_refusal() {
    refused_with(
        r#"{"verdicts": [["review", "pass"], ["quality", "fail"]]}"#,
        "hmac",
        "hmac",
        "lacks passing gates ['quality']",
        &["quality"],
        &[],
    )
    .await;
}

#[tokio::test]
async fn a_missing_gate_is_a_policy_refusal() {
    refused_with(
        r#"{"verdicts": [["review", "pass"]]}"#,
        "hmac",
        "hmac",
        "lacks passing gates ['quality']",
        &["quality"],
        &[],
    )
    .await;
}

#[tokio::test]
async fn a_gate_task_whose_result_was_never_accepted_is_an_unaccepted_gate() {
    let spec = r#"{"verdicts": [["review", "pass"], ["quality", "pass"]], "issuer": "FAILED"}"#;
    refused_with(
        spec,
        "hmac",
        "hmac",
        "has unaccepted gates ['quality', 'review']",
        &[],
        &["review", "quality"],
    )
    .await;
}

#[tokio::test]
async fn an_accepted_gate_task_approves() {
    let secrets = Secrets::new();
    let seeded = store(
        r#"{"verdicts": [["review", "pass"], ["quality", "pass"]], "issuer": "IN_REVIEW"}"#,
        &secrets.profile("hmac"),
    );
    let writer = writer_with(
        &seeded.db,
        Arc::new(FixedApprovalSource::new(secrets.profile("hmac"))),
    )
    .await;
    let applied = writer
        .transition(&request("APPROVED"), NOW, &HostDecider)
        .await
        .unwrap_or_else(|error| panic!("{}", rendered(&error)));
    writer.close().await;
    assert_eq!(
        obj_get(&applied.task, "state"),
        Some(&Json::Str("APPROVED".to_string()))
    );
}

#[tokio::test]
async fn no_key_is_a_policy_refusal_before_any_row_is_read() {
    let secrets = Secrets::new();
    let seeded = store(PASS, &secrets.profile("hmac"));
    let writer = writer_with(&seeded.db, Arc::new(FixedApprovalSource::new(Vec::new()))).await;
    let spy = Spy::default();
    let error = writer
        .transition(&request("APPROVED"), NOW, &spy)
        .await
        .expect_err("no key");
    writer.close().await;
    assert_eq!(error.message(), "fail-closed: no signing key configured");
    let facts = spy.approval().expect("facts");
    assert_eq!(
        (
            facts.require_key,
            facts.real_key,
            facts.dev_key,
            facts.missing_gates,
            facts.unaccepted_gates
        ),
        (false, false, false, Vec::new(), Vec::new()),
        "the key policy refuses first, so no row is read"
    );
}

#[tokio::test]
async fn require_key_without_a_real_key_is_a_policy_refusal_even_beside_the_dev_key() {
    let secrets = Secrets::new();
    let seeded = store(PASS, &secrets.profile("dev"));
    for vars in [
        vec![("SWARM_REQUIRE_KEY".to_string(), "1".to_string())],
        vec![
            ("SWARM_REQUIRE_KEY".to_string(), "1".to_string()),
            ("SWARM_ALLOW_INSECURE_DEV_KEY".to_string(), "1".to_string()),
        ],
    ] {
        let writer = writer_with(&seeded.db, Arc::new(FixedApprovalSource::new(vars))).await;
        let error = writer
            .transition(&request("APPROVED"), NOW, &HostDecider)
            .await
            .expect_err("require without a key");
        writer.close().await;
        assert_eq!(
            error.message(),
            "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured"
        );
    }
    assert_eq!(state_of(&seeded.db, "T-1").await, "IN_REVIEW");
}

#[tokio::test]
async fn an_unloadable_ed25519_seed_is_no_key_and_beside_an_hmac_key_the_hmac_key_serves() {
    let secrets = Secrets::new();
    let seeded = store(PASS, &secrets.profile("hmac"));
    let broken = vec![("SWARM_ED25519_KEY".to_string(), "zz".to_string())];
    let writer = writer_with(
        &seeded.db,
        Arc::new(FixedApprovalSource::new(broken.clone())),
    )
    .await;
    let error = writer
        .transition(&request("APPROVED"), NOW, &HostDecider)
        .await
        .expect_err("an unloadable seed alone");
    writer.close().await;
    assert_eq!(error.message(), "fail-closed: no signing key configured");

    let beside = [broken, secrets.profile("hmac")].concat();
    let writer = writer_with(&seeded.db, Arc::new(FixedApprovalSource::new(beside))).await;
    writer
        .transition(&request("APPROVED"), NOW, &HostDecider)
        .await
        .unwrap_or_else(|error| panic!("{}", rendered(&error)));
    writer.close().await;
}

// ---- before the write: what Python must answer ------------------------------------------------------------------------------

#[tokio::test]
async fn a_key_file_reference_refuses_before_the_transaction_and_before_the_decider() {
    let secrets = Secrets::new();
    let seeded = store(PASS, &secrets.profile("hmac"));
    let before = store_bytes(&seeded.db);
    let named = seeded.dir.path().join("key-file").join("ed25519.pem");
    let source = Arc::new(FixedApprovalSource::new(vec![(
        "SWARM_ED25519_KEY_FILE".to_string(),
        named.display().to_string(),
    )]));
    let writer = writer_with(&seeded.db, source).await;
    let spy = Spy::default();
    let error = writer
        .transition(&request("APPROVED"), NOW, &spy)
        .await
        .expect_err("a key-file reference");
    writer.close().await;
    assert_eq!(error.write_state(), WriteState::NotStarted);
    assert_eq!(error.code(), gsd_codes::ErrorCode::Dep);
    assert_eq!(
        spy.calls.load(Ordering::SeqCst),
        0,
        "the decider was never called"
    );
    assert_eq!(store_bytes(&seeded.db), before);
    assert!(
        !rendered(&error).contains(&*named.display().to_string()),
        "the path of the reference is not in the refusal either"
    );
}

#[tokio::test]
async fn a_verdict_row_this_crate_cannot_represent_declines_the_whole_call() {
    let secrets = Secrets::new();
    let seeded = store(
        r#"{"verdicts": [["review", "pass"], ["quality", "pass"]], "surrogate": true}"#,
        &secrets.profile("hmac"),
    );
    let before = store_bytes(&seeded.db);
    let writer = writer_with(
        &seeded.db,
        Arc::new(FixedApprovalSource::new(secrets.profile("hmac"))),
    )
    .await;
    let spy = Spy::default();
    let error = writer
        .transition(&request("APPROVED"), NOW, &spy)
        .await
        .expect_err("a lone surrogate in an envelope");
    writer.close().await;
    assert_eq!(
        error.write_state(),
        WriteState::NotStarted,
        "{}",
        rendered(&error)
    );
    assert_eq!(
        spy.calls.load(Ordering::SeqCst),
        0,
        "never `sig_ok = false`, never a decision"
    );
    assert_eq!(store_bytes(&seeded.db), before);
    // A key is configured here, so this is the one refusal of this test that could carry it.
    assert!(
        secrets.leaks_in(&rendered(&error)).is_empty(),
        "a secret is in the refusal of a declined row"
    );
}

#[tokio::test]
async fn a_risk_class_outside_the_table_is_left_to_python() {
    let secrets = Secrets::new();
    let seeded = store(
        r#"{"verdicts": [["review", "pass"], ["quality", "pass"]], "odd_risk": true}"#,
        &secrets.profile("hmac"),
    );
    let writer = writer_with(
        &seeded.db,
        Arc::new(FixedApprovalSource::new(secrets.profile("hmac"))),
    )
    .await;
    let error = writer
        .transition(&request("APPROVED"), NOW, &HostDecider)
        .await
        .expect_err("an unknown risk class");
    writer.close().await;
    assert_eq!(error.write_state(), WriteState::NotStarted);
}

#[tokio::test]
async fn a_writer_without_a_source_leaves_a_legal_approved_to_python() {
    let secrets = Secrets::new();
    let seeded = store(PASS, &secrets.profile("hmac"));
    let writer = TaskStoreWriter::new(
        open_for_write(&WriterConfig::new(&seeded.db))
            .await
            .expect("open"),
    );
    let error = writer
        .transition(&request("APPROVED"), NOW, &HostDecider)
        .await
        .expect_err("no source");
    writer.close().await;
    assert_eq!(error.write_state(), WriteState::NotStarted);
    assert_eq!(state_of(&seeded.db, "T-1").await, "IN_REVIEW");
}

// ---- keys are read once, only for APPROVED, and before the lock -------------------------------------------------------------

/// A source that counts its calls and refuses like a key-file reference does.
#[derive(Debug, Default)]
struct Refusing {
    calls: AtomicUsize,
}

impl ApprovalSource for Refusing {
    fn keys(&self) -> Result<ApprovalKeys, Declined> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        Err(Declined::KeyFileReference)
    }

    fn clock(&self, requested_now: f64) -> f64 {
        requested_now
    }
}

#[tokio::test]
async fn a_target_other_than_approved_never_asks_for_a_key() {
    let secrets = Secrets::new();
    let seeded = store(PASS, &secrets.profile("hmac"));
    let source = Arc::new(Refusing::default());
    let writer = writer_with(&seeded.db, source.clone()).await;
    let spy = Spy::default();
    writer
        .transition(&request("CHANGES_REQUESTED"), NOW, &spy)
        .await
        .unwrap_or_else(|error| panic!("{}", rendered(&error)));
    assert_eq!(
        source.calls.load(Ordering::SeqCst),
        0,
        "a rework reads no key"
    );
    assert!(spy.approval().is_none(), "and no approval facts");
    writer.close().await;
}

#[tokio::test]
async fn an_illegal_approved_asks_for_a_key_but_reads_no_row_and_python_text_stands() {
    let secrets = Secrets::new();
    let seeded = store(PASS, &secrets.profile("hmac"));
    let mut conn = connect(&seeded.db).await;
    sqlx::query("UPDATE tasks SET state='CREATED' WHERE task_id='T-1'")
        .execute(&mut conn)
        .await
        .expect("move");
    conn.close().await.expect("close");
    let writer = writer_with(
        &seeded.db,
        Arc::new(FixedApprovalSource::new(secrets.profile("hmac"))),
    )
    .await;
    let spy = Spy::default();
    let error = writer
        .transition(&request("APPROVED"), NOW, &spy)
        .await
        .expect_err("illegal");
    writer.close().await;
    assert_eq!(error.code(), gsd_codes::ErrorCode::Contract);
    assert!(
        spy.approval().is_none(),
        "an illegal target needs no gate facts"
    );
}

/// A source that tells the test when the writer has asked for the keys (the last thing it does before it opens the transaction).
#[derive(Debug)]
struct Announcing {
    inner: FixedApprovalSource,
    asked: Mutex<mpsc::Sender<()>>,
    calls: AtomicUsize,
}

impl ApprovalSource for Announcing {
    fn keys(&self) -> Result<ApprovalKeys, Declined> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        let keys = self.inner.keys();
        self.asked
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .send(())
            .ok();
        keys
    }

    fn clock(&self, requested_now: f64) -> f64 {
        requested_now
    }
}

/// The verdict rows are read inside the write lock. A second connection holds the lock; the writer is started and has asked for its
/// keys, so it is at `BEGIN IMMEDIATE`; the holder commits the row the task is missing; the writer must see it. A writer that read the
/// rows before it took the lock would refuse with the gate missing. The keys were asked for once.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn the_verdict_rows_are_read_in_the_transaction_that_writes_not_before_the_lock() {
    let secrets = Secrets::new();
    let seeded = store(
        r#"{"verdicts": [["review", "pass"]], "late": ["quality", "pass"]}"#,
        &secrets.profile("hmac"),
    );
    let late = seeded.late.as_ref().expect("the scenario made a late row");
    let (sender, asked) = mpsc::channel();
    let source = Arc::new(Announcing {
        inner: FixedApprovalSource::new(secrets.profile("hmac")),
        asked: Mutex::new(sender),
        calls: AtomicUsize::new(0),
    });
    let mut holder = connect(&seeded.db).await;
    sqlx::query("BEGIN IMMEDIATE")
        .execute(&mut holder)
        .await
        .expect("the holder takes the write lock");

    let writer = writer_with(&seeded.db, source.clone()).await;
    let call = tokio::spawn(async move {
        let outcome = writer
            .transition(&request("APPROVED"), NOW, &HostDecider)
            .await;
        writer.close().await;
        outcome
    });
    tokio::task::spawn_blocking(move || asked.recv_timeout(Duration::from_secs(60)))
        .await
        .expect("join")
        .expect("the writer asked for its keys");
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(
        !call.is_finished(),
        "the writer is waiting for the lock the holder has"
    );
    sqlx::query(
        "INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts, envelope_json, sig) VALUES (?,?,?,?,?,?,?,?,?)",
    )
    .bind("T-1")
    .bind(&late.gate)
    .bind(&late.verdict)
    .bind("A09")
    .bind(&late.findings)
    .bind(late.expires_at)
    .bind(late.ts)
    .bind(&late.envelope_json)
    .bind(&late.sig)
    .execute(&mut holder)
    .await
    .expect("the late row");
    sqlx::query("COMMIT")
        .execute(&mut holder)
        .await
        .expect("commit");
    holder.close().await.expect("close");

    let applied = call.await.expect("join").unwrap_or_else(|error| {
        panic!(
            "the row committed under the writer's feet was not seen: {}",
            rendered(&error)
        )
    });
    assert_eq!(
        obj_get(&applied.task, "state"),
        Some(&Json::Str("APPROVED".to_string()))
    );
    assert_eq!(
        source.calls.load(Ordering::SeqCst),
        1,
        "the keys are read once per call"
    );
}

// ---- a `findings` column Python's `json.loads` cannot read ---------------------------------------------------------------------
//
// Python's `_latest_rows` runs `json.loads(findings)` on EVERY row of the task with `ts >= since`: a required gate or not, a NULL gate or
// not. A NULL or invalid `findings` raises, and the transition is refused with Python's own text (`E-INPUT` JSONDecodeError, `E-INTERNAL`
// TypeError). The signed envelopes stay intact in all of these, so a host that never read `findings` would approve.

/// An extra row on `T-1`: `gate`, `findings` and `ts` are SQL literals. The envelope is NULL, so it is never a current required row.
fn extra_row(gate: &str, findings: &str, ts: &str) -> String {
    format!(
        "INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts, envelope_json, sig) VALUES ('T-1', {gate}, 'pass', 'A09', {findings}, 9e9, {ts}, NULL, NULL)"
    )
}

/// One case of the table: the statements run on the store, the notes of the task and whether the writer must decline (Python refuses
/// or answers) or approve (Python approves).
fn findings_scenario(name: &str) -> (Vec<String>, &'static str, bool) {
    let review = |findings: &str| {
        vec![format!(
            "UPDATE verdicts SET findings={findings} WHERE gate='review'"
        )]
    };
    let since = r#""verdicts_since": 1.0"#;
    match name {
        "required-row-invalid" => (review("'{oops'"), "", true),
        "required-row-null" => (review("NULL"), "", true),
        "required-row-empty" => (review("''"), "", true),
        "other-gate-invalid" => (vec![extra_row("'security'", "'{oops'", "9e9")], "", true),
        "other-gate-null" => (vec![extra_row("'security'", "NULL", "9e9")], "", true),
        "null-gate-invalid" => (vec![extra_row("NULL", "'{oops'", "9e9")], "", true),
        "null-gate-null" => (vec![extra_row("NULL", "NULL", "9e9")], "", true),
        // Python reads `NaN`; this crate does not, so Python answers (and approves). Never a refusal made by Rust.
        "required-row-nan" => (review("'NaN'"), "", true),
        "other-gate-valid" => (vec![extra_row("'security'", "'[]'", "9e9")], "", false),
        "null-gate-valid" => (vec![extra_row("NULL", "'[]'", "9e9")], "", false),
        // The row is older than `notes.verdicts_since`: Python's query never returns it, so it is never read.
        "invalid-row-before-since" => (
            vec![extra_row("'security'", "'{oops'", "0.5")],
            since,
            false,
        ),
        "null-gate-invalid-before-since" => (vec![extra_row("NULL", "NULL", "0.5")], since, false),
        other => panic!("no findings scenario {other}"),
    }
}

/// Python signed rows are intact and a `findings` column is changed by `name`: the writer declines before any write when Python would
/// raise (the decider is never called, the store is byte-identical), and approves when Python approves.
async fn findings_case(name: &str) {
    let (statements, notes, declines) = findings_scenario(name);
    let secrets = Secrets::new();
    let sql: Vec<String> = statements
        .iter()
        .map(|text| format!("\"{text}\""))
        .collect();
    let notes = if notes.is_empty() {
        String::new()
    } else {
        format!(", \"notes\": {{{notes}}}")
    };
    let seeded = store(
        &format!(
            r#"{{"verdicts": [["review", "pass"], ["quality", "pass"]]{notes}, "sql": [{}]}}"#,
            sql.join(", ")
        ),
        &secrets.profile("hmac"),
    );
    let before = store_bytes(&seeded.db);
    let writer = writer_with(
        &seeded.db,
        Arc::new(FixedApprovalSource::new(secrets.profile("hmac"))),
    )
    .await;
    let spy = Spy::default();
    let outcome = writer.transition(&request("APPROVED"), NOW, &spy).await;
    writer.close().await;
    if declines {
        let error = outcome.expect_err(&format!("{name}: Python cannot read findings"));
        assert_eq!(
            error.write_state(),
            WriteState::NotStarted,
            "{name}: {}",
            rendered(&error)
        );
        assert_eq!(
            spy.calls.load(Ordering::SeqCst),
            0,
            "{name}: no decision is made"
        );
        assert_eq!(store_bytes(&seeded.db), before, "{name}: nothing written");
        assert!(
            secrets.leaks_in(&rendered(&error)).is_empty(),
            "{name}: a secret is in the refusal"
        );
    } else {
        outcome.unwrap_or_else(|error| panic!("{name}: {}", rendered(&error)));
        assert_eq!(state_of(&seeded.db, "T-1").await, "APPROVED", "{name}");
    }
}

#[tokio::test]
async fn an_invalid_findings_column_on_a_required_row_is_left_to_python() {
    findings_case("required-row-invalid").await;
}

#[tokio::test]
async fn a_null_findings_column_on_a_required_row_is_left_to_python() {
    findings_case("required-row-null").await;
}

#[tokio::test]
async fn an_empty_findings_column_on_a_required_row_is_left_to_python() {
    findings_case("required-row-empty").await;
}

#[tokio::test]
async fn a_findings_text_this_crate_does_not_read_is_left_to_python() {
    findings_case("required-row-nan").await;
}

#[tokio::test]
async fn an_invalid_findings_column_on_a_gate_that_is_not_required_is_left_to_python() {
    findings_case("other-gate-invalid").await;
}

#[tokio::test]
async fn a_null_findings_column_on_a_gate_that_is_not_required_is_left_to_python() {
    findings_case("other-gate-null").await;
}

#[tokio::test]
async fn an_invalid_findings_column_on_a_row_with_a_null_gate_is_left_to_python() {
    findings_case("null-gate-invalid").await;
}

#[tokio::test]
async fn a_null_findings_column_on_a_row_with_a_null_gate_is_left_to_python() {
    findings_case("null-gate-null").await;
}

#[tokio::test]
async fn valid_findings_on_a_gate_that_is_not_required_do_not_stop_an_approval() {
    findings_case("other-gate-valid").await;
}

#[tokio::test]
async fn valid_findings_on_a_row_with_a_null_gate_do_not_stop_an_approval() {
    findings_case("null-gate-valid").await;
}

#[tokio::test]
async fn an_invalid_findings_column_on_a_row_before_verdicts_since_is_never_read() {
    findings_case("invalid-row-before-since").await;
}

#[tokio::test]
async fn a_null_gate_row_with_null_findings_before_verdicts_since_is_never_read() {
    findings_case("null-gate-invalid-before-since").await;
}

// ---- the expiry is compared with a clock read inside the lock -------------------------------------------------------------------

/// A source with a clock the test controls: it announces the keys request (the last thing the writer does before `BEGIN IMMEDIATE`),
/// counts its clock readings, and once `waited` is set its clock is `JUMP` seconds later than the call's own reading.
#[derive(Debug)]
struct Waited {
    inner: FixedApprovalSource,
    asked: Mutex<mpsc::Sender<()>>,
    waited: AtomicBool,
    clock_calls: AtomicUsize,
}

/// Far more than a verdict's life: a clock that has jumped expires every verdict signed today.
const JUMP: f64 = 2_000_000_000.0;

impl Waited {
    fn new(secrets: &Secrets, profile: &str) -> (Arc<Self>, mpsc::Receiver<()>) {
        let (sender, asked) = mpsc::channel();
        let source = Arc::new(Self {
            inner: FixedApprovalSource::new(secrets.profile(profile)),
            asked: Mutex::new(sender),
            waited: AtomicBool::new(false),
            clock_calls: AtomicUsize::new(0),
        });
        (source, asked)
    }
}

impl ApprovalSource for Waited {
    fn keys(&self) -> Result<ApprovalKeys, Declined> {
        let keys = self.inner.keys();
        self.asked
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .send(())
            .ok();
        keys
    }

    fn clock(&self, requested_now: f64) -> f64 {
        self.clock_calls.fetch_add(1, Ordering::SeqCst);
        if self.waited.load(Ordering::SeqCst) {
            requested_now + JUMP
        } else {
            requested_now
        }
    }
}

/// A writer is started while a second connection holds the write lock; once it has asked for its keys it is at `BEGIN IMMEDIATE`. After
/// half a second the test lets time pass (`jump`: the source's clock moves `JUMP` seconds on) and releases the lock. Returns what the
/// call answered, the store and the source.
async fn call_that_waits_for_the_lock(
    jump: bool,
) -> (Result<Applied, ApplyError>, Store, Arc<Waited>) {
    let secrets = Secrets::new();
    let seeded = store(PASS, &secrets.profile("hmac"));
    let (source, asked) = Waited::new(&secrets, "hmac");
    let mut holder = connect(&seeded.db).await;
    sqlx::query("BEGIN IMMEDIATE")
        .execute(&mut holder)
        .await
        .expect("the holder takes the write lock");
    let writer = writer_with(&seeded.db, source.clone()).await;
    let call = tokio::spawn(async move {
        let outcome = writer
            .transition(&request("APPROVED"), NOW, &HostDecider)
            .await;
        writer.close().await;
        outcome
    });
    tokio::task::spawn_blocking(move || asked.recv_timeout(Duration::from_secs(60)))
        .await
        .expect("join")
        .expect("the writer asked for its keys");
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(
        !call.is_finished(),
        "the writer is waiting for the lock the holder has"
    );
    assert_eq!(
        source.clock_calls.load(Ordering::SeqCst),
        0,
        "the clock is not read before the lock is held"
    );
    source.waited.store(jump, Ordering::SeqCst);
    sqlx::query("COMMIT")
        .execute(&mut holder)
        .await
        .expect("release");
    holder.close().await.expect("close");
    let outcome = call.await.expect("join");
    (outcome, seeded, source)
}

/// A verdict that was current when the call started and expired while it waited for the lock is expired: Python reads `time.time()`
/// inside its lock. A writer that compared the expiry with the reading taken before the wait would approve.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_verdict_that_expires_while_the_writer_waits_for_the_lock_is_refused() {
    let (outcome, seeded, source) = call_that_waits_for_the_lock(true).await;
    let error = outcome.expect_err("the verdicts expired during the wait");
    assert_eq!(error.code(), gsd_codes::ErrorCode::Policy);
    assert!(
        error
            .message()
            .contains("lacks passing gates ['review', 'quality']"),
        "{}",
        error.message()
    );
    assert_eq!(state_of(&seeded.db, "T-1").await, "IN_REVIEW");
    assert_eq!(
        source.clock_calls.load(Ordering::SeqCst),
        1,
        "one clock reading per call"
    );
}

/// The control of the test above: the same wait, the same store, a clock that did not move. The verdicts are approved, and the audit
/// `ts` is still the call's own reading (`NOW`), not the clock's.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn the_same_wait_with_a_clock_that_did_not_move_approves_and_the_audit_ts_is_the_calls_own() {
    let (outcome, seeded, source) = call_that_waits_for_the_lock(false).await;
    outcome.unwrap_or_else(|error| panic!("{}", rendered(&error)));
    assert_eq!(state_of(&seeded.db, "T-1").await, "APPROVED");
    assert_eq!(source.clock_calls.load(Ordering::SeqCst), 1);
    let mut conn = connect(&seeded.db).await;
    let row = sqlx::query("SELECT ts FROM transitions WHERE task_id='T-1' AND to_state='APPROVED'")
        .fetch_one(&mut conn)
        .await
        .expect("the audit row");
    let ts: f64 = row.get(0);
    conn.close().await.expect("close");
    assert_eq!(ts, NOW, "the audit ts keeps the call's single reading");
}

/// Only an APPROVED transition that reads rows reads the clock: another target and a key-policy refusal read none.
#[tokio::test]
async fn the_clock_is_read_only_when_the_expiry_is_compared() {
    let secrets = Secrets::new();
    let seeded = store(PASS, &secrets.profile("hmac"));
    let (source, _asked) = Waited::new(&secrets, "hmac");
    let writer = writer_with(&seeded.db, source.clone()).await;
    writer
        .transition(&request("CHANGES_REQUESTED"), NOW, &HostDecider)
        .await
        .expect("a rework");
    writer.close().await;
    assert_eq!(source.clock_calls.load(Ordering::SeqCst), 0);

    let seeded = store(PASS, &secrets.profile("hmac"));
    let (source, _asked) = Waited::new(&secrets, "none");
    let writer = writer_with(&seeded.db, source.clone()).await;
    writer
        .transition(&request("APPROVED"), NOW, &HostDecider)
        .await
        .expect_err("no key");
    writer.close().await;
    assert_eq!(source.clock_calls.load(Ordering::SeqCst), 0);
}

/// The production clock is the wall clock and never earlier than the call's own reading; a fixed source has no clock of its own.
#[test]
fn the_production_clock_is_the_wall_clock_never_earlier_than_the_request() {
    let wall = || {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("after the epoch")
            .as_secs_f64()
    };
    let before = wall();
    let read = EnvApprovalSource.clock(0.0);
    assert!(
        before <= read && read <= wall(),
        "the wall clock: {before} {read}"
    );
    assert_eq!(
        EnvApprovalSource.clock(1.0e18),
        1.0e18,
        "never earlier than the request"
    );
    assert_eq!(FixedApprovalSource::default().clock(42.5), 42.5);
}

// ---- nothing of a key anywhere -----------------------------------------------------------------------------------------------

#[tokio::test]
async fn no_key_is_in_a_refusal_or_in_the_debug_output_of_anything_the_writer_returns() {
    let secrets = Secrets::new();
    let seeded = store(
        r#"{"verdicts": [["review", "pass"], ["quality", "fail"]]}"#,
        &secrets.profile("both"),
    );
    let source = Arc::new(FixedApprovalSource::new(secrets.profile("both")));
    let debug = format!("{source:?}");
    assert!(
        secrets.leaks_in(&debug).is_empty(),
        "the Debug output of a source holds a secret"
    );
    let keys = source.keys().expect("keys");
    assert!(
        secrets.leaks_in(&format!("{keys:?}")).is_empty(),
        "the Debug output of the keys holds a secret"
    );
    let writer = writer_with(&seeded.db, source).await;
    let error = writer
        .transition(&request("APPROVED"), NOW, &HostDecider)
        .await
        .expect_err("a failing gate");
    writer.close().await;
    assert!(secrets.leaks_in(&rendered(&error)).is_empty());
}

// ---- a secret cannot be copied out ---------------------------------------------------------------------------------------------

/// The standard stable trick: `<T as AmbiguousIfClone<_>>::some_item` is ambiguous, so this does not compile, exactly when `T` is
/// `Clone`. A `KeyMaterial` or an `ApprovalKeys` that could be cloned would let a caller copy a secret out of its holder.
/// (The `Serialize` half of this check needs `serde` as a dev-dependency, which awaits the user's approval: R11 of decision D-P30.)
mod keys_are_not_clone {
    trait AmbiguousIfClone<A> {
        fn some_item() {}
    }
    impl<T: ?Sized> AmbiguousIfClone<()> for T {}
    impl<T: Clone> AmbiguousIfClone<u8> for T {}

    const _: fn() = || {
        let _ = <gsd_envelope::KeyMaterial as AmbiguousIfClone<_>>::some_item;
        let _ = <gsd_taskwriter::ApprovalKeys as AmbiguousIfClone<_>>::some_item;
    };
}
