//! Fault tests of the approval mode on the sandbox (Phase 14.5, pin P2): the gate facts an APPROVED transition reads, verified by the
//! guest as a shadow of the host's answer.
//!
//! What is proved here:
//!
//! - every case of the committed oracle vectors (`gate_facts.json`, recorded from the real Python `TaskStore`) is answered by the host
//!   function as Python answers it; the request round-trips; and the guest path (`core_invoke`, the function the wasm guest runs) replies
//!   with exactly the bytes of the host's answer;
//! - the refusal texts are Python's, in `check_approval` order;
//! - the verifier's outcomes per guest behaviour (honest, error, spent deadline, fuel, garbage, mangled, oversized, prepare failure,
//!   facts that differ from the answer);
//! - through the real writer: a spent deadline ends the APPROVED call with `E-TIMEOUT`, rolled back, the store byte-identical; a guest
//!   fault leaves the host's answer standing and is recorded; an agreeing guest records nothing.

use std::fs;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use gsd_adapter::sandbox_shadow::Disagreement;
use gsd_adapter::write_route::shadow_approval::SandboxApproval;
use gsd_failure::ErrorCode;
use gsd_route::approval_wire::{self, Inputs, Policy, TaskColumns};
use gsd_route::gate_facts::{IssuerState, VerdictFacts, VerdictRow};
use gsd_route::transition_plan::Request;
use gsd_route::{obj_get, parse_json, Json};
use gsd_taskwriter::{
    open_for_write, ApplyCause, ApplyError, ApprovalVerifier, FixedApprovalSource, HostDecider,
    TaskStoreWriter, WriteState, WriterConfig,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Row};
use tempfile::TempDir;

use super::{shadow, test_limits, verify_once, Behaviour, InProcessGuest};

const VECTORS: &str = include_str!("../../../gsd-route/tests/vectors/gate_facts.json");

/// The key policy of the vectors: a real key is configured, `SWARM_REQUIRE_KEY` is unset. The vectors pin no policy of their own.
const VECTOR_POLICY: Policy = Policy {
    require_key: false,
    real_key: true,
    dev_key: false,
};

// ---- the oracle vectors -----------------------------------------------------------------------------------------------------
//
// The vectors are read with the crate's own JSON parser, which reads every float as Rust's correctly rounded `f64` parse. (A parser
// that rounds differently would merge the vector's neighbouring clock readings, `1010.25` and `1010.2499999999999`.)

/// One query of a vector case: the clock and Python's answers for it.
struct Query {
    now: f64,
    reasons: Vec<(String, String)>,
    unaccepted: Vec<String>,
}

/// One vector case as the host reads it: the task, its verdict rows with the signature answers the vector records, and the issuers.
struct Case {
    name: String,
    task: TaskColumns,
    rows: Vec<VerdictFacts>,
    issuers: Vec<IssuerState>,
    queries: Vec<Query>,
}

fn field<'a>(value: &'a Json, key: &str) -> &'a Json {
    match value {
        Json::Obj(fields) => {
            obj_get(fields, key).unwrap_or_else(|| panic!("the vector has no {key}"))
        }
        other => panic!("expected an object for {key}, got {other:?}"),
    }
}

fn items(value: &Json) -> &[Json] {
    match value {
        Json::Arr(items) => items,
        other => panic!("expected an array, got {other:?}"),
    }
}

fn text(value: &Json) -> String {
    match value {
        Json::Str(text) => text.clone(),
        other => panic!("expected a string, got {other:?}"),
    }
}

fn opt_text(value: &Json) -> Option<String> {
    match value {
        Json::Null => None,
        other => Some(text(other)),
    }
}

fn number(value: &Json) -> f64 {
    match value {
        Json::Float(number) => *number,
        Json::Int(number) => *number as f64,
        other => panic!("expected a number, got {other:?}"),
    }
}

fn row_of(row: &Json) -> VerdictFacts {
    let id = match field(row, "id") {
        Json::Int(id) => *id,
        other => panic!("expected an integer id, got {other:?}"),
    };
    let facts = VerdictRow {
        id,
        task_id: text(field(row, "task_id")),
        gate: text(field(row, "gate")),
        verdict: opt_text(field(row, "verdict")),
        envelope_json: opt_text(field(row, "envelope_json")),
        ts: number(field(row, "ts")),
    };
    let sig_ok = matches!(field(row, "sig_ok"), Json::Bool(true));
    VerdictFacts::new(facts, sig_ok)
}

