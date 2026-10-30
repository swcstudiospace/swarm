//! The host `orch_plan` route (phase 14.5, unit U3.7b): `scripts/orch_plan.py` `run()` in Rust.
//!
//! [`served`] claims the argv grammar the route can prove identical to Python; [`answer`] runs the call. The pure part is
//! `gsd_route::plan_derive::derive_plan`; the store part is `gsd_taskwriter::create_plan`. The steps, in Python's order:
//!
//! 1. the ids (`--task-id`, `--correlation-id`, each with its `SWARM_*` default) are checked as `AgentScript.main` checks them,
//!    so a bad id is Python's `E-INPUT` and carries no `write_state`;
//! 2. a call with a substrate URL is `E-DEP` `not_started` (`_bind_graph` speaks to the substrate, which the route does not replay);
//! 3. the files `--brief` (when it names an existing path) and the brief text are read as Python reads them;
//! 4. a first derivation with no stored rows names the suffixes of the plan; `--dry-run` answers here, without a store;
//! 5. the store is opened with [`open_for_write`] (no DDL: a missing `tasks.db` is `not_started`, Python creates it); one
//!    `BEGIN IMMEDIATE` holds the rows the plan reads, the derivation, the writes and the `COMMIT`;
//! 6. after the `COMMIT`: `plans/<corr>.json` (atomic: a private temporary file, `fsync`, rename), `latest_correlation`, and the
//!    effects manifest for the selector to replay (`plan.updated` through `ctx.emit`, then the live refresh);
//! 7. the reply, with the signed controller kickoff when a real key is configured.
//!
//! The kickoff is signed before the `COMMIT`, so a signing failure is `not_started` and Python answers with no write done. Refusals
//! that `plan_derive` decides (`E-INPUT`, `E-CONTRACT` prefix collisions) are Python's own and carry no `write_state`.
//!
//! Declined, `E-DEP` `not_started` (Python answers): a substrate URL; `--plan` and every blast-radius form (`--slices`,
//! `--slices-json`, `--pattern custom` and `--pattern parallel`, and a brief with a blast-radii block), because the agent roster is
//! read by `swarm.manifest` with validation this route does not reproduce; an argv outside the grammar (abbreviations, repeated
//! scalars, an option-like value, a bad choice); a call without an `effects_path`; a `tasks.db` the writer will not open; a stored row
//! or plan file the writer or the derivation cannot read as Python does.
//!
//! The effects manifest (`write_manifest`) is `{"correlation_id", "effects": [{"type", "payload", "task_id"}], "live_refresh": bool}`.
//! `live_refresh` is true for every committed plan (each one transitions its tasks).

use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use gsd_envelope::{parse_envelope, sign_envelope, verify_envelope, KeyMaterial};
use gsd_failure::ErrorCode;
use gsd_route::plan_derive::{
    derive_plan, derived_prefix, FileRead, Kickoff, Outcome, PlanDerived, PlanEffect, PlanError,
    PlanFacts, PlanOp, PlanRequest, Signing, StoredPlanFile,
};
use gsd_route::pydumps::to_python_dumps;
use gsd_route::{check_safe_id, finish, is_safe_id, jstr, Fields, Json, Reply, SeamError};
use gsd_taskwriter::create_plan::{read_plan_rows, write_plan_ops};
use gsd_taskwriter::{
    open_for_write, ErrorKind, WriteState, WriteStore, WriterConfig, WriterError,
};

use crate::strangler_seam::{
    before_any_write, canonical_or, env_value, resolve_swarm_dir, Invocation,
};
use crate::write_route::write_manifest;

/// The value-taking options of `orch_plan.py`, in their long spelling (`--repo` is the alias of `--root`).
const VALUE_FLAGS: [&str; 15] = [
    "--root",
    "--repo",
    "--brief",
    "--brief-text",
    "--pattern",
    "--slices",
    "--slices-json",
    "--plan",
    "--risk-class",
    "--priority",
    "--prefix",
    "--acceptance",
    "--graph-id",
    "--correlation-id",
    "--task-id",
];

/// `--pattern` choices (`PATTERNS`, `custom`, `parallel`).
const PATTERNS: [&str; 8] = [
    "feature",
    "hotfix",
    "dependency",
    "cloud-native",
    "cluster",
    "web3",
    "custom",
    "parallel",
];

/// `--risk-class` choices.
const RISK_CLASSES: [&str; 3] = ["low", "medium", "high"];

