//! The host `qa_gate` route (phase 14.5, unit U3.6b).
//!
//! Runner detection, the count regexes, the findings and the subprocess (with the call's `--timeout`) match
//! `scripts/qa_gate.py`. The verdict tail is [`TaskStoreWriter::issue_gate`]. Anything this module cannot prove
//! identical to Python is `E-DEP` with `write_state = not_started`, and the selector runs the Python body.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use gsd_envelope::KeyMaterial;
use gsd_failure::ErrorCode;
use gsd_route::{jstr, to_python_json, Json, Reply, SeamError};
use gsd_taskwriter::{
    open_for_write, Decline, GateIssueRequest, SystemIds, TaskStoreWriter, WriterConfig,
};

use crate::strangler_seam::{
    after_commit, before_any_write, canonical_or, env_value, resolve_swarm_dir, Invocation,
};

const TIERS: [&str; 4] = ["unit", "integration", "e2e", "perf"];

struct Runner {
    name: &'static str,
    cmd: Vec<String>,
    pass_re: &'static str,
    fail_re: &'static str,
}

struct Executed {
    runner: String,
    returncode: i32,
    passed: i64,
    failed: i64,
    tail: String,
}

/// True when `argv` is the grammar this route serves. `--json` is required so Python's text mode stays Python's.
pub fn served(argv: &[String]) -> bool {
    parse(argv).is_some()
}

struct Args {
    root: Option<String>,
    risk: Option<String>,
    tier: Option<String>,
    timeout: u64,
    min_coverage: i64,
    per_target: Option<String>,
    task_id: Option<String>,
    correlation_id: Option<String>,
    dry_run: bool,
}

