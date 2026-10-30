//! The host `orch_status --transition` route (phase 14.5, unit U3.2a): `scripts/orch_status.py` `run` for `--transition`, in Rust.
//!
//! [`transition_reply`] answers a call inside the grammar of [`TransitionArgs`] with the bytes Python prints, or refuses it. The rule
//! of the route (Design 1 of the 14.5 plan) is that Rust answers only what it can prove byte-identical to Python, and refuses
//! everything else BEFORE the first write with `E-DEP` and `error.details.write_state = "not_started"`, so the Python body answers.
//! After the commit nothing is ever re-run.
//!
//! The steps, in Python's order:
//!
//! 1. the ids: `--task-id` (or `SWARM_TASK_ID`) and `--correlation-id` (or `SWARM_CORRELATION_ID`) must be safe ids, else
//!    `E-INPUT` (Python's `AgentScript.main` checks them before `run`; the reply carries no `write_state`: it is Python's own);
//! 2. the store: `<swarm dir>/tasks.db` is opened with [`open_for_write`], which runs no DDL. A missing file or schema is a
//!    not-started refusal, because Python's `TaskStore(root=...)` would create or migrate it first. An unreadable
//!    `latest_correlation` file (Python reads it before the transition) is the same;
//! 3. `--dry-run`: the canned reply, nothing written;
//! 4. [`TaskStoreWriter::transition`] with the decider of the backend (the host decision on the host backend; on the sandbox backend
//!    the guest as a verified shadow of it, which the host decision replaces on any fault or difference before the apply step) and
//!    `check_manual_transition` (`manual: true`), actor `A01`, reason `--reason` or `manual`: one `BEGIN IMMEDIATE` transaction from
//!    the read of the row to the `COMMIT`;
//! 5. after the `COMMIT`: the effects manifest is written to `Invocation::effects_path` (atomically: a temporary file in the same
//!    directory, then a rename) and the reply is built from the committed row.
//!
//! The effects stay Python (open question 6): the manifest tells the selector what to replay, in Python's order, after the
//! commit. See [`effects_manifest`] for its shape. A call without an `effects_path` that would commit is refused before the write,
//! because its effects would be lost without a trace.
//!
//! Errors keep Python's code, message and `details` when Python decided them (`E-CONTRACT` illegal transition, `E-POLICY` for a
//! gate task, `E-INPUT` unknown task, `E-TIMEOUT` `max_attempts exceeded`, `E-CONTRACT` concurrent transition); those replies
//! carry no `write_state`. A refusal that is Rust's own and happened before the first write is `write_state = "not_started"`; a
//! failure at or after the first write that is not Python's is `write_state = "final"`.
pub(crate) mod ingest;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use gsd_failure::ErrorCode;
use gsd_route::pydumps::to_python_dumps;
use gsd_route::transition_args::TransitionArgs;
use gsd_route::transition_plan::{PlanEffect, Request};
use gsd_route::{check_safe_id, finish, jstr, obj_get, Fields, Json, Reply, SeamError};
use gsd_taskwriter::{
    open_for_write, Applied, ApplyCause, ApplyError, Decider, EnvApprovalSource, ErrorKind,
    TaskStoreWriter, WriteState, WriterConfig, WriterError,
};

use crate::strangler_seam::{
    before_any_write, canonical_or, env_value, resolve_swarm_dir, Invocation,
};

/// The actor of a manual transition (`orch_status.py`: `actor="A01"`).
const ACTOR: &str = "A01";

/// The audit reason of a transition whose `--reason` is empty (`args.reason or "manual"`).
const DEFAULT_REASON: &str = "manual";