/// `--priority` choices.
const PRIORITIES: [&str; 4] = ["P0", "P1", "P2", "P3"];

/// The fields `orch_plan.py` reads from its argv, with argparse's defaults.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Args {
    root: Option<String>,
    task_id: Option<String>,
    correlation_id: Option<String>,
    brief: Option<String>,
    brief_text: Option<String>,
    pattern: String,
    slices: Option<String>,
    slices_json: Option<String>,
    plan: Option<String>,
    risk_class: String,
    priority: String,
    prefix: Option<String>,
    acceptance: Vec<String>,
    graph_id: Option<String>,
    dry_run: bool,
}

/// The argv grammar this route proves: exactly the long option names of `orch_plan.py`, each once (`--acceptance` may repeat),
/// values attached with `=` or as the next token when that token does not look like an option, and choices from argparse's list.
fn parse(argv: &[String]) -> Option<Args> {
    let mut args = Args {
        root: None,
        task_id: None,
        correlation_id: None,
        brief: None,
        brief_text: None,
        pattern: "feature".to_string(),
        slices: None,
        slices_json: None,
        plan: None,
        risk_class: "medium".to_string(),
        priority: "P2".to_string(),
        prefix: None,
        acceptance: Vec::new(),
        graph_id: None,
        dry_run: false,
    };
    let mut seen: Vec<&str> = Vec::new();
    let mut index = 0;
    while index < argv.len() {
        let token = argv[index].as_str();
        index += 1;
        match token {
            "--json" => continue,
            "--dry-run" => {
                args.dry_run = true;
                continue;
            }
            _ => {}
        }
        let (name, attached) = match token.split_once('=') {
            Some((name, value)) if name.starts_with("--") => (name, Some(value.to_string())),
            _ => (token, None),
        };
        if !VALUE_FLAGS.contains(&name) {
            return None;
        }
        let value = match attached {
            Some(value) => value,
            None => {
                let next = argv.get(index)?;
                // argparse reads a token that starts with `-` as an option unless it contains a space; decline both cases.
                if next.starts_with('-') && !next.contains(' ') {
                    return None;
                }
                index += 1;
                next.clone()
            }
        };
        let key = if name == "--repo" { "--root" } else { name };
        if key != "--acceptance" {
            if seen.contains(&key) {
                return None;
            }
            seen.push(key);
        }
        match key {
            "--root" => args.root = Some(value),
            "--task-id" => args.task_id = Some(value),
            "--correlation-id" => args.correlation_id = Some(value),
            "--brief" => args.brief = Some(value),
            "--brief-text" => args.brief_text = Some(value),
            "--pattern" => {
                if !PATTERNS.contains(&value.as_str()) {
                    return None;
                }
                args.pattern = value;
            }
            "--slices" => args.slices = Some(value),
            "--slices-json" => args.slices_json = Some(value),
            "--plan" => args.plan = Some(value),
            "--risk-class" => {
                if !RISK_CLASSES.contains(&value.as_str()) {
                    return None;
                }
                args.risk_class = value;
            }
            "--priority" => {
                if !PRIORITIES.contains(&value.as_str()) {
                    return None;
                }
                args.priority = value;
            }
            "--prefix" => args.prefix = Some(value),
            "--acceptance" => args.acceptance.push(value),
            "--graph-id" => args.graph_id = Some(value),
            _ => return None,
        }
    }
    Some(args)
}

/// Whether the call needs the agent roster: a custom plan or blast-radius slices. The route declines those (see the module doc).
fn needs_roster(args: &Args) -> bool {
    args.plan.is_some()
        || args.slices.is_some()
        || args.slices_json.is_some()
        || args.pattern == "custom"
        || args.pattern == "parallel"
}

/// Whether this route claims the argv: the grammar it proves, without the forms that need the agent roster.
pub fn served(argv: &[String]) -> bool {
    parse(argv).is_some_and(|args| !needs_roster(&args))
}

/// Answers one `orch_plan` call. Every `Err` is Python's own refusal (no `write_state`) or a not-started decline (`E-DEP`,
/// `write_state = "not_started"`) that the caller hands to Python. After the `COMMIT` nothing is refused or re-run.
///
/// The effects manifest is written for every call the route answers, even when nothing is to be replayed: a Python-own refusal, a
/// dry run and a reuse carry an empty manifest, so the selector can tell "nothing to replay" from "effects lost". A declined call
/// writes none, because Python answers it and writes its own.
pub async fn answer(call: &Invocation) -> Result<Reply, SeamError> {
    let result = answer_call(call).await;
    if let Err(error) = &result {
        if error.details().iter().all(|(key, _)| key != "write_state") {
            write_manifest(call, &manifest(&[], None, false)).ok();
        }
    }
    result
}