fn parse(argv: &[String]) -> Option<Args> {
    let mut out = Args {
        root: None,
        risk: None,
        tier: None,
        timeout: 1800,
        min_coverage: 0,
        per_target: None,
        task_id: None,
        correlation_id: None,
        dry_run: false,
    };
    let mut json = false;
    let mut index = 0;
    let mut take = |flag: &str, value: Option<String>| -> bool {
        let Some(value) = value else { return false };
        match flag {
            "--root" | "--repo" => out.root = Some(value),
            "--risk-class" if matches!(value.as_str(), "low" | "medium" | "high") => {
                out.risk = Some(value)
            }
            "--tier" => out.tier = Some(value),
            "--timeout" => match value.parse() {
                Ok(n) => out.timeout = n,
                Err(_) => return false,
            },
            "--min-coverage" => match value.parse() {
                Ok(n) => out.min_coverage = n,
                Err(_) => return false,
            },
            "--per-target-findings" => out.per_target = Some(value),
            "--task-id" => out.task_id = Some(value),
            "--correlation-id" => out.correlation_id = Some(value),
            _ => return false,
        }
        true
    };
    while index < argv.len() {
        let token = argv[index].as_str();
        index += 1;
        if token == "--json" {
            json = true;
            continue;
        }
        if token == "--dry-run" {
            out.dry_run = true;
            continue;
        }
        let (flag, inline) = token
            .split_once('=')
            .map(|(f, v)| (f, Some(v.to_string())))
            .unwrap_or((token, None));
        if !matches!(
            flag,
            "--root"
                | "--repo"
                | "--risk-class"
                | "--tier"
                | "--timeout"
                | "--min-coverage"
                | "--per-target-findings"
                | "--task-id"
                | "--correlation-id"
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
        if !take(flag, Some(value)) {
            return None;
        }
    }
    json.then_some(out)
}

fn python_bin() -> String {
    std::env::var("GSD_QA_PYTHON").unwrap_or_else(|_| "python3".to_string())
}

fn which(name: &str) -> bool {
    let Ok(path) = std::env::var("PATH") else {
        return false;
    };
    path.split(':').any(|dir| {
        let candidate = Path::new(dir).join(name);
        candidate.is_file()
            && candidate
                .metadata()
                .map(|meta| meta.permissions().mode() & 0o111 != 0)
                .unwrap_or(false)
    })
}

fn pytest_importable() -> bool {
    Command::new(python_bin())
        .arg("-c")
        .arg("import pytest")
        .output()
        .map(|out| out.status.success())
        .unwrap_or(false)
}

fn detect_runners(root: &Path) -> Vec<Runner> {
    let mut runners = Vec::new();
    let pytest_marker = root.join("pyproject.toml").exists()
        || root.join("pytest.ini").exists()
        || root.join("tests").read_dir().ok().is_some_and(|mut dir| {
            dir.any(|entry| {
                entry.ok().is_some_and(|item| {
                    item.file_name().to_string_lossy().starts_with("test_")
                        && item.path().extension().is_some_and(|ext| ext == "py")
                })
            })
        });
    if pytest_marker && pytest_importable() {
        runners.push(Runner {
            name: "pytest",
            cmd: vec![
                python_bin(),
                "-m".into(),
                "pytest".into(),
                "-q".into(),
                "--maxfail=50".into(),
                "-p".into(),
                "no:cacheprovider".into(),
            ],
            pass_re: r"(\d+) passed",
            fail_re: r"(\d+) failed|(\d+) error",
        });
    }
    if let Ok(text) = fs::read_to_string(root.join("package.json")) {
        let has_test = serde_json::from_str::<serde_json::Value>(&text)
            .ok()
            .and_then(|value| {
                value
                    .get("scripts")
                    .and_then(|scripts| scripts.get("test"))
                    .map(|_| ())
            })
            .is_some();
        if has_test && which("npm") {
            runners.push(Runner {
                name: "npm test",
                cmd: vec![
                    "npm".into(),
                    "test".into(),
                    "--silent".into(),
                    "--".into(),
                    "--ci".into(),
                ],
                pass_re: r"Tests:\s+(\d+) passed|(\d+) passing",
                fail_re: r"(\d+) failed|(\d+) failing",
            });
        }
    }
    if root.join("go.mod").exists() && which("go") {
        runners.push(Runner {
            name: "go test",
            cmd: vec!["go".into(), "test".into(), "./...".into()],
            pass_re: r"^ok",
            fail_re: r"^FAIL|--- FAIL",
        });
    }
    if root.join("Cargo.toml").exists() && which("cargo") {
        runners.push(Runner {
            name: "cargo test",
            cmd: vec!["cargo".into(), "test".into(), "-q".into()],
            pass_re: r"(\d+) passed",
            fail_re: r"(\d+) failed",
        });
    }
    runners
}

fn count(pattern: &str, text: &str) -> i64 {
    let Ok(re) = regex::RegexBuilder::new(pattern).multi_line(true).build() else {
        return 0;
    };
    re.find_iter(text)
        .map(|found| {
            re.captures_at(text, found.start())
                .and_then(|caps| {
                    caps.iter()
                        .skip(1)
                        .flatten()
                        .map(|m| m.as_str())
                        .find(|g| !g.is_empty() && g.chars().all(|c| c.is_ascii_digit()))
                })
                .and_then(|g| g.parse().ok())
                .unwrap_or(1)
        })
        .sum()
}

fn run_cmd(cmd: &[String], cwd: &Path, timeout: u64) -> Result<(i32, String), SeamError> {
    let mut child = Command::new(&cmd[0])
        .args(&cmd[1..])
        .current_dir(cwd)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .map_err(|_| {
            before_any_write(SeamError::new(
                ErrorCode::Dep,
                "a runner could not be started; Python answers",
            ))
        })?;
    let deadline = std::time::Instant::now() + Duration::from_secs(timeout.max(1));
    loop {
        if let Some(status) = child.try_wait().map_err(|_| {
            before_any_write(SeamError::new(
                ErrorCode::Dep,
                "a runner could not be waited; Python answers",
            ))
        })? {
            let output = child.wait_with_output().map_err(|_| {
                before_any_write(SeamError::new(
                    ErrorCode::Dep,
                    "a runner's output could not be read; Python answers",
                ))
            })?;
            let mut text = String::from_utf8_lossy(&output.stdout).into_owned();
            text.push_str(&String::from_utf8_lossy(&output.stderr));
            return Ok((status.code().unwrap_or(1), text));
        }
        if std::time::Instant::now() > deadline {
            child.kill().ok();
            return Err(before_any_write(SeamError::new(
                ErrorCode::Dep,
                "a runner exceeded --timeout; Python answers",
            )));
        }
        std::thread::sleep(Duration::from_millis(20));
    }
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

fn tiers_for(risk: &str, tier: Option<&str>) -> Vec<String> {
    if let Some(tier) = tier {
        return tier
            .split(',')
            .filter(|item| !item.is_empty())
            .map(str::to_string)
            .collect();
    }
    match risk {
        "low" => vec!["unit".into()],
        "high" => vec![
            "unit".into(),
            "integration".into(),
            "e2e".into(),
            "perf".into(),
        ],
        _ => vec!["unit".into(), "integration".into()],
    }
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// The commit condition of `issue_gate`: a store call (`task_id` given) whose plan has no error and writes rows. The no-task
/// path derives the plan without a store and commits nothing, so its writes are not recorded.
fn committed(request: &GateIssueRequest, plan: &gsd_taskwriter::GatePlan) -> bool {
    request.task_id.is_some() && plan.error.is_none() && !plan.writes.is_empty()
}

/// Writes the verdict file and its directory. A failure after a commit is final: the verdict is recorded and must not be
/// recorded again. Otherwise nothing was committed and the failure is not_started, so Python answers.
fn write_plan_file(
    swarm: &Path,
    plan: &gsd_taskwriter::GatePlan,
    committed: bool,
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
    Ok(())
}

fn reply_of(body: Json, exit_code: i32) -> Reply {
    Reply {
        exit_code,
        stdout: format!("{}\n", to_python_json(&body)),
    }
}

/// Answers one served `qa_gate` call. A [`SeamError`] of `E-DEP` is not-started: the selector runs Python.
pub async fn answer(call: &Invocation) -> Result<Reply, SeamError> {
    let Some(args) = parse(&call.argv) else {
        return Err(before_any_write(SeamError::new(
            ErrorCode::Dep,
            "qa_gate argv is outside the host grammar; Python answers",
        )));
    };
    let root = canonical_or(&call.root.join(args.root.as_deref().unwrap_or(".")));
    let risk = args.risk.clone().unwrap_or_else(|| "medium".to_string());
    if args.risk.is_none() && (args.task_id.is_some()) {
        return Err(before_any_write(SeamError::new(
            ErrorCode::Dep,
            "a risk class taken from the store is Python's; pass --risk-class",
        )));
    }
    let tiers = tiers_for(&risk, args.tier.as_deref());
    let mut runs: Vec<(String, Json)> = TIERS
        .iter()
        .map(|tier| ((*tier).to_string(), jstr("skipped:not-selected")))
        .collect();
    let mut findings: Vec<Json> = Vec::new();
    let mut executed: Vec<Executed> = Vec::new();
    if args.dry_run {
        for tier in &tiers {
            if let Some(slot) = runs.iter_mut().find(|(name, _)| name == tier) {
                slot.1 = jstr("pass");
            }
        }
    } else {
        let runners = detect_runners(&root);
        if runners.is_empty() {
            let severity = if risk == "low" { "minor" } else { "major" };
            findings.push(finding(
                "QF-000",
                severity,
                "coverage",
                "no test runner detected in repository",
                &root.display().to_string(),
                "A05",
            ));
            for tier in &tiers {
                if let Some(slot) = runs.iter_mut().find(|(name, _)| name == tier) {
                    slot.1 = jstr("skipped:infra");
                }
            }
        } else {
            for runner in &runners {
                let (code, out) = run_cmd(&runner.cmd, &root, args.timeout)?;
                let passed = count(runner.pass_re, &out);
                let failed = count(runner.fail_re, &out);
                let ok = code == 0 && failed == 0;
                let tail = out
                    .chars()
                    .rev()
                    .take(1500)
                    .collect::<String>()
                    .chars()
                    .rev()
                    .collect::<String>();
                executed.push(Executed {
                    runner: runner.name.into(),
                    returncode: code,
                    passed,
                    failed,
                    tail: tail.clone(),
                });
                if tiers.iter().any(|tier| tier == "unit") {
                    if let Some(slot) = runs.iter_mut().find(|(name, _)| name == "unit") {
                        slot.1 = jstr(if ok { "pass" } else { "fail" });
                    }
                }
                if tiers.iter().any(|tier| tier == "integration") {
                    if let Some(slot) = runs.iter_mut().find(|(name, value)| {
                        name == "integration"
                            && matches!(value, Json::Str(text) if text.starts_with("skipped"))
                    }) {
                        slot.1 = jstr(if ok { "pass" } else { "fail" });
                    }
                }
                if !ok {
                    let id = format!("QF-{:03}", findings.len() + 1);
                    let evidence: String = out
                        .chars()
                        .rev()
                        .take(800)
                        .collect::<String>()
                        .chars()
                        .rev()
                        .collect();
                    findings.push(finding(
                        &id,
                        "major",
                        "functional",
                        &format!("{} failed ({failed} failing)", runner.name),
                        &evidence,
                        "A05",
                    ));
                }
            }
            for tier in ["e2e", "perf"] {
                if tiers.iter().any(|item| item == tier) {
                    if let Some(slot) = runs.iter_mut().find(|(name, _)| name == tier) {
                        slot.1 = jstr("skipped:infra");
                    }
                    if risk == "high" {
                        let id = format!("QF-{:03}", findings.len() + 1);
                        findings.push(finding(&id, "major", tier, &format!("{tier} tier required for risk_class=high but no harness configured"), "", "A11"));
                    }
                }
            }
        }
        if args.min_coverage > 0 && executed.iter().any(|item| item.runner == "pytest") {
            let (code, out) = run_cmd(
                &[
                    python_bin(),
                    "-m".into(),
                    "pytest".into(),
                    "-q".into(),
                    "--cov".into(),
                    "--cov-report=term".into(),
                    "-p".into(),
                    "no:cacheprovider".into(),
                ],
                &root,
                args.timeout,
            )?;
            let _ = code;
            if let Some(found) = regex::Regex::new(r"TOTAL.*?(\d+)%")
                .ok()
                .and_then(|re| re.captures(&out))
                .and_then(|caps| caps.get(1))
            {
                if found.as_str().parse::<i64>().unwrap_or(100) < args.min_coverage {
                    let id = format!("QF-{:03}", findings.len() + 1);
                    findings.push(finding(
                        &id,
                        "major",
                        "coverage",
                        &format!("coverage {}% < {}%", found.as_str(), args.min_coverage),
                        "",
                        "A05",
                    ));
                }
            }
        }
    }
    let own = load_per_target(args.per_target.as_deref(), findings.len())?;
    let request = GateIssueRequest {
        gate: "quality".into(),
        agent_id: if args.dry_run {
            "A08@dry".into()
        } else {
            "A08@local".into()
        },
        findings: findings.clone(),
        runs: runs.clone(),
        expires_s: 86400,
        extra: Vec::new(),
        simulate: args.dry_run,
        per_target: own.clone(),
        task_id: args.task_id.clone(),
        correlation_id: args.correlation_id.clone(),
        session: env_value(&call.env, "SWARM_AGENT_SESSION") == Some("1"),
        dryrun_fail: std::env::var("SWARM_DRYRUN_FAIL").unwrap_or_default(),
        now: now_secs(),
    };
    let keys = KeyMaterial::from_env().map_err(|_| {
        before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the signing configuration is Python's",
        ))
    })?;
    let mut ids = SystemIds::open().map_err(|_| {
        before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the id source could not be opened; Python answers",
        ))
    })?;
    let swarm = resolve_swarm_dir(&root, env_value(&call.env, "SWARM_DIR"));
    let plan = if request.task_id.is_none() {
        gsd_taskwriter::derive_gate_issue(
            &request,
            &gsd_taskwriter::StoreFacts::default(),
            &keys,
            &mut ids,
        )
        .map_err(|Decline(reason)| before_any_write(SeamError::new(ErrorCode::Dep, reason)))?
    } else {
        let store = open_for_write(&WriterConfig::new(swarm.join("tasks.db")))
            .await
            .map_err(|error| {
                if error.write_state() == gsd_taskwriter::WriteState::NotStarted {
                    before_any_write(error.to_seam_error())
                } else {
                    error.to_seam_error()
                }
            })?;
        TaskStoreWriter::new(store)
            .issue_gate(&request, &keys, &mut ids)
            .await
            .map_err(|error| {
                if error.write_state() == gsd_taskwriter::WriteState::NotStarted {
                    before_any_write(error.to_seam_error())
                } else {
                    error.to_seam_error()
                }
            })?
    };
    if let Some(error) = &plan.error {
        write_plan_file(&swarm, &plan, committed(&request, &plan))?;
        return Err(error.clone());
    }
    write_plan_file(&swarm, &plan, committed(&request, &plan))?;
    let envelope = plan.envelope.clone().unwrap_or(Json::Obj(vec![]));
    let verdict = verdict_of(&envelope, &plan.recorded);
    let mut reported = findings;
    for (_, items) in &own {
        reported.extend(items.clone());
    }
    let runners: Vec<String> = executed.iter().map(|item| item.runner.clone()).collect();
    let summary = format!(
        "quality gate {} — runners: {}",
        verdict.to_uppercase(),
        if runners.is_empty() {
            "none".into()
        } else {
            format!("{runners:?}").replace('"', "'")
        }
    );
    let body = Json::Obj(vec![
        (
            "status".into(),
            jstr(if verdict == "pass" { "ok" } else { "fail" }),
        ),
        ("verdict".into(), jstr(&verdict)),
        ("risk_class".into(), jstr(&risk)),
        ("runs".into(), Json::Obj(runs)),
        ("findings".into(), Json::Arr(reported.clone())),
        (
            "executed".into(),
            Json::Arr(executed.iter().map(executed_json).collect()),
        ),
        ("envelope".into(), envelope),
        (
            "recorded".into(),
            Json::Arr(plan.recorded.iter().map(|(id, _)| jstr(id)).collect()),
        ),
        ("summary".into(), jstr(&summary)),
        ("agent".into(), jstr("A08")),
        ("script".into(), jstr("qa_gate")),
    ]);
    let major = reported.iter().any(|item| {
        matches!(
            obj_str(item, "severity"),
            Some("major" | "critical" | "blocker")
        )
    });
    let exit_code = if verdict == "fail" || major { 1 } else { 0 };
    Ok(reply_of(body, exit_code))
}