/// Answers one `--transition` call that [`TransitionArgs`] parsed, with the decide step named by the caller: the reply Python
/// prints, or the refusal. The host backend passes [`HostDecider`](gsd_taskwriter::HostDecider); the sandbox backend passes its guest
/// decider ([`SandboxDecider`](crate::sandbox_engine::SandboxDecider)). Everything else, the checks before the write and the
/// manifest and reply after the commit, is the same code, so the two backends differ only in who decided.
///
/// Every `Err` is a final reply: its `details` say whether anything was written (`write_state`, see the module documentation).
pub(crate) async fn transition_reply(
    args: &TransitionArgs,
    call: &Invocation,
    decider: &dyn Decider,
) -> Result<Reply, SeamError> {
    let context_task = match &args.context_task_id {
        Some(value) => Some(value.as_str()),
        None => env_value(&call.env, "SWARM_TASK_ID"),
    };
    if let Some(id) = context_task {
        check_safe_id(id, "task id")?;
    }
    let correlation = match &args.correlation_id {
        Some(value) => Some(value.as_str()),
        None => env_value(&call.env, "SWARM_CORRELATION_ID"),
    };
    if let Some(id) = correlation {
        check_safe_id(id, "correlation id")?;
    }

    let root = canonical_or(&call.root.join(args.root.as_deref().unwrap_or(".")));
    let swarm = resolve_swarm_dir(&root, env_value(&call.env, "SWARM_DIR"));
    if correlation.is_none() && swarm.join("latest_correlation").exists() {
        // Python reads the file before it transitions; text it cannot read or decode is a traceback there.
        fs::read_to_string(swarm.join("latest_correlation")).map_err(|error| {
            before_any_write(SeamError::new(
                ErrorCode::Dep,
                format!("latest_correlation is not readable: {}", error.kind()),
            ))
        })?;
    }
    let store = open_for_write(&WriterConfig::new(swarm.join("tasks.db")))
        .await
        .map_err(|error| store_refusal(&error))?;

    if args.dry_run {
        store.close().await;
        write_manifest(call, &no_effects()).map_err(before_any_write)?;
        return Ok(finish(
            vec![
                ("status".to_string(), jstr("ok")),
                (
                    "summary".to_string(),
                    jstr(&format!(
                        "dry-run: would transition {} → {}",
                        args.task_id, args.state
                    )),
                ),
            ],
            &call.agent,
            &call.script,
        ));
    }
    if call.effects_path.is_none() {
        store.close().await;
        return Err(before_any_write(SeamError::new(
            ErrorCode::Dep,
            "a transition needs an effects_path to hand its post-commit effects to the caller",
        )));
    }

    let request = Request {
        task_id: args.task_id.clone(),
        to: args.state.clone(),
        actor: ACTOR.to_string(),
        reason: effective_reason(args).to_string(),
        manual: true,
    };
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| {
            before_any_write(SeamError::new(
                ErrorCode::Internal,
                "the system clock is before the Unix epoch",
            ))
        })?
        .as_secs_f64();
    // A transition to APPROVED reads the signing keys from this process's environment, once, before the transaction opens (D-P24);
    // every other target reads none. The route never holds a key itself: it hands the writer a source.
    let writer = TaskStoreWriter::with_approval(store, Arc::new(EnvApprovalSource));
    let outcome = writer.transition(&request, now, decider).await;
    writer.close().await;
    let applied = outcome.map_err(|error| apply_refusal(&error))?;

    // The transaction is committed: from here on nothing is refused and nothing is re-run. A manifest that cannot be written is
    // a lost effect, the window Python has between its commit and its emit; the caller finds the file missing and says so.
    write_manifest(call, &effects_manifest(args, &applied)).ok();
    let Some(Json::Str(state)) = obj_get(&applied.task, "state") else {
        return Err(
            SeamError::new(ErrorCode::Internal, "the committed row has no state")
                .with_details(vec![("write_state".to_string(), jstr("final"))]),
        );
    };
    Ok(finish(
        vec![
            ("status".to_string(), jstr("ok")),
            ("task".to_string(), Json::Obj(applied.task.clone())),
            (
                "summary".to_string(),
                jstr(&format!("{} → {state}", args.task_id)),
            ),
        ],
        &call.agent,
        &call.script,
    ))
}

/// `args.reason or "manual"`: the reason of the audit row and of the inline run event.
fn effective_reason(args: &TransitionArgs) -> &str {
    if args.reason.is_empty() {
        DEFAULT_REASON
    } else {
        &args.reason
    }
}

/// The manifest of a call that changes nothing.
fn no_effects() -> Json {
    Json::Obj(vec![
        ("effects".to_string(), Json::Arr(Vec::new())),
        ("post".to_string(), Json::Arr(Vec::new())),
    ])
}

