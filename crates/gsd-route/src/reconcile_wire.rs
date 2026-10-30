//! The wire form of the second-batch guest mode `reconcile` (Phase 14.5 pin P2): the decision of [`plan_reconcile`] as bytes in and bytes
//! out. It also holds the codec of [`ReconcileFacts`] and of the operations of a reconcile plan, which `ingest_wire` shares.
//!
//! The host encodes the facts it reads with [`encode_request`], computes its own answer with [`plan_reconcile`], and holds the guest's
//! reply against [`encode_outcome`] of that answer. The guest side is [`invoke`]: it decodes the request, runs the same
//! [`plan_reconcile`] and renders the answer with [`encode_outcome`]. Nothing here reads a clock, a file, an environment variable or a
//! key: `now`, `approve` and every signature answer are request fields.
//!
//! # Request
//!
//! ```text
//! {"mode": "reconcile", "now": <number>, "approve": <bool>, "facts": <facts>}
//! <facts>    = {"correlation_id": str, "tasks": [<row>], "verdicts": [<verdict>],
//!               "other_tasks": [{"task_id": str, "state": str}], "key_policy": null | {"require_key", "real_key", "dev_key"}}
//! <row>      = a tasks row as a JSON object, its columns as stored
//! <verdict>  = {"row": {"id", "task_id", "gate", "verdict", "envelope_json", "ts"}, "envelope": <envelope>, "sig_ok": bool}
//! <envelope> = {"state": "invalid"} | {"state": "unsupported"} | {"state": "valid", "payload": <payload>}
//! <payload>  = {"gate", "task_id", "verdict", "correlation_id", "issued_at": <num>, "expires_s": <num>, "dry_run": bool,
//!               "gate_task": <gate_task>}
//! <num>      = {"state": "value", "value": number} | {"state": "invalid"} | {"state": "unsupported"}
//! <gate_task>= {"state": "none"} | {"state": "task", "task_id": str} | {"state": "unsupported"}
//! ```
//!
//! # Reply
//!
//! ```text
//! {"plan": {"ops": [<op>], "notes_log": [str]}}
//! {"not_started": str}
//! ```
//!
//! A refusal the planner reports as not started is an ordinary reply, not an `invoke-error`. A request the guest cannot read is an
//! `invoke-error`. Every object holds exactly the keys above: a missing, extra or mistyped key is `E-INPUT` in a request and `E-CONTRACT`
//! in a reply, and the message names the field, never its value.
//!
//! # Determinism
//!
//! Both directions render with [`to_python_dumps`] (keys in the order written, floats through [`py_float`](crate::py_float)) and read
//! with [`parse_python_json`], which refuses what [`Json`] cannot hold byte-exactly. A reply is therefore a pure function of the request.

use gsd_codes::ErrorCode;

use crate::gate_facts::{
    EnvelopeState, GateTask, IssuerState, Num, ParsedEnvelope, VerdictFacts, VerdictRow,
};
use crate::pydumps::{parse_python_json, to_python_dumps};
use crate::reconcile_plan::{
    plan_reconcile, EmitEvent, KeyPolicy, NewTask, NotStarted, ReconcileFacts, ReconcileOp,
    ReconcilePlan,
};
use crate::transition_plan::{AuditRow, PlanEffect, TransitionPlan};
use crate::{jstr, obj_get, Fields, Json, SeamError};

/// The `mode` of a reconcile request.
pub const MODE: &str = "reconcile";

const REQUEST_KEYS: [&str; 4] = ["mode", "now", "approve", "facts"];
pub(crate) const FACTS_KEYS: [&str; 5] = [
    "correlation_id",
    "tasks",
    "verdicts",
    "other_tasks",
    "key_policy",
];
const VERDICT_KEYS: [&str; 3] = ["row", "envelope", "sig_ok"];
const ROW_KEYS: [&str; 6] = ["id", "task_id", "gate", "verdict", "envelope_json", "ts"];
const PAYLOAD_KEYS: [&str; 8] = [
    "gate",
    "task_id",
    "verdict",
    "correlation_id",
    "issued_at",
    "expires_s",
    "dry_run",
    "gate_task",
];
const KEY_POLICY_KEYS: [&str; 3] = ["require_key", "real_key", "dev_key"];
const ISSUER_KEYS: [&str; 2] = ["task_id", "state"];
const PLAN_KEYS: [&str; 5] = ["from", "to", "columns", "audit", "effects"];
const AUDIT_KEYS: [&str; 4] = ["from", "to", "actor", "reason"];
const EFFECT_KEYS: [&str; 1] = ["kind"];
pub(crate) const NEW_TASK_KEYS: [&str; 12] = [
    "task_id",
    "correlation_id",
    "capability",
    "title",
    "agent_id",
    "dag_depth",
    "depends_on",
    "acceptance",
    "budget",
    "risk_class",
    "priority",
    "notes",
];
pub(crate) const EVENT_KEYS: [&str; 3] = ["type", "payload", "task_id"];
pub(crate) const ERROR_KEYS: [&str; 3] = ["code", "message", "details"];
const LIVE_REFRESH: &str = "live_refresh";
const CODES: [ErrorCode; 7] = [
    ErrorCode::Input,
    ErrorCode::Timeout,
    ErrorCode::Dep,
    ErrorCode::Capacity,
    ErrorCode::Contract,
    ErrorCode::Policy,
    ErrorCode::Internal,
];

