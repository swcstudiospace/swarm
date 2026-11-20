//! Phase 9 row 16 (B9): on a mismatch between the Rust and the Python answer of a route call, the seam returns the Python
//! answer and records the mismatch (G-python-truth). The call, the Python reply, a deliberately different Rust reply and the
//! expected outcome are read from `tests/parity/fixtures/happy_path.00.json`.

use std::time::Duration;

use gsd_adapter::strangler_seam::{invoke_python_truth, CoreBackend, Invocation};
use gsd_route::{Reply, SeamError};
use serde_json::Value;

const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../tests/parity/fixtures/happy_path.00.json"
);

/// A backend that answers every entrypoint with one canned reply and touches nothing else.
struct Canned(Reply);

impl CoreBackend for Canned {
    fn swarm_run(&self, _: &Invocation) -> Result<Reply, SeamError> {
        Ok(self.0.clone())
    }
    fn orch_plan(&self, _: &Invocation) -> Result<Reply, SeamError> {
        Ok(self.0.clone())
    }
    fn orch_status(&self, _: &Invocation) -> Result<Reply, SeamError> {
        Ok(self.0.clone())
    }
    fn orch_assign(&self, _: &Invocation) -> Result<Reply, SeamError> {
        Ok(self.0.clone())
    }
    fn qa_gate(&self, _: &Invocation) -> Result<Reply, SeamError> {
        Ok(self.0.clone())
    }
    fn rev_gate(&self, _: &Invocation) -> Result<Reply, SeamError> {
        Ok(self.0.clone())
    }
    fn sec_gate(&self, _: &Invocation) -> Result<Reply, SeamError> {
        Ok(self.0.clone())
    }
    fn rel_plan(&self, _: &Invocation) -> Result<Reply, SeamError> {
        Ok(self.0.clone())
    }
    fn swarm_tool(&self, _: &Invocation) -> Result<Reply, SeamError> {
        Ok(self.0.clone())
    }
    fn lane_verb(&self, _: &Invocation) -> Result<Reply, SeamError> {
        Ok(self.0.clone())
    }
}

struct Fixture {
    argv: Vec<String>,
    expected_exit: i64,
    expected_json_keys: Vec<String>,
    python: Reply,
    rust: Reply,
    expected_returned: String,
    expected_mismatch_recorded: bool,
}

fn strings(value: &Value, field: &str) -> Vec<String> {
    value[field]
        .as_array()
        .unwrap_or_else(|| panic!("fixture field {field} is not an array"))
        .iter()
        .map(|item| {
            item.as_str()
                .unwrap_or_else(|| panic!("fixture field {field} holds a non-string"))
                .to_string()
        })
        .collect()
}

fn reply_of(value: &Value) -> Reply {
    Reply {
        exit_code: value["exit_code"]
            .as_i64()
            .expect("reply.exit_code is an integer") as i32,
        stdout: value["stdout"]
            .as_str()
            .expect("reply.stdout is a string")
            .to_string(),
    }
}

fn load_fixture() -> Fixture {
    let text = std::fs::read_to_string(FIXTURE).unwrap_or_else(|e| panic!("read {FIXTURE}: {e}"));
    let value: Value =
        serde_json::from_str(&text).unwrap_or_else(|e| panic!("parse {FIXTURE}: {e}"));
    Fixture {
        argv: strings(&value, "argv"),
        expected_exit: value["expected_exit"]
            .as_i64()
            .expect("expected_exit is an integer"),
        expected_json_keys: strings(&value, "expected_json_keys"),
        python: reply_of(&value["python"]),
        rust: reply_of(&value["rust"]),
        expected_returned: value["expected"]["returned"]
            .as_str()
            .expect("expected.returned is a string")
            .to_string(),
        expected_mismatch_recorded: value["expected"]["mismatch_recorded"]
            .as_bool()
            .expect("expected.mismatch_recorded is a boolean"),
    }
}

/// The seam call of the fixture: `argv[0]` names the script, the rest are its flags.
fn invocation(fixture: &Fixture) -> Invocation {
    let (script, flags) = fixture
        .argv
        .split_first()
        .expect("fixture argv is not empty");
    let script = script
        .strip_suffix(".py")
        .expect("fixture argv[0] names a .py script");
    Invocation {
        agent: "A01".to_string(),
        script: script.to_string(),
        argv: flags.to_vec(),
        root: std::env::temp_dir(),
        env: Vec::new(),
        deadline: Duration::from_secs(60),
        effects_path: None,
    }
}

#[test]
fn mismatch_returns_python_reply_and_records_the_difference() {
    let fixture = load_fixture();
    assert_ne!(
        fixture.python, fixture.rust,
        "the fixture must hold two different replies"
    );
    let call = invocation(&fixture);
    let python = Canned(fixture.python.clone());
    let rust = Canned(fixture.rust.clone());

    let (reply, mismatch) = invoke_python_truth(&python, &rust, &call);

    // The fixture's own sanity: its exit code and keys describe its Python reply.
    assert_eq!(i64::from(fixture.python.exit_code), fixture.expected_exit);
    let python_json: Value =
        serde_json::from_str(&fixture.python.stdout).expect("Python stdout is JSON");
    let mut keys: Vec<String> = python_json
        .as_object()
        .expect("Python stdout is a JSON object")
        .keys()
        .cloned()
        .collect();
    let mut expected_keys = fixture.expected_json_keys.clone();
    keys.sort();
    expected_keys.sort();
    assert_eq!(keys, expected_keys);

    // The outcome the fixture expects: Python's reply is returned, and the mismatch is recorded.
    assert_eq!(fixture.expected_returned, "python");
    assert_eq!(reply, fixture.python, "the Python answer is returned");
    assert_eq!(mismatch.is_some(), fixture.expected_mismatch_recorded);
    let record = mismatch.expect("a differing Rust reply is recorded");
    assert_eq!(record.script, "orch_status");
    assert_eq!(record.argv, call.argv);
    assert_eq!(record.python, fixture.python);
    assert_eq!(record.rust, fixture.rust);
}

#[test]
fn agreeing_engines_return_python_reply_and_record_nothing() {
    let fixture = load_fixture();
    let call = invocation(&fixture);
    let python = Canned(fixture.python.clone());
    let rust = Canned(fixture.python.clone());

    let (reply, mismatch) = invoke_python_truth(&python, &rust, &call);

    assert_eq!(reply, fixture.python);
    assert!(mismatch.is_none(), "identical answers record no mismatch");
}
