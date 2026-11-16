//! Read-route budget record (U12.8): a test-only generator for the `orch_status` list snapshot.
//!
//! The test builds a 500-task snapshot and measures the guest boundary under an 8 MiB memory cap:
//! cold instantiate, host marshal, warm invoke, unmarshal, fuel per call, and a 1000-call run on
//! one instance. The figures go to `target/gsd-sandbox/read_route_budget.json` together with a
//! hardware fingerprint and `/proc/loadavg` read at measurement time. The only assertion is
//! correctness: every call succeeds and every reply decodes. No time threshold is set.

use std::fs;
use std::path::PathBuf;
use std::time::Instant;

use gsd_failure::ErrorCode;
use gsd_route::decode_reply_wire;
use gsd_sandbox::wasmtime_module_build::{describe, MODULE_NAME};
use gsd_sandbox::{GuestInstance, GuestInvokeError, GuestLoader, Limits, LoadedGuest};
use serde_json::{json, Value};

const GUEST: &[u8] = include_bytes!(env!("GSD_GUEST_WASM"));
const SEMVER: &str = "0.1.0";
/// Tasks in the snapshot.
const TASKS: usize = 500;
/// Cold instantiates timed.
const COLD_SAMPLES: usize = 20;
/// Marshal, warm invoke and unmarshal samples.
const WARM_SAMPLES: usize = 300;
/// Consecutive calls on one instance in the dealloc check.
const DEALLOC_CALLS: usize = 1000;
/// Linear memory cap for every measured call.
const MEMORY_BYTES: usize = 8 * 1024 * 1024;
/// Fuel for every measured call. Fuel per call is found below this ceiling.
const FUEL_CEILING: u64 = 1_000_000_000;
/// Output path, relative to this crate's directory (the workspace `target/`).
const OUTPUT: &str = "../../target/gsd-sandbox/read_route_budget.json";

fn limits(fuel: u64) -> Limits {
    Limits {
        fuel,
        memory_bytes: MEMORY_BYTES,
        deadline_ticks: None,
    }
}

fn instantiate(loader: &GuestLoader, guest: &LoadedGuest, fuel: u64) -> GuestInstance {
    loader
        .instantiate(guest, &limits(fuel))
        .expect("the verified guest instantiates with no imports")
}

/// The `orch_status` list request for `TASKS` tasks. The tasks form a binary-tree DAG: task `i`
/// depends on task `(i - 1) / 2`. Every third task is in review and holds one passing verdict.
fn snapshot() -> Value {
    let mut tasks = Vec::with_capacity(TASKS);
    let mut verdicts = Vec::new();
    for index in 0..TASKS {
        let task_id = format!("T-{index:04}");
        let in_review = index % 3 == 0;
        let state = if in_review { "IN_REVIEW" } else { "DONE" };
        let depends_on = if index == 0 {
            "[]".to_string()
        } else {
            format!("[\"T-{:04}\"]", (index - 1) / 2)
        };
        tasks.push(json!({
            "task_id": task_id,
            "state": state,
            "agent_id": "A05",
            "dag_depth": (index + 1).ilog2(),
            "attempt": 0,
            "rework_loops": 0,
            "risk_class": "low",
            "depends_on": depends_on,
            "notes": "{}",
            "outputs": "[]",
        }));
        if in_review {
            verdicts.push(json!({
                "task_id": task_id,
                "rows": [{
                    "id": index + 1,
                    "task_id": task_id,
                    "gate": "review",
                    "verdict": "pass",
                    "findings": "[]",
                    "ts": 1.5,
                    "envelope_json": null,
                }],
            }));
        }
    }
    json!({
        "mode": "list",
        "agent": "A01",
        "script": "orch_status",
        "correlation": null,
        "db_path": "/x/tasks.db",
        "swarm_dir": "/x",
        "events": TASKS,
        "tasks": tasks,
        "verdicts": verdicts,
        "history": null,
    })
}

fn millis(started: Instant) -> f64 {
    started.elapsed().as_secs_f64() * 1000.0
}

/// Median, p95, min and max of `samples` in milliseconds. Percentiles are nearest-rank.
fn summary(mut samples: Vec<f64>) -> Value {
    samples.sort_by(f64::total_cmp);
    let count = samples.len();
    let rank = |pct: usize| samples[(pct * count).div_ceil(100).max(1) - 1];
    json!({
        "n": count,
        "median_ms": rank(50),
        "p95_ms": rank(95),
        "min_ms": samples[0],
        "max_ms": samples[count - 1],
    })
}

/// Runs one call on a fresh instance under `fuel`. `true` when it completes, `false` when the
/// guest runs out of fuel. Any other failure is a test failure.
fn completes_under(loader: &GuestLoader, guest: &LoadedGuest, request: &[u8], fuel: u64) -> bool {
    let mut instance = instantiate(loader, guest, fuel);
    match instance.invoke(request) {
        Ok(_) => true,
        Err(GuestInvokeError::Trap(ErrorCode::Internal)) => false,
        Err(err) => panic!(
            "the call failed with {:?}, not fuel exhaustion",
            err.error_code()
        ),
    }
}

/// The fewest fuel units that complete one call, by bisection. Fuel use for a fixed request is
/// deterministic, so this is the call's fuel cost.
fn fuel_per_call(loader: &GuestLoader, guest: &LoadedGuest, request: &[u8]) -> u64 {
    assert!(
        completes_under(loader, guest, request, FUEL_CEILING),
        "the fuel ceiling covers one call"
    );
    // Invariant: `lacks` does not complete (0 is never probed, as fuel must be at least 1) and
    // `fits` completes.
    let (mut lacks, mut fits) = (0u64, FUEL_CEILING);
    while fits - lacks > 1 {
        let middle = lacks + (fits - lacks) / 2;
        if completes_under(loader, guest, request, middle) {
            fits = middle;
        } else {
            lacks = middle;
        }
    }
    fits
}