fn cases() -> Vec<Case> {
    let doc = parse_json(VECTORS).expect("the vector file parses");
    items(field(&doc, "cases"))
        .iter()
        .map(|case| {
            let task = field(case, "task");
            Case {
                name: text(field(case, "name")),
                task: TaskColumns {
                    task_id: text(field(task, "task_id")),
                    correlation_id: text(field(task, "correlation_id")),
                    risk_class: opt_text(field(task, "risk_class")),
                    notes: opt_text(field(task, "notes")),
                },
                rows: items(field(case, "rows")).iter().map(row_of).collect(),
                issuers: items(field(case, "task_states"))
                    .iter()
                    .map(|state| IssuerState {
                        task_id: text(field(state, "task_id")),
                        state: text(field(state, "state")),
                    })
                    .collect(),
                queries: items(field(case, "queries"))
                    .iter()
                    .map(|query| Query {
                        now: number(field(query, "now")),
                        reasons: items(field(query, "reasons"))
                            .iter()
                            .map(|pair| (text(&items(pair)[0]), text(&items(pair)[1])))
                            .collect(),
                        unaccepted: items(field(query, "unaccepted")).iter().map(text).collect(),
                    })
                    .collect(),
            }
        })
        .collect()
}

fn case_named(name: &str) -> Case {
    cases()
        .into_iter()
        .find(|case| case.name == name)
        .unwrap_or_else(|| panic!("no vector case {name}"))
}

/// The inputs of one query of `case` over `rows`, under the vector policy.
fn inputs(case: &Case, query: &Query, rows: Vec<VerdictFacts>) -> Inputs {
    Inputs {
        now: query.now,
        policy: VECTOR_POLICY,
        task: case.task.clone(),
        rows,
        issuers: case.issuers.clone(),
    }
}

/// The request bytes and the expected reply of the first query of the vector case `name`.
fn fixture(name: &str) -> (Vec<u8>, Vec<u8>) {
    let case = case_named(name);
    let facts = inputs(&case, &case.queries[0], case.rows.clone());
    let host = approval_wire::answer(&facts).unwrap_or_else(|error| panic!("{name}: {}", error.0));
    (
        approval_wire::encode_request(&facts).into_bytes(),
        approval_wire::encode_outcome(&host).into_bytes(),
    )
}

#[test]
fn every_vector_case_is_answered_as_python_answers_it_and_the_guest_agrees() {
    let cases = cases();
    assert_eq!(cases.len(), 253, "the vector file changed under the tests");
    for case in &cases {
        for query in &case.queries {
            let label = format!("{} now={}", case.name, query.now);
            let facts = inputs(case, query, case.rows.clone());
            let host = approval_wire::answer(&facts)
                .unwrap_or_else(|error| panic!("{label}: outside the subset: {}", error.0));

            let reasons: Vec<(String, String)> = host
                .missing
                .iter()
                .map(|(gate, outcome)| (gate.clone(), outcome.as_str().to_string()))
                .collect();
            assert_eq!(reasons, query.reasons, "{label}: missing gates");
            assert_eq!(
                host.unaccepted, query.unaccepted,
                "{label}: unaccepted gates"
            );
            assert_eq!(
                host.refusal.is_some(),
                !(query.reasons.is_empty() && query.unaccepted.is_empty()),
                "{label}: a refusal exactly when a gate is missing or unaccepted"
            );

            let request = approval_wire::encode_request(&facts);
            let decoded = approval_wire::decode_request(&request)
                .unwrap_or_else(|error| panic!("{label}: {}", error.message()));
            // Compared by bytes: a vector row can carry `issued_at: NaN` (inside the envelope text, which the facts hold parsed), and
            // `NaN != NaN` would fail a value comparison of an exact round trip. The answer is compared too.
            assert_eq!(
                approval_wire::encode_request(&decoded),
                request,
                "{label}: encode(decode(encode(facts))) == encode(facts)"
            );
            assert_eq!(
                approval_wire::answer(&decoded).map_err(|error| error.0),
                Ok(host.clone()),
                "{label}: the decoded facts answer as the facts"
            );

            let reply = gsd_route::core_invoke(request.as_bytes())
                .unwrap_or_else(|error| panic!("{label}: guest refused: {}", error.message()));
            assert_eq!(
                reply,
                approval_wire::encode_outcome(&host).into_bytes(),
                "{label}: the guest reply is the host answer, byte for byte"
            );

            let mut reversed = case.rows.clone();
            reversed.reverse();
            let flipped = inputs(case, query, reversed);
            let flipped_host = approval_wire::answer(&flipped)
                .unwrap_or_else(|error| panic!("{label}: {}", error.0));
            assert_eq!(
                approval_wire::encode_outcome(&flipped_host),
                approval_wire::encode_outcome(&host),
                "{label}: the row order is not an input"
            );
        }
    }
}