/// A shape problem, as the description of the field at fault. The public readers turn it into their own error code.
pub(crate) type Shape<T> = Result<T, String>;

pub(crate) fn bad(path: &str) -> String {
    format!("field {path} is missing or has the wrong type")
}

/// The fields of `value`, which must be an object holding exactly `keys`. A missing key is named; an unknown key is reported without
/// its name, which the request controls.
pub(crate) fn exact<'a>(value: &'a Json, path: &str, keys: &[&str]) -> Shape<&'a Fields> {
    let Json::Obj(fields) = value else {
        return Err(bad(path));
    };
    if let Some(key) = keys.iter().find(|key| obj_get(fields, key).is_none()) {
        return Err(bad(&format!("{path}.{key}")));
    }
    if fields.len() != keys.len() {
        return Err(format!("field {path} has a key outside its schema"));
    }
    Ok(fields)
}

/// The member `key` of `fields`; a missing one names `path.key`.
pub(crate) fn get<'a>(fields: &'a [(String, Json)], path: &str, key: &str) -> Shape<&'a Json> {
    obj_get(fields, key).ok_or_else(|| bad(&format!("{path}.{key}")))
}

pub(crate) fn text_of(value: &Json, path: &str) -> Shape<String> {
    match value {
        Json::Str(text) => Ok(text.clone()),
        _ => Err(bad(path)),
    }
}

pub(crate) fn text_at(fields: &[(String, Json)], path: &str, key: &str) -> Shape<String> {
    text_of(get(fields, path, key)?, &format!("{path}.{key}"))
}

pub(crate) fn flag_at(fields: &[(String, Json)], path: &str, key: &str) -> Shape<bool> {
    match get(fields, path, key)? {
        Json::Bool(flag) => Ok(*flag),
        _ => Err(bad(&format!("{path}.{key}"))),
    }
}

/// A JSON number read as `f64`: a float, or an integer that `f64` holds exactly.
pub(crate) fn number_of(value: &Json, path: &str) -> Shape<f64> {
    match value {
        Json::Float(number) => Ok(*number),
        Json::Int(number) if number.unsigned_abs() <= 1 << 53 => Ok(*number as f64),
        _ => Err(bad(path)),
    }
}

pub(crate) fn number_at(fields: &[(String, Json)], path: &str, key: &str) -> Shape<f64> {
    number_of(get(fields, path, key)?, &format!("{path}.{key}"))
}

pub(crate) fn int_of(value: &Json, path: &str) -> Shape<i64> {
    match value {
        Json::Int(number) => Ok(*number),
        _ => Err(bad(path)),
    }
}

pub(crate) fn items_of<'a>(value: &'a Json, path: &str) -> Shape<&'a [Json]> {
    match value {
        Json::Arr(items) => Ok(items),
        _ => Err(bad(path)),
    }
}

pub(crate) fn items_at<'a>(
    fields: &'a [(String, Json)],
    path: &str,
    key: &str,
) -> Shape<&'a [Json]> {
    items_of(get(fields, path, key)?, &format!("{path}.{key}"))
}

pub(crate) fn texts_of(value: &Json, path: &str) -> Shape<Vec<String>> {
    items_of(value, path)?
        .iter()
        .map(|item| text_of(item, path))
        .collect()
}

pub(crate) fn texts_at(fields: &[(String, Json)], path: &str, key: &str) -> Shape<Vec<String>> {
    texts_of(get(fields, path, key)?, &format!("{path}.{key}"))
}

pub(crate) fn fields_of(value: &Json, path: &str) -> Shape<Fields> {
    match value {
        Json::Obj(fields) => Ok(fields.clone()),
        _ => Err(bad(path)),
    }
}

/// `null` is `None`; text is `Some`.
pub(crate) fn opt_text_of(value: &Json, path: &str) -> Shape<Option<String>> {
    match value {
        Json::Null => Ok(None),
        Json::Str(text) => Ok(Some(text.clone())),
        _ => Err(bad(path)),
    }
}

/// `null` is `None`; an integer is `Some`.
pub(crate) fn opt_int_of(value: &Json, path: &str) -> Shape<Option<i64>> {
    match value {
        Json::Null => Ok(None),
        other => Ok(Some(int_of(other, path)?)),
    }
}

pub(crate) fn entry(key: &str, value: Json) -> (String, Json) {
    (key.to_string(), value)
}

pub(crate) fn object(entries: Vec<(String, Json)>) -> Json {
    Json::Obj(entries)
}

pub(crate) fn texts_json(list: &[String]) -> Json {
    Json::Arr(list.iter().map(|text| jstr(text)).collect())
}

pub(crate) fn rows_json(rows: &[Fields]) -> Json {
    Json::Arr(rows.iter().map(|row| Json::Obj(row.clone())).collect())
}

fn input(problem: String) -> SeamError {
    SeamError::new(ErrorCode::Input, problem)
}

