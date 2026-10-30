//! The host `orch_assign` verify route (phase 14.5, unit U3.7a).
//!
//! Serves the policy, identifier, key, store and manifest refusals, and a signed assignment whose
//! prompt needs no upstream task and no substrate memory. A task with dependencies, or a live
//! substrate URL, is `E-DEP` `not_started` before any read of those sources, so Python answers.
//! `--dry-run` is a served refusal. The default row stays Python.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use gsd_envelope::{parse_envelope, sign_envelope, verify_envelope, KeyMaterial};
use gsd_failure::ErrorCode;
use gsd_route::{is_safe_id, to_python_json, Reply, SeamError};
use serde_json::{Map, Value};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Row};

use crate::strangler_seam::{
    before_any_write, canonical_or, env_value, resolve_swarm_dir, Invocation,
};

struct Args {
    root: String,
    task_id: Option<String>,
    correlation_id: Option<String>,
    dry_run: bool,
}

/// True for the argv this route serves.
pub fn served(argv: &[String]) -> bool {
    parse(argv).is_some()
}

fn parse(argv: &[String]) -> Option<Args> {
    if !argv.iter().any(|token| token == "--json") {
        return None;
    }
    let mut root = ".".to_string();
    let mut task_id = None;
    let mut correlation_id = None;
    let mut dry_run = false;
    let mut index = 0;
    while index < argv.len() {
        let token = &argv[index];
        if token == "--json" || token == "--dry-run" {
            dry_run |= token == "--dry-run";
            index += 1;
            continue;
        }
        let (name, attached) = token
            .split_once('=')
            .map(|(n, v)| (n, Some(v.to_string())))
            .unwrap_or((token.as_str(), None));
        let known = matches!(name, "--root" | "--task-id" | "--correlation-id");
        if !known {
            return None;
        }
        let value = match attached {
            Some(value) => value,
            None => {
                index += 1;
                argv.get(index)?.clone()
            }
        };
        match name {
            "--root" => root = value,
            "--task-id" => task_id = Some(value),
            "--correlation-id" => correlation_id = Some(value),
            _ => return None,
        }
        index += 1;
    }
    Some(Args {
        root,
        task_id,
        correlation_id,
        dry_run,
    })
}

fn refusal(code: &str, message: &str) -> Reply {
    let handling = match code {
        "E-INPUT" => "Nack + task.status FAILED; notify artifact producer",
        "E-CONTRACT" => "Bounded fix loop per agent spec",
        "E-POLICY" => "Fail-closed; escalate to A10/A01",
        _ => "Exponential backoff 1s→60s (jitter, max 5), then degraded mode",
    };
    let body = serde_json::json!({
        "agent": "A01",
        "script": "orch_assign",
        "status": "error",
        "error": {
            "code": code,
            "message": format!("{code}: {message}"),
            "details": {},
            "handling": handling
        }
    });
    Reply {
        exit_code: 2,
        stdout: format!(
            "{}\n",
            serde_json::to_string_pretty(&body).unwrap_or_default()
        ),
    }
}

fn required_id(value: Option<&str>, what: &str) -> Result<String, Reply> {
    match value {
        None => Err(refusal("E-INPUT", &format!("{what} is required"))),
        Some(text) if is_safe_id(text) => Ok(text.to_string()),
        Some(_) => Err(refusal("E-INPUT", &format!("invalid {what}"))),
    }
}

fn env_flag(name: &str) -> bool {
    std::env::var(name).ok().as_deref() == Some("1")
}

fn substrate_on() -> bool {
    if std::env::var("SUBSTRATE_DISABLED")
        .ok()
        .as_deref()
        .map(str::trim)
        == Some("1")
    {
        return false;
    }
    std::env::var("SUBSTRATE_URL")
        .ok()
        .map(|url| {
            let url = url.trim().trim_end_matches('/');
            url.to_ascii_lowercase().starts_with("http://")
                || url.to_ascii_lowercase().starts_with("https://")
        })
        .unwrap_or(false)
}

