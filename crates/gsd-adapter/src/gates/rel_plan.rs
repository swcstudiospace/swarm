//! The host `rel_plan` route (phase 14.5, unit U3.6c).
//!
//! Serves a release evaluation whose required gates have no verdict rows yet (every required gate is `absent`),
//! plus the no-task path and the freeze file. A task that already has a verdict row is `E-DEP` `not_started`
//! before any write, because signature verification stays Python's until that check is ported. `--dry-run` stays
//! Python so the existing selector seam is unchanged. The default row stays Python.

use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use gsd_envelope::KeyMaterial;
use gsd_failure::ErrorCode;
use gsd_route::{is_safe_id, jstr, parse_json, to_python_json, Json, Reply, SeamError};
use gsd_taskwriter::{
    open_for_write, GateIssueRequest, GatePlan, SystemIds, TaskStoreWriter, WriteState,
    WriterConfig, WriterError,
};
use rustix::io::Errno;
use serde_json::Value;
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Row};

use crate::strangler_seam::{
    after_commit, before_any_write, canonical_or, env_value, outcome_unknown, resolve_swarm_dir,
    Invocation,
};

const GATES_LOW: &[&str] = &["review"];
const GATES_MEDIUM: &[&str] = &["review", "quality"];
const GATES_HIGH: &[&str] = &["review", "quality", "security", "release"];

struct Args {
    root: Option<String>,
    task_id: Option<String>,
    task_ids: Option<String>,
    correlation_id: Option<String>,
    release_id: Option<String>,
    freeze: Option<String>,
    unfreeze: bool,
}

/// True for the argv this route serves. `--dry-run` stays Python.
pub fn served(argv: &[String]) -> bool {
    parse(argv).is_some()
}

fn parse(argv: &[String]) -> Option<Args> {
    let mut out = Args {
        root: None,
        task_id: None,
        task_ids: None,
        correlation_id: None,
        release_id: None,
        freeze: None,
        unfreeze: false,
    };
    let mut json = false;
    let mut index = 0;
    while index < argv.len() {
        let token = argv[index].as_str();
        index += 1;
        if token == "--json" {
            json = true;
            continue;
        }
        if token == "--unfreeze" {
            out.unfreeze = true;
            continue;
        }
        if token == "--dry-run" || token == "--help" || token == "-h" {
            return None;
        }
        let (flag, inline) = token
            .split_once('=')
            .map(|(f, v)| (f, Some(v.to_string())))
            .unwrap_or((token, None));
        if !matches!(
            flag,
            "--root"
                | "--repo"
                | "--task-id"
                | "--task-ids"
                | "--correlation-id"
                | "--release-id"
                | "--freeze"
        ) {
            return None;
        }
        let value = match inline {
            Some(value) => value,
            None => {
                let next = argv.get(index)?;
                if next.starts_with('-') {
                    return None;
                }
                index += 1;
                next.clone()
            }
        };
        match flag {
            "--root" | "--repo" => out.root = Some(value),
            "--task-id" => out.task_id = Some(value),
            "--task-ids" => out.task_ids = Some(value),
            "--correlation-id" => out.correlation_id = Some(value),
            "--release-id" => out.release_id = Some(value),
            "--freeze" => out.freeze = Some(value),
            _ => return None,
        }
    }
    json.then_some(out)
}

fn utc_now() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let days = seconds.div_euclid(86_400);
    let rest = seconds.rem_euclid(86_400);
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rest / 3600,
        rest % 3600 / 60,
        rest % 60
    )
}

fn gates_for(risk: &str) -> &'static [&'static str] {
    match risk {
        "high" => GATES_HIGH,
        "medium" => GATES_MEDIUM,
        _ => GATES_LOW,
    }
}

fn guardrails(risk: &str) -> &'static str {
    match risk {
        "high" => "    \"error_rate_max\": 0.002,\n    \"p95_ms_max\": 250,\n    \"slo_burn_max\": 1.0,\n    \"soak_min\": 30",
        "medium" => "    \"error_rate_max\": 0.005,\n    \"p95_ms_max\": 320,\n    \"slo_burn_max\": 2.0,\n    \"soak_min\": 15",
        _ => "    \"error_rate_max\": 0.01,\n    \"p95_ms_max\": 500,\n    \"slo_burn_max\": 6.0,\n    \"soak_min\": 5",
    }
}

fn json_string(value: &str) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "\"\"".into())
}

