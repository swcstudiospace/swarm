//! The wire form of the guest `transition` mode: the decision of [`decide_transition`] as bytes in and bytes out.
//!
//! The host reads the task row and the approval facts, encodes one request with [`encode_request`], calls the guest, and reads the
//! answer with [`decode_reply`]. The guest side is [`transition_invoke`]: it decodes the request, runs the same
//! [`decide_transition`] the host can run natively, and renders the answer with [`encode_reply`]. Nothing here reads a clock, a
//! file, an environment variable or a key: `now` is a request field and the approval facts are booleans and gate names.
//!
//! # Request
//!
//! ```text
//! {"mode": "transition", "now": <number>,
//!  "task": <the tasks row as a Python dict, or null>,
//!  "request": {"task_id": .., "to": .., "actor": .., "reason": .., "manual": <bool>},
//!  "facts": {"approval": null | {"require_key", "real_key", "dev_key", "missing_gates", "unaccepted_gates"}}}
//! ```
//!
//! `task_id` is not in the Design 4 sketch of the phase plan but the decision needs it: an unknown task (`task: null`) has no row to
//! read it from, and Python's message names it. Every object holds exactly the keys above; a missing, extra or mistyped key is
//! `E-INPUT` and the message names the field, never its value.
//!
//! # Reply
//!
//! ```text
//! {"plan": {"from": .., "to": .., "columns": {..}, "audit": [{"from", "to", "actor", "reason"}], "effects": [{"kind": "live_refresh"}]}}
//! {"error": {"code": "E-..", "message": .., "details": {..}, "write_state": "not_started" | "final"}}
//! ```
//!
//! A refusal of the decision (Python's own answer, or a `not_started` marker) is an ordinary reply, not an `invoke-error`: the WIT
//! `invoke-error` holds only a code and a message, and the route needs `details` and `write_state` too. A request the guest cannot
//! read is an `invoke-error`, as in the read modes.
//!
//! # Determinism
//!
//! Both directions render with [`to_python_dumps`] (keys in the order written, floats through [`py_float`](crate::py_float)) and read
//! with [`parse_python_json`], which refuses what [`Json`] cannot hold byte-exactly (an integer outside `i64`, a lone surrogate, a
//! non-finite literal). A plan is therefore a pure function of its content: equal plans give equal bytes. The `columns` object keeps
//! the plan's order and is not checked here; the host's plan validator owns the allowlist.

use gsd_codes::ErrorCode;

use crate::pydumps::{parse_python_json, to_python_dumps};
use crate::transition_plan::{
    decide_transition, ApprovalFacts, AuditRow, Facts, PlanEffect, Request, TransitionError,
    TransitionPlan, WriteState,
};
use crate::{jstr, obj_get, Fields, Json, SeamError};

/// The `mode` of a transition request.
pub const MODE: &str = "transition";

/// What the decision answered: the plan to write, or the refusal.
pub type Decision = Result<TransitionPlan, TransitionError>;

const REQUEST_KEYS: [&str; 5] = ["mode", "now", "task", "request", "facts"];
const CALL_KEYS: [&str; 5] = ["task_id", "to", "actor", "reason", "manual"];
const FACTS_KEYS: [&str; 1] = ["approval"];
const APPROVAL_KEYS: [&str; 5] = [
    "require_key",
    "real_key",
    "dev_key",
    "missing_gates",
    "unaccepted_gates",
];
const PLAN_KEYS: [&str; 5] = ["from", "to", "columns", "audit", "effects"];
const AUDIT_KEYS: [&str; 4] = ["from", "to", "actor", "reason"];
const ERROR_KEYS: [&str; 4] = ["code", "message", "details", "write_state"];
const EFFECT_KEYS: [&str; 1] = ["kind"];
const LIVE_REFRESH: &str = "live_refresh";
const NOT_STARTED: &str = "not_started";
const FINAL: &str = "final";
const CODES: [ErrorCode; 7] = [
    ErrorCode::Input,
    ErrorCode::Timeout,
    ErrorCode::Dep,
    ErrorCode::Capacity,
    ErrorCode::Contract,
    ErrorCode::Policy,
    ErrorCode::Internal,
];

fn entry(key: &str, value: Json) -> (String, Json) {
    (key.to_string(), value)
}

fn object(entries: Vec<(String, Json)>) -> Json {
    Json::Obj(entries)
}

fn names(list: &[String]) -> Json {
    Json::Arr(list.iter().map(|name| jstr(name)).collect())
}