async fn answer_call(call: &Invocation) -> Result<Reply, SeamError> {
    let Some(args) = parse(&call.argv).filter(|args| !needs_roster(args)) else {
        return Err(decline("orch_plan argv is outside the host grammar"));
    };
    if substrate_on() {
        return Err(decline("a plan bound to a substrate Graph ID is Python's"));
    }
    if let Some(id) = args
        .task_id
        .as_deref()
        .or_else(|| env_value(&call.env, "SWARM_TASK_ID").filter(|value| !value.is_empty()))
    {
        check_safe_id(id, "task id")?;
    }
    let correlation = match args.correlation_id.as_deref() {
        Some(value) => Some(value),
        None => env_value(&call.env, "SWARM_CORRELATION_ID").filter(|value| !value.is_empty()),
    };
    if let Some(id) = correlation {
        check_safe_id(id, "correlation id")?;
    }
    let keys =
        KeyMaterial::from_env().map_err(|_| decline("a key file is opened only by Python"))?;
    let signing = signing_state(&keys, call)?;

    let root = canonical_or(&call.root.join(args.root.as_deref().unwrap_or(".")));
    let swarm = resolve_swarm_dir(&root, env_value(&call.env, "SWARM_DIR"));

    let brief_file = match args.brief.as_deref().filter(|path| !path.is_empty()) {
        Some(path) => {
            let full = call.root.join(path);
            full.exists().then(|| read_text_file(&full))
        }
        None => None,
    };
    let effective_brief = match (&brief_file, &args.brief_text) {
        (Some(FileRead::Text(text)), _) => Some(text.as_str()),
        (Some(FileRead::Unreadable), _) => None,
        (None, text) => text.as_deref(),
    };
    if effective_brief.is_some_and(|text| text.contains("blast-radii")) {
        return Err(decline(
            "a blast-radii block needs the agent roster, which the route does not read",
        ));
    }
    let request = PlanRequest {
        pattern: args.pattern.clone(),
        risk_class: args.risk_class.clone(),
        priority: args.priority.clone(),
        prefix: args.prefix.clone(),
        acceptance: args.acceptance.clone(),
        graph_id: args.graph_id.clone(),
        brief_file,
        brief_text: args.brief_text.clone(),
        plan_file: None,
        slices_json: None,
        slices_file: None,
        correlation_id: correlation.map(str::to_string),
        dry_run: args.dry_run,
    };
    let new_correlation =
        random_uuid4().map_err(|_| decline("no entropy source for a correlation id"))?;
    let now = now_seconds()?;
    let plans_dir = swarm.join("plans");
    let base = PlanFacts {
        new_correlation_id: new_correlation.clone(),
        agents: Vec::new(),
        plans_dir: plans_dir.to_string_lossy().into_owned(),
        signing,
        prefix_tasks: Vec::new(),
        correlation_tasks: Vec::new(),
        plan_files: Vec::new(),
    };
    let probe = match derive_plan(&base, &request, now) {
        Ok(derived) => derived,
        Err(PlanError::Refused(error)) => return Err(error),
        Err(PlanError::NotStarted(why)) => return Err(decline(&why)),
    };
    let corr = correlation.map_or_else(|| new_correlation.clone(), str::to_string);
    match probe.outcome {
        Outcome::DryRun => {
            write_manifest(
                call,
                &manifest(&[], probe.ctx_correlation_id.as_deref(), false),
            )
            .ok();
            return Ok(finish(probe.reply, &call.agent, &call.script));
        }
        Outcome::Created => {}
        Outcome::Reused => return Err(decline("a plan with no stored rows cannot be a reuse")),
    }
    let prefix = request
        .prefix
        .clone()
        .unwrap_or_else(|| derived_prefix(&corr, 4));
    let suffixes = suffixes_of(&probe.ops, &prefix)?;
    if call.effects_path.is_none() {
        return Err(decline(
            "a plan needs an effects_path to hand its post-commit effects to the caller",
        ));
    }

    let store = open_for_write(&WriterConfig::new(swarm.join("tasks.db")))
        .await
        .map_err(|error| store_refusal(&error))?;
    let outcome = plan_in_transaction(
        &store,
        call,
        &keys,
        &Planning {
            base,
            request: &request,
            corr: &corr,
            suffixes: &suffixes,
            swarm: &swarm,
            plans_dir: &plans_dir,
            now,
        },
    )
    .await;
    store.close().await;
    outcome
}