fn contract(problem: String) -> SeamError {
    SeamError::new(ErrorCode::Contract, format!("reply {problem}"))
}

pub(crate) fn code_of(name: &str) -> Option<ErrorCode> {
    CODES.into_iter().find(|code| code.as_str() == name)
}

/// A taxonomy error as the object `{"code", "message", "details"}`; the message is kept verbatim.
pub(crate) fn error_json(error: &SeamError) -> Json {
    object(vec![
        entry("code", jstr(error.code().as_str())),
        entry("message", jstr(error.message())),
        entry("details", Json::Obj(error.details().to_vec())),
    ])
}

pub(crate) fn error_of(value: &Json, path: &str) -> Shape<SeamError> {
    let fields = exact(value, path, &ERROR_KEYS)?;
    let code =
        code_of(&text_at(fields, path, "code")?).ok_or_else(|| bad(&format!("{path}.code")))?;
    let details = fields_of(get(fields, path, "details")?, &format!("{path}.details"))?;
    Ok(SeamError::unprefixed(code, text_at(fields, path, "message")?).with_details(details))
}

pub(crate) fn event_json(event: &EmitEvent) -> Json {
    object(vec![
        entry("type", jstr(&event.event_type)),
        entry("payload", Json::Obj(event.payload.clone())),
        entry("task_id", event.task_id.as_deref().map_or(Json::Null, jstr)),
    ])
}

pub(crate) fn event_of(value: &Json, path: &str) -> Shape<EmitEvent> {
    let fields = exact(value, path, &EVENT_KEYS)?;
    Ok(EmitEvent {
        event_type: text_at(fields, path, "type")?,
        payload: fields_of(get(fields, path, "payload")?, &format!("{path}.payload"))?,
        task_id: opt_text_of(get(fields, path, "task_id")?, &format!("{path}.task_id"))?,
    })
}

pub(crate) fn plan_json(plan: &TransitionPlan) -> Json {
    object(vec![
        entry("from", jstr(&plan.from)),
        entry("to", jstr(&plan.to)),
        entry("columns", Json::Obj(plan.columns.clone())),
        entry(
            "audit",
            Json::Arr(plan.audit.iter().map(audit_json).collect()),
        ),
        entry(
            "effects",
            Json::Arr(plan.effects.iter().map(effect_json).collect()),
        ),
    ])
}

fn audit_json(row: &AuditRow) -> Json {
    object(vec![
        entry("from", jstr(&row.from)),
        entry("to", jstr(&row.to)),
        entry("actor", jstr(&row.actor)),
        entry("reason", jstr(&row.reason)),
    ])
}

fn effect_json(effect: &PlanEffect) -> Json {
    match effect {
        PlanEffect::LiveRefresh => object(vec![entry("kind", jstr(LIVE_REFRESH))]),
    }
}

/// Reads a transition plan: the same shape `transition_wire` reads, with its own field paths.
pub(crate) fn plan_of(value: &Json, path: &str) -> Shape<TransitionPlan> {
    let fields = exact(value, path, &PLAN_KEYS)?;
    let audit_path = format!("{path}.audit");
    let audit = items_at(fields, path, "audit")?
        .iter()
        .map(|row| {
            let row = exact(row, &audit_path, &AUDIT_KEYS)?;
            Ok(AuditRow {
                from: text_at(row, &audit_path, "from")?,
                to: text_at(row, &audit_path, "to")?,
                actor: text_at(row, &audit_path, "actor")?,
                reason: text_at(row, &audit_path, "reason")?,
            })
        })
        .collect::<Shape<Vec<_>>>()?;
    let effect_path = format!("{path}.effects");
    let effects = items_at(fields, path, "effects")?
        .iter()
        .map(|effect| {
            let effect = exact(effect, &effect_path, &EFFECT_KEYS)?;
            match text_at(effect, &effect_path, "kind")?.as_str() {
                LIVE_REFRESH => Ok(PlanEffect::LiveRefresh),
                _ => Err(bad(&format!("{effect_path}.kind"))),
            }
        })
        .collect::<Shape<Vec<_>>>()?;
    Ok(TransitionPlan {
        from: text_at(fields, path, "from")?,
        to: text_at(fields, path, "to")?,
        columns: fields_of(get(fields, path, "columns")?, &format!("{path}.columns"))?,
        audit,
        effects,
    })
}

pub(crate) fn new_task_json(task: &NewTask) -> Json {
    object(vec![
        entry("task_id", jstr(&task.task_id)),
        entry("correlation_id", jstr(&task.correlation_id)),
        entry("capability", task.capability.clone()),
        entry("title", jstr(&task.title)),
        entry("agent_id", task.agent_id.clone()),
        entry("dag_depth", task.dag_depth.map_or(Json::Null, Json::Int)),
        entry("depends_on", texts_json(&task.depends_on)),
        entry("acceptance", task.acceptance.clone()),
        entry("budget", task.budget.clone()),
        entry("risk_class", task.risk_class.clone()),
        entry("priority", task.priority.clone()),
        entry("notes", Json::Obj(task.notes.clone())),
    ])
}

