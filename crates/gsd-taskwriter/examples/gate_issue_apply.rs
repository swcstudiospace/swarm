//! Runs `TaskStoreWriter::issue_gate` for a script: `gate_issue_apply <tasks.db> < request.json`, so that Python can check the rows the
//! Rust signer wrote (`tests/test_gate_signing_keys.py`) with its own `verify_envelope` and `TaskStore.missing_gates`.
//!
//! The request is the wire form of `GateIssueRequest::from_json` (it carries `now`). The signing keys come from this process's
//! environment (`KeyMaterial::from_env`), with random ids from `/dev/urandom`. The verdict rows and `notes.feedback` rewrites are
//! committed to the given store in one transaction; the plan (file, events, envelopes, error) is printed as one JSON line, and the
//! verdict file is written by this helper to `$GATE_ISSUE_OUT_DIR/<name>` when that variable is set.
//!
//! Exit codes: 0 the plan is printed; 3 declined or not started (stderr says why, never with a key); 2 usage or an unreadable request.

use std::io::Read;
use std::process::ExitCode;

use gsd_envelope::KeyMaterial;
use gsd_route::parse_json;
use gsd_route::pydumps::to_python_dumps;
use gsd_taskwriter::{open_for_write, GateIssueRequest, SystemIds, TaskStoreWriter, WriterConfig};

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let Some(db_path) = std::env::args().nth(1) else {
        eprintln!("usage: gate_issue_apply <tasks.db> < request.json");
        return ExitCode::from(2);
    };
    let mut text = String::new();
    if std::io::stdin().read_to_string(&mut text).is_err() {
        eprintln!("gate_issue_apply: stdin is not UTF-8 text");
        return ExitCode::from(2);
    }
    let Ok(value) = parse_json(&text) else {
        eprintln!("gate_issue_apply: the request is not JSON");
        return ExitCode::from(2);
    };
    let request = match GateIssueRequest::from_json(&value) {
        Ok(request) => request,
        Err(declined) => {
            eprintln!("{declined}");
            return ExitCode::from(3);
        }
    };
    let keys = match KeyMaterial::from_env() {
        Ok(keys) => keys,
        Err(declined) => {
            eprintln!("gate_issue_apply: declined: {declined}");
            return ExitCode::from(3);
        }
    };
    let mut ids = match SystemIds::open() {
        Ok(ids) => ids,
        Err(declined) => {
            eprintln!("{declined}");
            return ExitCode::from(3);
        }
    };
    let store = match open_for_write(&WriterConfig::new(db_path)).await {
        Ok(store) => store,
        Err(error) => {
            eprintln!("gate_issue_apply: {error}");
            return ExitCode::from(3);
        }
    };
    let writer = TaskStoreWriter::new(store);
    let outcome = writer.issue_gate(&request, &keys, &mut ids).await;
    writer.close().await;
    match outcome {
        Ok(plan) => {
            if let (Some(file), Some(dir)) = (&plan.file, std::env::var_os("GATE_ISSUE_OUT_DIR")) {
                let path = std::path::Path::new(&dir).join(&file.name);
                if std::fs::write(path, &file.bytes).is_err() {
                    eprintln!("gate_issue_apply: the verdict file cannot be written");
                    return ExitCode::from(2);
                }
            }
            println!("{}", to_python_dumps(&plan.to_json()));
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!(
                "gate_issue_apply: {error} ({})",
                error.write_state().as_str()
            );
            ExitCode::from(3)
        }
    }
}