/// What the transaction needs beyond the store and the keys.
struct Planning<'a> {
    base: PlanFacts,
    request: &'a PlanRequest,
    corr: &'a str,
    suffixes: &'a [String],
    swarm: &'a Path,
    plans_dir: &'a Path,
    now: f64,
}

/// The `BEGIN IMMEDIATE` part: reads the rows, derives, writes, commits, and then publishes the files and the manifest.
async fn plan_in_transaction(
    store: &WriteStore,
    call: &Invocation,
    keys: &KeyMaterial,
    planning: &Planning<'_>,
) -> Result<Reply, SeamError> {
    let request = planning.request;
    let candidates: Vec<String> = candidate_prefixes_of(planning.corr, request.prefix.as_deref())
        .iter()
        .flat_map(|prefix| {
            planning
                .suffixes
                .iter()
                .map(move |suffix| format!("{prefix}-{suffix}"))
        })
        .collect();
    let mut tx = store.begin().await.map_err(|error| store_refusal(&error))?;
    let rows = match read_plan_rows(&mut tx, planning.corr, &candidates).await {
        Ok(rows) => rows,
        Err(error) => {
            tx.rollback().await.ok();
            return Err(store_refusal(&error));
        }
    };
    let mut plan_files = Vec::with_capacity(rows.correlations.len());
    for owner in &rows.correlations {
        if !is_safe_id(owner) {
            tx.rollback().await.ok();
            return Err(decline(
                "a stored correlation id names no plan file this route reads",
            ));
        }
        match read_plan_text(planning.plans_dir, owner) {
            Ok(text) => plan_files.push(StoredPlanFile {
                correlation_id: owner.clone(),
                text,
            }),
            Err(why) => {
                tx.rollback().await.ok();
                return Err(decline(why));
            }
        }
    }
    let facts = PlanFacts {
        prefix_tasks: rows.prefix_tasks,
        correlation_tasks: rows.correlation_tasks,
        plan_files,
        ..planning.base.clone()
    };
    let derived = match derive_plan(&facts, request, planning.now) {
        Ok(derived) => derived,
        Err(PlanError::Refused(error)) => {
            tx.rollback().await.ok();
            return Err(error);
        }
        Err(PlanError::NotStarted(why)) => {
            tx.rollback().await.ok();
            return Err(decline(&why));
        }
    };
    match derived.outcome {
        Outcome::Reused => {
            let reply =
                match with_kickoff(keys, derived.kickoff.as_ref(), derived.reply, planning.now) {
                    Ok(reply) => reply,
                    Err(error) => {
                        tx.rollback().await.ok();
                        return Err(error);
                    }
                };
            tx.commit().await.map_err(|error| store_refusal(&error))?;
            write_manifest(call, &manifest(&[], Some(planning.corr), false)).ok();
            Ok(finish(reply, &call.agent, &call.script))
        }
        Outcome::Created => create(tx, call, keys, planning, derived).await,
        Outcome::DryRun => {
            tx.rollback().await.ok();
            Err(decline("a dry run is answered before the store is read"))
        }
    }
}

/// The creation: the kickoff is signed, the ops are written, the transaction commits, then the files, the manifest and the reply.
async fn create(
    mut tx: gsd_taskwriter::Tx,
    call: &Invocation,
    keys: &KeyMaterial,
    planning: &Planning<'_>,
    derived: PlanDerived,
) -> Result<Reply, SeamError> {
    let reply = match with_kickoff(keys, derived.kickoff.as_ref(), derived.reply, planning.now) {
        Ok(reply) => reply,
        Err(error) => {
            tx.rollback().await.ok();
            return Err(error);
        }
    };
    let Some(files) = derived.files.as_ref() else {
        tx.rollback().await.ok();
        return Err(decline("a creation without files"));
    };
    if let Err(error) = write_plan_ops(&mut tx, &derived.ops, planning.now).await {
        tx.rollback().await.ok();
        return Err(store_refusal(&error));
    }
    tx.commit().await.map_err(|error| store_refusal(&error))?;

    // The transaction is committed: from here on nothing is refused and nothing is re-run. A file that cannot be published is
    // reported as final, because Python would have raised at the same point with the commit already done.
    publish_files(
        planning,
        &files.plan_name,
        &files.plan_text,
        &files.latest_correlation,
    )
    .map_err(|error| {
        SeamError::new(
            ErrorCode::Internal,
            format!(
                "plan committed but its files are not written: {}",
                error.kind()
            ),
        )
        .with_details(vec![("write_state".to_string(), jstr("final"))])
    })?;
    let transitions = derived
        .ops
        .iter()
        .any(|op| matches!(op, PlanOp::Transition(_)));
    write_manifest(
        call,
        &manifest(&derived.effects, Some(planning.corr), transitions),
    )
    .ok();
    Ok(finish(reply, &call.agent, &call.script))
}

