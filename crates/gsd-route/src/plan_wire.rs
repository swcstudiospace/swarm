//! The wire form of the second-batch guest mode `plan` (Phase 14.5 pin P2): the derivation of [`derive_plan`] as bytes in and bytes out.
//!
//! The host reads the store and the files, derives the answer with [`derive_plan`], and holds the guest's reply against
//! [`encode_outcome`] of that answer. The guest decodes its request with [`decode_request`], runs the same [`derive_plan`] and renders
//! the answer with [`encode_outcome`]: that is [`invoke`]. Nothing here reads a clock, a file, an environment variable or a key. `now` is
//! a request field, the file reads are data, and the signing state is two booleans and an error text. Keys stay on the host.
//!
//! # Request
//!
//! ```text
//! {"mode": "plan", "now": <number>,
//!  "facts": {"new_correlation_id": .., "agents": [{"id", "code", "slug", "capabilities"}], "plans_dir": ..,
//!            "signing": {"agent_session": <bool>, "config_error": null | .., "real_key": <bool>},
//!            "prefix_tasks": [<row>], "correlation_tasks": [<row>],
//!            "plan_files": [{"correlation_id": .., "text": null | ..}]},
//!  "request": {"pattern", "risk_class", "priority", "prefix", "acceptance": [..], "graph_id", "brief_file", "brief_text",
//!              "plan_file", "slices_json", "slices_file", "correlation_id", "dry_run": <bool>}}
//! ```
//!
//! A row is a `tasks` row as a JSON object, its values as the store holds them (JSON columns as text). A file read is `null` (no file was
//! asked for), `{"text": ..}` (read) or `{"unreadable": true}` (Python's read raises). That is the shape of the oracle vectors
//! (`tests/vectors/plan_derive.json`): a vector case is a request once `mode`, `now` and the roster are added. Every object holds exactly
//! the keys above. A missing, extra or mistyped key is `E-INPUT` and the message names the field, never its value.
//!
//! # Reply
//!
//! ```text
//! {"plan": {"outcome": "dry_run" | "reused" | "created", "reply": {..}, "ctx_correlation_id": null | ..,
//!           "ops": [{"create": {..}} | {"transition": {"task_id", "from", "to", "actor", "reason"}}],
//!           "files": null | {"plan_name", "plan_text", "latest_correlation"},
//!           "effects": [{"bind_graph": {..}} | {"emit": {"event_type", "payload"}}],
//!           "kickoff": null | {"correlation_id", "risk_class", "payload"}}}
//! {"refused": {"code": .., "message": .., "details": {..}}}
//! {"not_started": ..}
//! ```
//!
//! The first form is a derivation, the second Python's own refusal, the third a decline Python answers. The host compares replies byte for
//! byte, so [`encode_outcome`] renders every field of [`PlanDerived`] and [`PlanError`] in its order: two answers that differ in any field
//! differ in their bytes. A reply is never decoded, so it has no reader here.
//!
//! # Determinism
//!
//! Both directions render with [`to_python_dumps`] (keys in the order written, floats through [`py_float`](crate::py_float)) and read with
//! [`parse_python_json`], which refuses what [`Json`] cannot hold byte-exactly. A request is therefore a pure function of its content.

use gsd_codes::ErrorCode;

use crate::plan_derive::{
    derive_plan, Agent, FileRead, Kickoff, NewTask, Outcome, PlanDerived, PlanEffect, PlanError,
    PlanFacts, PlanFiles, PlanOp, PlanRequest, Signing, Step, StoredPlanFile,
};
use crate::pydumps::{parse_python_json, to_python_dumps};
use crate::{jstr, obj_get, Fields, Json, SeamError};

/// The `mode` of a plan request.
pub const MODE: &str = "plan";

const REQUEST_KEYS: [&str; 4] = ["mode", "now", "facts", "request"];
const FACTS_KEYS: [&str; 7] = [
    "new_correlation_id",
    "agents",
    "plans_dir",
    "signing",
    "prefix_tasks",
    "correlation_tasks",
    "plan_files",
];
const AGENT_KEYS: [&str; 4] = ["id", "code", "slug", "capabilities"];
const SIGNING_KEYS: [&str; 3] = ["agent_session", "config_error", "real_key"];
const PLAN_FILE_KEYS: [&str; 2] = ["correlation_id", "text"];
const ARGS_KEYS: [&str; 13] = [
    "pattern",
    "risk_class",
    "priority",
    "prefix",
    "acceptance",
    "graph_id",
    "brief_file",
    "brief_text",
    "plan_file",
    "slices_json",
    "slices_file",
    "correlation_id",
    "dry_run",
];

