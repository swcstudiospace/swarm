//! The gate-issue tail (`TaskStoreWriter::issue_gate`, U3.6a) against the oracle.
//!
//! Stores are built by the REAL Python `TaskStore` (one empty template, copied per test; rows are inserted as Python made them). The
//! vectors of `tests/vectors/gate_issue.json` come from `tests/parity/gen_gate_issue_vectors.py`, which runs the real `issue_gate` under
//! one fake clock and a pinned id source with throwaway keys derived from the two phrases below. Replaying them proves, case by case:
//!
//! - the verdict file, the envelopes `issue_gate` returns, the events, the refusal (code, message, details) and the rows of `tasks`,
//!   `transitions` and `verdicts` afterwards are byte-identical to Python's, `sig` included (the same canonical bytes and the same key
//!   give the same HMAC and the same Ed25519 signature), and every pinned id is drawn, in order, exactly as often as Python drew it;
//! - what Rust must leave to Python (`rust.outcome == "not_started"`) is refused as `E-DEP` before the first write and leaves the store
//!   file byte-identical.
//!
//! Also here: the rows of one run commit together or not at all (a trigger fails the second `INSERT`); and the rows Rust signs are
//! accepted by Python's own `verify_envelope` and `TaskStore.missing_gate_reasons`, with the key in the Python child's environment only.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::LazyLock;