/// Appends the signed kickoff to a reply, as `_execution_result` does: `execution_assignment` and `execution_assignment_verified`.
fn with_kickoff(
    keys: &KeyMaterial,
    kickoff: Option<&Kickoff>,
    mut reply: Fields,
    now: f64,
) -> Result<Fields, SeamError> {
    if let Some(kickoff) = kickoff {
        let assignment = sign_kickoff(keys, kickoff, now)?;
        reply.push(("execution_assignment".to_string(), assignment));
        reply.push((
            "execution_assignment_verified".to_string(),
            Json::Bool(true),
        ));
    }
    Ok(reply)
}

/// `build_envelope(source="A01@controller", target="A01", msg_type="task.assign", ...)` then `sign_envelope` and the check of
/// `verify_envelope`. Any failure is not started: the transaction is not committed and Python answers.
fn sign_kickoff(keys: &KeyMaterial, kickoff: &Kickoff, now: f64) -> Result<Json, SeamError> {
    let undecided = |why: &str| {
        decline(&format!(
            "the controller kickoff cannot be signed here: {why}"
        ))
    };
    let random = random_u128().map_err(|_| undecided("no entropy source"))?;
    let envelope = Json::Obj(vec![
        (
            "msg_id".to_string(),
            jstr(&uuid7((now * 1000.0) as u64, random)),
        ),
        ("correlation_id".to_string(), jstr(&kickoff.correlation_id)),
        (
            "trace_id".to_string(),
            jstr(&format!(
                "{:032x}",
                uuid4_bits().map_err(|_| undecided("no entropy source"))?
            )),
        ),
        ("causation_id".to_string(), Json::Null),
        ("source".to_string(), jstr("A01@controller")),
        ("target".to_string(), jstr("A01")),
        ("type".to_string(), jstr("task.assign")),
        ("schema".to_string(), jstr("swarm.v1.task.assign")),
        ("ts".to_string(), jstr(&iso_seconds(now))),
        ("ttl_s".to_string(), Json::Int(300)),
        ("priority".to_string(), jstr("P2")),
        ("risk_class".to_string(), jstr(&kickoff.risk_class)),
        ("payload".to_string(), kickoff.payload.clone()),
        ("sig".to_string(), Json::Null),
    ]);
    let mut parsed =
        parse_envelope(&to_python_dumps(&envelope)).map_err(|_| undecided("envelope text"))?;
    sign_envelope(&mut parsed, keys).map_err(|_| undecided("signing"))?;
    if !verify_envelope(&parsed, keys) {
        return Err(undecided("verification"));
    }
    Ok(parsed.as_json().clone())
}

/// The effects manifest the selector replays after the call: `ctx.emit` for each event of `run()` (in order), then the live refresh
/// when a transition committed. `correlation_id` is the run's correlation, `null` when the call names none. `BindGraph` is absent by
/// construction: this route declines a call with a substrate URL, where `_bind_graph` is the only effect.
fn manifest(effects: &[PlanEffect], correlation: Option<&str>, live_refresh: bool) -> Json {
    let items = effects
        .iter()
        .filter_map(|effect| match effect {
            PlanEffect::Emit {
                event_type,
                payload,
            } => Some(Json::Obj(vec![
                ("type".to_string(), jstr(event_type)),
                ("payload".to_string(), Json::Obj(payload.clone())),
                ("task_id".to_string(), Json::Null),
            ])),
            PlanEffect::BindGraph(_) => None,
        })
        .collect();
    Json::Obj(vec![
        (
            "correlation_id".to_string(),
            correlation.map_or(Json::Null, jstr),
        ),
        ("effects".to_string(), Json::Arr(items)),
        ("live_refresh".to_string(), Json::Bool(live_refresh)),
    ])
}

