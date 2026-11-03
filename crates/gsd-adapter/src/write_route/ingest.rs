//! The host `orch_status --ingest` route (phase 14.5, unit U3.5e part 2).
//!
//! [`ingest_reply_checked`] reads the result file, calls [`TaskStoreWriter::ingest`], and hands the post-commit effects to the
//! selector in the effects manifest. Rust answers only what it can prove byte-identical to Python, and refuses everything
//! else BEFORE the first write with `E-DEP` and `write_state = "not_started"`, so the Python body answers. After the
//! commit nothing is ever re-run.
//!
//! The manifest is ingest-shaped, not the transition manifest: `{"effects": [{"type", "payload", "task_id"}], "inline":
//! {"task_id", "from", "reason"} | null, "live_refresh": bool}`. On a committed call `inline` is set and the selector
//! emits the events, then the inline transition, then one live refresh when `live_refresh` is true. On Python's own
//! refusal the events are in the manifest and `inline` is null; the selector emits those and prints the error.

use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use gsd_failure::ErrorCode;
use gsd_route::ingest_args::IngestArgs;
use gsd_route::reconcile_plan::EmitEvent;
use gsd_route::result_parse::{parse_result, validate_result, ResultError};
use gsd_route::{check_safe_id, finish, jstr, obj_get, Json, Reply, SeamError};
use gsd_taskwriter::ingest::IngestCheck;
use gsd_taskwriter::{
    open_for_write, ApprovalSource, IngestCause, IngestError, IngestRequest, TaskStoreWriter,
    WriteState, WriterConfig,
};

use crate::strangler_seam::{
    before_any_write, canonical_or, env_value, resolve_swarm_dir, Invocation,
};
use crate::write_route::write_manifest;

/// Answers one `--ingest` call that [`IngestArgs`] parsed on the host: the host verifies nothing, its plan is the answer. Every `Err` is
/// a final reply, or a not-started refusal the caller hands to Python. `check` runs on the plan inside the write transaction, before its
/// first write (see [`IngestCheck`]). A check that stops the call answers `E-TIMEOUT` with nothing written. `source` gives the keys an
/// APPROVED step is checked with.
pub(crate) async fn ingest_reply_checked(
    args: &IngestArgs,
    call: &Invocation,
    check: &dyn IngestCheck,
    source: &dyn ApprovalSource,
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

    let text = match fs::read_to_string(&args.path) {
        Ok(text) => text,
        Err(_) => {
            return Err(before_any_write(SeamError::new(
                ErrorCode::Dep,
                "the result file is not one this route read; Python answers",
            )));
        }
    };
    let parsed = parse_result(&text).map_err(|_| {
        before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the result text is not one this route proves; Python answers",
        ))
    })?;
    let task_id = context_task
        .map(str::to_string)
        .or_else(|| task_id_of(parsed.as_ref()));
    let Some(task_id) = task_id else {
        return no_task_id(call);
    };
    let result = match validate_result(parsed.as_ref(), &task_id) {
        Ok(value) => Ok(value.clone()),
        Err(ResultError::NotStarted(_)) => {
            return Err(before_any_write(SeamError::new(
                ErrorCode::Dep,
                "the result is not one this route proves; Python answers",
            )));
        }
        Err(error) => Err(error),
    };
    if call.effects_path.is_none() {
        return Err(before_any_write(SeamError::new(
            ErrorCode::Dep,
            "an ingest needs an effects_path to hand its post-commit effects to the caller",
        )));
    }

    let root = canonical_or(&call.root.join(args.root.as_deref().unwrap_or(".")));
    let swarm = resolve_swarm_dir(&root, env_value(&call.env, "SWARM_DIR"));
    let store = open_for_write(&WriterConfig::new(swarm.join("tasks.db")))
        .await
        .map_err(|error| before_any_write(SeamError::new(error.code(), error.message())))?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| {
            before_any_write(SeamError::new(
                ErrorCode::Internal,
                "the system clock is before the Unix epoch",
            ))
        })?
        .as_secs_f64();
    let request = IngestRequest {
        task_id: task_id.clone(),
        result,
        meta: Json::Obj(vec![("ingest".to_string(), jstr(&args.path))]),
        advisory: args.advisory,
    };
    let writer = TaskStoreWriter::new(store);
    let outcome = writer.ingest_with(&request, now, source, check).await;
    writer.close().await;
    match outcome {
        Ok(applied) => {
            let now_state = match obj_get(&applied.task, "state") {
                Some(Json::Str(text)) => text.clone(),
                _ => applied.after.clone(),
            };
            write_manifest(
                call,
                &manifest(
                    &applied.events,
                    Some((&task_id, &applied.before, &file_name(&args.path))),
                    !applied.applied.effects.is_empty(),
                ),
            )
            .ok();
            Ok(finish(
                vec![
                    ("status".to_string(), jstr("ok")),
                    ("task".to_string(), Json::Obj(applied.task)),
                    (
                        "reconcile".to_string(),
                        Json::Arr(
                            applied
                                .notes_log
                                .into_iter()
                                .map(|line| jstr(&line))
                                .collect(),
                        ),
                    ),
                    (
                        "summary".to_string(),
                        jstr(&format!(
                            "ingested task.result for {task_id} → {} (now {now_state})",
                            applied.reply
                        )),
                    ),
                ],
                &call.agent,
                &call.script,
            ))
        }
        Err(error) => {
            if error.write_state() == WriteState::Final
                && matches!(error.cause(), IngestCause::Refused)
            {
                write_manifest(call, &manifest(error.events(), None, false)).ok();
            }
            Err(ingest_refusal(&error))
        }
    }
}