struct TaskRow {
    state: String,
    correlation_id: String,
    attempt: i64,
    agent_id: String,
    capability: String,
    title: String,
    budget: Value,
    inputs: Value,
    acceptance: Value,
    risk_class: String,
    priority: String,
    rework_loops: i64,
    notes: Map<String, Value>,
    depends_on: Vec<Value>,
}

async fn read_task(db: &Path, id: &str) -> Result<Option<TaskRow>, Reply> {
    let mut conn = SqliteConnectOptions::new()
        .filename(db)
        .read_only(true)
        .connect()
        .await
        .map_err(|_| refusal("E-INPUT", "existing task store is unavailable"))?;
    let row = sqlx::query(
        "SELECT state, correlation_id, attempt, agent_id, capability, title, budget, inputs, acceptance, risk_class, priority, rework_loops, notes, depends_on FROM tasks WHERE task_id=?",
    ).bind(id).fetch_optional(&mut conn).await.map_err(|_| refusal("E-INPUT", "existing task store is unavailable"))?;
    let Some(row) = row else { return Ok(None) };
    let text = |index: usize, fallback: &str| -> String {
        row.try_get(index).unwrap_or_else(|_| fallback.to_string())
    };
    let notes: Map<String, Value> = serde_json::from_str(&text(12, "{}"))
        .ok()
        .and_then(|v: Value| v.as_object().cloned())
        .unwrap_or_default();
    let depends_on = serde_json::from_str::<Vec<Value>>(&text(13, "[]")).unwrap_or_default();
    Ok(Some(TaskRow {
        state: text(0, ""),
        correlation_id: text(1, ""),
        attempt: row.try_get(2).unwrap_or(0),
        agent_id: text(3, ""),
        capability: text(4, ""),
        title: text(5, ""),
        budget: serde_json::from_str(&text(6, "{}")).unwrap_or(Value::Object(Map::new())),
        inputs: serde_json::from_str(&text(7, "[]")).unwrap_or(Value::Array(vec![])),
        acceptance: serde_json::from_str(&text(8, "[]")).unwrap_or(Value::Array(vec![])),
        risk_class: text(9, "low"),
        priority: text(10, "P2"),
        rework_loops: row.try_get(11).unwrap_or(0),
        notes,
        depends_on,
    }))
}

fn agent_record(runtime: &Path, ident: &str) -> Result<Value, Reply> {
    let path = std::env::var("SWARM_AGENTS_FILE")
        .ok()
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| runtime.join("agents.json"));
    let text = fs::read_to_string(&path)
        .map_err(|_| refusal("E-CONTRACT", "task requires an assigned manifest agent"))?;
    let roster: Value = serde_json::from_str(&text)
        .map_err(|_| refusal("E-CONTRACT", "task requires an assigned manifest agent"))?;
    let agents = roster
        .get("agents")
        .and_then(Value::as_array)
        .ok_or_else(|| refusal("E-CONTRACT", "task requires an assigned manifest agent"))?;
    let ident_l = ident.to_ascii_lowercase();
    agents
        .iter()
        .find(|agent| {
            ["id", "code", "slug"].iter().any(|key| {
                agent
                    .get(*key)
                    .and_then(Value::as_str)
                    .is_some_and(|value| value.to_ascii_lowercase() == ident_l)
            })
        })
        .cloned()
        .ok_or_else(|| refusal("E-CONTRACT", "task requires an assigned manifest agent"))
}

fn py_list(value: &Value) -> String {
    let Some(items) = value.as_array() else {
        return value.to_string();
    };
    let body = items
        .iter()
        .map(|item| match item.as_str() {
            Some(text) => format!("'{text}'"),
            None => item.to_string(),
        })
        .collect::<Vec<_>>()
        .join(", ");
    format!("[{body}]")
}