/// The plan file and `latest_correlation`, in Python's order: the plan is published atomically, the pointer is written in place.
fn publish_files(
    planning: &Planning<'_>,
    plan_name: &str,
    plan_text: &str,
    latest: &str,
) -> io::Result<()> {
    fs::create_dir_all(planning.plans_dir)?;
    write_plan_file(planning.plans_dir, plan_name, plan_text)?;
    fs::write(planning.swarm.join("latest_correlation"), latest)
}

/// `_write_text_atomic`: a private temporary file beside the target (`mkstemp`, so mode `0600`), written and `fsync`ed, then renamed.
fn write_plan_file(dir: &Path, name: &str, text: &str) -> io::Result<()> {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_nanos());
    let temporary = dir.join(format!(".{name}.{}.{nonce:x}.tmp", std::process::id()));
    let written = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(text.as_bytes())?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, dir.join(name))
    })();
    if written.is_err() {
        fs::remove_file(&temporary).ok();
    }
    written
}

/// The text of `plans/<owner>.json` as `_read_plan_tasks` reads it: `None` when it cannot be opened (`OSError`); text that is not
/// UTF-8 is a traceback in Python, so it is declined.
fn read_plan_text(dir: &Path, owner: &str) -> Result<Option<String>, &'static str> {
    match fs::read(dir.join(format!("{owner}.json"))) {
        Err(_) => Ok(None),
        Ok(bytes) => String::from_utf8(bytes)
            .map(|text| Some(universal_newlines(&text)))
            .map_err(|_| "a stored plan file is not UTF-8 text"),
    }
}

/// `Path.read_text(encoding="utf-8")`: universal newlines over the decoded file; any failure is [`FileRead::Unreadable`].
fn read_text_file(path: &Path) -> FileRead {
    match fs::read(path)
        .ok()
        .and_then(|bytes| String::from_utf8(bytes).ok())
    {
        Some(text) => FileRead::Text(universal_newlines(&text)),
        None => FileRead::Unreadable,
    }
}

fn universal_newlines(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

/// The suffixes of the tasks the first derivation names: `<prefix>-<suffix>` for each `create`.
fn suffixes_of(ops: &[PlanOp], prefix: &str) -> Result<Vec<String>, SeamError> {
    let lead = format!("{prefix}-");
    ops.iter()
        .filter_map(|op| match op {
            PlanOp::Create(task) => Some(
                task.task_id
                    .strip_prefix(&lead)
                    .map(str::to_string)
                    .ok_or_else(|| decline("a created task id does not carry the plan prefix")),
            ),
            PlanOp::Transition(_) => None,
        })
        .collect()
}

/// `plan_derive::candidate_prefixes`: the explicit prefix, else the 4 and the 6 character derived prefixes of the correlation.
fn candidate_prefixes_of(corr: &str, explicit: Option<&str>) -> Vec<String> {
    gsd_route::plan_derive::candidate_prefixes(corr, explicit)
}

/// The signing state `_execution_result` reads. Python reads `SWARM_AGENT_SESSION` and `SWARM_CHILD` from its process environment;
/// the request forwards the same two keys, and a forwarded value that disagrees with the process is declined, not guessed.
fn signing_state(keys: &KeyMaterial, call: &Invocation) -> Result<Signing, SeamError> {
    let mut agent_session = false;
    for name in ["SWARM_AGENT_SESSION", "SWARM_CHILD"] {
        let process = env_flag(name);
        if env_value(&call.env, name).is_some_and(|forwarded| (forwarded == "1") != process) {
            return Err(decline(
                "the forwarded session flags disagree with the process environment",
            ));
        }
        agent_session |= process;
    }
    Ok(Signing {
        agent_session,
        config_error: keys.config_error().map(str::to_string),
        real_key: keys.real_key_configured(),
    })
}

fn env_flag(name: &str) -> bool {
    std::env::var(name).ok().as_deref() == Some("1")
}

/// `_substrate_on`: the substrate integration is on when a `SUBSTRATE_URL` names http(s) and `SUBSTRATE_DISABLED` is not `1`.
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
            let url = url.trim().trim_end_matches('/').to_ascii_lowercase();
            url.starts_with("http://") || url.starts_with("https://")
        })
        .unwrap_or(false)
}