fn plan_text(
    release_id: &str,
    risk: &str,
    tasks: &[String],
    gates: &[String],
    artifacts: &[String],
    created_at: &str,
    verdict: &str,
) -> String {
    let approval = if risk == "high" {
        "L4:human-four-eyes"
    } else {
        "L2:auto"
    };
    let g = guardrails(risk);
    let rate = match risk {
        "high" => "0.002",
        "medium" => "0.005",
        _ => "0.01",
    };
    let p95 = match risk {
        "high" => "250",
        "medium" => "320",
        _ => "500",
    };
    let burn = match risk {
        "high" => "1.0",
        "medium" => "2.0",
        _ => "6.0",
    };
    let tasks_json = serde_json::to_string(tasks).unwrap_or_else(|_| "[]".into());
    let gates_json = serde_json::to_string(gates).unwrap_or_else(|_| "[]".into());
    let artifacts_json = serde_json::to_string(artifacts).unwrap_or_else(|_| "[]".into());
    format!(
        "{{\n  \"kind\": \"release.plan\",\n  \"release_id\": {rid},\n  \"strategy\": \"canary\",\n  \"steps_pct\": [\n    5,\n    25,\n    50,\n    100\n  ],\n  \"guardrails\": {{\n{g}\n  }},\n  \"gates_required\": {gates},\n  \"risk_class\": {risk},\n  \"auto_rollback\": true,\n  \"approval\": {approval},\n  \"rollback_triggers\": [\n    \"error_rate > {rate} over 5m at any step\",\n    \"p95_latency_ms > {p95} over 5m at any step\",\n    \"slo_burn_rate > {burn}x (1h window)\",\n    \"deploy.telemetry verdict == rollback-suggested\",\n    \"incident.alert sev<=2 naming this release\",\n    \"monitoring.degraded broadcast (fail-closed: halt at current step)\"\n  ],\n  \"tasks\": {tasks},\n  \"artifacts\": {artifacts},\n  \"created_at\": {created},\n  \"gate_verdict\": {verdict}\n}}",
        rid = json_string(release_id),
        g = g,
        gates = gates_json.replace('[', "[\n    ").replace(", ", ",\n    ").replace(']', "\n  ]").replace("[\n    \n  ]", "[]"),
        risk = json_string(risk),
        approval = json_string(approval),
        rate = rate,
        p95 = p95,
        burn = burn,
        tasks = if tasks.is_empty() { "[]".into() } else { tasks_json.replace('[', "[\n    ").replace(", ", ",\n    ").replace(']', "\n  ]") },
        artifacts = if artifacts.is_empty() { "[]".into() } else { artifacts_json.replace('[', "[\n    ").replace(", ", ",\n    ").replace(']', "\n  ]") },
        created = json_string(created_at),
        verdict = json_string(verdict),
    )
}

fn finding(
    id: &str,
    severity: &str,
    kind: &str,
    summary: &str,
    evidence: &str,
    owner: &str,
) -> Json {
    Json::Obj(vec![
        ("id".into(), jstr(id)),
        ("severity".into(), jstr(severity)),
        ("kind".into(), jstr(kind)),
        ("summary".into(), jstr(summary)),
        ("evidence".into(), jstr(evidence)),
        ("ac_ref".into(), Json::Null),
        ("owner_suggestion".into(), jstr(owner)),
        ("location".into(), Json::Null),
    ])
}

/// Whether a freeze file is present. ENOENT, ENOTDIR and ELOOP mean the path names no file; any other stat error is
/// returned, so the caller reads the freeze as active rather than absent.
fn freeze_present(path: &Path) -> std::io::Result<bool> {
    match fs::metadata(path) {
        Ok(_) => Ok(true),
        Err(error) => match Errno::from_io_error(&error) {
            Some(errno)
                if errno == Errno::NOENT || errno == Errno::NOTDIR || errno == Errno::LOOP =>
            {
                Ok(false)
            }
            _ => Err(error),
        },
    }
}

/// The freeze record at `path`, or `None` when no freeze file is present. A file that cannot be statted is an active
/// freeze with an unreadable record.
fn read_freeze(path: &Path) -> Option<Json> {
    match freeze_present(path) {
        Ok(false) => None,
        Ok(true) => Some(
            fs::read_to_string(path)
                .ok()
                .and_then(|text| parse_json(&text).ok())
                .unwrap_or_else(unreadable_freeze),
        ),
        Err(_) => Some(unreadable_freeze()),
    }
}

fn unreadable_freeze() -> Json {
    Json::Obj(vec![
        ("reason".into(), jstr("unreadable freeze file")),
        ("frozen_at".into(), Json::Null),
        ("by".into(), jstr("unknown")),
    ])
}

fn freeze_field<'a>(record: &'a Json, key: &str) -> Option<&'a Json> {
    match record {
        Json::Obj(fields) => fields
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value),
        _ => None,
    }
}

/// Records one task's entry, replacing an earlier entry for the same id as Python's dict does.
fn put_task(tasks: &mut Vec<(String, Json)>, id: &str, entry: Json) {
    match tasks.iter_mut().find(|(known, _)| known == id) {
        Some(slot) => slot.1 = entry,
        None => tasks.push((id.to_string(), entry)),
    }
}

struct TaskRow {
    state: String,
    risk: String,
    notes: Value,
    outputs: Value,
}
async fn open_read(db: &Path) -> Result<sqlx::SqliteConnection, SeamError> {
    SqliteConnectOptions::new()
        .filename(db)
        .read_only(true)
        .connect()
        .await
        .map_err(|_| {
            before_any_write(SeamError::new(
                ErrorCode::Dep,
                "the store could not be opened; Python answers",
            ))
        })
}