// ---- refusal texts ----------------------------------------------------------------------------------------------------------

fn policy(require_key: bool, real_key: bool, dev_key: bool) -> Policy {
    Policy {
        require_key,
        real_key,
        dev_key,
    }
}

#[test]
fn refusals_are_python_texts_in_check_approval_order() {
    let missing = case_named("absent-no-rows-medium");
    let query = &missing.queries[0];
    let refusal_under = |policy: Policy| {
        let mut facts = inputs(&missing, query, Vec::new());
        facts.policy = policy;
        approval_wire::answer(&facts)
            .unwrap_or_else(|error| panic!("{}", error.0))
            .refusal
    };
    let lacking = "fail-closed: T-target lacks passing gates ['review', 'quality']";
    assert_eq!(
        refusal_under(policy(true, false, true)).as_deref(),
        Some("fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured"),
        "SWARM_REQUIRE_KEY=1 without a real key is refused first"
    );
    assert_eq!(
        refusal_under(policy(false, false, false)).as_deref(),
        Some("fail-closed: no signing key configured")
    );
    assert_eq!(
        refusal_under(policy(false, false, true)).as_deref(),
        Some(lacking),
        "the dev key lets the gates decide"
    );
    assert_eq!(
        refusal_under(policy(false, true, false)).as_deref(),
        Some(lacking)
    );

    let (case, query_now) = cases()
        .into_iter()
        .find_map(|case| {
            let now = case
                .queries
                .iter()
                .find(|query| query.reasons.is_empty() && !query.unaccepted.is_empty())?
                .now;
            Some((case, now))
        })
        .expect("the vectors hold an unaccepted case");
    let mut facts = inputs(&case, &case.queries[0], case.rows.clone());
    facts.now = query_now;
    let refusal = approval_wire::answer(&facts)
        .unwrap_or_else(|error| panic!("{}", error.0))
        .refusal
        .expect("an unaccepted gate is refused");
    assert!(
        refusal.starts_with(&format!(
            "fail-closed: {} has unaccepted gates [",
            case.task.task_id
        )),
        "{refusal}"
    );
}