pub(crate) fn new_task_of(value: &Json, path: &str) -> Shape<NewTask> {
    let fields = exact(value, path, &NEW_TASK_KEYS)?;
    Ok(NewTask {
        task_id: text_at(fields, path, "task_id")?,
        correlation_id: text_at(fields, path, "correlation_id")?,
        capability: get(fields, path, "capability")?.clone(),
        title: text_at(fields, path, "title")?,
        agent_id: get(fields, path, "agent_id")?.clone(),
        dag_depth: opt_int_of(
            get(fields, path, "dag_depth")?,
            &format!("{path}.dag_depth"),
        )?,
        depends_on: texts_at(fields, path, "depends_on")?,
        acceptance: get(fields, path, "acceptance")?.clone(),
        budget: get(fields, path, "budget")?.clone(),
        risk_class: get(fields, path, "risk_class")?.clone(),
        priority: get(fields, path, "priority")?.clone(),
        notes: fields_of(get(fields, path, "notes")?, &format!("{path}.notes"))?,
    })
}

fn key_policy_json(policy: &KeyPolicy) -> Json {
    object(vec![
        entry("require_key", Json::Bool(policy.require_key)),
        entry("real_key", Json::Bool(policy.real_key)),
        entry("dev_key", Json::Bool(policy.dev_key)),
    ])
}

fn key_policy_of(value: &Json, path: &str) -> Shape<KeyPolicy> {
    let fields = exact(value, path, &KEY_POLICY_KEYS)?;
    Ok(KeyPolicy {
        require_key: flag_at(fields, path, "require_key")?,
        real_key: flag_at(fields, path, "real_key")?,
        dev_key: flag_at(fields, path, "dev_key")?,
    })
}

fn num_json(num: &Num) -> Json {
    match num {
        Num::Value(number) => object(vec![
            entry("state", jstr("value")),
            entry("value", Json::Float(*number)),
        ]),
        Num::Invalid => object(vec![entry("state", jstr("invalid"))]),
        Num::Unsupported => object(vec![entry("state", jstr("unsupported"))]),
    }
}

fn num_of(value: &Json, path: &str) -> Shape<Num> {
    let Json::Obj(fields) = value else {
        return Err(bad(path));
    };
    match text_at(fields, path, "state")?.as_str() {
        "value" => {
            let fields = exact(value, path, &["state", "value"])?;
            Ok(Num::Value(number_at(fields, path, "value")?))
        }
        "invalid" => {
            exact(value, path, &["state"])?;
            Ok(Num::Invalid)
        }
        "unsupported" => {
            exact(value, path, &["state"])?;
            Ok(Num::Unsupported)
        }
        _ => Err(bad(&format!("{path}.state"))),
    }
}

fn gate_task_json(gate_task: &GateTask) -> Json {
    match gate_task {
        GateTask::None => object(vec![entry("state", jstr("none"))]),
        GateTask::Task(task_id) => object(vec![
            entry("state", jstr("task")),
            entry("task_id", jstr(task_id)),
        ]),
        GateTask::Unsupported => object(vec![entry("state", jstr("unsupported"))]),
    }
}

fn gate_task_of(value: &Json, path: &str) -> Shape<GateTask> {
    let Json::Obj(fields) = value else {
        return Err(bad(path));
    };
    match text_at(fields, path, "state")?.as_str() {
        "none" => {
            exact(value, path, &["state"])?;
            Ok(GateTask::None)
        }
        "task" => {
            let fields = exact(value, path, &["state", "task_id"])?;
            Ok(GateTask::Task(text_at(fields, path, "task_id")?))
        }
        "unsupported" => {
            exact(value, path, &["state"])?;
            Ok(GateTask::Unsupported)
        }
        _ => Err(bad(&format!("{path}.state"))),
    }
}

fn payload_json(parsed: &ParsedEnvelope) -> Json {
    object(vec![
        entry("gate", parsed.gate.clone()),
        entry("task_id", parsed.task_id.clone()),
        entry("verdict", parsed.verdict.clone()),
        entry("correlation_id", parsed.correlation_id.clone()),
        entry("issued_at", num_json(&parsed.issued_at)),
        entry("expires_s", num_json(&parsed.expires_s)),
        entry("dry_run", Json::Bool(parsed.dry_run)),
        entry("gate_task", gate_task_json(&parsed.gate_task)),
    ])
}

fn payload_of(value: &Json, path: &str) -> Shape<ParsedEnvelope> {
    let fields = exact(value, path, &PAYLOAD_KEYS)?;
    Ok(ParsedEnvelope {
        gate: get(fields, path, "gate")?.clone(),
        task_id: get(fields, path, "task_id")?.clone(),
        verdict: get(fields, path, "verdict")?.clone(),
        correlation_id: get(fields, path, "correlation_id")?.clone(),
        issued_at: num_of(
            get(fields, path, "issued_at")?,
            &format!("{path}.issued_at"),
        )?,
        expires_s: num_of(
            get(fields, path, "expires_s")?,
            &format!("{path}.expires_s"),
        )?,
        dry_run: flag_at(fields, path, "dry_run")?,
        gate_task: gate_task_of(
            get(fields, path, "gate_task")?,
            &format!("{path}.gate_task"),
        )?,
    })
}