async fn task_row(db: &Path, id: &str) -> Result<Option<TaskRow>, SeamError> {
    if !db.is_file() {
        return Ok(None);
    }
    let mut conn = open_read(db).await?;
    let row = sqlx::query("SELECT state, risk_class, notes, outputs FROM tasks WHERE task_id=?")
        .bind(id)
        .fetch_optional(&mut conn)
        .await
        .map_err(|_| {
            before_any_write(SeamError::new(
                ErrorCode::Dep,
                "the store could not be read; Python answers",
            ))
        })?;
    Ok(row.map(|row| {
        let notes: String = row.try_get(2).unwrap_or_else(|_| "{}".into());
        let outputs: String = row.try_get(3).unwrap_or_else(|_| "[]".into());
        TaskRow {
            state: row.try_get(0).unwrap_or_default(),
            risk: row.try_get(1).unwrap_or_else(|_| "low".into()),
            notes: serde_json::from_str(&notes).unwrap_or(Value::Object(Default::default())),
            outputs: serde_json::from_str(&outputs).unwrap_or(Value::Array(vec![])),
        }
    }))
}

async fn verdict_rows_exist(db: &Path, id: &str) -> Result<bool, SeamError> {
    if !db.is_file() {
        return Ok(false);
    }
    let mut conn = open_read(db).await?;
    let count: i64 = sqlx::query("SELECT count(*) FROM verdicts WHERE task_id=?")
        .bind(id)
        .fetch_one(&mut conn)
        .await
        .map(|row| row.try_get(0).unwrap_or(0))
        .unwrap_or(0);
    Ok(count > 0)
}

fn reply_of(body: Json, exit_code: i32) -> Reply {
    Reply {
        exit_code,
        stdout: format!("{}\n", to_python_json(&body)),
    }
}

