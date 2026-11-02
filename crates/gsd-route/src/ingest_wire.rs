//! The wire form of the second-batch guest mode `ingest` (Phase 14.5 pin P2): the plan of [`plan_ingest`] as bytes in and bytes out.
//!
//! The host reads the facts of one call, encodes them with [`encode_request`], computes its own answer with [`plan_ingest`], and holds
//! the guest's reply against [`encode_outcome`] of that answer. The guest side is [`invoke`]: it decodes the request, runs the same
//! [`plan_ingest`] and renders the answer with [`encode_outcome`]. The reconcile stage of the plan is planned inside [`plan_ingest`];
//! its codec is the one of [`reconcile_wire`](crate::reconcile_wire). Nothing here reads a clock, a file, an environment variable or a
//! key: `now` and every signature answer are request fields.
//!
//! # Request
//!
//! ```text
//! {"mode": "ingest", "now": <number>,
//!  "facts": {"caller": "ingest" | "headless", "task_id": str, "agent_id": str,
//!            "result": {"value": <json>} | {"contract": <error>} | {"not_started": <name>},
//!            "meta": <json>, "digest": str | null, "advisory": bool, "claims": [<number>],
//!            "reconcile": <reconcile facts>}}
//! ```
//!
//! `<error>` is `{"code", "message", "details"}`, a taxonomy error kept verbatim; `<name>` is the kebab-case name of a
//! [`NotStarted`](crate::result_parse::NotStarted) reason. `<reconcile facts>` is the facts object of
//! [`reconcile_wire`](crate::reconcile_wire).
//!
//! # Reply
//!
//! ```text
//! {"plan": {"ops": [<op>], "reply": str, "notes_log": [str], "before": str, "after": str}}
//! {"refused": {"error": <error>, "events": [<event>]}}
//! {"not_started": str}
//! ```
//!
//! An op is one of `transition`, `set_notes`, `add_artifact`, `append_feedback`, `create_task`, `set_depends_on` and `emit`; its
//! fields are those of the matching [`IngestOp`] variant. Every object holds exactly its keys: a missing, extra or mistyped key is
//! `E-INPUT` in a request (the message names the field, never its value) and `E-CONTRACT` in a reply.
//!
//! # Determinism
//!
//! Both directions render with [`to_python_dumps`] and read with [`parse_python_json`]: a reply is a pure function of the request.

use gsd_codes::ErrorCode;

use crate::ingest_plan::{
    plan_ingest, Artifact, IngestError, IngestFacts, IngestOp, IngestPlan, Mode, Refusal,
};
use crate::pydumps::{parse_python_json, to_python_dumps};
use crate::reconcile_wire::{
    bad, entry, error_json, error_of, event_json, event_of, exact, facts_json, facts_of, fields_of,
    get, items_at, items_of, new_task_json, new_task_of, number_at, number_of, object, opt_text_of,
    plan_json, plan_of, text_at, text_of, texts_at, texts_json, Shape,
};
use crate::result_parse::{NotStarted, ResultError};
use crate::{jstr, Json, SeamError};

/// The `mode` of an ingest request.
pub const MODE: &str = "ingest";

const REQUEST_KEYS: [&str; 3] = ["mode", "now", "facts"];
const FACTS_KEYS: [&str; 9] = [
    "caller",
    "task_id",
    "agent_id",
    "result",
    "meta",
    "digest",
    "advisory",
    "claims",
    "reconcile",
];
const PLAN_KEYS: [&str; 5] = ["ops", "reply", "notes_log", "before", "after"];
const ARTIFACT_KEYS: [&str; 6] = ["task_id", "kind", "uri", "version", "digest", "producer"];
const REFUSED_KEYS: [&str; 2] = ["error", "events"];
const RESULT_NOT_STARTED: [NotStarted; 5] = [
    NotStarted::NestingTooDeep,
    NotStarted::IntegerOutOfRange,
    NotStarted::NonFiniteNumber,
    NotStarted::LoneSurrogate,
    NotStarted::ReprNotReproducible,
];

fn input(problem: String) -> SeamError {
    SeamError::new(ErrorCode::Input, problem)
}

fn contract(problem: String) -> SeamError {
    SeamError::new(ErrorCode::Contract, format!("reply {problem}"))
}