fn envelope_json(state: &EnvelopeState) -> Json {
    match state {
        EnvelopeState::Invalid => object(vec![entry("state", jstr("invalid"))]),
        EnvelopeState::Unsupported => object(vec![entry("state", jstr("unsupported"))]),
        EnvelopeState::Valid(parsed) => object(vec![
            entry("state", jstr("valid")),
            entry("payload", payload_json(parsed)),
        ]),
    }
}

fn envelope_of(value: &Json, path: &str) -> Shape<EnvelopeState> {
    let Json::Obj(fields) = value else {
        return Err(bad(path));
    };
    match text_at(fields, path, "state")?.as_str() {
        "invalid" => {
            exact(value, path, &["state"])?;
            Ok(EnvelopeState::Invalid)
        }
        "unsupported" => {
            exact(value, path, &["state"])?;
            Ok(EnvelopeState::Unsupported)
        }
        "valid" => {
            let fields = exact(value, path, &["state", "payload"])?;
            let payload = payload_of(get(fields, path, "payload")?, &format!("{path}.payload"))?;
            Ok(EnvelopeState::Valid(Box::new(payload)))
        }
        _ => Err(bad(&format!("{path}.state"))),
    }
}

fn row_json(row: &VerdictRow) -> Json {
    object(vec![
        entry("id", Json::Int(row.id)),
        entry("task_id", jstr(&row.task_id)),
        entry("gate", jstr(&row.gate)),
        entry("verdict", row.verdict.as_deref().map_or(Json::Null, jstr)),
        entry(
            "envelope_json",
            row.envelope_json.as_deref().map_or(Json::Null, jstr),
        ),
        entry("ts", Json::Float(row.ts)),
    ])
}

fn row_of(value: &Json, path: &str) -> Shape<VerdictRow> {
    let fields = exact(value, path, &ROW_KEYS)?;
    Ok(VerdictRow {
        id: int_of(get(fields, path, "id")?, &format!("{path}.id"))?,
        task_id: text_at(fields, path, "task_id")?,
        gate: text_at(fields, path, "gate")?,
        verdict: opt_text_of(get(fields, path, "verdict")?, &format!("{path}.verdict"))?,
        envelope_json: opt_text_of(
            get(fields, path, "envelope_json")?,
            &format!("{path}.envelope_json"),
        )?,
        ts: number_at(fields, path, "ts")?,
    })
}

fn verdict_json(verdict: &VerdictFacts) -> Json {
    object(vec![
        entry("row", row_json(&verdict.row)),
        entry("envelope", envelope_json(&verdict.envelope)),
        entry("sig_ok", Json::Bool(verdict.sig_ok)),
    ])
}

fn verdict_of(value: &Json, path: &str) -> Shape<VerdictFacts> {
    let fields = exact(value, path, &VERDICT_KEYS)?;
    Ok(VerdictFacts {
        row: row_of(get(fields, path, "row")?, &format!("{path}.row"))?,
        envelope: envelope_of(get(fields, path, "envelope")?, &format!("{path}.envelope"))?,
        sig_ok: flag_at(fields, path, "sig_ok")?,
    })
}

/// The facts of one correlation, as the request carries them.
pub(crate) fn facts_json(facts: &ReconcileFacts) -> Json {
    object(vec![
        entry("correlation_id", jstr(&facts.correlation_id)),
        entry("tasks", rows_json(&facts.tasks)),
        entry(
            "verdicts",
            Json::Arr(facts.verdicts.iter().map(verdict_json).collect()),
        ),
        entry(
            "other_tasks",
            Json::Arr(
                facts
                    .other_tasks
                    .iter()
                    .map(|issuer| {
                        object(vec![
                            entry("task_id", jstr(&issuer.task_id)),
                            entry("state", jstr(&issuer.state)),
                        ])
                    })
                    .collect(),
            ),
        ),
        entry(
            "key_policy",
            facts
                .key_policy
                .as_ref()
                .map_or(Json::Null, key_policy_json),
        ),
    ])
}

/// Reads the facts of [`facts_json`]; `path` names the object in messages.
pub(crate) fn facts_of(value: &Json, path: &str) -> Shape<ReconcileFacts> {
    let fields = exact(value, path, &FACTS_KEYS)?;
    let tasks_path = format!("{path}.tasks");
    let tasks = items_at(fields, path, "tasks")?
        .iter()
        .map(|row| fields_of(row, &tasks_path))
        .collect::<Shape<Vec<_>>>()?;
    let verdicts = items_at(fields, path, "verdicts")?
        .iter()
        .map(|row| verdict_of(row, &format!("{path}.verdicts")))
        .collect::<Shape<Vec<_>>>()?;
    let issuer_path = format!("{path}.other_tasks");
    let other_tasks = items_at(fields, path, "other_tasks")?
        .iter()
        .map(|issuer| {
            let issuer = exact(issuer, &issuer_path, &ISSUER_KEYS)?;
            Ok(IssuerState {
                task_id: text_at(issuer, &issuer_path, "task_id")?,
                state: text_at(issuer, &issuer_path, "state")?,
            })
        })
        .collect::<Shape<Vec<_>>>()?;
    let key_policy = match get(fields, path, "key_policy")? {
        Json::Null => None,
        other => Some(key_policy_of(other, &format!("{path}.key_policy"))?),
    };
    Ok(ReconcileFacts {
        correlation_id: text_at(fields, path, "correlation_id")?,
        tasks,
        verdicts,
        other_tasks,
        key_policy,
    })
}