fn obj_str<'a>(value: &'a Json, key: &str) -> Option<&'a str> {
    let Json::Obj(fields) = value else {
        return None;
    };
    fields
        .iter()
        .find(|(name, _)| name == key)
        .and_then(|(_, value)| match value {
            Json::Str(text) => Some(text.as_str()),
            _ => None,
        })
}

fn executed_json(item: &Executed) -> Json {
    Json::Obj(vec![
        ("runner".into(), jstr(&item.runner)),
        ("returncode".into(), Json::Int(i64::from(item.returncode))),
        ("passed".into(), Json::Int(item.passed)),
        ("failed".into(), Json::Int(item.failed)),
        ("tail".into(), jstr(&item.tail)),
    ])
}

fn verdict_of(envelope: &Json, recorded: &[(String, Json)]) -> String {
    let mut fail = false;
    if let Json::Obj(fields) = envelope {
        if let Some(Json::Obj(payload)) = fields
            .iter()
            .find(|(name, _)| name == "payload")
            .map(|(_, value)| value)
        {
            fail |= obj_str(&Json::Obj(payload.clone()), "verdict") == Some("fail");
        }
    }
    for (_, env) in recorded {
        if let Json::Obj(fields) = env {
            if let Some(Json::Obj(payload)) = fields
                .iter()
                .find(|(name, _)| name == "payload")
                .map(|(_, value)| value)
            {
                fail |= obj_str(&Json::Obj(payload.clone()), "verdict") == Some("fail");
            }
        }
    }
    if fail { "fail" } else { "pass" }.into()
}