fn task_id_of(result: Option<&Json>) -> Option<String> {
    let fields = match result {
        Some(Json::Obj(fields)) => fields,
        _ => return None,
    };
    match obj_get(fields, "task_id") {
        Some(Json::Str(text)) if !text.is_empty() => Some(text.clone()),
        _ => None,
    }
}

fn file_name(path: &str) -> String {
    Path::new(path)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or(path)
        .to_string()
}

fn no_task_id(call: &Invocation) -> Result<Reply, SeamError> {
    let event = EmitEvent {
        event_type: "task.result.rejected".to_string(),
        payload: vec![
            ("task_id".to_string(), Json::Null),
            ("mode".to_string(), jstr("ingest")),
            ("reason".to_string(), jstr("no task_id in result")),
        ],
        task_id: None,
    };
    write_manifest(call, &manifest(std::slice::from_ref(&event), None, false)).ok();
    Err(SeamError::new(
        ErrorCode::Contract,
        "task.result has no task_id (use --task-id or include task_id)",
    ))
}

fn ingest_refusal(error: &IngestError) -> SeamError {
    let seam = error.to_seam_error();
    match error.write_state() {
        WriteState::NotStarted => before_any_write(seam),
        WriteState::Final if matches!(error.cause(), IngestCause::Refused) => seam,
        WriteState::Final => {
            let mut details = seam.details().to_vec();
            details.push(("write_state".to_string(), jstr("final")));
            seam.with_details(details)
        }
    }
}

fn manifest(events: &[EmitEvent], inline: Option<(&str, &str, &str)>, live_refresh: bool) -> Json {
    let effects = events
        .iter()
        .map(|event| {
            Json::Obj(vec![
                ("type".to_string(), jstr(&event.event_type)),
                ("payload".to_string(), Json::Obj(event.payload.clone())),
                (
                    "task_id".to_string(),
                    event.task_id.as_deref().map(jstr).unwrap_or(Json::Null),
                ),
            ])
        })
        .collect();
    let inline = match inline {
        Some((task_id, from, name)) => Json::Obj(vec![
            ("task_id".to_string(), jstr(task_id)),
            ("from".to_string(), jstr(from)),
            ("reason".to_string(), jstr(&format!("ingest {name}"))),
        ]),
        None => Json::Null,
    };
    Json::Obj(vec![
        ("effects".to_string(), Json::Arr(effects)),
        ("inline".to_string(), inline),
        ("live_refresh".to_string(), Json::Bool(live_refresh)),
    ])
}