fn ingest_facts_json(facts: &IngestFacts) -> Json {
    object(vec![
        entry("caller", jstr(facts.mode.as_str())),
        entry("task_id", jstr(&facts.task_id)),
        entry("agent_id", jstr(&facts.agent_id)),
        entry("result", result_json(&facts.result)),
        entry("meta", facts.meta.clone()),
        entry("digest", facts.digest.as_deref().map_or(Json::Null, jstr)),
        entry("advisory", Json::Bool(facts.advisory)),
        entry(
            "claims",
            Json::Arr(
                facts
                    .claims
                    .iter()
                    .map(|claim| Json::Float(*claim))
                    .collect(),
            ),
        ),
        entry("reconcile", facts_json(&facts.reconcile)),
    ])
}

fn result_json(result: &Result<Json, ResultError>) -> Json {
    match result {
        Ok(value) => object(vec![entry("value", value.clone())]),
        Err(ResultError::Contract(error)) => object(vec![entry("contract", error_json(error))]),
        Err(ResultError::NotStarted(why)) => object(vec![entry("not_started", jstr(why.as_str()))]),
    }
}

fn result_of(value: &Json, path: &str) -> Shape<Result<Json, ResultError>> {
    let Json::Obj(fields) = value else {
        return Err(bad(path));
    };
    match fields.as_slice() {
        [(key, inner)] if key.as_str() == "value" => Ok(Ok(inner.clone())),
        [(key, inner)] if key.as_str() == "contract" => Ok(Err(ResultError::Contract(error_of(
            inner,
            &format!("{path}.contract"),
        )?))),
        [(key, inner)] if key.as_str() == "not_started" => {
            let name = text_of(inner, &format!("{path}.not_started"))?;
            let why = RESULT_NOT_STARTED
                .into_iter()
                .find(|why| why.as_str() == name)
                .ok_or_else(|| bad(&format!("{path}.not_started")))?;
            Ok(Err(ResultError::NotStarted(why)))
        }
        _ => Err(bad(path)),
    }
}

fn mode_of(value: &Json, path: &str) -> Shape<Mode> {
    match text_of(value, path)?.as_str() {
        "ingest" => Ok(Mode::Ingest),
        "headless" => Ok(Mode::Headless),
        _ => Err(bad(path)),
    }
}

fn claims_of(value: &Json, path: &str) -> Shape<Vec<f64>> {
    items_of(value, path)?
        .iter()
        .map(|claim| number_of(claim, path))
        .collect()
}

fn ingest_facts_of(value: &Json, path: &str) -> Shape<IngestFacts> {
    let fields = exact(value, path, &FACTS_KEYS)?;
    Ok(IngestFacts {
        mode: mode_of(get(fields, path, "caller")?, &format!("{path}.caller"))?,
        task_id: text_at(fields, path, "task_id")?,
        agent_id: text_at(fields, path, "agent_id")?,
        result: result_of(get(fields, path, "result")?, &format!("{path}.result"))?,
        meta: get(fields, path, "meta")?.clone(),
        digest: opt_text_of(get(fields, path, "digest")?, &format!("{path}.digest"))?,
        advisory: match get(fields, path, "advisory")? {
            Json::Bool(flag) => *flag,
            _ => return Err(bad(&format!("{path}.advisory"))),
        },
        claims: claims_of(get(fields, path, "claims")?, &format!("{path}.claims"))?,
        reconcile: facts_of(
            get(fields, path, "reconcile")?,
            &format!("{path}.reconcile"),
        )?,
    })
}

/// Renders the request of one ingest call.
pub fn encode_request(facts: &IngestFacts, now: f64) -> String {
    to_python_dumps(&object(vec![
        entry("mode", jstr(MODE)),
        entry("now", Json::Float(now)),
        entry("facts", ingest_facts_json(facts)),
    ]))
}

/// Reads one request value into the facts and `now`. A shape problem is `E-INPUT` naming the field.
pub fn decode_request(value: &Json) -> Result<(IngestFacts, f64), SeamError> {
    let fields = exact(value, "request", &REQUEST_KEYS).map_err(input)?;
    if text_at(fields, "request", "mode").map_err(input)? != MODE {
        return Err(input(bad("request.mode")));
    }
    let now = number_at(fields, "request", "now").map_err(input)?;
    let facts =
        ingest_facts_of(get(fields, "request", "facts").map_err(input)?, "facts").map_err(input)?;
    Ok((facts, now))
}

fn artifact_json(artifact: &Artifact) -> Json {
    object(vec![
        entry("task_id", jstr(&artifact.task_id)),
        entry("kind", jstr(&artifact.kind)),
        entry("uri", jstr(&artifact.uri)),
        entry("version", jstr(&artifact.version)),
        entry("digest", jstr(&artifact.digest)),
        entry("producer", jstr(&artifact.producer)),
    ])
}