/// The post-commit effects of a committed transition, as the caller replays them (Design 8 of the 14.5 plan).
///
/// `{"effects": [{"type", "payload", "task_id"}], "post": [{"kind": "live_refresh"}, {"kind": "lease_settle", "task_id"},
/// {"kind": "inline_transition", "task_id", "from", "reason"}]}`. The caller runs `post` in order (`TaskStore`'s observer refresh,
/// `substrate_lease.settle_mirrored`, `observability.emit_inline_transition`), then emits each of `effects` (`ctx.emit`), which
/// is the order of `scripts/orch_status.py`. `from` is the row's state before the call (an auto-escalation's first audit row);
/// `reason` of `inline_transition` is `--reason or "manual"`, the `reason` of the `task.transition` payload is `--reason` as
/// given.
fn effects_manifest(args: &TransitionArgs, applied: &Applied) -> Json {
    let task_id = jstr(&args.task_id);
    let state = obj_get(&applied.task, "state")
        .cloned()
        .unwrap_or(Json::Null);
    let mut post: Vec<Json> = applied
        .effects
        .iter()
        .map(|effect| match effect {
            PlanEffect::LiveRefresh => Json::Obj(vec![("kind".to_string(), jstr("live_refresh"))]),
        })
        .collect();
    post.push(Json::Obj(vec![
        ("kind".to_string(), jstr("lease_settle")),
        ("task_id".to_string(), task_id.clone()),
    ]));
    post.push(Json::Obj(vec![
        ("kind".to_string(), jstr("inline_transition")),
        ("task_id".to_string(), task_id.clone()),
        (
            "from".to_string(),
            applied
                .audit
                .first()
                .map_or(Json::Null, |step| jstr(&step.from)),
        ),
        ("reason".to_string(), jstr(effective_reason(args))),
    ]));
    let transition: Fields = vec![
        ("task_id".to_string(), task_id.clone()),
        ("state".to_string(), state),
        ("reason".to_string(), jstr(&args.reason)),
    ];
    Json::Obj(vec![
        (
            "effects".to_string(),
            Json::Arr(vec![Json::Obj(vec![
                ("type".to_string(), jstr("task.transition")),
                ("payload".to_string(), Json::Obj(transition)),
                ("task_id".to_string(), task_id),
            ])]),
        ),
        ("post".to_string(), Json::Arr(post)),
    ])
}

/// Writes `manifest` to the call's `effects_path`, atomically: a private temporary file in the same directory, then a rename over
pub(crate) fn write_manifest(call: &Invocation, manifest: &Json) -> Result<(), SeamError> {
    let Some(path) = &call.effects_path else {
        return Ok(());
    };
    let fail = |error: std::io::Error| {
        SeamError::new(
            ErrorCode::Dep,
            format!("effects manifest not written: {}", error.kind()),
        )
    };
    let temporary = temporary_beside(path);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)
        .map_err(fail)?;
    let written = file
        .write_all(to_python_dumps(manifest).as_bytes())
        .and_then(|()| file.flush());
    drop(file);
    match written.and_then(|()| fs::rename(&temporary, path)) {
        Ok(()) => Ok(()),
        Err(error) => {
            fs::remove_file(&temporary).ok();
            Err(fail(error))
        }
    }
}

/// A name for the temporary file beside `path` that no other process of this host uses at the same time.
fn temporary_beside(path: &Path) -> std::path::PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(format!(".{}.tmp", std::process::id()));
    path.with_file_name(name)
}

/// A refusal of the store layer before any write: nothing was written and Python answers. A busy store is `E-INTERNAL`.
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