use gsd_envelope::{KeyFilePresence, KeyMaterial};
use gsd_route::{jstr, obj_get, parse_json, Fields, Json};
use gsd_taskwriter::{
    derive_gate_issue, open_for_write, EnvelopeIds, GateIssueRequest, GatePlan, GateWrite,
    StoreFacts, SystemIds, TaskStoreWriter, WriteState, WriterConfig, WriterError,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{Column, ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};

const VECTORS: &str = include_str!("vectors/gate_issue.json");

/// Throwaway test data, the same phrases as the generator. They are public and protect nothing.
const HMAC_PHRASE: &str = "gate-issue-vectors-throwaway-hmac";
const ED25519_SEED_PHRASE: &str = "gate-issue-vectors-throwaway-key";

const TEMPLATE_SCRIPT: &str =
    "import sys\nfrom swarm.taskstore import TaskStore\nTaskStore(sys.argv[1]).conn.close()\n";

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

fn python(script: &str, args: &[&str], env: &[(&str, &str)]) -> String {
    let root = repo_root();
    assert!(
        root.join("swarm/taskstore.py").is_file(),
        "no swarm/taskstore.py under {}: set SWARM_REPO_ROOT",
        root.display()
    );
    let mut command = Command::new("python3");
    command
        .arg("-c")
        .arg(script)
        .args(args)
        .current_dir(&root)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("SWARM_GSD_STATE_FILE", "")
        .env_remove("SWARM_TASKSTORE_WAL")
        .env_remove("SWARM_SIGNING_KEY")
        .env_remove("SWARM_ED25519_KEY")
        .env_remove("SWARM_ED25519_KEY_FILE")
        .env_remove("SWARM_ALLOW_INSECURE_DEV_KEY")
        .env_remove("SWARM_REQUIRE_KEY")
        .env_remove("SWARM_AGENT_SESSION");
    for (name, value) in env {
        command.env(name, value);
    }
    let output = command.output().expect("run python3");
    assert!(
        output.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).into_owned()
}

static TEMPLATE: LazyLock<Vec<u8>> = LazyLock::new(|| {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("tasks.db");
    python(TEMPLATE_SCRIPT, &[&db.to_string_lossy()], &[]);
    std::fs::read(&db).expect("read template")
});

fn ed25519_seed_hex() -> String {
    ED25519_SEED_PHRASE
        .bytes()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// A throwaway-key environment: the lookup `KeyMaterial::from_lookup` reads, with the placeholders of the vectors replaced.
fn keys_from(env: &Fields) -> KeyMaterial {
    let get = |name: &str| -> Option<OsString> {
        match obj_get(env, name) {
            Some(Json::Str(value)) => Some(OsString::from(match value.as_str() {
                "@hmac" => HMAC_PHRASE.to_string(),
                "@ed25519" => ed25519_seed_hex(),
                other => other.to_string(),
            })),
            _ => None,
        }
    };
    KeyMaterial::from_lookup(get, |_| KeyFilePresence::Absent).expect("key material")
}

// ---- raw SQL access, independent of the writer under test -----------------------------------------------------------------

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("raw connection")
}

async fn exec(path: &Path, sql: &str) {
    let mut conn = connect(path).await;
    sqlx::query(sql).execute(&mut conn).await.expect(sql);
    conn.close().await.expect("close");
}

async fn insert_rows(path: &Path, rows: &[(&str, Fields)]) {
    let mut conn = connect(path).await;
    sqlx::query("BEGIN")
        .execute(&mut conn)
        .await
        .expect("begin");
    for (table, row) in rows {
        let names: Vec<&str> = row.iter().map(|(name, _)| name.as_str()).collect();
        let sql = format!(
            "INSERT INTO {table} ({}) VALUES ({})",
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
        insert.execute(&mut conn).await.expect(&sql);
    }
    sqlx::query("COMMIT")
        .execute(&mut conn)
        .await
        .expect("commit");
    conn.close().await.expect("close");
}

/// The three tables as Python's `dict(row)` lists, values typed as stored (the shape of `python.after` in the vectors).
async fn dump(path: &Path) -> Json {
    let mut conn = connect(path).await;
    let mut out: Fields = Vec::new();
    for table in ["tasks", "transitions", "verdicts"] {
        let rows = sqlx::query(&format!("SELECT * FROM {table} ORDER BY rowid"))
            .fetch_all(&mut conn)
            .await
            .expect(table);
        let mut list = Vec::new();
        for row in &rows {
            let mut line: Fields = Vec::new();
            for (index, column) in row.columns().iter().enumerate() {
                let raw = row.try_get_raw(index).expect("raw value");
                let value = if raw.is_null() {
                    Json::Null
                } else {
                    match raw.type_info().name() {
                        "INTEGER" => Json::Int(row.get(index)),
                        "REAL" => Json::Float(row.get(index)),
                        "TEXT" => Json::Str(row.get(index)),
                        other => panic!("{table}.{}: unexpected {other}", column.name()),
                    }
                };
                line.push((column.name().to_string(), value));
            }
            list.push(Json::Obj(line));
        }
        out.push((table.to_string(), Json::Arr(list)));
    }
    conn.close().await.expect("close");
    Json::Obj(out)
}

// ---- vector access --------------------------------------------------------------------------------------------------------

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

fn items(value: &Json) -> &[Json] {
    match value {
        Json::Arr(items) => items,
        other => panic!("not a list: {other:?}"),
    }
}

fn fields(value: &Json) -> Fields {
    match value {
        Json::Obj(fields) => fields.clone(),
        other => panic!("not an object: {other:?}"),
    }
}

/// The pinned ids of a case, drawn in the order Python drew them.
struct ScriptedIds {
    msg: Vec<String>,
    uuid4: Vec<String>,
    msg_used: usize,
    uuid4_used: usize,
}

impl ScriptedIds {
    fn from_case(case: &Json) -> Self {
        let ids = member(case, "ids");
        let strings = |key: &str| -> Vec<String> {
            items(member(ids, key))
                .iter()
                .map(|v| text(v).to_string())
                .collect()
        };
        Self {
            msg: strings("msg"),
            uuid4: strings("uuid4"),
            msg_used: 0,
            uuid4_used: 0,
        }
    }

    fn exhausted(&self) -> bool {
        self.msg_used == self.msg.len() && self.uuid4_used == self.uuid4.len()
    }
}

impl EnvelopeIds for ScriptedIds {
    fn msg_id(&mut self, _now_ms: u64) -> Option<String> {
        self.msg_used += 1;
        self.msg.get(self.msg_used - 1).cloned()
    }

    fn correlation_id(&mut self) -> Option<String> {
        self.uuid4_used += 1;
        let hex = self.uuid4.get(self.uuid4_used - 1)?;
        Some(format!(
            "{}-{}-{}-{}-{}",
            &hex[0..8],
            &hex[8..12],
            &hex[12..16],
            &hex[16..20],
            &hex[20..32]
        ))
    }

    fn trace_id(&mut self) -> Option<String> {
        self.uuid4_used += 1;
        self.uuid4.get(self.uuid4_used - 1).cloned()
    }
}

fn request_of(case: &Json) -> Result<GateIssueRequest, gsd_taskwriter::Decline> {
    let env = fields(member(case, "env"));
    let mut wire = fields(member(case, "request"));
    wire.push((
        "session".to_string(),
        Json::Bool(
            matches!(obj_get(&env, "SWARM_AGENT_SESSION"), Some(Json::Str(flag)) if flag == "1"),
        ),
    ));
    if let Some(Json::Str(fail)) = obj_get(&env, "SWARM_DRYRUN_FAIL") {
        wire.push(("dryrun_fail".to_string(), jstr(fail)));
    }
    wire.push(("now".to_string(), member(case, "now").clone()));
    GateIssueRequest::from_json(&Json::Obj(wire))
}

async fn seeded(path: &Path, initial: &Json) {
    std::fs::write(path, &*TEMPLATE).expect("copy template");
    let mut rows = Vec::new();
    for name in ["tasks", "transitions"] {
        rows.extend(
            items(member(initial, name))
                .iter()
                .map(|row| (name, fields(row))),
        );
    }
    insert_rows(path, &rows).await;
}

async fn issue(
    path: &Path,
    request: &GateIssueRequest,
    keys: &KeyMaterial,
    ids: &mut dyn EnvelopeIds,
) -> Result<GatePlan, WriterError> {
    let store = open_for_write(&WriterConfig::new(path))
        .await
        .expect("open store");
    let writer = TaskStoreWriter::new(store);
    let outcome = writer.issue_gate(request, keys, ids).await;
    writer.close().await;
    outcome
}

/// Replays one case; the error says what differs.
async fn replay(case: &Json) -> Result<(), String> {
    let id = text(member(case, "id")).to_string();
    let fail = |what: String| Err(format!("{id}: {what}"));
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    seeded(&path, member(case, "initial")).await;
    let bytes = std::fs::read(&path).expect("read seeded store");
    let keys = keys_from(&fields(member(case, "env")));
    let mut ids = ScriptedIds::from_case(case);
    let python = member(case, "python");
    let expected_not_started = text(member(member(case, "rust"), "outcome")) == "not_started";

    let outcome = match request_of(case) {
        Ok(request) => issue(&path, &request, &keys, &mut ids).await,
        Err(declined) => Err(declined.into()),
    };
    if expected_not_started {
        let Err(error) = outcome else {
            return fail("Rust answered a request it must leave to Python".to_string());
        };
        if error.write_state() != WriteState::NotStarted || error.code().as_str() != "E-DEP" {
            return fail(format!("refusal is {error}, {:?}", error.write_state()));
        }
        if std::fs::read(&path).expect("read store") != bytes {
            return fail("the store file changed after a not-started refusal".to_string());
        }
        return Ok(());
    }
    let plan = match outcome {
        Ok(plan) => plan,
        Err(error) => return fail(format!("Rust refused a request Python answers: {error}")),
    };
    let shown = plan.to_json();
    let got = |key: &str| member(&shown, key);
    if got("ensure_verdicts_dir") != member(python, "verdicts_dir") {
        return fail("the verdicts directory decision differs".to_string());
    }
    if got("file") != member(python, "file") {
        return fail(format!(
            "verdict file differs:\nrust   {:?}\npython {:?}",
            got("file"),
            member(python, "file")
        ));
    }
    if got("events") != member(python, "events") {
        return fail(format!(
            "events differ:\nrust   {:?}\npython {:?}",
            got("events"),
            member(python, "events")
        ));
    }
    match text(member(python, "outcome")) {
        "ok" => {
            if got("error") != &Json::Null {
                return fail(format!("Rust refused: {:?}", got("error")));
            }
            if got("envelope") != member(python, "envelope") {
                return fail(format!(
                    "returned envelope differs:\nrust   {:?}\npython {:?}",
                    got("envelope"),
                    member(python, "envelope")
                ));
            }
            if got("recorded") != member(python, "recorded") {
                return fail(format!(
                    "recorded envelopes differ:\nrust   {:?}\npython {:?}",
                    got("recorded"),
                    member(python, "recorded")
                ));
            }
        }
        "error" => {
            if got("error") != member(python, "error") {
                return fail(format!(
                    "refusal differs:\nrust   {:?}\npython {:?}",
                    got("error"),
                    member(python, "error")
                ));
            }
            if got("writes") != &Json::Int(0) || got("envelope") != &Json::Null {
                return fail("a refused run returns writes or an envelope".to_string());
            }
        }
        other => return fail(format!("unknown python outcome {other}")),
    }
    let after = dump(&path).await;
    if &after != member(python, "after") {
        return fail(format!(
            "the store differs after the run:\nrust   {after:?}\npython {:?}",
            member(python, "after")
        ));
    }
    if !ids.exhausted() {
        return fail(format!(
            "ids drawn: {} of {} message, {} of {} uuid4",
            ids.msg_used,
            ids.msg.len(),
            ids.uuid4_used,
            ids.uuid4.len()
        ));
    }
    Ok(())
}

fn cases() -> Vec<Json> {
    let doc = parse_json(VECTORS).expect("vectors parse");
    items(member(&doc, "cases")).to_vec()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn every_oracle_case_replays_byte_for_byte() {
    let all = cases();
    assert!(all.len() >= 80, "the corpus shrank to {}", all.len());
    let mut failures = Vec::new();
    for case in &all {
        if let Err(message) = replay(case).await {
            failures.push(message);
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        all.len(),
        failures.join("\n")
    );
}

#[test]
fn the_corpus_covers_the_branches_it_is_for() {
    let all = cases();
    let has = |wanted: &str| all.iter().any(|case| text(member(case, "id")) == wanted);
    for id in [
        "hmac-pass",
        "ed25519-fail-feedback",
        "per-target-one-omitted",
        "per-target-every-severity",
        "simulate-runner-dry-run-sim-fail",
        "simulate-not-a-runner-dry-run",
        "advisory-fail",
        "advisory-per-target-blocks-file",
        "session-per-target-forces-fail",
        "no-task-id",
        "foreign-correlation",
        "expires-negative",
        "extra-and-runs",
        "rework-suffix-targets",
        "dev-key-signs-and-audits",
        "unknown-target",
        "duplicate-target",
    ] {
        assert!(has(id), "case {id} is missing");
    }
    let sigs = |kind: &str| {
        all.iter()
            .filter(|case| matches!(member(member(case, "python"), "outcome"), Json::Str(o) if o == "ok"))
            .flat_map(|case| items(member(member(case, "python"), "recorded")).to_vec())
            .filter(|pair| {
                matches!(&items(pair)[1], Json::Obj(f) if matches!(obj_get(f, "sig"), Some(Json::Str(s)) if s.starts_with(kind)))
            })
            .count()
    };
    assert!(
        sigs("hmac:") > 0 && sigs("ed25519:") > 0,
        "both signature kinds must be in the corpus"
    );
}

// ---- one transaction ------------------------------------------------------------------------------------------------------

/// A store built with Python's `TaskStore`: a running quality gate task with two targets that need the quality gate.
const SEED_SCRIPT: &str = r#"
import sys
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
s.create(task_id="G1", correlation_id="corr-g1", capability="cap", notes={"gate": "quality", "gate_for": ["T-a", "T-b"]})
for t in ("T-a", "T-b"):
    s.create(task_id=t, correlation_id="corr-" + t, capability="cap", notes={"gates": ["quality"]})
s.conn.execute("UPDATE tasks SET state='IN_PROGRESS' WHERE task_id='G1'")
s.conn.execute("UPDATE tasks SET state='IN_REVIEW' WHERE task_id IN ('T-a','T-b')")
s.conn.commit()
s.conn.close()
"#;

fn real_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_secs_f64()
}

fn major() -> Json {
    Json::Obj(vec![
        ("id".to_string(), jstr("F-1")),
        ("severity".to_string(), jstr("major")),
        ("kind".to_string(), jstr("functional")),
        ("summary".to_string(), jstr("missing test")),
    ])
}

fn quality_request(per_target: Vec<(String, Vec<Json>)>) -> GateIssueRequest {
    GateIssueRequest {
        gate: "quality".to_string(),
        agent_id: "A08@local".to_string(),
        findings: Vec::new(),
        runs: Vec::new(),
        expires_s: 86_400,
        extra: Vec::new(),
        simulate: false,
        per_target,
        task_id: Some("G1".to_string()),
        correlation_id: None,
        session: false,
        dryrun_fail: String::new(),
        now: real_now(),
    }
}

fn hmac_keys() -> KeyMaterial {
    KeyMaterial::from_lookup(
        |name| (name == "SWARM_SIGNING_KEY").then(|| OsString::from(HMAC_PHRASE)),
        |_| KeyFilePresence::Absent,
    )
    .expect("keys")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_rows_of_one_run_commit_together_or_not_at_all() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    python(SEED_SCRIPT, &[&path.to_string_lossy()], &[]);
    // The first target's row and feedback are fine, the second target's INSERT fails: nothing of the run may stay.
    exec(
        &path,
        "CREATE TRIGGER fail_b BEFORE INSERT ON verdicts WHEN NEW.task_id='T-b' BEGIN SELECT RAISE(ABORT, 'boom'); END",
    )
    .await;
    let before = dump(&path).await;
    let bytes = std::fs::read(&path).expect("read store");
    let request = quality_request(vec![
        ("T-a".to_string(), vec![major()]),
        ("T-b".to_string(), vec![major()]),
    ]);
    let mut ids = SystemIds::open().expect("ids");
    let outcome = issue(&path, &request, &hmac_keys(), &mut ids).await;
    let error = outcome.expect_err("the second insert fails");
    assert_eq!(error.write_state(), WriteState::Final);
    assert_eq!(dump(&path).await, before, "rows of an aborted run stayed");
    assert_eq!(
        std::fs::read(&path).expect("read store"),
        bytes,
        "the store file changed"
    );

    exec(&path, "DROP TRIGGER fail_b").await;
    let mut ids = SystemIds::open().expect("ids");
    let plan = issue(&path, &request, &hmac_keys(), &mut ids)
        .await
        .expect("the run commits once the trigger is gone");
    assert_eq!(
        plan.writes.len(),
        4,
        "two verdict rows and two feedback rewrites"
    );
    let rows = member(&dump(&path).await, "verdicts").clone();
    assert_eq!(items(&rows).len(), 2);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_run_with_nothing_to_write_or_a_refusal_leaves_the_store_untouched() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    python(SEED_SCRIPT, &[&path.to_string_lossy()], &[]);
    let bytes = std::fs::read(&path).expect("read store");
    // A foreign correlation id is Python's own E-POLICY: no row, no file bytes in the store.
    let mut request = quality_request(Vec::new());
    request.correlation_id = Some("corr-other".to_string());
    let mut ids = SystemIds::open().expect("ids");
    let plan = issue(&path, &request, &hmac_keys(), &mut ids)
        .await
        .expect("a refusal is a plan");
    let error = plan.error.expect("E-POLICY");
    assert_eq!(error.code().as_str(), "E-POLICY");
    assert!(plan.writes.is_empty() && plan.file.is_none());
    assert_eq!(std::fs::read(&path).expect("read store"), bytes);
}

// ---- Rust-signed rows verify in Python ------------------------------------------------------------------------------------

/// Python checks the rows with its own code. The key is in this child's environment only.
const VERIFY_SCRIPT: &str = r#"
import json, sys
from swarm.envelope import verify_envelope
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
for target in ("T-a", "T-b"):
    latest = s.latest_verdicts(target)
    row = latest["quality"]
    env = json.loads(row["envelope_json"])
    assert verify_envelope(env), target
    assert env["payload"]["gate_task"] == "G1", target
    assert row["sig"] == env["sig"], target
reasons_a = s.missing_gate_reasons("T-a")
reasons_b = s.missing_gate_reasons("T-b")
print(json.dumps([sys.argv[2], reasons_a, reasons_b, s.get("T-a")["notes_json"].get("feedback")]))
"#;

async fn rust_signed_rows_verify_in_python(
    env_name: &str,
    env_value: &str,
    keys: &KeyMaterial,
    prefix: &str,
) {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    python(SEED_SCRIPT, &[&path.to_string_lossy()], &[]);
    let request = quality_request(vec![
        ("T-a".to_string(), vec![major()]),
        ("T-b".to_string(), Vec::new()),
    ]);
    let mut ids = SystemIds::open().expect("ids");
    let plan = issue(&path, &request, keys, &mut ids).await.expect("issue");
    for write in &plan.writes {
        if let GateWrite::Verdict(row) = write {
            assert!(row.sig.starts_with(prefix), "{}", row.sig);
        }
    }
    let shown = python(
        VERIFY_SCRIPT,
        &[&path.to_string_lossy(), prefix],
        &[(env_name, env_value)],
    );
    let reply = parse_json(shown.trim()).expect("python reply");
    let reply = items(&reply);
    // T-a failed its own findings: a verified row that is not a pass. T-b passed: nothing is missing.
    assert_eq!(
        reply[1],
        Json::Obj(vec![("quality".to_string(), jstr("fail"))])
    );
    assert_eq!(reply[2], Json::Obj(Vec::new()));
    let feedback = items(&reply[3]);
    assert_eq!(
        feedback.len(),
        1,
        "one feedback entry for the failing target"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn rows_signed_with_hmac_by_rust_verify_in_python() {
    rust_signed_rows_verify_in_python("SWARM_SIGNING_KEY", HMAC_PHRASE, &hmac_keys(), "hmac:")
        .await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn rows_signed_with_ed25519_by_rust_verify_in_python() {
    let seed = ed25519_seed_hex();
    let keys = KeyMaterial::from_lookup(
        |name| (name == "SWARM_ED25519_KEY").then(|| OsString::from(seed.clone())),
        |_| KeyFilePresence::Absent,
    )
    .expect("keys");
    rust_signed_rows_verify_in_python("SWARM_ED25519_KEY", &seed, &keys, "ed25519:").await;
}

/// P1-06 at the public boundary: a request whose `extra` names a signed verdict field is declined, so Python answers it and
/// Rust never signs a payload in which `extra` overrides the verdict.
#[test]
fn extra_naming_a_signed_field_is_declined_for_python() {
    struct NoIds;
    impl EnvelopeIds for NoIds {
        fn msg_id(&mut self, _now_ms: u64) -> Option<String> {
            None
        }
        fn correlation_id(&mut self) -> Option<String> {
            None
        }
        fn trace_id(&mut self) -> Option<String> {
            None
        }
    }
    let keys =
        KeyMaterial::from_lookup(|_| None::<OsString>, |_| KeyFilePresence::Absent).expect("keys");
    for key in [
        "gate",
        "task_id",
        "verdict",
        "findings",
        "runs",
        "expires_s",
    ] {
        let request = GateIssueRequest {
            gate: "review".to_string(),
            agent_id: "A09".to_string(),
            findings: Vec::new(),
            runs: Vec::new(),
            expires_s: 86_400,
            extra: vec![(key.to_string(), Json::Null)],
            simulate: false,
            per_target: Vec::new(),
            task_id: Some("T-1".to_string()),
            correlation_id: None,
            session: false,
            dryrun_fail: String::new(),
            now: 1_700_000_000.5,
        };
        assert!(
            derive_gate_issue(&request, &StoreFacts::default(), &keys, &mut NoIds).is_err(),
            "extra naming {key} must be declined"
        );
    }
}