fn artifact_of(value: &Json, path: &str) -> Shape<Artifact> {
    let fields = exact(value, path, &ARTIFACT_KEYS)?;
    Ok(Artifact {
        task_id: text_at(fields, path, "task_id")?,
        kind: text_at(fields, path, "kind")?,
        uri: text_at(fields, path, "uri")?,
        version: text_at(fields, path, "version")?,
        digest: text_at(fields, path, "digest")?,
        producer: text_at(fields, path, "producer")?,
    })
}

fn op_json(op: &IngestOp) -> Json {
    match op {
        IngestOp::Transition { task_id, plan } => object(vec![
            entry("op", jstr("transition")),
            entry("task_id", jstr(task_id)),
            entry("plan", plan_json(plan)),
        ]),
        IngestOp::SetNotes { task_id, entries } => object(vec![
            entry("op", jstr("set_notes")),
            entry("task_id", jstr(task_id)),
            entry("entries", Json::Obj(entries.clone())),
        ]),
        IngestOp::AddArtifact(artifact) => object(vec![
            entry("op", jstr("add_artifact")),
            entry("artifact", artifact_json(artifact)),
        ]),
        IngestOp::AppendFeedback {
            task_id,
            entry: feedback,
        } => object(vec![
            entry("op", jstr("append_feedback")),
            entry("task_id", jstr(task_id)),
            entry("entry", feedback.clone()),
        ]),
        IngestOp::CreateTask(task) => object(vec![
            entry("op", jstr("create_task")),
            entry("task", new_task_json(task)),
        ]),
        IngestOp::SetDependsOn {
            task_id,
            depends_on,
        } => object(vec![
            entry("op", jstr("set_depends_on")),
            entry("task_id", jstr(task_id)),
            entry("depends_on", texts_json(depends_on)),
        ]),
        IngestOp::Emit(event) => object(vec![
            entry("op", jstr("emit")),
            entry("event", event_json(event)),
        ]),
    }
}

fn op_of(value: &Json, path: &str) -> Shape<IngestOp> {
    let Json::Obj(fields) = value else {
        return Err(bad(path));
    };
    match text_at(fields, path, "op")?.as_str() {
        "transition" => {
            let fields = exact(value, path, &["op", "task_id", "plan"])?;
            Ok(IngestOp::Transition {
                task_id: text_at(fields, path, "task_id")?,
                plan: plan_of(get(fields, path, "plan")?, &format!("{path}.plan"))?,
            })
        }
        "set_notes" => {
            let fields = exact(value, path, &["op", "task_id", "entries"])?;
            Ok(IngestOp::SetNotes {
                task_id: text_at(fields, path, "task_id")?,
                entries: fields_of(get(fields, path, "entries")?, &format!("{path}.entries"))?,
            })
        }
        "add_artifact" => {
            let fields = exact(value, path, &["op", "artifact"])?;
            Ok(IngestOp::AddArtifact(artifact_of(
                get(fields, path, "artifact")?,
                &format!("{path}.artifact"),
            )?))
        }
        "append_feedback" => {
            let fields = exact(value, path, &["op", "task_id", "entry"])?;
            Ok(IngestOp::AppendFeedback {
                task_id: text_at(fields, path, "task_id")?,
                entry: get(fields, path, "entry")?.clone(),
            })
        }
        "create_task" => {
            let fields = exact(value, path, &["op", "task"])?;
            Ok(IngestOp::CreateTask(new_task_of(
                get(fields, path, "task")?,
                &format!("{path}.task"),
            )?))
        }
        "set_depends_on" => {
            let fields = exact(value, path, &["op", "task_id", "depends_on"])?;
            Ok(IngestOp::SetDependsOn {
                task_id: text_at(fields, path, "task_id")?,
                depends_on: texts_at(fields, path, "depends_on")?,
            })
        }
        "emit" => {
            let fields = exact(value, path, &["op", "event"])?;
            Ok(IngestOp::Emit(event_of(
                get(fields, path, "event")?,
                &format!("{path}.event"),
            )?))
        }
        _ => Err(bad(&format!("{path}.op"))),
    }
}

fn refusal_json(refusal: &Refusal) -> Json {
    object(vec![
        entry("error", error_json(&refusal.error)),
        entry(
            "events",
            Json::Arr(refusal.events.iter().map(event_json).collect()),
        ),
    ])
}