fn lane_note(notes: &Map<String, Value>) -> Option<String> {
    match notes.get("role").and_then(Value::as_str) {
        Some("slice") => {
            let paths = notes.get("blast_radius").and_then(Value::as_array)
                .map(|items| items.iter().filter_map(Value::as_str).collect::<Vec<_>>().join(", "))
                .unwrap_or_default();
            let branch = notes.get("branch").and_then(Value::as_str).unwrap_or("");
            Some(format!("\n## Blast radius\nEdit only these paths: {paths}\nBranch: {branch}\nYou are one replica of your agent. Other replicas are running other blast radii at the same time, each in its own worktree. Stay inside this radius and this branch. Do not review this branch and do not merge it. The join merges every lane, then one Greptile review covers the merged branch.\n"))
        }
        Some("join") => {
            let branches = notes.get("merge_branches").and_then(Value::as_array)
                .map(|items| items.iter().filter_map(Value::as_str).map(|b| format!("- {b}")).collect::<Vec<_>>().join("\n"))
                .unwrap_or_default();
            Some(format!("\n## Join\nThe lane branches below merge into the integration branch. Do not review them.\n{branches}\n"))
        }
        _ if notes.get("review_source").and_then(Value::as_str) == Some("greptile") => Some("\n## Greptile review\nThis is the only code review for the plan. The slice branches are already merged. Review that merged branch once with scripts/rev_greptile_ingest.py and your review gate. Do not send the lanes back for separate reviews. If the merged branch has no open pull request to ingest, finish BLOCKED and name the missing pull request; do not invent findings.\n".into()),
        _ => Some(String::new()),
    }
}

fn uuid4() -> String {
    let mut bytes = [0u8; 16];
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    for (i, slot) in bytes.iter_mut().enumerate() {
        *slot = ((nanos >> ((i % 8) * 8)) as u8)
            .wrapping_add(i as u8)
            .wrapping_mul(17);
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!("{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7], bytes[8], bytes[9],
        bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15])
}

fn utc_millis() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    let seconds = millis.div_euclid(1000);
    let ms = millis.rem_euclid(1000);
    let days = seconds.div_euclid(86_400);
    let rest = seconds.rem_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{ms:03}Z",
        rest / 3600,
        rest % 3600 / 60,
        rest % 60
    )
}

/// Answers one served `orch_assign` call. A complex prompt returns `not_started` so Python answers.
pub async fn answer(call: &Invocation) -> Result<Reply, SeamError> {
    let Some(args) = parse(&call.argv) else {
        return Err(before_any_write(SeamError::new(
            ErrorCode::Dep,
            "orch_assign argv is outside the host grammar; Python answers",
        )));
    };
    if call.agent != "A01" || env_flag("SWARM_CHILD") || env_flag("SWARM_AGENT_SESSION") {
        return Ok(refusal(
            "E-POLICY",
            "specialist assignments require the trusted A01 controller",
        ));
    }
    if let Ok(agent) = std::env::var("SWARM_AGENT") {
        if !agent.is_empty() && agent != "A01" && agent != "a01-orchestrator" {
            return Ok(refusal(
                "E-POLICY",
                "specialist assignments require the trusted A01 controller",
            ));
        }
    }
    if args.dry_run {
        return Ok(refusal(
            "E-POLICY",
            "authenticated specialist assignments are unavailable in dry-run",
        ));
    }
    let task_id = match required_id(
        args.task_id
            .as_deref()
            .or_else(|| env_value(&call.env, "SWARM_TASK_ID")),
        "task id",
    ) {
        Ok(id) => id,
        Err(reply) => return Ok(reply),
    };
    let correlation_id = match required_id(
        args.correlation_id
            .as_deref()
            .or_else(|| env_value(&call.env, "SWARM_CORRELATION_ID")),
        "correlation id",
    ) {
        Ok(id) => id,
        Err(reply) => return Ok(reply),
    };
    let keys = match KeyMaterial::from_env() {
        Ok(keys) => keys,
        Err(_) => {
            return Err(before_any_write(SeamError::new(
                ErrorCode::Dep,
                "a key file is opened only by Python",
            )))
        }
    };
    if !keys.real_key_configured() {
        let message = if env_flag("SWARM_REQUIRE_KEY") {
            "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured"
        } else {
            "fail-closed: a real controller signer is required"
        };
        return Ok(refusal("E-POLICY", message));
    }
    let repo = canonical_or(&call.root.join(&args.root));
    let swarm_dir_env = std::env::var("SWARM_DIR").ok();
    let swarm = resolve_swarm_dir(
        &repo,
        env_value(&call.env, "SWARM_DIR").or(swarm_dir_env.as_deref()),
    );
    let db = swarm.join("tasks.db");
    if !db.is_file() {
        return Ok(refusal("E-INPUT", "existing task store is unavailable"));
    }
    let task = match read_task(&db, &task_id).await {
        Ok(Some(task)) => task,
        Ok(None) => return Ok(refusal("E-INPUT", "existing task is required")),
        Err(reply) => return Ok(reply),
    };
    if task.correlation_id != correlation_id {
        return Ok(refusal("E-CONTRACT", "task correlation mismatch"));
    }
    if task.state != "IN_PROGRESS" || task.attempt < 1 {
        return Ok(refusal(
            "E-POLICY",
            "specialist assignment requires an IN_PROGRESS leased task",
        ));
    }
    if task.agent_id.is_empty() {
        return Ok(refusal(
            "E-CONTRACT",
            "task requires an assigned manifest agent",
        ));
    }
    let agent = match agent_record(&call.root, &task.agent_id) {
        Ok(agent) => agent,
        Err(reply) => return Ok(reply),
    };
    let agent_id = agent.get("id").and_then(Value::as_str).unwrap_or("");
    let capabilities = agent.get("capabilities").and_then(Value::as_array);
    let capable = capabilities.is_some_and(|items| {
        items
            .iter()
            .any(|item| item.as_str() == Some(task.capability.as_str()))
    });
    if !capable || agent_id == "A01" {
        return Ok(refusal(
            "E-CONTRACT",
            "task requires a matching specialist capability",
        ));
    }
    if !task.depends_on.is_empty() || substrate_on() {
        return Err(before_any_write(SeamError::new(
            ErrorCode::Dep,
            "an assignment with upstream or substrate context is Python's",
        )));
    }
    if !keys.real_key_configured() {
        return Ok(refusal(
            "E-POLICY",
            "fail-closed: a real controller signer is required",
        ));
    }
    match signed_prompt(call, &repo, &task, &task_id, &agent, &keys) {
        Ok(reply) => Ok(reply),
        Err(reply) => Ok(reply),
    }
}