pub(crate) fn op_json(op: &ReconcileOp) -> Json {
    match op {
        ReconcileOp::Transition { task_id, plan } => object(vec![
            entry("op", jstr("transition")),
            entry("task_id", jstr(task_id)),
            entry("plan", plan_json(plan)),
        ]),
        ReconcileOp::CreateTask(task) => object(vec![
            entry("op", jstr("create_task")),
            entry("task", new_task_json(task)),
        ]),
        ReconcileOp::SetDependsOn {
            task_id,
            depends_on,
        } => object(vec![
            entry("op", jstr("set_depends_on")),
            entry("task_id", jstr(task_id)),
            entry("depends_on", texts_json(depends_on)),
        ]),
        ReconcileOp::SetNotes {
            task_id,
            key,
            value,
        } => object(vec![
            entry("op", jstr("set_notes")),
            entry("task_id", jstr(task_id)),
            entry("key", jstr(key)),
            entry("value", value.clone()),
        ]),
        ReconcileOp::Emit(event) => object(vec![
            entry("op", jstr("emit")),
            entry("event", event_json(event)),
        ]),
    }
}

fn op_of(value: &Json, path: &str) -> Shape<ReconcileOp> {
    let Json::Obj(fields) = value else {
        return Err(bad(path));
    };
    match text_at(fields, path, "op")?.as_str() {
        "transition" => {
            let fields = exact(value, path, &["op", "task_id", "plan"])?;
            Ok(ReconcileOp::Transition {
                task_id: text_at(fields, path, "task_id")?,
                plan: plan_of(get(fields, path, "plan")?, &format!("{path}.plan"))?,
            })
        }
        "create_task" => {
            let fields = exact(value, path, &["op", "task"])?;
            Ok(ReconcileOp::CreateTask(new_task_of(
                get(fields, path, "task")?,
                &format!("{path}.task"),
            )?))
        }
        "set_depends_on" => {
            let fields = exact(value, path, &["op", "task_id", "depends_on"])?;
            Ok(ReconcileOp::SetDependsOn {
                task_id: text_at(fields, path, "task_id")?,
                depends_on: texts_at(fields, path, "depends_on")?,
            })
        }
        "set_notes" => {
            let fields = exact(value, path, &["op", "task_id", "key", "value"])?;
            Ok(ReconcileOp::SetNotes {
                task_id: text_at(fields, path, "task_id")?,
                key: text_at(fields, path, "key")?,
                value: get(fields, path, "value")?.clone(),
            })
        }
        "emit" => {
            let fields = exact(value, path, &["op", "event"])?;
            Ok(ReconcileOp::Emit(event_of(
                get(fields, path, "event")?,
                &format!("{path}.event"),
            )?))
        }
        _ => Err(bad(&format!("{path}.op"))),
    }
}

/// Renders the request of one reconcile call.
pub fn encode_request(facts: &ReconcileFacts, approve: bool, now: f64) -> String {
    to_python_dumps(&object(vec![
        entry("mode", jstr(MODE)),
        entry("now", Json::Float(now)),
        entry("approve", Json::Bool(approve)),
        entry("facts", facts_json(facts)),
    ]))
}

/// Reads one request value into the facts, the `approve` flag and `now`. A shape problem is `E-INPUT` naming the field.
pub fn decode_request(value: &Json) -> Result<(ReconcileFacts, bool, f64), SeamError> {
    let fields = exact(value, "request", &REQUEST_KEYS).map_err(input)?;
    let mode = text_at(fields, "request", "mode").map_err(input)?;
    if mode != MODE {
        return Err(input(bad("request.mode")));
    }
    let now = number_at(fields, "request", "now").map_err(input)?;
    let approve = flag_at(fields, "request", "approve").map_err(input)?;
    let facts =
        facts_of(get(fields, "request", "facts").map_err(input)?, "facts").map_err(input)?;
    Ok((facts, approve, now))
}

/// Renders the answer of one reconcile call: `{"plan": ..}` or `{"not_started": ..}`.
pub fn encode_outcome(outcome: &Result<ReconcilePlan, NotStarted>) -> String {
    let body = match outcome {
        Ok(plan) => entry(
            "plan",
            object(vec![
                entry("ops", Json::Arr(plan.ops.iter().map(op_json).collect())),
                entry("notes_log", texts_json(&plan.notes_log)),
            ]),
        ),
        Err(NotStarted(why)) => entry("not_started", jstr(why)),
    };
    to_python_dumps(&object(vec![body]))
}