/// Answers one served `rel_plan` call.
pub async fn answer(call: &Invocation) -> Result<Reply, SeamError> {
    let Some(args) = parse(&call.argv) else {
        return Err(before_any_write(SeamError::new(
            ErrorCode::Dep,
            "rel_plan argv is outside the host grammar; Python answers",
        )));
    };
    let root = canonical_or(&call.root.join(args.root.as_deref().unwrap_or(".")));
    let swarm = resolve_swarm_dir(&root, env_value(&call.env, "SWARM_DIR"));
    let db = swarm.join("tasks.db");
    let mut ids: Vec<String> = Vec::new();
    if let Some(task_id) = &args.task_id {
        ids.push(task_id.clone());
    }
    if let Some(extra) = &args.task_ids {
        ids.extend(
            extra
                .split(',')
                .filter(|item| !item.is_empty())
                .map(str::to_string),
        );
    }
    if ids.is_empty() {
        if args.correlation_id.is_some() {
            return Err(before_any_write(SeamError::new(
                ErrorCode::Dep,
                "a correlation listing is Python's",
            )));
        }
    } else {
        for id in &ids {
            if verdict_rows_exist(&db, id).await? {
                return Err(before_any_write(SeamError::new(
                    ErrorCode::Dep,
                    "a task with verdict rows is verified by Python",
                )));
            }
        }
    }
    fs::create_dir_all(&swarm).map_err(|_| {
        before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the swarm dir could not be created; Python answers",
        ))
    })?;
    let freeze_path = swarm.join("release.freeze");
    if let Some(reason) = &args.freeze {
        let rec = serde_json::json!({"reason": reason, "frozen_at": utc_now(), "by": "A12@local", "correlation_id": args.correlation_id});
        fs::write(
            &freeze_path,
            serde_json::to_string_pretty(&rec).unwrap_or_default(),
        )
        .map_err(|_| {
            before_any_write(SeamError::new(
                ErrorCode::Dep,
                "the freeze file could not be written; Python answers",
            ))
        })?;
    }
    if args.unfreeze && matches!(freeze_present(&freeze_path), Ok(true)) {
        fs::remove_file(&freeze_path).ok();
    }
    let frozen = read_freeze(&freeze_path);
    let mut findings = Vec::new();
    let mut risk = "low".to_string();
    let mut artifacts = Vec::new();
    let mut per_task = Vec::new();
    let mut tasks: Vec<(String, Json)> = Vec::new();
    if ids.is_empty() {
        findings.push(finding(
            "RF-000",
            "major",
            "input",
            "no task identified (pass --task-id/--task-ids/--correlation-id)",
            "",
            "A01",
        ));
    }
    for tid in &ids {
        match task_row(&db, tid).await? {
            None => {
                findings.push(finding(
                    &format!("RF-{:03}", findings.len() + 1),
                    "major",
                    "input",
                    &format!("unknown task {tid} in Task Store"),
                    "",
                    "A01",
                ));
                put_task(
                    &mut tasks,
                    tid,
                    Json::Obj(vec![
                        ("gates".into(), Json::Obj(vec![])),
                        ("verdicts".into(), Json::Obj(vec![])),
                        ("required".into(), Json::Arr(vec![])),
                        ("overall".into(), jstr("fail")),
                        ("problems".into(), Json::Arr(vec![jstr("task:unknown")])),
                    ]),
                );
            }
            Some(task) => {
                if task.notes.get("gate").is_some() {
                    return Err(before_any_write(SeamError::new(
                        ErrorCode::Dep,
                        "a gate task's targets are Python's",
                    )));
                }
                if matches!(task.risk.as_str(), "medium" | "high")
                    && (risk == "low" || (risk == "medium" && task.risk == "high"))
                {
                    risk = task.risk.clone();
                }
                let required: Vec<String> =
                    if let Some(list) = task.notes.get("gates").and_then(|v| v.as_array()) {
                        list.iter()
                            .filter_map(|v| v.as_str().map(str::to_string))
                            .filter(|g| g != "release")
                            .collect()
                    } else {
                        gates_for(&task.risk)
                            .iter()
                            .copied()
                            .filter(|g| *g != "release")
                            .map(str::to_string)
                            .collect()
                    };
                for gate in &required {
                    let owner = match gate.as_str() {
                        "review" => "A09",
                        "quality" => "A08",
                        "security" => "A10",
                        _ => "A01",
                    };
                    findings.push(finding(
                        &format!("RF-{:03}", findings.len() + 1),
                        "major",
                        "gate",
                        &format!("{tid}: {gate} gate absent"),
                        "",
                        owner,
                    ));
                }
                if let Some(outputs) = task.outputs.as_array() {
                    for item in outputs {
                        if item.get("kind").and_then(|v| v.as_str()) == Some("build.artifact") {
                            if let Some(uri) = item.get("uri").and_then(|v| v.as_str()) {
                                artifacts.push(uri.to_string());
                            }
                        }
                    }
                }
                if artifacts.is_empty() && task.risk != "low" {
                    findings.push(finding(&format!("RF-{:03}", findings.len() + 1), "minor", "provenance", &format!("{tid}: no build.artifact registered — A11 provenance required before promote"), "", "A11"));
                }
                let problems: Vec<String> = required
                    .iter()
                    .map(|gate| format!("{gate}:absent"))
                    .collect();
                let absent = Json::Obj(
                    required
                        .iter()
                        .map(|gate| (gate.clone(), jstr("absent")))
                        .collect(),
                );
                put_task(
                    &mut tasks,
                    tid,
                    Json::Obj(vec![
                        ("state".into(), jstr(&task.state)),
                        ("risk_class".into(), jstr(&task.risk)),
                        (
                            "required".into(),
                            Json::Arr(required.iter().map(|gate| jstr(gate)).collect()),
                        ),
                        ("gates".into(), absent.clone()),
                        ("verdicts".into(), absent),
                        (
                            "overall".into(),
                            jstr(if problems.is_empty() { "pass" } else { "fail" }),
                        ),
                        (
                            "problems".into(),
                            Json::Arr(problems.iter().map(|problem| jstr(problem)).collect()),
                        ),
                        ("expired".into(), Json::Arr(Vec::new())),
                    ]),
                );
                per_task.push((tid.clone(), task.state, task.risk, required));
            }
        }
    }
    if let Some(frozen) = &frozen {
        let reason = match freeze_field(frozen, "reason") {
            Some(Json::Str(text)) => text.as_str(),
            _ => "",
        };
        let since = freeze_field(frozen, "frozen_at")
            .map(to_python_json)
            .unwrap_or_else(|| "null".into());
        findings.push(finding(
            &format!("RF-{:03}", findings.len() + 1),
            "blocker",
            "freeze",
            &format!("deploy freeze active: {reason} (since {since})"),
            &freeze_path.display().to_string(),
            "A13",
        ));
    }
    let primary = ids.first().map(String::as_str).unwrap_or("T-unassigned");
    let release_id = args
        .release_id
        .clone()
        .unwrap_or_else(|| format!("REL-{primary}"));
    if !is_safe_id(&release_id) {
        return Err(before_any_write(SeamError::new(
            ErrorCode::Dep,
            "an unsafe release id is Python's",
        )));
    }
    let gates: Vec<String> = {
        let mut set: Vec<String> = per_task
            .iter()
            .flat_map(|(_, _, _, required)| required.clone())
            .collect();
        set.sort();
        set.dedup();
        if set.is_empty() {
            gates_for(&risk)
                .iter()
                .copied()
                .filter(|g| *g != "release")
                .map(str::to_string)
                .collect()
        } else {
            set
        }
    };
    let created = utc_now();
    let runs: Vec<(String, Json)> = gates
        .iter()
        .map(|gate| {
            let pass = per_task
                .iter()
                .all(|(_, _, _, required)| !required.iter().any(|item| item == gate));
            let state = if ids.is_empty() {
                "skipped:no-task"
            } else if pass {
                "pass"
            } else {
                "fail"
            };
            (gate.clone(), jstr(state))
        })
        .chain(std::iter::once((
            "freeze".into(),
            jstr(if frozen.is_some() { "active" } else { "none" }),
        )))
        .collect();
    let request = GateIssueRequest {
        gate: "release".into(),
        agent_id: "A12@local".into(),
        findings: findings.clone(),
        runs: runs.clone(),
        expires_s: 86400,
        extra: vec![
            ("release_id".into(), jstr(&release_id)),
            ("frozen".into(), Json::Bool(frozen.is_some())),
        ],
        simulate: false,
        per_target: Vec::new(),
        task_id: args.task_id.clone(),
        correlation_id: args.correlation_id.clone(),
        session: env_value(&call.env, "SWARM_AGENT_SESSION") == Some("1"),
        dryrun_fail: std::env::var("SWARM_DRYRUN_FAIL").unwrap_or_default(),
        now: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0),
    };
    let keys = KeyMaterial::from_env().map_err(|_| {
        before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the signing configuration is Python's",
        ))
    })?;
    let mut id_source = SystemIds::open().map_err(|_| {
        before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the id source could not be opened; Python answers",
        ))
    })?;
    let plan = if request.task_id.is_none() {
        gsd_taskwriter::derive_gate_issue(
            &request,
            &gsd_taskwriter::StoreFacts::default(),
            &keys,
            &mut id_source,
        )
        .map_err(|gsd_taskwriter::Decline(reason)| {
            before_any_write(SeamError::new(ErrorCode::Dep, reason))
        })?
    } else {
        let store = open_for_write(&WriterConfig::new(&db))
            .await
            .map_err(|error| before_any_write(error.to_seam_error()))?;
        TaskStoreWriter::new(store)
            .issue_gate(&request, &keys, &mut id_source)
            .await
            .map_err(|error| issue_gate_refusal(&error))?
    };
    if let Some(error) = &plan.error {
        return Err(error.clone());
    }
    let envelope = plan.envelope.clone().unwrap_or(Json::Obj(vec![]));
    let verdict = envelope_verdict(&envelope);
    let plan_body = plan_text(
        &release_id,
        &risk,
        &ids,
        &gates,
        &artifacts,
        &created,
        &verdict,
    );
    write_files(
        &swarm,
        &plan,
        committed(&request, &plan),
        &release_id,
        &plan_body,
    )?;
    let summary =
        format!(
        "release gate {} for {release_id} ({} task(s), risk={risk}{}) — canary [5, 25, 50, 100]",
        verdict.to_uppercase(), ids.len(), if frozen.is_some() { ", FROZEN" } else { "" }
    );
    let body = Json::Obj(vec![
        (
            "recorded".into(),
            Json::Arr(plan.recorded.iter().map(|(id, _)| jstr(id)).collect()),
        ),
        (
            "status".into(),
            jstr(if verdict == "pass" { "ok" } else { "fail" }),
        ),
        ("verdict".into(), jstr(&verdict)),
        ("frozen".into(), Json::Bool(frozen.is_some())),
        ("freeze".into(), frozen.clone().unwrap_or(Json::Null)),
        ("release_id".into(), jstr(&release_id)),
        ("tasks".into(), Json::Obj(tasks)),
        ("findings".into(), Json::Arr(findings)),
        ("summary".into(), jstr(&summary)),
        ("agent".into(), jstr("A12")),
        ("script".into(), jstr("rel_plan")),
    ]);
    let major = body_has_blocking(&body);
    Ok(reply_of(
        body,
        if verdict == "fail" || major { 1 } else { 0 },
    ))
}

