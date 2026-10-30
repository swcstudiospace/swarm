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
use gsd_route::{is_safe_id, jstr, to_python_json, Json, Reply, SeamError};
use gsd_taskwriter::{
    open_for_write, GateIssueRequest, SystemIds, TaskStoreWriter, WriteState, WriterConfig,
    WriterError,
};
use serde_json::Value;
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Row};

use crate::strangler_seam::{
    before_any_write, canonical_or, env_value, outcome_unknown, resolve_swarm_dir, Invocation,
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

fn read_freeze(path: &Path) -> Option<Value> {
    if !path.exists() {
        return None;
    }
    fs::read_to_string(path).ok().and_then(|text| serde_json::from_str(&text).ok())
        .or_else(|| Some(serde_json::json!({"reason": "unreadable freeze file", "frozen_at": null, "by": "unknown"})))
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
    if args.unfreeze && freeze_path.exists() {
        fs::remove_file(&freeze_path).ok();
    }
    let frozen = read_freeze(&freeze_path);
    let mut findings = Vec::new();
    let mut risk = "low".to_string();
    let mut artifacts = Vec::new();
    let mut per_task = Vec::new();
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
                per_task.push((tid.clone(), task.state, task.risk, required));
            }
        }
    }
    if let Some(frozen) = &frozen {
        let reason = frozen.get("reason").and_then(|v| v.as_str()).unwrap_or("");
        let since = frozen
            .get("frozen_at")
            .map(|v| v.to_string())
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
            (gate.clone(), jstr(if pass { "pass" } else { "fail" }))
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
    if plan.ensure_verdicts_dir {
        fs::create_dir_all(swarm.join("verdicts")).ok();
    }
    if let Some(file) = &plan.file {
        fs::write(swarm.join("verdicts").join(&file.name), &file.bytes).ok();
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
    fs::create_dir_all(swarm.join("releases")).ok();
    fs::write(
        swarm
            .join("releases")
            .join(format!("{release_id}.plan.json")),
        &plan_body,
    )
    .ok();
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
        ("freeze".into(), Json::Null),
        ("release_id".into(), jstr(&release_id)),
        ("tasks".into(), Json::Obj(vec![])),
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

#[cfg(test)]
mod tests {
    use super::{issue_gate_refusal, parse};
    use gsd_envelope::{KeyFilePresence, KeyMaterial};
    use gsd_failure::ErrorCode;
    use gsd_route::{Json, SeamError};
    use gsd_taskwriter::{
        open_for_write, GateIssueRequest, SystemIds, TaskStoreWriter, WriteState, WriterConfig,
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

    /// A swarm store with a running release gate `T-G` over `T-1`, and a trigger that refuses every verdict insert. The writer's
    /// insert then fails after its first write, a final failure, as a failed commit would be.
    async fn store_refusing_verdicts(swarm: &std::path::Path) -> std::path::PathBuf {
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
        for sql in [
            "INSERT INTO tasks (task_id, correlation_id, capability, state, notes) VALUES ('T-G', 'C-1', 'release', 'IN_PROGRESS', '{\"gate\": \"release\", \"gate_for\": [\"T-1\"]}')",
            "INSERT INTO tasks (task_id, correlation_id, capability, state) VALUES ('T-1', 'C-1', 'build', 'DONE')",
            "CREATE TRIGGER refuse_verdicts BEFORE INSERT ON verdicts BEGIN SELECT RAISE(ABORT, 'refused for the test'); END",
        ] {
            sqlx::query(sql).execute(&mut conn).await.expect(sql);
        }
        conn.close().await.expect("close");
        db
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
            .err()
            .expect("no store");
        let refusal = issue_gate_refusal(&error);
        assert_eq!(write_state(&refusal), Some("not_started"));
        assert_eq!(refusal.code(), ErrorCode::Dep);
    }
}