/// Reads the reply bytes into the answer they carry. A reply of another shape is `E-CONTRACT`.
pub fn decode_outcome(bytes: &[u8]) -> Result<Result<ReconcilePlan, NotStarted>, SeamError> {
    let text = std::str::from_utf8(bytes).map_err(|_| contract("is not valid UTF-8".into()))?;
    let value = parse_python_json(text)?;
    let Json::Obj(top) = &value else {
        return Err(contract("must be a JSON object".into()));
    };
    let decoded = match top.as_slice() {
        [(key, plan)] if key == "plan" => {
            let fields = exact(plan, "plan", &["ops", "notes_log"]).map_err(contract)?;
            let ops = items_at(fields, "plan", "ops")
                .map_err(contract)?
                .iter()
                .map(|op| op_of(op, "plan.ops"))
                .collect::<Shape<Vec<_>>>()
                .map_err(contract)?;
            let notes_log = texts_at(fields, "plan", "notes_log").map_err(contract)?;
            Ok(ReconcilePlan { ops, notes_log })
        }
        [(key, why)] if key == "not_started" => {
            Err(NotStarted(text_of(why, "not_started").map_err(contract)?))
        }
        _ => return Err(contract(bad("plan or not_started"))),
    };
    Ok(decoded)
}

/// The guest entry of the `reconcile` mode: UTF-8 request bytes in, encoded reply bytes out.
///
/// A request that is not UTF-8 or breaks the schema is `E-INPUT`; one holding what [`Json`] cannot carry is `E-DEP` (see
/// [`parse_python_json`]). A request that reads is always answered: a refusal of the planner is a reply.
pub fn invoke(input_bytes: &[u8]) -> Result<Vec<u8>, SeamError> {
    let text = std::str::from_utf8(input_bytes)
        .map_err(|_| SeamError::new(ErrorCode::Input, "request is not valid UTF-8"))?;
    let (facts, approve, now) = decode_request(&parse_python_json(text)?)?;
    Ok(encode_outcome(&plan_reconcile(&facts, approve, now)).into_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gate_facts::VerdictRow;
    use crate::parse_json;

    const VECTORS: &str = include_str!("../tests/vectors/reconcile.json");

    /// One recorded reconcile call: the facts the host reads and the flags it passes.
    struct Case {
        name: String,
        facts: ReconcileFacts,
        approve: bool,
        now: f64,
    }

    fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
        match value {
            Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
            other => panic!("not an object looking for {key}: {other:?}"),
        }
    }

    fn text(value: &Json) -> &str {
        match value {
            Json::Str(text) => text,
            other => panic!("not text: {other:?}"),
        }
    }

    fn flag(value: &Json) -> bool {
        match value {
            Json::Bool(flag) => *flag,
            other => panic!("not a bool: {other:?}"),
        }
    }

    fn items(value: &Json) -> &[Json] {
        match value {
            Json::Arr(items) => items,
            other => panic!("not a list: {other:?}"),
        }
    }

    fn fields(value: &Json) -> Fields {
        match value {
            Json::Obj(fields) => fields.clone(),
            other => panic!("not an object: {other:?}"),
        }
    }

    fn number(value: &Json) -> f64 {
        match value {
            Json::Int(number) => *number as f64,
            Json::Float(number) => *number,
            other => panic!("not a number: {other:?}"),
        }
    }

    fn integer(value: &Json) -> i64 {
        match value {
            Json::Int(number) => *number,
            other => panic!("not an integer: {other:?}"),
        }
    }

    fn optional_text(value: &Json) -> Option<String> {
        match value {
            Json::Null => None,
            other => Some(text(other).to_string()),
        }
    }

    fn parse_case(value: &Json) -> Case {
        let correlation = text(member(value, "correlation_id")).to_string();
        let before: Vec<Fields> = items(member(value, "tasks_before"))
            .iter()
            .map(fields)
            .collect();
        let (mine, others): (Vec<Fields>, Vec<Fields>) = before.into_iter().partition(|row| {
            matches!(obj_get(row, "correlation_id"), Some(Json::Str(id)) if *id == correlation)
        });
        let policy = member(value, "key_policy");
        let key_policy = match policy {
            Json::Null => None,
            policy => Some(KeyPolicy {
                require_key: flag(member(policy, "require_key")),
                real_key: flag(member(policy, "real_key")),
                dev_key: flag(member(policy, "dev_key")),
            }),
        };
        let verdicts = items(member(value, "verdicts"))
            .iter()
            .map(|item| {
                let row = VerdictRow {
                    id: integer(member(item, "id")),
                    task_id: text(member(item, "task_id")).to_string(),
                    gate: text(member(item, "gate")).to_string(),
                    verdict: optional_text(member(item, "verdict")),
                    envelope_json: optional_text(member(item, "envelope_json")),
                    ts: number(member(item, "ts")),
                };
                VerdictFacts::new(row, flag(member(item, "sig_ok")))
            })
            .collect();
        let other_tasks = others
            .iter()
            .map(|row| IssuerState {
                task_id: text(member_of(row, "task_id")).to_string(),
                state: text(member_of(row, "state")).to_string(),
            })
            .collect();
        Case {
            name: text(member(value, "name")).to_string(),
            facts: ReconcileFacts {
                correlation_id: correlation,
                tasks: mine,
                verdicts,
                other_tasks,
                key_policy,
            },
            approve: flag(member(value, "approve")),
            now: number(member(value, "now")),
        }
    }

    fn member_of<'a>(row: &'a Fields, key: &str) -> &'a Json {
        obj_get(row, key).unwrap_or_else(|| panic!("no column {key}"))
    }

    fn cases() -> Vec<Case> {
        let doc = parse_json(VECTORS).expect("the vector file parses");
        items(member(&doc, "cases"))
            .iter()
            .map(parse_case)
            .collect()
    }

    #[test]
    fn every_vector_case_round_trips_and_the_guest_answers_the_host_bytes() {
        let cases = cases();
        assert!(
            cases.len() >= 200,
            "the vector file holds {} cases",
            cases.len()
        );
        for case in &cases {
            let request = encode_request(&case.facts, case.approve, case.now);
            let host = plan_reconcile(&case.facts, case.approve, case.now);
            let reply = invoke(request.as_bytes())
                .unwrap_or_else(|error| panic!("{}: {}", case.name, error.message()));
            assert_eq!(
                reply,
                encode_outcome(&host).into_bytes(),
                "{}: the guest reply differs from the host bytes",
                case.name
            );
            assert_eq!(
                decode_outcome(&reply).unwrap_or_else(|error| panic!(
                    "{}: {}",
                    case.name,
                    error.message()
                )),
                host,
                "{}: the reply does not decode to the host answer",
                case.name
            );
            let value = parse_python_json(&request).expect("the request parses");
            let (facts, approve, now) = decode_request(&value)
                .unwrap_or_else(|error| panic!("{}: {}", case.name, error.message()));
            assert_eq!(
                encode_request(&facts, approve, now),
                request,
                "{}: the request does not survive a decode",
                case.name
            );
        }
    }

    fn request_value(case: &Case) -> Json {
        parse_python_json(&encode_request(&case.facts, case.approve, case.now))
            .expect("the request parses")
    }

    fn field_mut<'a>(fields: &'a mut Fields, key: &str) -> &'a mut Json {
        fields
            .iter_mut()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value)
            .unwrap_or_else(|| panic!("no key {key}"))
    }

    fn message_of(value: &Json) -> String {
        decode_request(value)
            .expect_err("the request is refused")
            .message()
            .to_string()
    }

    #[test]
    fn a_missing_extra_or_mistyped_key_is_input_naming_the_field() {
        let cases = cases();
        let case = &cases[0];

        let mut missing = request_value(case);
        let Json::Obj(top) = &mut missing else {
            panic!("object")
        };
        top.retain(|(name, _)| name != "approve");
        assert!(
            message_of(&missing).contains("approve"),
            "{}",
            message_of(&missing)
        );

        let mut extra = request_value(case);
        let Json::Obj(top) = &mut extra else {
            panic!("object")
        };
        top.push(("extra".to_string(), Json::Int(1)));
        assert!(
            message_of(&extra).contains("request"),
            "{}",
            message_of(&extra)
        );

        let mut mistyped = request_value(case);
        let Json::Obj(top) = &mut mistyped else {
            panic!("object")
        };
        *field_mut(top, "now") = Json::Str("3000".to_string());
        assert!(
            message_of(&mistyped).contains("now"),
            "{}",
            message_of(&mistyped)
        );

        let with_verdict = cases
            .iter()
            .find(|case| !case.facts.verdicts.is_empty())
            .expect("a case with a verdict");
        let mut verdict = request_value(with_verdict);
        let Json::Obj(top) = &mut verdict else {
            panic!("object")
        };
        let Json::Obj(facts) = field_mut(top, "facts") else {
            panic!("object")
        };
        let Json::Arr(verdicts) = field_mut(facts, "verdicts") else {
            panic!("list")
        };
        let Json::Obj(first) = &mut verdicts[0] else {
            panic!("object")
        };
        first.push(("extra".to_string(), Json::Null));
        assert!(
            message_of(&verdict).contains("verdicts"),
            "{}",
            message_of(&verdict)
        );
    }

    fn op_kind(op: &ReconcileOp) -> &'static str {
        match op {
            ReconcileOp::Transition { .. } => "transition",
            ReconcileOp::CreateTask(_) => "create_task",
            ReconcileOp::SetDependsOn { .. } => "set_depends_on",
            ReconcileOp::SetNotes { .. } => "set_notes",
            ReconcileOp::Emit(_) => "emit",
        }
    }

    #[test]
    fn the_vectors_exercise_every_op_kind() {
        let mut kinds = std::collections::BTreeSet::new();
        for case in cases() {
            if let Ok(plan) = plan_reconcile(&case.facts, case.approve, case.now) {
                kinds.extend(plan.ops.iter().map(op_kind));
            }
        }
        let every: std::collections::BTreeSet<&str> = [
            "transition",
            "create_task",
            "set_depends_on",
            "set_notes",
            "emit",
        ]
        .into_iter()
        .collect();
        assert_eq!(kinds, every);
    }

    #[test]
    fn a_reply_of_another_shape_is_contract() {
        for text in [
            "[]",
            "{}",
            r#"{"plan": {"ops": []}}"#,
            r#"{"not_started": 1}"#,
            r#"{"plan": {"ops": [{"op": "nope"}], "notes_log": []}}"#,
        ] {
            let error = decode_outcome(text.as_bytes()).expect_err(text);
            assert_eq!(error.code(), ErrorCode::Contract, "{text}");
        }
    }
}