fn signed_prompt(
    call: &Invocation,
    repo: &Path,
    task: &TaskRow,
    task_id: &str,
    agent: &Value,
    keys: &KeyMaterial,
) -> Result<Reply, Reply> {
    let agent_id = agent.get("id").and_then(Value::as_str).unwrap_or("");
    let slug = agent.get("slug").and_then(Value::as_str).unwrap_or("");
    let lease = task
        .budget
        .get("max_wall_s")
        .and_then(Value::as_i64)
        .unwrap_or(1800);
    let mut payload = Map::new();
    payload.insert("task_id".into(), Value::String(task_id.into()));
    payload.insert(
        "correlation_id".into(),
        Value::String(task.correlation_id.clone()),
    );
    payload.insert("agent_id".into(), Value::String(agent_id.into()));
    payload.insert("capability".into(), Value::String(task.capability.clone()));
    payload.insert("title".into(), Value::String(task.title.clone()));
    payload.insert("lease_s".into(), Value::from(lease));
    payload.insert("inputs".into(), task.inputs.clone());
    payload.insert("acceptance".into(), task.acceptance.clone());
    payload.insert("budget".into(), task.budget.clone());
    payload.insert("risk_class".into(), Value::String(task.risk_class.clone()));
    payload.insert("priority".into(), Value::String(task.priority.clone()));
    payload.insert("attempt".into(), Value::from(task.attempt));
    payload.insert("rework_loop".into(), Value::from(task.rework_loops));
    if task.notes.get("gate").is_some() && task.notes.get("gate_for").is_none() {
        return Err(refusal(
            "E-CONTRACT",
            "canonical specialist prompt is unavailable",
        ));
    }
    if let Some(gate) = task.notes.get("gate") {
        payload.insert("gate".into(), gate.clone());
        payload.insert(
            "gate_for".into(),
            task.notes
                .get("gate_for")
                .cloned()
                .unwrap_or(Value::Array(vec![])),
        );
    }
    let mut envelope = serde_json::json!({
        "msg_id": uuid4(),
        "correlation_id": task.correlation_id,
        "trace_id": uuid4().replace('-', ""),
        "causation_id": null,
        "source": "A01@runner",
        "target": agent_id,
        "type": "task.assign",
        "schema": "swarm.v1.task.assign",
        "ts": utc_millis(),
        "ttl_s": 300,
        "priority": task.priority,
        "risk_class": task.risk_class,
        "payload": payload,
        "sig": null
    });
    let text = serde_json::to_string(&envelope)
        .map_err(|_| refusal("E-CONTRACT", "canonical specialist prompt is unavailable"))?;
    let mut parsed = parse_envelope(&text)
        .map_err(|_| refusal("E-CONTRACT", "canonical specialist prompt is unavailable"))?;
    sign_envelope(&mut parsed, keys).map_err(|_| {
        refusal(
            "E-POLICY",
            "fail-closed: a real controller signer is required",
        )
    })?;
    if !verify_envelope(&parsed, keys) {
        return Err(refusal(
            "E-POLICY",
            "fail-closed: specialist assignment signature invalid or ambiguous",
        ));
    }
    let envelope_text = to_python_json(parsed.as_json());
    envelope = serde_json::from_str(&envelope_text).unwrap_or(envelope);
    let lane = lane_note(&task.notes).unwrap_or_default();
    let brief = task
        .notes
        .get("brief_excerpt")
        .and_then(Value::as_str)
        .unwrap_or("");
    let gate_note = match (task.notes.get("gate").and_then(Value::as_str), task.notes.get("gate_for")) {
        (Some(gate), Some(targets)) => format!(
            "\n## Gate instructions\nYou are issuing the **{gate}** gate for tasks {}. Run your gate script with `--task-id {task_id}` (this gate task's own id) to see its findings; in this headless session it records nothing. After the session the runner re-runs it with the signing key and records the verdict on each gate_for target. Then report one JSON with `\"gate\": \"{gate}\"` and `\"verdicts\": {{\"<target_task_id>\": {{\"verdict\": \"pass|fail\", \"findings\": [...]}}}}` (advisory: fail findings become rework feedback, review findings are passed to the review gate script; only script-written verdicts count).",
            py_list(targets)
        ),
        _ => String::new(),
    };
    let rework = if task.rework_loops > 0 {
        if let Some(feedback) = task.notes.get("feedback") {
            format!("\n## Rework feedback (CHANGES_REQUESTED)\nAddress every finding below before reporting IN_REVIEW:\n{}", serde_json::to_string_pretty(feedback).unwrap_or_default())
        } else {
            String::new()
        }
    } else {
        String::new()
    };
    let runtime = call.root.display();
    let repo_text = repo.display();
    let prompt = format!("# task.assign (signed envelope)\n```json\n{envelope_text}\n```\n\n## Brief\n{brief}\n{lane}\n## Working repository\n{repo_text}\nSwarm runtime lives at {runtime} (scripts: `python3 {runtime}/scripts/<script>.py --root {repo_text} --task-id {task_id} --correlation-id {corr} --json`).\n\n## Upstream artifacts\n(no upstream tasks){gate_note}{rework}\n\nExecute the task per your agent instructions and finish with your summary and the single ```json result block.", corr = task.correlation_id);
    let body = serde_json::json!({
        "status": "ok",
        "task_id": task_id,
        "correlation_id": task.correlation_id,
        "agent_slug": slug,
        "assignment_verified": true,
        "assignment": envelope,
        "prompt": prompt,
        "agent": "A01",
        "script": "orch_assign"
    });
    Ok(Reply {
        exit_code: 0,
        stdout: format!(
            "{}\n",
            serde_json::to_string_pretty(&body).unwrap_or_default()
        ),
    })
}

#[cfg(test)]
mod tests {
    use super::parse;

    #[test]
    fn an_unknown_flag_is_left_to_python() {
        let argv = ["--json".into(), "--payload".into(), "x".into()];
        assert!(parse(&argv).is_none());
    }
}