fn refusal_of(value: &Json, path: &str) -> Shape<Refusal> {
    let fields = exact(value, path, &REFUSED_KEYS)?;
    let events_path = format!("{path}.events");
    let events = items_at(fields, path, "events")?
        .iter()
        .map(|event| event_of(event, &events_path))
        .collect::<Shape<Vec<_>>>()?;
    Ok(Refusal {
        error: error_of(get(fields, path, "error")?, &format!("{path}.error"))?,
        events,
    })
}

/// Renders the answer of one ingest call: `{"plan": ..}`, `{"refused": ..}` or `{"not_started": ..}`.
pub fn encode_outcome(outcome: &Result<IngestPlan, IngestError>) -> String {
    let body = match outcome {
        Ok(plan) => entry(
            "plan",
            object(vec![
                entry("ops", Json::Arr(plan.ops.iter().map(op_json).collect())),
                entry("reply", jstr(&plan.reply)),
                entry("notes_log", texts_json(&plan.notes_log)),
                entry("before", jstr(&plan.before)),
                entry("after", jstr(&plan.after)),
            ]),
        ),
        Err(IngestError::NotStarted(why)) => entry("not_started", jstr(why)),
        Err(IngestError::Refused(refusal)) => entry("refused", refusal_json(refusal)),
    };
    to_python_dumps(&object(vec![body]))
}

/// Reads the reply bytes into the answer they carry. A reply of another shape is `E-CONTRACT`.
pub fn decode_outcome(bytes: &[u8]) -> Result<Result<IngestPlan, IngestError>, SeamError> {
    let text = std::str::from_utf8(bytes).map_err(|_| contract("is not valid UTF-8".into()))?;
    let value = parse_python_json(text)?;
    let Json::Obj(top) = &value else {
        return Err(contract("must be a JSON object".into()));
    };
    let decoded = match top.as_slice() {
        [(key, plan)] if key.as_str() == "plan" => {
            let fields = exact(plan, "plan", &PLAN_KEYS).map_err(contract)?;
            let ops = items_at(fields, "plan", "ops")
                .map_err(contract)?
                .iter()
                .map(|op| op_of(op, "plan.ops"))
                .collect::<Shape<Vec<_>>>()
                .map_err(contract)?;
            Ok(IngestPlan {
                ops,
                reply: text_at(fields, "plan", "reply").map_err(contract)?,
                notes_log: texts_at(fields, "plan", "notes_log").map_err(contract)?,
                before: text_at(fields, "plan", "before").map_err(contract)?,
                after: text_at(fields, "plan", "after").map_err(contract)?,
            })
        }
        [(key, refused)] if key.as_str() == "refused" => Err(IngestError::Refused(
            refusal_of(refused, "refused").map_err(contract)?,
        )),
        [(key, why)] if key.as_str() == "not_started" => Err(IngestError::NotStarted(
            text_of(why, "not_started").map_err(contract)?,
        )),
        _ => return Err(contract(bad("plan, refused or not_started"))),
    };
    Ok(decoded)
}