#[test]
fn a_gate_name_python_would_escape_is_outside_the_subset_and_the_guest_refuses_it() {
    let case = case_named("absent-no-rows-medium");
    let mut facts = inputs(&case, &case.queries[0], Vec::new());
    facts.task.notes = Some(r#"{"gates": ["it's"]}"#.to_string());
    assert!(approval_wire::answer(&facts).is_err());
    let request = approval_wire::encode_request(&facts);
    let refused = gsd_route::core_invoke(request.as_bytes()).expect_err("the guest refuses");
    assert_eq!(refused.code(), ErrorCode::Input);
}

#[test]
fn a_request_the_guest_cannot_read_names_the_field() {
    let (request, _) = fixture("ok-low-pass");
    let text = String::from_utf8(request).expect("UTF-8");
    let renamed = text.replacen("\"sig_ok\"", "\"sig_okay\"", 1);
    let error = gsd_route::core_invoke(renamed.as_bytes()).expect_err("an unknown key is refused");
    assert_eq!(error.code(), ErrorCode::Input);
    assert!(error.message().contains("rows[]"), "{}", error.message());
    assert!(
        !error.message().contains("T-target"),
        "no value in the message"
    );
}

// ---- the verifier per guest behaviour ---------------------------------------------------------------------------------------

#[test]
fn an_honest_guest_agrees_with_the_host() {
    let (request, expected) = fixture("absent-no-rows-medium");
    let guest = InProcessGuest::new(Behaviour::Honest);
    assert_eq!(verify_once(&guest, &request, &expected), Ok(()));
    assert_eq!(guest.calls(), 1);
}

#[test]
fn a_guest_error_is_a_fault_with_its_code() {
    let (request, expected) = fixture("absent-no-rows-medium");
    for code in [ErrorCode::Dep, ErrorCode::Internal, ErrorCode::Input] {
        let guest = InProcessGuest::new(Behaviour::Fail(code));
        assert_eq!(
            verify_once(&guest, &request, &expected),
            Err(Disagreement::Fault(code)),
            "{code:?}"
        );
    }
}

#[test]
fn a_spent_deadline_is_a_timeout_and_no_failover() {
    let (request, expected) = fixture("absent-no-rows-medium");
    // The guest sleeps 50 ms; a 40 ms call deadline is spent before its reply.
    let guest = InProcessGuest::new(Behaviour::Spent);
    let verification = shadow(&guest, Duration::from_millis(40));
    let outcome = verification.verify(&request, &expected);
    assert_eq!(
        verification.settle(outcome.err()),
        Err(Disagreement::Timeout)
    );
    assert_eq!(verification.failover(), None, "a timeout is not a fault");
}

#[test]
fn fuel_while_time_remains_is_the_guest_fault_internal() {
    let (request, expected) = fixture("absent-no-rows-medium");
    let guest = InProcessGuest::new(Behaviour::Fuel);
    assert_eq!(
        verify_once(&guest, &request, &expected),
        Err(Disagreement::Fault(ErrorCode::Internal))
    );
}

#[test]
fn garbage_and_mangled_replies_are_contract_faults() {
    let (request, expected) = fixture("absent-no-rows-medium");
    for behaviour in [Behaviour::Garbage, Behaviour::Mangled] {
        let guest = InProcessGuest::new(behaviour);
        assert_eq!(
            verify_once(&guest, &request, &expected),
            Err(Disagreement::Fault(ErrorCode::Contract)),
            "{behaviour:?}"
        );
    }
}

#[test]
fn an_oversized_reply_is_a_capacity_fault() {
    let (request, expected) = fixture("absent-no-rows-medium");
    let guest = InProcessGuest::new(Behaviour::Oversized);
    assert_eq!(
        verify_once(&guest, &request, &expected),
        Err(Disagreement::Fault(ErrorCode::Capacity))
    );
}

#[test]
fn a_guest_that_cannot_be_prepared_is_a_fault_and_is_never_called() {
    let (request, expected) = fixture("absent-no-rows-medium");
    let guest = InProcessGuest::new(Behaviour::Prepare(ErrorCode::Dep));
    let verification = shadow(&guest, Duration::from_secs(30));
    let outcome = verification.verify(&request, &expected);
    assert_eq!(outcome, Err(Disagreement::Fault(ErrorCode::Dep)));
    assert_eq!(guest.calls(), 0);
    assert_eq!(
        verification.settle(outcome.err()),
        Err(Disagreement::Fault(ErrorCode::Dep))
    );
    assert_eq!(verification.failover(), Some(ErrorCode::Dep));
}

#[test]
fn facts_that_differ_from_the_host_answer_are_a_contract_fault() {
    let (request, _) = fixture("absent-no-rows-medium");
    let (_, expected) = fixture("ok-low-pass");
    let guest = InProcessGuest::new(Behaviour::Honest);
    assert_eq!(
        verify_once(&guest, &request, &expected),
        Err(Disagreement::Fault(ErrorCode::Contract))
    );
}

// ---- the writer: the verifier inside the APPROVED transaction ---------------------------------------------------------------

/// A tasks database made by the Python `TaskStore`: one low-risk task `T-1` in `IN_REVIEW`, with no verdict rows.
const TEMPLATE_SCRIPT: &str = r#"
import sys
from swarm.taskstore import TaskStore
store = TaskStore(sys.argv[1])
store.create(task_id='T-1', correlation_id='C-1', capability='code.backend', risk_class='low', notes={})
store.conn.execute("UPDATE tasks SET state='IN_REVIEW' WHERE task_id='T-1'")
store.conn.commit()
store.conn.close()
"#;

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
        .env("SWARM_GSD_STATE_FILE", "")
        .env("SWARM_CLAUDE_STATE_DIR", dir.path())
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

/// A scratch repository holding its own copy of the template database.
struct Store {
    _dir: TempDir,
    db: PathBuf,
}

impl Store {
    fn new() -> Self {
        let dir = TempDir::new().expect("tempdir");
        let db = dir.path().join("tasks.db");
        fs::write(&db, &*TEMPLATE).expect("copy template");
        Self { _dir: dir, db }
    }

    fn state(&self) -> String {
        block(async {
            let mut conn = SqliteConnectOptions::new()
                .filename(&self.db)
                .connect()
                .await
                .expect("open the store");
            let row = sqlx::query("SELECT state FROM tasks WHERE task_id = 'T-1'")
                .fetch_one(&mut conn)
                .await
                .expect("the task row");
            row.get::<String, _>("state")
        })
    }

    fn bytes(&self) -> Vec<u8> {
        fs::read(&self.db).expect("read the store")
    }
}

/// A source whose environment is the dev-key opt-in and an empty home: the gate facts are read, with no signing key of any kind.
fn dev_key_source(home: &Path) -> FixedApprovalSource {
    let home = home.display().to_string();
    FixedApprovalSource::new(vec![
        ("SWARM_ALLOW_INSECURE_DEV_KEY".to_string(), "1".to_string()),
        ("HOME".to_string(), home.clone()),
        ("XDG_CONFIG_HOME".to_string(), home),
    ])
}

fn approve_request() -> Request {
    Request {
        task_id: "T-1".to_string(),
        to: "APPROVED".to_string(),
        actor: "A01".to_string(),
        reason: "manual".to_string(),
        manual: false,
    }
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("the clock is after the epoch")
        .as_secs_f64()
}

/// Runs one APPROVED transition of `T-1` on `store` with `verifier`, returning the writer's answer.
fn approve(store: &Store, verifier: Option<&dyn ApprovalVerifier>) -> Result<(), ApplyError> {
    let home = TempDir::new().expect("tempdir");
    let source = dev_key_source(home.path());
    block(async {
        let opened = open_for_write(&WriterConfig::new(store.db.clone()))
            .await
            .expect("open for write");
        let writer = TaskStoreWriter::with_approval(opened, Arc::new(source));
        let outcome = writer
            .transition_with_verifier(&approve_request(), now(), &HostDecider, verifier)
            .await;
        writer.close().await;
        outcome.map(drop)
    })
}

#[test]
fn a_spent_deadline_during_the_approval_check_rolls_back_and_writes_nothing() {
    let store = Store::new();
    let before = store.bytes();
    let guest = InProcessGuest::new(Behaviour::Spent);
    // The guest sleeps 50 ms; a 40 ms deadline is spent before its reply, so the call is E-TIMEOUT whatever stage the clock is in.
    let verifier = SandboxApproval::new(
        &guest,
        test_limits,
        Instant::now(),
        Duration::from_millis(40),
    );
    let error = approve(&store, Some(&verifier)).expect_err("a spent deadline ends the call");
    assert_eq!(error.code(), ErrorCode::Timeout);
    assert_eq!(error.write_state(), WriteState::Final);
    assert_eq!(error.cause(), &ApplyCause::Verification);
    assert_eq!(store.bytes(), before, "the store is byte-identical");
    assert_eq!(store.state(), "IN_REVIEW");
    assert_eq!(
        verifier.failover(),
        None,
        "a timeout is not recorded as a fault"
    );
}

#[test]
fn a_guest_fault_leaves_the_host_answer_standing_and_is_recorded() {
    let store = Store::new();
    let before = store.bytes();
    let guest = InProcessGuest::new(Behaviour::Fail(ErrorCode::Dep));
    let verifier =
        SandboxApproval::new(&guest, test_limits, Instant::now(), Duration::from_secs(30));
    let error = approve(&store, Some(&verifier)).expect_err("the host refuses: no passing gate");
    assert_eq!(error.code(), ErrorCode::Policy, "the host's own refusal");
    assert!(
        error.message().contains("lacks passing gates ['review']"),
        "{}",
        error.message()
    );
    assert_eq!(error.cause(), &ApplyCause::Refused);
    assert_eq!(guest.calls(), 1);
    assert_eq!(verifier.failover(), Some(ErrorCode::Dep));
    assert_eq!(store.bytes(), before, "the refusal is rolled back");
    assert_eq!(store.state(), "IN_REVIEW");
}

#[test]
fn an_agreeing_guest_leaves_no_failover() {
    let store = Store::new();
    let before = store.bytes();
    let guest = InProcessGuest::new(Behaviour::Honest);
    let verifier =
        SandboxApproval::new(&guest, test_limits, Instant::now(), Duration::from_secs(30));
    let error = approve(&store, Some(&verifier)).expect_err("the host refuses: no passing gate");
    assert_eq!(error.code(), ErrorCode::Policy);
    assert_eq!(guest.calls(), 1);
    assert_eq!(verifier.failover(), None);
    assert_eq!(store.bytes(), before);
}

#[test]
fn without_a_verifier_the_host_path_is_unchanged() {
    let store = Store::new();
    let error = approve(&store, None).expect_err("the host refuses: no passing gate");
    assert_eq!(error.code(), ErrorCode::Policy);
    assert_eq!(error.cause(), &ApplyCause::Refused);
    assert_eq!(store.state(), "IN_REVIEW");
}