fn entry(key: &str, value: Json) -> (String, Json) {
    (key.to_string(), value)
}

fn object(entries: Vec<(String, Json)>) -> Json {
    Json::Obj(entries)
}

fn texts(list: &[String]) -> Json {
    Json::Arr(list.iter().map(|text| jstr(text)).collect())
}

fn optional(text: Option<&str>) -> Json {
    text.map_or(Json::Null, jstr)
}

fn file_read(file: &Option<FileRead>) -> Json {
    match file {
        None => Json::Null,
        Some(FileRead::Text(text)) => object(vec![entry("text", jstr(text))]),
        Some(FileRead::Unreadable) => object(vec![entry("unreadable", Json::Bool(true))]),
    }
}

fn agent_json(agent: &Agent) -> Json {
    object(vec![
        entry("id", jstr(&agent.id)),
        entry("code", jstr(&agent.code)),
        entry("slug", jstr(&agent.slug)),
        entry("capabilities", texts(&agent.capabilities)),
    ])
}

fn signing_json(signing: &Signing) -> Json {
    object(vec![
        entry("agent_session", Json::Bool(signing.agent_session)),
        entry("config_error", optional(signing.config_error.as_deref())),
        entry("real_key", Json::Bool(signing.real_key)),
    ])
}

fn plan_file_json(file: &StoredPlanFile) -> Json {
    object(vec![
        entry("correlation_id", jstr(&file.correlation_id)),
        entry("text", optional(file.text.as_deref())),
    ])
}

fn facts_json(facts: &PlanFacts) -> Json {
    object(vec![
        entry("new_correlation_id", jstr(&facts.new_correlation_id)),
        entry(
            "agents",
            Json::Arr(facts.agents.iter().map(agent_json).collect()),
        ),
        entry("plans_dir", jstr(&facts.plans_dir)),
        entry("signing", signing_json(&facts.signing)),
        entry(
            "prefix_tasks",
            Json::Arr(
                facts
                    .prefix_tasks
                    .iter()
                    .map(|row| Json::Obj(row.clone()))
                    .collect(),
            ),
        ),
        entry(
            "correlation_tasks",
            Json::Arr(
                facts
                    .correlation_tasks
                    .iter()
                    .map(|row| Json::Obj(row.clone()))
                    .collect(),
            ),
        ),
        entry(
            "plan_files",
            Json::Arr(facts.plan_files.iter().map(plan_file_json).collect()),
        ),
    ])
}

fn args_json(request: &PlanRequest) -> Json {
    object(vec![
        entry("pattern", jstr(&request.pattern)),
        entry("risk_class", jstr(&request.risk_class)),
        entry("priority", jstr(&request.priority)),
        entry("prefix", optional(request.prefix.as_deref())),
        entry("acceptance", texts(&request.acceptance)),
        entry("graph_id", optional(request.graph_id.as_deref())),
        entry("brief_file", file_read(&request.brief_file)),
        entry("brief_text", optional(request.brief_text.as_deref())),
        entry("plan_file", file_read(&request.plan_file)),
        entry("slices_json", optional(request.slices_json.as_deref())),
        entry("slices_file", file_read(&request.slices_file)),
        entry(
            "correlation_id",
            optional(request.correlation_id.as_deref()),
        ),
        entry("dry_run", Json::Bool(request.dry_run)),
    ])
}

/// Renders one plan request for the guest, byte for byte what [`decode_request`] reads back.
pub fn encode_request(facts: &PlanFacts, request: &PlanRequest, now: f64) -> String {
    to_python_dumps(&object(vec![
        entry("mode", jstr(MODE)),
        entry("now", Json::Float(now)),
        entry("facts", facts_json(facts)),
        entry("request", args_json(request)),
    ]))
}