/// Renders one transition request for the guest, byte for byte what [`decode_request`] reads back.
pub fn encode_request(facts: &Facts, request: &Request, now: f64) -> String {
    let approval = facts.approval.as_ref().map_or(Json::Null, |approval| {
        object(vec![
            entry("require_key", Json::Bool(approval.require_key)),
            entry("real_key", Json::Bool(approval.real_key)),
            entry("dev_key", Json::Bool(approval.dev_key)),
            entry("missing_gates", names(&approval.missing_gates)),
            entry("unaccepted_gates", names(&approval.unaccepted_gates)),
        ])
    });
    let task = facts
        .task
        .as_ref()
        .map_or(Json::Null, |row| Json::Obj(row.clone()));
    to_python_dumps(&object(vec![
        entry("mode", jstr(MODE)),
        entry("now", Json::Float(now)),
        entry("task", task),
        entry(
            "request",
            object(vec![
                entry("task_id", jstr(&request.task_id)),
                entry("to", jstr(&request.to)),
                entry("actor", jstr(&request.actor)),
                entry("reason", jstr(&request.reason)),
                entry("manual", Json::Bool(request.manual)),
            ]),
        ),
        entry("facts", object(vec![entry("approval", approval)])),
    ]))
}

/// Renders the answer of one decision: `{"plan": ..}` or `{"error": ..}`.
pub fn encode_reply(decision: &Decision) -> String {
    let body = match decision {
        Ok(plan) => entry(
            "plan",
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
            ]),
        ),
        Err(error) => entry(
            "error",
            object(vec![
                entry("code", jstr(error.code.as_str())),
                entry("message", jstr(&error.message)),
                entry("details", Json::Obj(error.details.clone())),
                entry(
                    "write_state",
                    jstr(match error.write_state {
                        WriteState::NotStarted => NOT_STARTED,
                        WriteState::Final => FINAL,
                    }),
                ),
            ]),
        ),
    };
    to_python_dumps(&object(vec![body]))
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

/// A shape problem, as the description of the field at fault. The public readers turn it into their own error code.
type Shape<T> = Result<T, String>;

fn bad(path: &str) -> String {
    format!("field {path} is missing or has the wrong type")
}

/// The fields of the object `value`, which must hold exactly `keys`.
fn exact<'a>(value: &'a Json, path: &str, keys: &[&str]) -> Shape<&'a [(String, Json)]> {
    match value {
        Json::Obj(fields)
            if fields.len() == keys.len()
                && keys.iter().all(|key| obj_get(fields, key).is_some()) =>
        {
            Ok(fields)
        }
        _ => Err(bad(path)),
    }
}

fn member<'a>(fields: &'a [(String, Json)], scope: &str, key: &str) -> Shape<&'a Json> {
    obj_get(fields, key).ok_or_else(|| bad(&format!("{scope}.{key}")))
}