/// The guest entry of the `ingest` mode: UTF-8 request bytes in, encoded reply bytes out.
///
/// A request that is not UTF-8 or breaks the schema is `E-INPUT`; one holding what [`Json`] cannot carry is `E-DEP` (see
/// [`parse_python_json`]). A request that reads is always answered: a refusal or a not-started plan is a reply.
pub fn invoke(input_bytes: &[u8]) -> Result<Vec<u8>, SeamError> {
    let text = std::str::from_utf8(input_bytes)
        .map_err(|_| SeamError::new(ErrorCode::Input, "request is not valid UTF-8"))?;
    let (facts, now) = decode_request(&parse_python_json(text)?)?;
    Ok(encode_outcome(&plan_ingest(&facts, now)).into_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gate_facts::{IssuerState, VerdictFacts, VerdictRow};
    use crate::reconcile_plan::{EmitEvent, KeyPolicy, NewTask, ReconcileFacts};
    use crate::transition_plan::{AuditRow, PlanEffect, TransitionPlan};
    use crate::{parse_json, Fields};

    const VECTORS: &str = include_str!("../tests/vectors/ingest.json");

    /// One recorded call: the facts the host reads, as the planner takes them, and `now`.
    struct Case {
        name: String,
        facts: IngestFacts,
        now: f64,
    }

    fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
        match value {
            Json::Obj(fields) => obj_get_or_panic(fields, key),
            other => panic!("not an object looking for {key}: {other:?}"),
        }
    }

    fn obj_get_or_panic<'a>(fields: &'a Fields, key: &str) -> &'a Json {
        crate::obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}"))
    }

    fn has(value: &Json, key: &str) -> bool {
        matches!(value, Json::Obj(fields) if crate::obj_get(fields, key).is_some())
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
            matches!(crate::obj_get(row, "correlation_id"), Some(Json::Str(id)) if *id == correlation)
        });
        let result = match member(value, "result") {
            contract if has(contract, "contract") => Err(ResultError::Contract(
                SeamError::unprefixed(ErrorCode::Contract, text(member(contract, "contract"))),
            )),
            valid => Ok(member(valid, "value").clone()),
        };
        let mode = match text(member(value, "mode")) {
            "ingest" => Mode::Ingest,
            "headless" => Mode::Headless,
            other => panic!("unknown mode {other}"),
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
                task_id: text(obj_get_or_panic(row, "task_id")).to_string(),
                state: text(obj_get_or_panic(row, "state")).to_string(),
            })
            .collect();
        let policy = member(value, "key_policy");
        Case {
            name: text(member(value, "name")).to_string(),
            facts: IngestFacts {
                mode,
                task_id: text(member(value, "task_id")).to_string(),
                agent_id: match member(value, "agent_id") {
                    Json::Str(agent) => agent.clone(),
                    _ => String::new(),
                },
                result,
                meta: match member(value, "meta") {
                    Json::Null => Json::Obj(Vec::new()),
                    other => other.clone(),
                },
                digest: optional_text(member(value, "digest")),
                advisory: flag(member(value, "advisory")),
                claims: items(member(value, "claims")).iter().map(number).collect(),
                reconcile: ReconcileFacts {
                    correlation_id: correlation,
                    tasks: mine,
                    verdicts,
                    other_tasks,
                    key_policy: Some(KeyPolicy {
                        require_key: flag(member(policy, "require_key")),
                        real_key: flag(member(policy, "real_key")),
                        dev_key: flag(member(policy, "dev_key")),
                    }),
                },
            },
            now: number(member(value, "now")),
        }
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
            cases.len() >= 400,
            "the vector file holds {} cases",
            cases.len()
        );
        for case in &cases {
            let request = encode_request(&case.facts, case.now);
            let host = plan_ingest(&case.facts, case.now);
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
            let (facts, now) = decode_request(&value)
                .unwrap_or_else(|error| panic!("{}: {}", case.name, error.message()));
            assert_eq!(
                encode_request(&facts, now),
                request,
                "{}: the request does not survive a decode",
                case.name
            );
        }
    }

    fn request_value(case: &Case) -> Json {
        parse_python_json(&encode_request(&case.facts, case.now)).expect("the request parses")
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
        top.retain(|(name, _)| name != "now");
        assert!(
            message_of(&missing).contains("now"),
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
        let Json::Obj(facts) = field_mut(top, "facts") else {
            panic!("object")
        };
        *field_mut(facts, "advisory") = Json::Str("no".to_string());
        assert!(
            message_of(&mistyped).contains("advisory"),
            "{}",
            message_of(&mistyped)
        );

        let mut claims = request_value(case);
        let Json::Obj(top) = &mut claims else {
            panic!("object")
        };
        let Json::Obj(facts) = field_mut(top, "facts") else {
            panic!("object")
        };
        *field_mut(facts, "claims") = Json::Arr(vec![Json::Str("1".to_string())]);
        assert!(
            message_of(&claims).contains("claims"),
            "{}",
            message_of(&claims)
        );

        let mut reconcile = request_value(case);
        let Json::Obj(top) = &mut reconcile else {
            panic!("object")
        };
        let Json::Obj(facts) = field_mut(top, "facts") else {
            panic!("object")
        };
        let Json::Obj(stage) = field_mut(facts, "reconcile") else {
            panic!("object")
        };
        stage.push(("extra".to_string(), Json::Null));
        assert!(
            message_of(&reconcile).contains("reconcile"),
            "{}",
            message_of(&reconcile)
        );
    }

    /// One plan holding every kind of op, round-tripped through the reply codec. The vectors do not reach every kind (no recorded
    /// ingest sets `depends_on` through the reconcile stage), so the codec of each kind is proved here as well.
    #[test]
    fn every_op_kind_round_trips_through_the_reply_codec() {
        let plan = IngestPlan {
            ops: vec![
                IngestOp::Transition {
                    task_id: "T-1".to_string(),
                    plan: TransitionPlan {
                        from: "PLANNED".to_string(),
                        to: "CLAIMED".to_string(),
                        columns: vec![("state".to_string(), jstr("CLAIMED"))],
                        audit: vec![AuditRow {
                            from: "PLANNED".to_string(),
                            to: "CLAIMED".to_string(),
                            actor: "A05".to_string(),
                            reason: "claim".to_string(),
                        }],
                        effects: vec![PlanEffect::LiveRefresh],
                    },
                },
                IngestOp::SetNotes {
                    task_id: "T-1".to_string(),
                    entries: vec![("gate".to_string(), Json::Null)],
                },
                IngestOp::AddArtifact(Artifact {
                    task_id: "T-1".to_string(),
                    kind: "code".to_string(),
                    uri: "file://t".to_string(),
                    version: "1".to_string(),
                    digest: "d".to_string(),
                    producer: "A05".to_string(),
                }),
                IngestOp::AppendFeedback {
                    task_id: "T-2".to_string(),
                    entry: Json::Obj(vec![("finding".to_string(), jstr("F-1"))]),
                },
                IngestOp::CreateTask(NewTask {
                    task_id: "T-2.r1".to_string(),
                    correlation_id: "corr-a".to_string(),
                    capability: jstr("gate.review"),
                    title: "review (rerun)".to_string(),
                    agent_id: Json::Null,
                    dag_depth: Some(2),
                    depends_on: vec!["T-1".to_string()],
                    acceptance: Json::Arr(Vec::new()),
                    budget: Json::Obj(Vec::new()),
                    risk_class: jstr("low"),
                    priority: jstr("P2"),
                    notes: vec![("gate".to_string(), jstr("review"))],
                }),
                IngestOp::SetDependsOn {
                    task_id: "T-3".to_string(),
                    depends_on: vec!["T-1".to_string(), "T-2".to_string()],
                },
                IngestOp::Emit(EmitEvent {
                    event_type: "task.claimed".to_string(),
                    payload: vec![("task_id".to_string(), jstr("T-1"))],
                    task_id: Some("T-1".to_string()),
                }),
            ],
            reply: "IN_REVIEW".to_string(),
            notes_log: vec!["T-1: rework".to_string()],
            before: "PLANNED".to_string(),
            after: "IN_REVIEW".to_string(),
        };
        let outcome = Ok(plan);
        let bytes = encode_outcome(&outcome);
        let decoded = decode_outcome(bytes.as_bytes()).expect("the reply decodes");
        assert_eq!(decoded, outcome);
        assert_eq!(encode_outcome(&decoded), bytes);
    }

    #[test]
    fn every_result_shape_round_trips_through_the_guest() {
        let base = cases().remove(0).facts;
        let shapes = [
            Ok(Json::Null),
            Err(ResultError::Contract(
                SeamError::new(ErrorCode::Contract, "bad result")
                    .with_details(vec![("field".to_string(), jstr("result"))]),
            )),
        ]
        .into_iter()
        .chain(
            [
                NotStarted::NestingTooDeep,
                NotStarted::IntegerOutOfRange,
                NotStarted::NonFiniteNumber,
                NotStarted::LoneSurrogate,
                NotStarted::ReprNotReproducible,
            ]
            .map(|why| Err(ResultError::NotStarted(why))),
        );
        for result in shapes {
            let mut facts = base.clone();
            facts.result = result;
            let request = encode_request(&facts, 1.0);
            let reply = invoke(request.as_bytes()).expect("the guest answers");
            assert_eq!(
                reply,
                encode_outcome(&plan_ingest(&facts, 1.0)).into_bytes(),
                "{:?}",
                facts.result
            );
            let value = parse_python_json(&request).expect("the request parses");
            let (back, now) = decode_request(&value).expect("the request decodes");
            assert_eq!(encode_request(&back, now), request, "{:?}", facts.result);
        }
    }

    #[test]
    fn a_reply_of_another_shape_is_contract() {
        for text in [
            "[]",
            "{}",
            r#"{"plan": {"ops": []}}"#,
            r#"{"not_started": 1}"#,
            r#"{"refused": {"error": {"code": "E-NOPE", "message": "x", "details": {}}, "events": []}}"#,
            r#"{"plan": {"ops": [{"op": "nope"}], "reply": "", "notes_log": [], "before": "", "after": ""}}"#,
        ] {
            let error = decode_outcome(text.as_bytes()).expect_err(text);
            assert_eq!(error.code(), ErrorCode::Contract, "{text}");
        }
    }
}