fn new_task_json(task: &NewTask) -> Json {
    object(vec![
        entry("task_id", jstr(&task.task_id)),
        entry("correlation_id", jstr(&task.correlation_id)),
        entry("capability", jstr(&task.capability)),
        entry("title", task.title.clone()),
        entry("agent_id", jstr(&task.agent_id)),
        entry("dag_depth", Json::Int(task.dag_depth)),
        entry("depends_on", texts(&task.depends_on)),
        entry("acceptance", task.acceptance.clone()),
        entry("budget", task.budget.clone()),
        entry("risk_class", jstr(&task.risk_class)),
        entry("priority", jstr(&task.priority)),
        entry("notes", Json::Obj(task.notes.clone())),
        entry("created_at", Json::Float(task.created_at)),
    ])
}

fn step_json(step: &Step) -> Json {
    object(vec![
        entry("task_id", jstr(&step.task_id)),
        entry("from", jstr(step.from)),
        entry("to", jstr(step.to)),
        entry("actor", jstr(step.actor)),
        entry("reason", jstr(&step.reason)),
    ])
}

fn op_json(op: &PlanOp) -> Json {
    match op {
        PlanOp::Create(task) => object(vec![entry("create", new_task_json(task))]),
        PlanOp::Transition(step) => object(vec![entry("transition", step_json(step))]),
    }
}

fn effect_json(effect: &PlanEffect) -> Json {
    match effect {
        PlanEffect::BindGraph(bind) => object(vec![entry(
            "bind_graph",
            object(vec![
                entry("correlation_id", jstr(&bind.correlation_id)),
                entry(
                    "explicit_graph_id",
                    optional(bind.explicit_graph_id.as_deref()),
                ),
                entry("brief", jstr(&bind.brief)),
                entry("task_ids", texts(&bind.task_ids)),
            ]),
        )]),
        PlanEffect::Emit {
            event_type,
            payload,
        } => object(vec![entry(
            "emit",
            object(vec![
                entry("event_type", jstr(event_type)),
                entry("payload", Json::Obj(payload.clone())),
            ]),
        )]),
    }
}

fn files_json(files: &PlanFiles) -> Json {
    object(vec![
        entry("plan_name", jstr(&files.plan_name)),
        entry("plan_text", jstr(&files.plan_text)),
        entry("latest_correlation", jstr(&files.latest_correlation)),
    ])
}

fn kickoff_json(kickoff: &Kickoff) -> Json {
    object(vec![
        entry("correlation_id", jstr(&kickoff.correlation_id)),
        entry("risk_class", jstr(&kickoff.risk_class)),
        entry("payload", kickoff.payload.clone()),
    ])
}

fn outcome_name(outcome: Outcome) -> &'static str {
    match outcome {
        Outcome::DryRun => "dry_run",
        Outcome::Reused => "reused",
        Outcome::Created => "created",
    }
}

fn derived_json(derived: &PlanDerived) -> Json {
    object(vec![
        entry("outcome", jstr(outcome_name(derived.outcome))),
        entry("reply", Json::Obj(derived.reply.clone())),
        entry(
            "ctx_correlation_id",
            optional(derived.ctx_correlation_id.as_deref()),
        ),
        entry("ops", Json::Arr(derived.ops.iter().map(op_json).collect())),
        entry(
            "files",
            derived.files.as_ref().map_or(Json::Null, files_json),
        ),
        entry(
            "effects",
            Json::Arr(derived.effects.iter().map(effect_json).collect()),
        ),
        entry(
            "kickoff",
            derived.kickoff.as_ref().map_or(Json::Null, kickoff_json),
        ),
    ])
}

/// Renders the answer of one derivation: `{"plan": ..}`, `{"refused": ..}` or `{"not_started": ..}` (see the module documentation).
///
/// This is the function both sides run: the guest on the answer it derived, the host on the answer it derived, and the host compares the
/// two byte strings.
pub fn encode_outcome(outcome: &Result<PlanDerived, PlanError>) -> String {
    let body = match outcome {
        Ok(derived) => entry("plan", derived_json(derived)),
        Err(PlanError::NotStarted(why)) => entry("not_started", jstr(why)),
        Err(PlanError::Refused(error)) => entry(
            "refused",
            object(vec![
                entry("code", jstr(error.code().as_str())),
                entry("message", jstr(error.message())),
                entry("details", Json::Obj(error.details().to_vec())),
            ]),
        ),
    };
    to_python_dumps(&object(vec![body]))
}