fn envelope_verdict(envelope: &Json) -> String {
    let Json::Obj(fields) = envelope else {
        return "pass".into();
    };
    let Some(Json::Obj(payload)) = fields
        .iter()
        .find(|(name, _)| name == "payload")
        .map(|(_, value)| value)
    else {
        return "pass".into();
    };
    payload
        .iter()
        .find(|(name, _)| name == "verdict")
        .and_then(|(_, value)| match value {
            Json::Str(text) => Some(text.clone()),
            _ => None,
        })
        .unwrap_or_else(|| "pass".into())
}

fn body_has_blocking(body: &Json) -> bool {
    let Json::Obj(fields) = body else {
        return false;
    };
    let Some(Json::Arr(items)) = fields
        .iter()
        .find(|(name, _)| name == "findings")
        .map(|(_, value)| value)
    else {
        return false;
    };
    items.iter().any(|item| matches!(item, Json::Obj(fields) if fields.iter().any(|(name, value)| name == "severity" && matches!(value, Json::Str(text) if matches!(text.as_str(), "major" | "critical" | "blocker")))))
}

/// The refusal of a failed `issue_gate` call. Before its first write nothing was written and Python answers. A final failure is
/// a failed write or a failed commit, and the writer does not say which, so the verdict may be recorded: the outcome is unknown
/// and the call is not run again.
fn issue_gate_refusal(error: &WriterError) -> SeamError {
    match error.write_state() {
        WriteState::NotStarted => before_any_write(error.to_seam_error()),
        WriteState::Final => outcome_unknown(SeamError::new(
            ErrorCode::Internal,
            format!(
                "the gate verdict may or may not be recorded in the Task Store ({}); the outcome is unknown and the call is not run again",
                error.message()
            ),
        )),
    }
}

/// The commit condition of `issue_gate`: a store call (`task_id` given) whose plan has no error and writes rows. The no-task
/// path derives the plan without a store and commits nothing, so its writes are not recorded.
fn committed(request: &GateIssueRequest, plan: &GatePlan) -> bool {
    request.task_id.is_some() && plan.error.is_none() && !plan.writes.is_empty()
}