fn now_seconds() -> Result<f64, SeamError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .map_err(|_| decline("the system clock is before the Unix epoch"))
}

/// A not-started refusal: nothing was written and Python answers.
fn decline(why: &str) -> SeamError {
    before_any_write(SeamError::new(
        ErrorCode::Dep,
        format!("orch_plan: {why}; Python answers"),
    ))
}

/// The store layer's refusal, with the write state Python's own answer would carry.
fn store_refusal(error: &WriterError) -> SeamError {
    let code = match error.kind() {
        ErrorKind::Busy => ErrorCode::Internal,
        _ => error.code(),
    };
    let refusal = SeamError::new(code, error.message());
    match error.write_state() {
        WriteState::NotStarted => before_any_write(refusal),
        WriteState::Final => refusal.with_details(vec![("write_state".to_string(), jstr("final"))]),
    }
}

/// 16 bytes from the kernel's random pool (`/dev/urandom`): the entropy of `uuid.uuid4()` and `_uuid7`.
fn random_u128() -> io::Result<u128> {
    let mut buffer = [0u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut buffer)?;
    Ok(u128::from_be_bytes(buffer))
}

/// The bits of a version 4 UUID.
fn uuid4_bits() -> io::Result<u128> {
    let mut bytes = random_u128()?.to_be_bytes();
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(u128::from_be_bytes(bytes))
}

/// `str(uuid.uuid4())`.
fn random_uuid4() -> io::Result<String> {
    Ok(format_uuid(uuid4_bits()?))
}

fn format_uuid(bits: u128) -> String {
    let hex = format!("{bits:032x}");
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

/// `orch_plan`'s envelope `_uuid7`: the millisecond time, the version 7 nibble, the variant bits and the random bits of `rand`.
fn uuid7(millis: u64, rand: u128) -> String {
    let rand = rand & ((1u128 << 74) - 1);
    let value = ((millis as u128) << 80)
        | (0x7u128 << 76)
        | (((rand >> 2) & 0xfff) << 64)
        | (0b10u128 << 62)
        | (rand & ((1u128 << 62) - 1));
    format_uuid(value)
}

/// `time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"` for `seconds` since the epoch.
fn iso_seconds(seconds: f64) -> String {
    let secs = seconds.floor() as i64;
    let days = secs.div_euclid(86_400);
    let rest = secs.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.000Z",
        rest / 3600,
        (rest % 3600) / 60,
        rest % 60
    )
}