/// The CPU model and logical CPU count from `/proc/cpuinfo`. A read failure is recorded as its
/// text.
fn cpu_fingerprint() -> (Value, Value) {
    match fs::read_to_string("/proc/cpuinfo") {
        Ok(text) => {
            let model = text
                .lines()
                .find_map(|line| {
                    let (key, value) = line.split_once(':')?;
                    (key.trim() == "model name").then(|| value.trim().to_string())
                })
                .unwrap_or_else(|| "model name not listed".to_string());
            let processors = text
                .lines()
                .filter(|line| {
                    line.split_once(':')
                        .is_some_and(|(key, _)| key.trim() == "processor")
                })
                .count();
            (json!(model), json!(processors))
        }
        Err(err) => (json!(err.to_string()), json!(err.to_string())),
    }
}

/// The trimmed text of a `/proc` file, or the read failure's text when it cannot be read.
fn read_text(path: &str) -> Value {
    match fs::read_to_string(path) {
        Ok(text) => json!(text.trim()),
        Err(err) => json!(err.to_string()),
    }
}

fn write_record(record: &Value) {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(OUTPUT);
    let parent = path
        .parent()
        .expect("the output path has a parent directory");
    fs::create_dir_all(parent).expect("the target directory can be created");
    let text = serde_json::to_string_pretty(record).expect("the record serialises");
    fs::write(&path, format!("{text}\n")).expect("the record file can be written");
}

#[test]
fn read_route_budget_record() {
    let loader = GuestLoader::new().expect("the loader builds");
    let manifest = describe(GUEST, MODULE_NAME, SEMVER);
    let compiled = Instant::now();
    let guest = loader
        .load_verified(GUEST, &manifest, SEMVER)
        .expect("the built guest verifies and compiles");
    let compile_ms = millis(compiled);

    let loadavg_start = read_text("/proc/loadavg");
    let request_value = snapshot();

    let mut cold = Vec::with_capacity(COLD_SAMPLES);
    for _ in 0..COLD_SAMPLES {
        let started = Instant::now();
        let _instance = instantiate(&loader, &guest, FUEL_CEILING);
        cold.push(millis(started));
    }

    let request = serde_json::to_vec(&request_value).expect("the snapshot serialises");
    let mut marshal = Vec::with_capacity(WARM_SAMPLES);
    for _ in 0..WARM_SAMPLES {
        let started = Instant::now();
        let bytes = serde_json::to_vec(&request_value).expect("the snapshot serialises");
        marshal.push(millis(started));
        assert_eq!(
            bytes, request,
            "marshalling the same snapshot is deterministic"
        );
    }

    let mut warm_instance = instantiate(&loader, &guest, FUEL_CEILING);
    // The first call sets up the instance's lazy state, so it is not sampled.
    warm_instance
        .invoke(&request)
        .expect("the warm-up call succeeds");
    let mut warm = Vec::with_capacity(WARM_SAMPLES);
    let mut replies = Vec::with_capacity(WARM_SAMPLES);
    for index in 0..WARM_SAMPLES {
        let started = Instant::now();
        match warm_instance.invoke(&request) {
            Ok(reply) => {
                warm.push(millis(started));
                replies.push(reply);
            }
            Err(err) => panic!("warm call {index} failed with {:?}", err.error_code()),
        }
    }

    let mut unmarshal = Vec::with_capacity(WARM_SAMPLES);
    for (index, reply) in replies.iter().enumerate() {
        let started = Instant::now();
        let decoded = decode_reply_wire(reply);
        unmarshal.push(millis(started));
        assert!(decoded.is_ok(), "reply {index} does not decode");
    }

    let fuel = fuel_per_call(&loader, &guest, &request);

    let mut dealloc_instance = instantiate(&loader, &guest, FUEL_CEILING);
    let dealloc_started = Instant::now();
    let mut calls_ok = 0usize;
    for index in 0..DEALLOC_CALLS {
        match dealloc_instance.invoke(&request) {
            Ok(_) => calls_ok += 1,
            Err(err) => panic!("dealloc call {index} failed with {:?}", err.error_code()),
        }
    }
    let dealloc_ms = millis(dealloc_started);
    let loadavg_end = read_text("/proc/loadavg");

    let (cpu_model, logical_cpus) = cpu_fingerprint();
    let record = json!({
        "arch": std::env::consts::ARCH,
        "os": std::env::consts::OS,
        "cpu_model": cpu_model,
        "logical_cpus": logical_cpus,
        "loadavg_start": loadavg_start,
        "loadavg_end": loadavg_end,
        "tasks": TASKS,
        "request_bytes": request.len(),
        "reply_bytes": replies[0].len(),
        "limits": {
            "fuel": FUEL_CEILING,
            "memory_bytes": MEMORY_BYTES,
            "deadline_ticks": null,
        },
        "verify_and_compile_ms": compile_ms,
        "cold_instantiate_ms": summary(cold),
        "host_marshal_ms": summary(marshal),
        "warm_invoke_ms": summary(warm),
        "unmarshal_ms": summary(unmarshal),
        "fuel_per_call": fuel,
        "dealloc_check": {
            "calls": DEALLOC_CALLS,
            "calls_ok": calls_ok,
            "memory_bytes": MEMORY_BYTES,
            "total_ms": dealloc_ms,
        },
    });
    write_record(&record);
}