/// Writes the verdict file, its directory and the release plan file. A failure after the commit is final: the verdict is recorded
/// and must not be recorded again. Otherwise nothing was committed and the failure is not_started, so Python answers.
fn write_files(
    swarm: &Path,
    plan: &GatePlan,
    committed: bool,
    release_id: &str,
    plan_body: &str,
) -> Result<(), SeamError> {
    let refuse = |reason: &str| {
        if committed {
            after_commit(SeamError::new(
                ErrorCode::Internal,
                format!("the verdict is recorded in the Task Store, but {reason}; the outcome is final and is not run again"),
            ))
        } else {
            before_any_write(SeamError::new(
                ErrorCode::Dep,
                format!("{reason}; Python answers"),
            ))
        }
    };
    if plan.ensure_verdicts_dir {
        fs::create_dir_all(swarm.join("verdicts"))
            .map_err(|_| refuse("the verdicts directory could not be created"))?;
    }
    if let Some(file) = &plan.file {
        fs::write(swarm.join("verdicts").join(&file.name), &file.bytes)
            .map_err(|_| refuse("the verdict file could not be written"))?;
    }
    fs::create_dir_all(swarm.join("releases"))
        .map_err(|_| refuse("the releases directory could not be created"))?;
    fs::write(
        swarm
            .join("releases")
            .join(format!("{release_id}.plan.json")),
        plan_body,
    )
    .map_err(|_| refuse("the release plan file could not be written"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{answer, committed, issue_gate_refusal, parse, read_freeze, write_files};
    use crate::strangler_seam::Invocation;
    use gsd_envelope::{KeyFilePresence, KeyMaterial};
    use gsd_failure::ErrorCode;
    use gsd_route::{Json, SeamError};
    use gsd_taskwriter::gate_issue::VerdictRow;
    use gsd_taskwriter::{
        open_for_write, GateFile, GateIssueRequest, GatePlan, GateWrite, SystemIds,
        TaskStoreWriter, WriteState, WriterConfig,
    };
    use sqlx::sqlite::SqliteConnectOptions;
    use sqlx::ConnectOptions;
    use sqlx::Connection;
    use std::ffi::OsString;

    #[test]
    fn dry_run_is_left_to_python() {
        let argv = ["--dry-run".into(), "--json".into()];
        assert!(parse(&argv).is_none());
    }

    fn write_state(error: &SeamError) -> Option<&str> {
        error.details().iter().find_map(|(key, value)| match value {
            Json::Str(state) if key == "write_state" => Some(state.as_str()),
            _ => None,
        })
    }

    fn release_request() -> GateIssueRequest {
        GateIssueRequest {
            gate: "release".into(),
            agent_id: "A12".into(),
            findings: Vec::new(),
            runs: Vec::new(),
            expires_s: 3600,
            extra: Vec::new(),
            simulate: false,
            per_target: Vec::new(),
            task_id: Some("T-G".into()),
            correlation_id: Some("C-1".into()),
            session: false,
            dryrun_fail: String::new(),
            now: 1_700_000_000.0,
        }
    }

    /// A swarm store with a running release gate `T-G` over `T-1`, then the `extra` statements.
    async fn seeded_store(swarm: &std::path::Path, extra: &[&str]) -> std::path::PathBuf {
        let pool = crate::strangler_seam::open_store(swarm)
            .await
            .expect("schema");
        pool.close().await;
        let db = swarm.join("tasks.db");
        let mut conn = SqliteConnectOptions::new()
            .filename(&db)
            .connect()
            .await
            .expect("connect");
        let seed = [
            "INSERT INTO tasks (task_id, correlation_id, capability, state, notes) VALUES ('T-G', 'C-1', 'release', 'IN_PROGRESS', '{\"gate\": \"release\", \"gate_for\": [\"T-1\"]}')",
            "INSERT INTO tasks (task_id, correlation_id, capability, state) VALUES ('T-1', 'C-1', 'build', 'DONE')",
        ];
        for sql in seed.into_iter().chain(extra.iter().copied()) {
            sqlx::query(sql).execute(&mut conn).await.expect(sql);
        }
        conn.close().await.expect("close");
        db
    }

    /// A swarm store whose verdict inserts are refused: the writer's insert then fails after its first write, a final failure,
    /// as a failed commit would be.
    async fn store_refusing_verdicts(swarm: &std::path::Path) -> std::path::PathBuf {
        seeded_store(
            swarm,
            &["CREATE TRIGGER refuse_verdicts BEFORE INSERT ON verdicts BEGIN SELECT RAISE(ABORT, 'refused for the test'); END"],
        )
        .await
    }

    #[tokio::test]
    async fn a_final_failure_of_the_gate_writer_is_unknown() {
        let dir = tempfile::tempdir().expect("tempdir");
        let db = store_refusing_verdicts(dir.path()).await;
        let keys = KeyMaterial::from_lookup(
            |name| (name == "SWARM_ALLOW_INSECURE_DEV_KEY").then(|| OsString::from("1")),
            |_| KeyFilePresence::Absent,
        )
        .expect("key material");
        let mut ids = SystemIds::open().expect("id source");
        let store = open_for_write(&WriterConfig::new(&db))
            .await
            .expect("store");
        let writer = TaskStoreWriter::new(store);
        let error = writer
            .issue_gate(&release_request(), &keys, &mut ids)
            .await
            .expect_err("the verdict insert is refused");
        writer.close().await;
        assert_eq!(error.write_state(), WriteState::Final);
        let refusal = issue_gate_refusal(&error);
        assert_eq!(write_state(&refusal), Some("unknown"));
        assert_eq!(refusal.code(), ErrorCode::Internal);
    }

    #[tokio::test]
    async fn a_refusal_before_the_first_write_stays_not_started() {
        let dir = tempfile::tempdir().expect("tempdir");
        // No store file: the writer refuses before any write, and Python answers.
        let error = open_for_write(&WriterConfig::new(dir.path().join("tasks.db")))
            .await
            .expect_err("no store");
        let refusal = issue_gate_refusal(&error);
        assert_eq!(write_state(&refusal), Some("not_started"));
        assert_eq!(refusal.code(), ErrorCode::Dep);
    }

    #[tokio::test]
    async fn a_failed_verdict_write_after_the_commit_is_final() {
        let dir = tempfile::tempdir().expect("tempdir");
        let db = seeded_store(dir.path(), &[]).await;
        let keys = KeyMaterial::from_lookup(
            |name| (name == "SWARM_ALLOW_INSECURE_DEV_KEY").then(|| OsString::from("1")),
            |_| KeyFilePresence::Absent,
        )
        .expect("key material");
        let mut ids = SystemIds::open().expect("id source");
        let store = open_for_write(&WriterConfig::new(&db))
            .await
            .expect("store");
        let writer = TaskStoreWriter::new(store);
        let request = release_request();
        let plan = writer
            .issue_gate(&request, &keys, &mut ids)
            .await
            .expect("the verdict commits");
        writer.close().await;
        let commits = committed(&request, &plan);
        assert!(commits, "the store call commits this plan");
        // A regular file where the verdicts directory should be: the verdict write fails after the commit.
        std::fs::write(dir.path().join("verdicts"), "not a directory").expect("collide");
        let error =
            write_files(dir.path(), &plan, commits, "R-1", "{}").expect_err("the write fails");
        assert_eq!(write_state(&error), Some("final"));
        assert_eq!(error.code(), ErrorCode::Internal);
        assert!(
            error.message().contains("the verdict is recorded"),
            "{}",
            error.message()
        );
    }

    #[test]
    fn a_plan_without_a_store_call_commits_nothing() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("verdicts"), "not a directory").expect("collide");
        let mut request = release_request();
        request.task_id = None;
        let plan = GatePlan {
            ensure_verdicts_dir: true,
            writes: vec![GateWrite::Verdict(VerdictRow {
                task_id: "T-G".into(),
                gate: "release".into(),
                verdict: "pass".into(),
                agent_id: "A12".into(),
                findings: "[]".into(),
                expires_at: 1_700_003_600.0,
                ts: 1_700_000_000.0,
                envelope_json: "{}".into(),
                sig: String::new(),
            })],
            ..GatePlan::default()
        };
        let commits = committed(&request, &plan);
        assert!(!commits, "the no-task path writes no store rows");
        let error =
            write_files(dir.path(), &plan, commits, "R-1", "{}").expect_err("the write fails");
        assert_eq!(write_state(&error), Some("not_started"));
        assert_eq!(error.code(), ErrorCode::Dep);
    }

    #[test]
    fn a_verdict_write_that_lands_is_ok() {
        let dir = tempfile::tempdir().expect("tempdir");
        let plan = GatePlan {
            ensure_verdicts_dir: true,
            file: Some(GateFile {
                name: "T-G.release.json".into(),
                bytes: "{}".into(),
            }),
            ..GatePlan::default()
        };
        write_files(dir.path(), &plan, true, "R-1", "{}").expect("both files land");
        assert!(dir.path().join("verdicts/T-G.release.json").is_file());
        assert!(dir.path().join("releases/R-1.plan.json").is_file());
    }

    fn field<'a>(value: &'a Json, key: &str) -> Option<&'a Json> {
        let Json::Obj(fields) = value else {
            return None;
        };
        fields
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, item)| item)
    }

    const CHILD_OUT: &str = "GSD_REL_PLAN_CHILD_OUT";
    const CHILD_ROOT: &str = "GSD_REL_PLAN_CHILD_ROOT";
    const CHILD_SWARM: &str = "GSD_REL_PLAN_CHILD_SWARM";
    const CHILD_ARGV: &str = "GSD_REL_PLAN_CHILD_ARGV";
    const CHILD_TEST: &str = "gates::rel_plan::tests::child_answers_one_call";

    /// `answer` resolves its signing keys from the process environment, and this test process must not be written. The call
    /// therefore runs in a child test process whose own environment carries the insecure dev key and an empty config home.
    /// Returns the exit code and the parsed reply.
    fn answer_in_child(
        root: &std::path::Path,
        swarm: &std::path::Path,
        argv: &[&str],
    ) -> (i32, Json) {
        let out = root.join("child-reply.txt");
        let status = std::process::Command::new(std::env::current_exe().expect("test binary"))
            .args(["--exact", CHILD_TEST, "--nocapture", "--test-threads=1"])
            .env(CHILD_OUT, &out)
            .env(CHILD_ROOT, root)
            .env(CHILD_SWARM, swarm)
            .env(CHILD_ARGV, argv.join("\n"))
            .env("SWARM_ALLOW_INSECURE_DEV_KEY", "1")
            .env("XDG_CONFIG_HOME", root.join("xdg"))
            .env_remove("SWARM_ED25519_KEY")
            .env_remove("SWARM_SIGNING_KEY")
            .env_remove("SWARM_ED25519_KEY_FILE")
            .env_remove("SWARM_REQUIRE_KEY")
            .env_remove("SWARM_AGENT_SESSION")
            .env_remove("SWARM_DRYRUN_FAIL")
            .status()
            .expect("the child test process starts");
        assert!(
            status.success(),
            "the child test process failed: {status:?}"
        );
        let text = std::fs::read_to_string(&out).expect("the child wrote its reply");
        if let Some(reason) = text.strip_prefix("refused: ") {
            panic!("rel_plan refused the call: {reason}");
        }
        let (code, stdout) = text.split_once('\n').expect("an exit code line");
        let code: i32 = code.parse().expect("an integer exit code");
        let body = gsd_route::parse_json(stdout).expect("stdout is one JSON object");
        (code, body)
    }

    /// The child half of [`answer_in_child`]. It runs only when the parent names an output file, so the ordinary run passes it
    /// at once.
    #[test]
    fn child_answers_one_call() {
        let Some(out) = std::env::var_os(CHILD_OUT) else {
            return;
        };
        let var = |name: &str| std::env::var(name).expect("the parent sets the call");
        let call = Invocation {
            agent: "A12".into(),
            script: "rel_plan".into(),
            argv: var(CHILD_ARGV).split('\n').map(str::to_string).collect(),
            root: var(CHILD_ROOT).into(),
            env: vec![("SWARM_DIR".into(), var(CHILD_SWARM))],
            deadline: std::time::Duration::from_secs(60),
            effects_path: None,
        };
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        let text = match runtime.block_on(answer(&call)) {
            Ok(reply) => format!("{}\n{}", reply.exit_code, reply.stdout),
            Err(error) => format!("refused: {}", error.message()),
        };
        std::fs::write(out, text).expect("write the reply");
    }

    fn running_as_root() -> bool {
        use std::os::unix::fs::MetadataExt;
        std::fs::metadata("/proc/self")
            .map(|meta| meta.uid() == 0)
            .unwrap_or(false)
    }

    /// Restores a directory's mode when the test ends, so the tempdir can be removed.
    struct Reopen(std::path::PathBuf);

    impl Drop for Reopen {
        fn drop(&mut self) {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&self.0, std::fs::Permissions::from_mode(0o755));
        }
    }

    #[test]
    fn absent_freeze_file_is_no_freeze() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert!(read_freeze(&dir.path().join("release.freeze")).is_none());
    }

    #[test]
    fn freeze_path_under_a_regular_file_is_no_freeze() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("plain"), "not a directory").expect("write");
        assert!(read_freeze(&dir.path().join("plain").join("release.freeze")).is_none());
    }

    #[test]
    fn freeze_path_that_loops_is_no_freeze() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::os::unix::fs::symlink(dir.path().join("b"), dir.path().join("a")).expect("symlink");
        std::os::unix::fs::symlink(dir.path().join("a"), dir.path().join("b")).expect("symlink");
        assert!(read_freeze(&dir.path().join("a")).is_none());
    }

    /// ADG-07: a freeze file whose stat fails with EACCES is an active freeze, not an absent one. The freeze file is a symlink
    /// into a directory this process cannot search, so `.swarm` stays writable for the verdict files. A `.swarm` of mode 000
    /// would fail the verdict write itself, and no reply could carry the blocker.
    #[test]
    fn unstatable_freeze_file_is_an_active_freeze() {
        if running_as_root() {
            eprintln!("skipped: root searches a mode 000 directory, so EACCES cannot occur");
            return;
        }
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().expect("tempdir");
        let swarm = dir.path().join(".swarm");
        let locked = dir.path().join("locked");
        std::fs::create_dir_all(&swarm).expect("swarm dir");
        std::fs::create_dir_all(&locked).expect("locked dir");
        std::fs::write(
            locked.join("release.freeze.json"),
            r#"{"reason": "launch freeze", "frozen_at": null, "by": "A13"}"#,
        )
        .expect("freeze record");
        std::os::unix::fs::symlink(
            locked.join("release.freeze.json"),
            swarm.join("release.freeze"),
        )
        .expect("symlink");
        std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).expect("chmod");
        let _reopen = Reopen(locked.clone());
        let (code, body) = answer_in_child(dir.path(), &swarm, &["--json", "--root", "."]);
        let Some(Json::Arr(findings)) = field(&body, "findings") else {
            panic!("the reply carries a findings array: {body:?}");
        };
        let blocker = findings.iter().any(|item| {
            matches!(
                (field(item, "kind"), field(item, "severity")),
                (Some(Json::Str(kind)), Some(Json::Str(severity)))
                    if kind == "freeze" && severity == "blocker"
            )
        });
        assert!(
            blocker,
            "an unstatable freeze file must add a blocker freeze finding; findings: {findings:?}"
        );
        assert_ne!(code, 0, "an active freeze fails the verdict");
    }

    /// ADG-06: the reply carries the freeze record and one entry per task, as Python's reply does.
    #[tokio::test]
    async fn rel_plan_reply_carries_the_freeze_record_and_task_map() {
        let dir = tempfile::tempdir().expect("tempdir");
        let swarm = dir.path().join(".swarm");
        std::fs::create_dir_all(&swarm).expect("swarm dir");
        let _db = seeded_store(&swarm, &[]).await;
        let (_code, body) = answer_in_child(
            dir.path(),
            &swarm,
            &[
                "--json",
                "--root",
                ".",
                "--task-ids",
                "T-1",
                "--freeze",
                "launch freeze",
            ],
        );
        let freeze = field(&body, "freeze");
        assert!(
            matches!(freeze, Some(Json::Obj(_))),
            "an active freeze is reported as its record; freeze: {freeze:?}"
        );
        let tasks = field(&body, "tasks");
        assert!(
            matches!(tasks, Some(Json::Obj(entries)) if entries.iter().any(|(id, _)| id == "T-1")),
            "tasks maps each identified task; tasks: {tasks:?}"
        );
    }
}