/// Days since 1970-01-01 to a proleptic Gregorian `(year, month, day)`.
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;
    use std::time::Duration;

    use gsd_envelope::KeyFilePresence;
    use gsd_route::plan_derive::Kickoff;

    use super::*;

    const SEED: &str = concat!(
        "1111111111111111",
        "1111111111111111",
        "1111111111111111",
        "1111111111111111"
    );

    fn argv(tokens: &[&str]) -> Vec<String> {
        tokens.iter().map(|token| token.to_string()).collect()
    }

    #[test]
    fn the_grammar_is_the_long_names_each_once_with_python_choices() {
        assert!(served(&argv(&[
            "--correlation-id",
            "c1",
            "--brief-text",
            "x"
        ])));
        assert!(served(&argv(&[
            "--correlation-id=c1",
            "--pattern=hotfix",
            "--dry-run"
        ])));
        assert!(served(&argv(&[
            "--brief-text",
            "- item",
            "--acceptance",
            "a",
            "--acceptance",
            "b"
        ])));
        assert!(served(&argv(&["--repo", "/tmp/repo", "--json"])));
        assert!(
            !served(&argv(&["--dr"])),
            "an abbreviation is argparse's, not the grammar's"
        );
        assert!(!served(&argv(&["positional"])));
        assert!(
            !served(&argv(&["--brief-text", "-x"])),
            "an option-like value is argparse's"
        );
        assert!(
            !served(&argv(&["--prefix", "a", "--prefix", "b"])),
            "a repeated scalar is not proven"
        );
        assert!(
            !served(&argv(&["--root", "/a", "--repo", "/b"])),
            "--repo is an alias of --root"
        );
        assert!(!served(&argv(&["--pattern", "bogus"])));
        assert!(!served(&argv(&["--priority", "P9"])));
        assert!(!served(&argv(&["--risk-class", "critical"])));
        assert!(!served(&argv(&["--dry-run=1"])));
    }

    #[test]
    fn the_roster_forms_are_declined_before_any_read() {
        assert!(!served(&argv(&[
            "--pattern",
            "custom",
            "--plan",
            "plan.json"
        ])));
        assert!(!served(&argv(&["--plan", "plan.json"])));
        assert!(!served(&argv(&["--slices", "slices.json"])));
        assert!(!served(&argv(&["--slices-json", "[]"])));
        assert!(!served(&argv(&["--pattern", "parallel"])));
    }

    #[test]
    fn the_clock_and_id_formatters_equal_pythons() {
        assert_eq!(
            uuid7(1_700_000_123_456, 0x0123_4567_89ab_cdef_0fed_cba9_8765_4321),
            "018bcfe7-4a40-70c8-8fed-cba987654321"
        );
        assert_eq!(
            uuid7(1, (1u128 << 74) - 1),
            "00000000-0001-7fff-bfff-ffffffffffff"
        );
        assert_eq!(iso_seconds(1_700_000_123.5), "2023-11-14T22:15:23.000Z");
        assert_eq!(iso_seconds(951_782_400.0), "2000-02-29T00:00:00.000Z");
        assert_eq!(iso_seconds(4_102_444_799.0), "2099-12-31T23:59:59.000Z");
        assert_eq!(
            format_uuid(0x0123_4567_89ab_cdef_0123_4567_89ab_cdef).len(),
            36
        );
    }

    #[test]
    fn read_text_applies_universal_newlines_as_python_does() {
        assert_eq!(universal_newlines("a\r\nb\rc\n"), "a\nb\nc\n");
        assert!(matches!(
            read_text_file(Path::new("/nonexistent/brief.md")),
            FileRead::Unreadable
        ));
    }

    #[test]
    fn the_kickoff_is_signed_and_verifies_under_the_configured_key() {
        let keys = KeyMaterial::from_lookup(
            |name| (name == "SWARM_ED25519_KEY").then(|| OsString::from(SEED)),
            |_| KeyFilePresence::Absent,
        )
        .expect("the key loads");
        assert!(keys.real_key_configured());
        let kickoff = Kickoff {
            correlation_id: "c1".to_string(),
            risk_class: "medium".to_string(),
            payload: Json::Obj(vec![("task_id".to_string(), jstr("c1"))]),
        };
        let signed = sign_kickoff(&keys, &kickoff, 1_700_000_123.5).expect("the kickoff signs");
        let Json::Obj(fields) = &signed else {
            panic!("an envelope object");
        };
        let get = |key: &str| {
            fields
                .iter()
                .find(|(name, _)| name == key)
                .map(|(_, value)| value.clone())
        };
        assert_eq!(get("type"), Some(jstr("task.assign")));
        assert_eq!(get("source"), Some(jstr("A01@controller")));
        assert_eq!(get("ts"), Some(jstr("2023-11-14T22:15:23.000Z")));
        assert_eq!(get("correlation_id"), Some(jstr("c1")));
        assert!(matches!(get("sig"), Some(Json::Str(sig)) if !sig.is_empty()));
        let parsed = gsd_envelope::parse_envelope(&to_python_dumps(&signed)).expect("parses back");
        assert!(
            verify_envelope(&parsed, &keys),
            "the signed kickoff verifies"
        );
    }

    #[tokio::test]
    async fn a_call_outside_the_grammar_is_declined_as_not_started() {
        let call = Invocation {
            agent: "A01".to_string(),
            script: "orch_plan".to_string(),
            argv: argv(&["--dr"]),
            root: std::env::temp_dir(),
            env: Vec::new(),
            deadline: Duration::from_secs(5),
            effects_path: None,
        };
        let error = answer(&call).await.expect_err("declined");
        assert_eq!(error.code(), ErrorCode::Dep);
        assert!(error
            .details()
            .iter()
            .any(|(key, value)| key == "write_state" && *value == jstr("not_started")));
    }

    #[test]
    fn a_random_correlation_id_is_a_version_four_uuid() {
        let id = random_uuid4().expect("entropy");
        let bytes = id.as_bytes();
        assert_eq!(id.len(), 36);
        assert_eq!(bytes[8], b'-');
        assert_eq!(bytes[14], b'4', "version nibble");
        assert!(
            matches!(bytes[19], b'8' | b'9' | b'a' | b'b'),
            "variant bits"
        );
        assert!(
            is_safe_id(&id),
            "a random id passes the id rule it is checked against"
        );
    }
}