fn text<'a>(fields: &'a [(String, Json)], scope: &str, key: &str) -> Shape<&'a str> {
    match member(fields, scope, key)? {
        Json::Str(text) => Ok(text),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn flag(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<bool> {
    match member(fields, scope, key)? {
        Json::Bool(flag) => Ok(*flag),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn items<'a>(fields: &'a [(String, Json)], scope: &str, key: &str) -> Shape<&'a [Json]> {
    match member(fields, scope, key)? {
        Json::Arr(items) => Ok(items),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn texts(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<Vec<String>> {
    items(fields, scope, key)?
        .iter()
        .map(|item| match item {
            Json::Str(text) => Ok(text.clone()),
            _ => Err(bad(&format!("{scope}.{key}"))),
        })
        .collect()
}

fn fields_of<'a>(fields: &'a [(String, Json)], scope: &str, key: &str) -> Shape<&'a Fields> {
    match member(fields, scope, key)? {
        Json::Obj(inner) => Ok(inner),
        _ => Err(bad(&format!("{scope}.{key}"))),
    }
}

fn input(problem: String) -> SeamError {
    SeamError::new(ErrorCode::Input, problem)
}

fn approval_of(value: &Json) -> Shape<Option<ApprovalFacts>> {
    if value == &Json::Null {
        return Ok(None);
    }
    let fields = exact(value, "facts.approval", &APPROVAL_KEYS)?;
    Ok(Some(ApprovalFacts {
        require_key: flag(fields, "facts.approval", "require_key")?,
        real_key: flag(fields, "facts.approval", "real_key")?,
        dev_key: flag(fields, "facts.approval", "dev_key")?,
        missing_gates: texts(fields, "facts.approval", "missing_gates")?,
        unaccepted_gates: texts(fields, "facts.approval", "unaccepted_gates")?,
    }))
}

fn request_of(value: &Json) -> Shape<(Facts, Request, f64)> {
    let top = exact(value, "request", &REQUEST_KEYS)?;
    if text(top, "request", "mode")? != MODE {
        return Err(bad("request.mode"));
    }
    let now = match member(top, "request", "now")? {
        Json::Float(now) => *now,
        // An integral clock reading may arrive as an integer; every Python float is exact in f64 only up to 2^53.
        Json::Int(now) if now.unsigned_abs() <= 1 << 53 => *now as f64,
        _ => return Err(bad("request.now")),
    };
    let task = match member(top, "request", "task")? {
        Json::Null => None,
        Json::Obj(row) => Some(row.clone()),
        _ => return Err(bad("request.task")),
    };
    let call = exact(
        member(top, "request", "request")?,
        "request.request",
        &CALL_KEYS,
    )?;
    let request = Request {
        task_id: text(call, "request.request", "task_id")?.to_string(),
        to: text(call, "request.request", "to")?.to_string(),
        actor: text(call, "request.request", "actor")?.to_string(),
        reason: text(call, "request.request", "reason")?.to_string(),
        manual: flag(call, "request.request", "manual")?,
    };
    let facts = exact(
        member(top, "request", "facts")?,
        "request.facts",
        &FACTS_KEYS,
    )?;
    let approval = approval_of(member(facts, "request.facts", "approval")?)?;
    Ok((Facts { task, approval }, request, now))
}

/// Reads one request value into what [`decide_transition`] takes. A shape problem is `E-INPUT`.
pub fn decode_request(value: &Json) -> Result<(Facts, Request, f64), SeamError> {
    request_of(value).map_err(input)
}

/// The guest entry of the `transition` mode: UTF-8 request bytes in, encoded reply bytes out.
///
/// A request that is not UTF-8 or breaks the schema is `E-INPUT`; one holding what [`Json`] cannot carry is `E-DEP` (see
/// [`parse_python_json`]). A request that reads is always answered: the decision's refusal is a reply (see the module
/// documentation). Messages name the field and never echo a value.
pub fn transition_invoke(input_bytes: &[u8]) -> Result<Vec<u8>, SeamError> {
    let text = std::str::from_utf8(input_bytes)
        .map_err(|_| SeamError::new(ErrorCode::Input, "request is not valid UTF-8"))?;
    let (facts, request, now) = decode_request(&parse_python_json(text)?)?;
    Ok(encode_reply(&decide_transition(&facts, &request, now)).into_bytes())
}

fn contract(problem: String) -> SeamError {
    SeamError::new(ErrorCode::Contract, format!("reply {problem}"))
}

fn code_of(name: &str) -> Shape<ErrorCode> {
    CODES
        .into_iter()
        .find(|code| code.as_str() == name)
        .ok_or_else(|| bad("error.code"))
}

fn plan_of(value: &Json) -> Shape<TransitionPlan> {
    let fields = exact(value, "plan", &PLAN_KEYS)?;
    let audit = items(fields, "plan", "audit")?
        .iter()
        .map(|row| {
            let row = exact(row, "plan.audit", &AUDIT_KEYS)?;
            Ok(AuditRow {
                from: text(row, "plan.audit", "from")?.to_string(),
                to: text(row, "plan.audit", "to")?.to_string(),
                actor: text(row, "plan.audit", "actor")?.to_string(),
                reason: text(row, "plan.audit", "reason")?.to_string(),
            })
        })
        .collect::<Shape<Vec<_>>>()?;
    let effects = items(fields, "plan", "effects")?
        .iter()
        .map(|effect| {
            let effect = exact(effect, "plan.effects", &EFFECT_KEYS)?;
            match text(effect, "plan.effects", "kind")? {
                LIVE_REFRESH => Ok(PlanEffect::LiveRefresh),
                _ => Err(bad("plan.effects.kind")),
            }
        })
        .collect::<Shape<Vec<_>>>()?;
    Ok(TransitionPlan {
        from: text(fields, "plan", "from")?.to_string(),
        to: text(fields, "plan", "to")?.to_string(),
        columns: fields_of(fields, "plan", "columns")?.clone(),
        audit,
        effects,
    })
}

fn error_of(value: &Json) -> Shape<TransitionError> {
    let fields = exact(value, "error", &ERROR_KEYS)?;
    let write_state = match text(fields, "error", "write_state")? {
        NOT_STARTED => WriteState::NotStarted,
        FINAL => WriteState::Final,
        _ => return Err(bad("error.write_state")),
    };
    Ok(TransitionError {
        code: code_of(text(fields, "error", "code")?)?,
        message: text(fields, "error", "message")?.to_string(),
        details: fields_of(fields, "error", "details")?.clone(),
        write_state,
    })
}

/// Reads the guest's reply bytes into the decision it made.
///
/// Anything but an object with exactly one of `plan` and `error`, each in the shape of the module documentation, is `E-CONTRACT`;
/// the plan's content (state names, columns) is not judged here. Text that is not JSON, or holds what [`Json`] cannot carry, is
/// `E-DEP`, the refusal of [`parse_python_json`].
pub fn decode_reply(bytes: &[u8]) -> Result<Decision, SeamError> {
    let text = std::str::from_utf8(bytes).map_err(|_| contract("is not valid UTF-8".into()))?;
    let value = parse_python_json(text)?;
    let Json::Obj(top) = &value else {
        return Err(contract("must be a JSON object".into()));
    };
    let decoded = match top.as_slice() {
        [(key, plan)] if key == "plan" => plan_of(plan).map(Ok),
        [(key, error)] if key == "error" => error_of(error).map(Err),
        _ => Err(bad("plan or error")),
    };
    decoded.map_err(contract)
}