/// A shape problem, as the description of the field at fault. The readers turn it into `E-INPUT`.
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
        _ => Err(format!(
            "field {path} must hold exactly the keys {}",
            keys.join(", ")
        )),
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

fn optional_text(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<Option<String>> {
    match member(fields, scope, key)? {
        Json::Null => Ok(None),
        Json::Str(text) => Ok(Some(text.clone())),
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

fn texts_of(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<Vec<String>> {
    items(fields, scope, key)?
        .iter()
        .map(|item| match item {
            Json::Str(text) => Ok(text.clone()),
            _ => Err(bad(&format!("{scope}.{key}"))),
        })
        .collect()
}

fn rows_of(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<Vec<Fields>> {
    items(fields, scope, key)?
        .iter()
        .map(|row| match row {
            Json::Obj(inner) => Ok(inner.clone()),
            _ => Err(bad(&format!("{scope}.{key}"))),
        })
        .collect()
}

/// A file read: `null`, `{"text": ..}` or `{"unreadable": true}`.
fn file_of(fields: &[(String, Json)], scope: &str, key: &str) -> Shape<Option<FileRead>> {
    let path = format!("{scope}.{key}");
    match member(fields, scope, key)? {
        Json::Null => Ok(None),
        Json::Obj(inner) => match inner.as_slice() {
            [(name, Json::Str(content))] if name.as_str() == "text" => {
                Ok(Some(FileRead::Text(content.clone())))
            }
            [(name, Json::Bool(true))] if name.as_str() == "unreadable" => {
                Ok(Some(FileRead::Unreadable))
            }
            _ => Err(bad(&path)),
        },
        _ => Err(bad(&path)),
    }
}

fn agent_of(value: &Json) -> Shape<Agent> {
    let fields = exact(value, "facts.agents", &AGENT_KEYS)?;
    Ok(Agent {
        id: text(fields, "facts.agents", "id")?.to_string(),
        code: text(fields, "facts.agents", "code")?.to_string(),
        slug: text(fields, "facts.agents", "slug")?.to_string(),
        capabilities: texts_of(fields, "facts.agents", "capabilities")?,
    })
}

fn signing_of(value: &Json) -> Shape<Signing> {
    let fields = exact(value, "facts.signing", &SIGNING_KEYS)?;
    Ok(Signing {
        agent_session: flag(fields, "facts.signing", "agent_session")?,
        config_error: optional_text(fields, "facts.signing", "config_error")?,
        real_key: flag(fields, "facts.signing", "real_key")?,
    })
}

fn plan_file_of(value: &Json) -> Shape<StoredPlanFile> {
    let fields = exact(value, "facts.plan_files", &PLAN_FILE_KEYS)?;
    Ok(StoredPlanFile {
        correlation_id: text(fields, "facts.plan_files", "correlation_id")?.to_string(),
        text: optional_text(fields, "facts.plan_files", "text")?,
    })
}

fn facts_of(value: &Json) -> Shape<PlanFacts> {
    let fields = exact(value, "facts", &FACTS_KEYS)?;
    Ok(PlanFacts {
        new_correlation_id: text(fields, "facts", "new_correlation_id")?.to_string(),
        agents: items(fields, "facts", "agents")?
            .iter()
            .map(agent_of)
            .collect::<Shape<Vec<_>>>()?,
        plans_dir: text(fields, "facts", "plans_dir")?.to_string(),
        signing: signing_of(member(fields, "facts", "signing")?)?,
        prefix_tasks: rows_of(fields, "facts", "prefix_tasks")?,
        correlation_tasks: rows_of(fields, "facts", "correlation_tasks")?,
        plan_files: items(fields, "facts", "plan_files")?
            .iter()
            .map(plan_file_of)
            .collect::<Shape<Vec<_>>>()?,
    })
}

fn args_of(value: &Json) -> Shape<PlanRequest> {
    let fields = exact(value, "request", &ARGS_KEYS)?;
    Ok(PlanRequest {
        pattern: text(fields, "request", "pattern")?.to_string(),
        risk_class: text(fields, "request", "risk_class")?.to_string(),
        priority: text(fields, "request", "priority")?.to_string(),
        prefix: optional_text(fields, "request", "prefix")?,
        acceptance: texts_of(fields, "request", "acceptance")?,
        graph_id: optional_text(fields, "request", "graph_id")?,
        brief_file: file_of(fields, "request", "brief_file")?,
        brief_text: optional_text(fields, "request", "brief_text")?,
        plan_file: file_of(fields, "request", "plan_file")?,
        slices_json: optional_text(fields, "request", "slices_json")?,
        slices_file: file_of(fields, "request", "slices_file")?,
        correlation_id: optional_text(fields, "request", "correlation_id")?,
        dry_run: flag(fields, "request", "dry_run")?,
    })
}

fn request_of(value: &Json) -> Shape<(PlanFacts, PlanRequest, f64)> {
    let top = exact(value, "request", &REQUEST_KEYS)?;
    if text(top, "request", "mode")? != MODE {
        return Err(bad("request.mode"));
    }
    let now = match member(top, "request", "now")? {
        Json::Float(now) => *now,
        // An integral clock reading may arrive as an integer; a Python float is exact in f64 up to 2^53.
        Json::Int(now) if now.unsigned_abs() <= 1 << 53 => *now as f64,
        _ => return Err(bad("request.now")),
    };
    let facts = facts_of(member(top, "request", "facts")?)?;
    let request = args_of(member(top, "request", "request")?)?;
    Ok((facts, request, now))
}

fn input(problem: String) -> SeamError {
    SeamError::new(ErrorCode::Input, problem)
}

/// Reads one request value into what [`derive_plan`] takes. A shape problem is `E-INPUT`, naming the field.
pub fn decode_request(value: &Json) -> Result<(PlanFacts, PlanRequest, f64), SeamError> {
    request_of(value).map_err(input)
}

/// The guest entry of the `plan` mode: UTF-8 request bytes in, encoded answer bytes out.
///
/// A request that is not UTF-8 or breaks the schema is `E-INPUT`; text that is not JSON, or holds what [`Json`] cannot carry, is `E-DEP`
/// (see [`parse_python_json`]). A request that reads is always answered: a refusal or a decline is a reply, not an `invoke-error`.
pub fn invoke(input_bytes: &[u8]) -> Result<Vec<u8>, SeamError> {
    let text = std::str::from_utf8(input_bytes)
        .map_err(|_| SeamError::new(ErrorCode::Input, "request is not valid UTF-8"))?;
    let (facts, request, now) = decode_request(&parse_python_json(text)?)?;
    Ok(encode_outcome(&derive_plan(&facts, &request, now)).into_bytes())
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::*;
    use crate::parse_json;
    use crate::plan_derive::{candidate_prefixes, derived_prefix};

    const VECTORS: &str = include_str!("../tests/vectors/plan_derive.json");

    fn member_of<'a>(value: &'a Json, key: &str) -> &'a Json {
        match value {
            Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
            other => panic!("not an object looking for {key}: {other:?}"),
        }
    }

    fn list_of(value: &Json) -> &[Json] {
        match value {
            Json::Arr(items) => items,
            other => panic!("not a list: {other:?}"),
        }
    }

    fn text_of(value: &Json) -> &str {
        match value {
            Json::Str(text) => text,
            other => panic!("not text: {other:?}"),
        }
    }

    /// The wire request of one recorded case: its own `request` and `facts`, with the roster the document holds once.
    fn case_wire(document: &Json, case: &Json) -> Json {
        let mut facts = member_of(case, "facts").clone();
        if let Json::Obj(inner) = &mut facts {
            inner.push(entry("agents", member_of(document, "agents").clone()));
        }
        object(vec![
            entry("mode", jstr(MODE)),
            entry("now", member_of(case, "now").clone()),
            entry("facts", facts),
            entry("request", member_of(case, "request").clone()),
        ])
    }

    fn same_facts(left: &PlanFacts, right: &PlanFacts) -> bool {
        left.new_correlation_id == right.new_correlation_id
            && left.agents == right.agents
            && left.plans_dir == right.plans_dir
            && left.signing == right.signing
            && left.prefix_tasks == right.prefix_tasks
            && left.correlation_tasks == right.correlation_tasks
            && left.plan_files == right.plan_files
    }

    /// The correlation a call uses: the request's when it names a non-empty one, else the host's fresh id.
    fn correlation_of(facts: &PlanFacts, request: &PlanRequest) -> String {
        request
            .correlation_id
            .clone()
            .filter(|id| !id.is_empty())
            .unwrap_or_else(|| facts.new_correlation_id.clone())
    }

    fn with_prefix(task_id: &str, prefix: &str) -> bool {
        task_id
            .strip_prefix(prefix)
            .is_some_and(|rest| rest.starts_with('-'))
    }

    #[test]
    fn every_vector_case_gives_the_guest_the_host_answer_byte_for_byte() {
        let document = parse_json(VECTORS).expect("the vector file parses");
        let mut failures = Vec::new();
        let mut kinds: BTreeSet<&'static str> = BTreeSet::new();
        let mut refusals: BTreeSet<String> = BTreeSet::new();
        let mut reuse = 0;
        let mut six_char_prefix = 0;
        let mut created_checked = 0;
        for case in list_of(member_of(&document, "cases")) {
            let name = text_of(member_of(case, "name"));
            let wire = to_python_dumps(&case_wire(&document, case));
            let Ok(value) = parse_python_json(&wire) else {
                failures.push(format!("{name}: the case is not a wire request"));
                continue;
            };
            let (facts, request, now) = match decode_request(&value) {
                Ok(decoded) => decoded,
                Err(error) => {
                    failures.push(format!("{name}: decode refused: {}", error.message()));
                    continue;
                }
            };
            // decode(encode(facts)) == facts: the encoding is read back to the same values.
            let encoded = encode_request(&facts, &request, now);
            let (back, back_request, back_now) =
                match parse_python_json(&encoded).and_then(|value| decode_request(&value)) {
                    Ok(decoded) => decoded,
                    Err(error) => {
                        failures.push(format!("{name}: round trip refused: {}", error.message()));
                        continue;
                    }
                };
            if !same_facts(&facts, &back) || request != back_request || now != back_now {
                failures.push(format!("{name}: round trip changes the request"));
                continue;
            }
            // The guest runs the host's function on the request; the reply must equal the host's answer.
            let host = derive_plan(&facts, &request, now);
            let host_bytes = encode_outcome(&host);
            for bytes in [encoded.as_bytes(), wire.as_bytes()] {
                match invoke(bytes) {
                    Ok(guest) if guest == host_bytes.as_bytes() => {}
                    Ok(_) => {
                        failures.push(format!("{name}: the guest's reply differs from the host's"))
                    }
                    Err(error) => {
                        failures.push(format!("{name}: the guest refused: {}", error.message()))
                    }
                }
            }
            // The envelope is one of the three forms, and the host's answer is what the reply names.
            let reply = parse_python_json(&host_bytes).expect("the answer is JSON");
            match reply {
                Json::Obj(fields) if fields.len() == 1 => match fields[0].0.as_str() {
                    "plan" => {
                        kinds.insert("plan");
                    }
                    "refused" => {
                        kinds.insert("refused");
                    }
                    "not_started" => {
                        kinds.insert("not_started");
                    }
                    other => failures.push(format!("{name}: unknown envelope {other}")),
                },
                _ => failures.push(format!("{name}: the answer is not one envelope")),
            }
            match &host {
                Ok(derived) => {
                    if derived.outcome == Outcome::Reused {
                        reuse += 1;
                    }
                    if let Outcome::Created = derived.outcome {
                        // Every created task lives under a candidate prefix: the set the host reads and the derivation writes agree.
                        let corr = correlation_of(&facts, &request);
                        let candidates = candidate_prefixes(&corr, request.prefix.as_deref());
                        for op in &derived.ops {
                            if let PlanOp::Create(task) = op {
                                created_checked += 1;
                                if !candidates
                                    .iter()
                                    .any(|prefix| with_prefix(&task.task_id, prefix))
                                {
                                    failures.push(format!(
                                        "{name}: created {} is outside the candidate prefixes",
                                        task.task_id
                                    ));
                                }
                                if request.prefix.is_none()
                                    && with_prefix(&task.task_id, &derived_prefix(&corr, 6))
                                    && !with_prefix(&task.task_id, &derived_prefix(&corr, 4))
                                {
                                    six_char_prefix += 1;
                                }
                            }
                        }
                    }
                }
                Err(PlanError::Refused(error)) => {
                    refusals.insert(error.message().to_string());
                }
                Err(PlanError::NotStarted(_)) => {}
            }
        }
        assert!(
            failures.is_empty(),
            "{} wire answers differ:\n{}",
            failures.len(),
            failures.join("\n")
        );
        assert_eq!(kinds, BTreeSet::from(["plan", "refused", "not_started"]));
        assert!(reuse > 0, "no vector case is a reuse");
        assert!(
            six_char_prefix > 0,
            "no vector case takes the six-character prefix"
        );
        assert!(
            created_checked > 0,
            "no created task was checked against the candidate prefixes"
        );
        for needle in [
            "already used by correlation",
            "already in use; pass a new --prefix",
            "ends in .rN",
        ] {
            assert!(
                refusals.iter().any(|message| message.contains(needle)),
                "no vector case is refused with {needle:?}"
            );
        }
    }

    #[test]
    fn the_request_schema_is_strict_and_names_the_field() {
        let document = parse_json(VECTORS).expect("the vector file parses");
        let case = list_of(member_of(&document, "cases"))
            .iter()
            .find(|case| text_of(member_of(case, "name")) == "pattern-feature-low")
            .expect("the case exists");
        let valid = case_wire(&document, case);
        assert!(decode_request(&valid).is_ok());

        let with = |path: &[&str], edit: fn(&mut Fields)| -> Json {
            fn walk(value: &mut Json, path: &[&str], edit: fn(&mut Fields)) {
                match value {
                    Json::Obj(fields) => match path {
                        [] => edit(fields),
                        [key, rest @ ..] => {
                            let slot = fields
                                .iter_mut()
                                .find(|(name, _)| name == key)
                                .expect("the key exists");
                            walk(&mut slot.1, rest, edit);
                        }
                    },
                    Json::Arr(items) => {
                        if let Some(first) = items.first_mut() {
                            walk(first, path, edit);
                        }
                    }
                    _ => panic!("the path leaves the objects"),
                }
            }
            let mut value = valid.clone();
            walk(&mut value, path, edit);
            value
        };
        let problem = |value: &Json| decode_request(value).err().map(|e| e.message().to_string());

        // A missing key, an extra key and a mistyped one each name their field.
        let missing = with(&["request"], |fields| {
            fields.retain(|(k, _)| k != "dry_run")
        });
        assert!(problem(&missing).unwrap_or_default().contains("request"));
        let extra = with(&["request"], |fields| {
            fields.push(entry("extra", Json::Null))
        });
        assert!(problem(&extra).is_some());
        let mistyped = with(&["request"], |fields| {
            for (key, value) in fields.iter_mut() {
                if key == "dry_run" {
                    *value = jstr("SECRET-VALUE");
                }
            }
        });
        let message = problem(&mistyped).expect("refused");
        assert!(message.contains("request.dry_run"), "{message}");
        assert!(
            !message.contains("SECRET-VALUE"),
            "the message echoes a value: {message}"
        );
        let bad_agent = with(&["facts", "agents"], |fields| {
            fields.push(entry("note", jstr("x")));
        });
        assert!(problem(&bad_agent).is_some());
        let bad_mode = with(&[], |fields| {
            for (key, value) in fields.iter_mut() {
                if key.as_str() == "mode" {
                    *value = jstr("transition");
                }
            }
        });
        assert!(problem(&bad_mode).is_some());
    }

    #[test]
    fn invoke_refuses_what_it_cannot_read_with_the_codes_of_the_seam() {
        let not_utf8 = invoke(&[0xff, 0xfe]).expect_err("not UTF-8");
        assert_eq!(not_utf8.code(), ErrorCode::Input);
        let not_json = invoke(b"{ not json").expect_err("not JSON");
        assert_eq!(not_json.code(), ErrorCode::Dep);
        let wrong_shape = invoke(br#"{"mode": "plan"}"#).expect_err("missing keys");
        assert_eq!(wrong_shape.code(), ErrorCode::Input);
    }

    #[test]
    fn the_answer_bytes_separate_every_kind_of_answer() {
        let refused = Err(PlanError::Refused(SeamError::new(ErrorCode::Input, "x")));
        let stopped = Err(PlanError::NotStarted("x".to_string()));
        let rendered = [encode_outcome(&refused), encode_outcome(&stopped)];
        assert_ne!(rendered[0], rendered[1]);
        assert!(rendered[0].starts_with("{\"refused\""));
        assert!(rendered[1].starts_with("{\"not_started\""));
    }
}
