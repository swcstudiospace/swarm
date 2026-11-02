//! A Task Store whose verdict rows are signed by the REAL Python `make_verdict` (phase 14.5, unit U3.4d): shared by the adapter tests that
//! need a transition to APPROVED to reach the key-reading path. Python signs, the Rust host verifies. Keys are the caller's: they travel
//! in the environment of the one Python process that signs and are written nowhere.
//!
//! The task `T-1` (correlation `C-1`, risk class `medium`, so the gates are `review` and `quality`) ends in `IN_REVIEW` with the rows of
//! the scenario recorded on it. The scenario is a JSON object: `verdicts` (a list of `[gate, verdict]`), optional `notes`, `risk`,
//! `issuer` (the state of a gate task `T-g` that the rows name as their `gate_task`), `late` (a `[gate, verdict]` row that is signed and
//! returned but not recorded), `expires_s` (how many seconds the signed verdicts live; default one day) and `surrogate` (a lone
//! surrogate escape in the first stored envelope). Not every test uses every member.
#![allow(dead_code)]

use std::path::Path;
use std::process::Command;

use gsd_route::{obj_get, parse_json, Json};

/// Builds the store at `argv[2]` from the scenario in `argv[1]` and prints `{"late": <a verdict row, not inserted>}`.
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
                        root=spec['scratch'], audit=False, expires_s=spec.get('expires_s', 86400))

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
store.conn.commit()
store.conn.close()
print(json.dumps({'late': late}))
"#;

/// A verdict row that was signed but not recorded: the columns of `verdicts` as `record_verdict` writes them.
pub struct LateRow {
    pub gate: String,
    pub verdict: String,
    pub findings: String,
    pub expires_at: f64,
    pub ts: f64,
    pub envelope_json: String,
    pub sig: String,
}

fn text_of(value: &Json, key: &str) -> String {
    match value {
        Json::Obj(fields) => match obj_get(fields, key) {
            Some(Json::Str(text)) => text.clone(),
            other => panic!("{key} is not text: {other:?}"),
        },
        other => panic!("not an object: {other:?}"),
    }
}

fn number_of(value: &Json, key: &str) -> f64 {
    match value {
        Json::Obj(fields) => match obj_get(fields, key) {
            Some(Json::Float(number)) => *number,
            Some(Json::Int(number)) => *number as f64,
            other => panic!("{key} is not a number: {other:?}"),
        },
        other => panic!("not an object: {other:?}"),
    }
}

/// Builds the store at `db` from `spec` with the rows signed under `sign` (the whole key environment of the signing). `scratch` is an
/// empty directory that takes the run-log events of the signing. Returns the late row when the scenario made one.
pub fn seed_into(
    repo_root: &Path,
    db: &Path,
    scratch: &Path,
    spec: &str,
    sign: &[(String, String)],
) -> Option<LateRow> {
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
        .arg(db)
        .current_dir(repo_root)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", scratch)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("SWARM_PURE_PYTHON", "1")
        .env("SWARM_DIR", scratch)
        .env("SWARM_GSD_STATE_FILE", "")
        .env("SWARM_CLAUDE_STATE_DIR", scratch.join("claude-state"));
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
        parse_json(String::from_utf8_lossy(&output.stdout).trim()).expect("the seed prints JSON");
    match &printed {
        Json::Obj(fields) => match obj_get(fields, "late") {
            Some(row @ Json::Obj(_)) => Some(LateRow {
                gate: text_of(row, "gate"),
                verdict: text_of(row, "verdict"),
                findings: text_of(row, "findings"),
                expires_at: number_of(row, "expires_at"),
                ts: number_of(row, "ts"),
                envelope_json: text_of(row, "envelope_json"),
                sig: text_of(row, "sig"),
            }),
            _ => None,
        },
        other => panic!("seed output is not an object: {other:?}"),
    }
}