/// The reply for a failed [`TaskStoreWriter::transition`].
///
/// Python's own refusals (the decision's `Final` refusals and a concurrent transition) keep their code, message and `details`
/// unchanged, so the stdout bytes are Python's. A `NotStarted` failure adds `write_state = "not_started"`; any other failure that
/// Python would not have answered the same way adds `write_state = "final"`.
fn apply_refusal(error: &ApplyError) -> SeamError {
    let seam = SeamError::new(error.code(), error.message()).with_details(error.details().to_vec());
    match (error.write_state(), error.cause()) {
        (WriteState::NotStarted, ApplyCause::Store(ErrorKind::Busy)) => {
            before_any_write(SeamError::new(ErrorCode::Internal, error.message()))
        }
        (WriteState::NotStarted, _) => before_any_write(seam),
        (
            WriteState::Final,
            ApplyCause::Refused | ApplyCause::Store(ErrorKind::ConcurrentTransition),
        ) => seam,
        (WriteState::Final, _) => {
            let mut details = error.details().to_vec();
            details.push(("write_state".to_string(), jstr("final")));
            seam.with_details(details)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_taskwriter::AppliedAudit;

    fn args(task_id: &str, state: &str, reason: &str) -> TransitionArgs {
        TransitionArgs {
            task_id: task_id.to_string(),
            state: state.to_string(),
            reason: reason.to_string(),
            ..TransitionArgs::default()
        }
    }

    fn applied(from: &str, to: &str, effects: Vec<PlanEffect>) -> Applied {
        Applied {
            task: vec![
                ("task_id".to_string(), jstr("T-1")),
                ("state".to_string(), jstr(to)),
            ],
            audit: vec![AppliedAudit {
                from: from.to_string(),
                to: to.to_string(),
                actor: ACTOR.to_string(),
                reason: "r".to_string(),
                ts: 1.0,
            }],
            effects,
        }
    }

    #[test]
    fn the_manifest_lists_the_post_steps_in_pythons_order_and_the_event_after_them() {
        let manifest = effects_manifest(
            &args("T-1", "DONE", "because"),
            &applied("APPROVED", "DONE", vec![PlanEffect::LiveRefresh]),
        );
        assert_eq!(
            to_python_dumps(&manifest),
            concat!(
                "{\"effects\": [{\"type\": \"task.transition\", \"payload\": {\"task_id\": \"T-1\", \"state\": \"DONE\", ",
                "\"reason\": \"because\"}, \"task_id\": \"T-1\"}], \"post\": [{\"kind\": \"live_refresh\"}, ",
                "{\"kind\": \"lease_settle\", \"task_id\": \"T-1\"}, {\"kind\": \"inline_transition\", \"task_id\": \"T-1\", ",
                "\"from\": \"APPROVED\", \"reason\": \"because\"}]}"
            )
        );
    }

    #[test]
    fn the_inline_reason_defaults_to_manual_and_the_event_payload_keeps_the_empty_reason() {
        let manifest = effects_manifest(
            &args("T-1", "DONE", ""),
            &applied("APPROVED", "DONE", Vec::new()),
        );
        let text = to_python_dumps(&manifest);
        assert!(
            text.contains(
                "\"payload\": {\"task_id\": \"T-1\", \"state\": \"DONE\", \"reason\": \"\"}"
            ),
            "{text}"
        );
        assert!(
            text.contains("\"from\": \"APPROVED\", \"reason\": \"manual\"}]}"),
            "{text}"
        );
        // No plan effect, no live refresh: the manifest says what the plan said.
        assert!(!text.contains("live_refresh"), "{text}");
    }

    #[test]
    fn a_manifest_is_written_private_and_replaces_the_callers_empty_file() -> Result<(), String> {
        let dir = tempfile::TempDir::new().map_err(|e| e.to_string())?;
        let path = dir.path().join("effects.json");
        fs::write(&path, "").map_err(|e| e.to_string())?;
        let mut call = Invocation {
            agent: "A01".to_string(),
            script: "orch_status".to_string(),
            argv: Vec::new(),
            root: dir.path().to_path_buf(),
            env: Vec::new(),
            deadline: std::time::Duration::from_secs(5),
            effects_path: Some(path.clone()),
        };
        write_manifest(&call, &no_effects()).map_err(|e| e.message().to_string())?;
        assert_eq!(
            fs::read_to_string(&path).map_err(|e| e.to_string())?,
            "{\"effects\": [], \"post\": []}"
        );
        let mode = std::os::unix::fs::PermissionsExt::mode(
            &fs::metadata(&path)
                .map_err(|e| e.to_string())?
                .permissions(),
        );
        assert_eq!(mode & 0o777, 0o600);
        assert_eq!(
            fs::read_dir(dir.path()).map_err(|e| e.to_string())?.count(),
            1
        );

        // A directory that does not exist is an error and leaves nothing behind; no effects_path writes nothing at all.
        call.effects_path = Some(dir.path().join("gone").join("effects.json"));
        assert!(write_manifest(&call, &no_effects()).is_err());
        call.effects_path = None;
        assert!(write_manifest(&call, &no_effects()).is_ok());
        assert_eq!(
            fs::read_dir(dir.path()).map_err(|e| e.to_string())?.count(),
            1
        );
        Ok(())
    }

    #[test]
    fn the_temporary_name_is_beside_the_target_and_not_the_target() {
        let target = Path::new("/a/b/effects.json");
        let temporary = temporary_beside(target);
        assert_eq!(temporary.parent(), target.parent());
        assert_ne!(temporary, target);
        assert!(temporary.to_string_lossy().ends_with(".tmp"));
    }

    #[test]
    fn an_empty_reason_is_manual_and_a_given_one_is_kept() {
        assert_eq!(effective_reason(&args("T", "DONE", "")), "manual");
        assert_eq!(effective_reason(&args("T", "DONE", "why")), "why");
        assert_eq!(effective_reason(&args("T", "DONE", " ")), " ");
    }

    #[test]
    fn a_store_that_cannot_be_opened_is_a_not_started_refusal() -> Result<(), String> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|e| e.to_string())?;
        let dir = tempfile::TempDir::new().map_err(|e| e.to_string())?;
        let error = runtime
            .block_on(open_for_write(&WriterConfig::new(
                dir.path().join("tasks.db"),
            )))
            .err()
            .ok_or("opened a store that does not exist")?;
        let refusal = store_refusal(&error);
        assert_eq!(refusal.code(), ErrorCode::Dep);
        assert_eq!(
            refusal.details().to_vec(),
            vec![("write_state".to_string(), jstr("not_started"))]
        );
        Ok(())
    }
}