fn load_per_target(
    path: Option<&str>,
    offset: usize,
) -> Result<Vec<(String, Vec<Json>)>, SeamError> {
    let Some(path) = path else {
        return Ok(Vec::new());
    };
    let text = fs::read_to_string(path).map_err(|_| {
        before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the findings file could not be read; Python answers",
        ))
    })?;
    let value: serde_json::Value = serde_json::from_str(&text).map_err(|_| {
        before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the findings file is not JSON this route proves; Python answers",
        ))
    })?;
    let Some(object) = value.as_object() else {
        return Err(before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the findings file is not an object; Python answers",
        )));
    };
    let mut out = Vec::new();
    let mut index = offset;
    for (target, items) in object {
        let Some(list) = items.as_array() else {
            return Err(before_any_write(SeamError::new(
                ErrorCode::Dep,
                "a findings value is not a list; Python answers",
            )));
        };
        let mut parsed = Vec::new();
        for item in list {
            index += 1;
            let Some(object) = item.as_object() else {
                return Err(before_any_write(SeamError::new(
                    ErrorCode::Dep,
                    "a finding is not an object; Python answers",
                )));
            };
            let severity = object
                .get("severity")
                .and_then(|v| v.as_str())
                .unwrap_or("minor");
            let id = object
                .get("id")
                .and_then(|v| v.as_str())
                .map(str::to_string)
                .unwrap_or_else(|| format!("QF-{index:03}"));
            let kind = object
                .get("kind")
                .and_then(|v| v.as_str())
                .unwrap_or("semantic");
            let summary = object.get("summary").and_then(|v| v.as_str()).unwrap_or("");
            let evidence = object
                .get("evidence")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let owner = object
                .get("owner_suggestion")
                .and_then(|v| v.as_str())
                .unwrap_or("A05");
            parsed.push(finding(&id, severity, kind, summary, evidence, owner));
        }
        out.push((target.clone(), parsed));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::{committed, count, write_plan_file};
    use gsd_failure::ErrorCode;
    use gsd_route::{Json, SeamError};
    use gsd_taskwriter::{GateFile, GateIssueRequest, GatePlan, GateWrite};

    #[test]
    fn the_count_sums_the_first_digit_group_of_each_match() {
        assert_eq!(count(r"(\d+) passed", "3 passed, 1 passed"), 4);
        assert_eq!(count(r"(\d+) failed|(\d+) error", "2 failed\n1 error"), 3);
        assert_eq!(count(r"^ok", "ok\nok"), 2);
    }

    fn write_state(error: &SeamError) -> Option<&str> {
        error.details().iter().find_map(|(key, value)| match value {
            Json::Str(state) if key == "write_state" => Some(state.as_str()),
            _ => None,
        })
    }

    fn plan(writes: Vec<GateWrite>) -> GatePlan {
        GatePlan {
            file: Some(GateFile {
                name: "T-1.quality.json".into(),
                bytes: "{}".into(),
            }),
            writes,
            ..GatePlan::default()
        }
    }

    #[test]
    fn a_verdict_file_that_fails_after_the_commit_is_final() {
        let dir = tempfile::tempdir().expect("tempdir");
        // `verdicts` is a file, so the verdict file cannot be created under it.
        std::fs::write(dir.path().join("verdicts"), "not a directory").expect("collide");
        let committed = plan(vec![GateWrite::Notes {
            task_id: "T-1".into(),
            text: "{}".into(),
            updated_at: 1.0,
        }]);
        let error = write_plan_file(dir.path(), &committed, true).expect_err("the write fails");
        assert_eq!(write_state(&error), Some("final"));
        assert_eq!(error.code(), ErrorCode::Internal);
        assert!(
            error.message().contains("the verdict is recorded"),
            "{}",
            error.message()
        );
    }

    #[test]
    fn a_verdict_file_that_fails_without_a_commit_is_not_started() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("verdicts"), "not a directory").expect("collide");
        let error =
            write_plan_file(dir.path(), &plan(Vec::new()), false).expect_err("the write fails");
        assert_eq!(write_state(&error), Some("not_started"));
        assert_eq!(error.code(), ErrorCode::Dep);
    }

    fn no_task_request() -> GateIssueRequest {
        GateIssueRequest {
            gate: "quality".into(),
            agent_id: "A08".into(),
            findings: Vec::new(),
            runs: Vec::new(),
            expires_s: 3600,
            extra: Vec::new(),
            simulate: false,
            per_target: Vec::new(),
            task_id: None,
            correlation_id: None,
            session: false,
            dryrun_fail: String::new(),
            now: 1_700_000_000.0,
        }
    }

    #[test]
    fn a_no_task_verdict_file_failure_is_not_started() {
        let dir = tempfile::tempdir().expect("tempdir");
        // `verdicts` is a file, so the verdict file cannot be created under it.
        std::fs::write(dir.path().join("verdicts"), "not a directory").expect("collide");
        let derived = plan(vec![GateWrite::Notes {
            task_id: "T-1".into(),
            text: "{}".into(),
            updated_at: 1.0,
        }]);
        let commits = committed(&no_task_request(), &derived);
        assert!(!commits, "the no-task path writes no store rows");
        let error = write_plan_file(dir.path(), &derived, commits).expect_err("the write fails");
        assert_eq!(write_state(&error), Some("not_started"));
        assert_eq!(error.code(), ErrorCode::Dep);
    }
}
