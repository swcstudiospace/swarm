//! The wire form of the guest `transition` mode: round trips over the whole vector set, golden bytes, and refusals.
#![cfg(feature = "write-routes")]

#[path = "support/transition_vectors.rs"]
mod transition_vectors;

use gsd_codes::ErrorCode;
use gsd_route::pydumps::parse_python_json;
use gsd_route::transition_plan::{
    decide_transition, ApprovalFacts, Facts, Request, TransitionError, WriteState,
};
use gsd_route::transition_wire::{
    decode_reply, decode_request, encode_reply, encode_request, transition_invoke, Decision,
};
use gsd_route::Json;
use transition_vectors::{cases, Case, CASES};

fn decide(case: &Case) -> Decision {
    decide_transition(&case.facts, &case.request, case.now)
}

fn case_with(id: &str) -> Case {
    cases()
        .into_iter()
        .find(|case| case.id == id)
        .unwrap_or_else(|| panic!("no case {id}"))
}

#[test]
fn the_vector_file_holds_every_case() {
    assert_eq!(cases().len(), CASES);
}

#[test]
fn a_request_reads_back_to_the_inputs_that_were_written() {
    for case in cases() {
        let wire = encode_request(&case.facts, &case.request, case.now);
        let value = parse_python_json(&wire).expect("the wire is JSON");
        let (facts, request, now) =
            decode_request(&value).unwrap_or_else(|err| panic!("{}: {err:?}", case.id));
        assert_eq!(facts.task, case.facts.task, "{}: task", case.id);
        assert_eq!(facts.approval, case.facts.approval, "{}: approval", case.id);
        assert_eq!(request, case.request, "{}: request", case.id);
        assert_eq!(now, case.now, "{}: now", case.id);
    }
}

#[test]
fn a_reply_reads_back_to_the_decision_that_was_written() {
    let (mut plans, mut final_errors, mut not_started) = (0, 0, 0);
    for case in cases() {
        let decision = decide(&case);
        match &decision {
            Ok(_) => plans += 1,
            Err(err) if err.write_state == WriteState::Final => final_errors += 1,
            Err(_) => not_started += 1,
        }
        let wire = encode_reply(&decision);
        assert_eq!(wire, encode_reply(&decision), "{}: rendering", case.id);
        let read =
            decode_reply(wire.as_bytes()).unwrap_or_else(|err| panic!("{}: {err:?}", case.id));
        assert_eq!(read, decision, "{}", case.id);
    }
    // Every kind of answer is exercised, or the round trip would prove nothing about the others.
    assert!(plans > 100 && final_errors > 100 && not_started >= 10);
}

#[test]
fn the_guest_entry_answers_every_case_with_the_bytes_of_the_direct_decision() {
    for case in cases() {
        let request = encode_request(&case.facts, &case.request, case.now);
        let reply = transition_invoke(request.as_bytes())
            .unwrap_or_else(|err| panic!("{}: {err:?}", case.id));
        assert_eq!(
            String::from_utf8(reply).expect("UTF-8"),
            encode_reply(&decide(&case)),
            "{}",
            case.id
        );
    }
}

/// Golden replies, written by hand from Python's `json.dumps` of the vector's expected row and audit rows (the oracle), so a change
/// of key order, spacing, escaping or float text fails here and not only in a round trip.
#[test]
fn a_reply_renders_as_python_dumps() {
    let table = [
        (
            "matrix-CREATED-VALIDATED",
            r#"{"plan": {"from": "CREATED", "to": "VALIDATED", "columns": {"state": "VALIDATED"}, "audit": [{"from": "CREATED", "to": "VALIDATED", "actor": "A01", "reason": "matrix"}], "effects": [{"kind": "live_refresh"}]}}"#,
        ),
        (
            "rework-plain",
            r#"{"plan": {"from": "IN_REVIEW", "to": "CHANGES_REQUESTED", "columns": {"state": "CHANGES_REQUESTED", "rework_loops": 1, "notes": {"verdicts_since": 1700000123.457789}}, "audit": [{"from": "IN_REVIEW", "to": "CHANGES_REQUESTED", "actor": "A01", "reason": "needs work"}], "effects": [{"kind": "live_refresh"}]}}"#,
        ),
        (
            "rework-cap-no-prior",
            r#"{"plan": {"from": "IN_REVIEW", "to": "ESCALATED", "columns": {"state": "ESCALATED"}, "audit": [{"from": "IN_REVIEW", "to": "CHANGES_REQUESTED", "actor": "A01", "reason": "rework cap reached"}, {"from": "CHANGES_REQUESTED", "to": "ESCALATED", "actor": "A01", "reason": "rework loops exhausted (2); needs work"}], "effects": [{"kind": "live_refresh"}]}}"#,
        ),
        (
            "matrix-CREATED-CREATED",
            r#"{"error": {"code": "E-CONTRACT", "message": "E-CONTRACT: illegal transition CREATED \u2192 CREATED for T-matrix-CREATED-CREATED", "details": {"task_id": "T-matrix-CREATED-CREATED"}, "write_state": "final"}}"#,
        ),
        (
            "notes-corrupt-text",
            r#"{"error": {"code": "E-DEP", "message": "E-DEP: not started: notes is not JSON that Rust can read", "details": {}, "write_state": "not_started"}}"#,
        ),
    ];
    for (id, golden) in table {
        assert_eq!(encode_reply(&decide(&case_with(id))), golden, "{id}");
    }
}

#[test]
fn a_request_renders_as_python_dumps() {
    let row = vec![
        ("task_id".to_string(), Json::Str("T-1".into())),
        ("created_at".to_string(), Json::Float(1700000000.0)),
        ("attempt".to_string(), Json::Int(0)),
        ("agent_id".to_string(), Json::Null),
        ("title".to_string(), Json::Str("caf\u{e9}".into())),
    ];
    let facts = Facts {
        task: Some(row),
        approval: Some(ApprovalFacts {
            require_key: true,
            real_key: false,
            dev_key: true,
            missing_gates: vec!["qa".into()],
            unaccepted_gates: Vec::new(),
        }),
    };
    let request = Request {
        task_id: "T-1".into(),
        to: "APPROVED".into(),
        actor: "A01".into(),
        reason: String::new(),
        manual: false,
    };
    assert_eq!(
        encode_request(&facts, &request, 1700000123.456789),
        r#"{"mode": "transition", "now": 1700000123.456789, "task": {"task_id": "T-1", "created_at": 1700000000.0, "attempt": 0, "agent_id": null, "title": "caf\u00e9"}, "request": {"task_id": "T-1", "to": "APPROVED", "actor": "A01", "reason": "", "manual": false}, "facts": {"approval": {"require_key": true, "real_key": false, "dev_key": true, "missing_gates": ["qa"], "unaccepted_gates": []}}}"#
    );
}

const BASE: &str = r#"{"mode": "transition", "now": 1.5, "task": null, "request": {"task_id": "T-1", "to": "VALIDATED", "actor": "A01", "reason": "r", "manual": true}, "facts": {"approval": null}}"#;

fn refused(text: &[u8]) -> (ErrorCode, String) {
    let err = transition_invoke(text).expect_err("the request is refused");
    (err.code(), err.message().to_string())
}

#[test]
fn the_base_request_used_by_the_refusal_table_is_answered() {
    let reply = transition_invoke(BASE.as_bytes()).expect("answered");
    let Err(error) = decode_reply(&reply).expect("a reply") else {
        panic!("an unknown task is refused");
    };
    assert_eq!(error.message, "E-INPUT: unknown task T-1");
}

#[test]
fn a_request_that_breaks_the_schema_is_input_and_names_only_the_field() {
    let table = [
        (
            r#""mode": "transition""#,
            r#""mode": "list""#,
            "request.mode",
        ),
        (r#""mode": "transition", "#, "", "request"),
        (r#""now": 1.5"#, r#""now": "1.5""#, "request.now"),
        (r#""now": 1.5"#, r#""now": 9007199254740993"#, "request.now"),
        (r#""now": 1.5"#, r#""now": null"#, "request.now"),
        (r#""task": null"#, r#""task": []"#, "request.task"),
        (
            r#""task_id": "T-1""#,
            r#""task_id": 5"#,
            "request.request.task_id",
        ),
        (
            r#""manual": true"#,
            r#""manual": "yes""#,
            "request.request.manual",
        ),
        (r#""reason": "r", "#, "", "request.request"),
        (
            r#""manual": true"#,
            r#""manual": true, "extra": 1"#,
            "request.request",
        ),
        (
            r#""facts": {"approval": null}"#,
            r#""facts": {}"#,
            "request.facts",
        ),
        (
            r#""facts": {"approval": null}"#,
            r#""facts": []"#,
            "request.facts",
        ),
        (r#""approval": null"#, r#""approval": {}"#, "facts.approval"),
        (
            r#""approval": null"#,
            r#""approval": {"require_key": true, "real_key": true, "dev_key": true, "missing_gates": [], "unaccepted_gates": [], "x": 1}"#,
            "facts.approval",
        ),
        (
            r#""approval": null"#,
            r#""approval": {"require_key": 1, "real_key": true, "dev_key": true, "missing_gates": [], "unaccepted_gates": []}"#,
            "facts.approval.require_key",
        ),
        (
            r#""approval": null"#,
            r#""approval": {"require_key": true, "real_key": true, "dev_key": true, "missing_gates": [7], "unaccepted_gates": []}"#,
            "facts.approval.missing_gates",
        ),
    ];
    for (from, to, field) in table {
        let text = BASE.replacen(from, to, 1);
        assert_ne!(text, BASE, "{field}: the edit changed nothing");
        let (code, message) = refused(text.as_bytes());
        assert_eq!(code, ErrorCode::Input, "{field}");
        assert_eq!(
            message,
            format!("E-INPUT: field {field} is missing or has the wrong type"),
            "{field}"
        );
    }
}

#[test]
fn an_integral_clock_reading_is_accepted() {
    let text = BASE.replacen(r#""now": 1.5"#, r#""now": 2"#, 1);
    transition_invoke(text.as_bytes()).expect("an integer now is a number");
}

#[test]
fn bytes_that_are_not_a_readable_request_are_refused_by_class() {
    let (code, message) = refused(b"\xff\xfe");
    assert_eq!(
        (code, message.as_str()),
        (ErrorCode::Input, "E-INPUT: request is not valid UTF-8")
    );
    let big = BASE.replacen(
        r#""task": null"#,
        r#""task": {"attempt": 123456789012345678901234567890}"#,
        1,
    );
    assert_eq!(
        refused(big.as_bytes()).0,
        ErrorCode::Dep,
        "an integer outside i64 is refused, not rounded"
    );
    assert_eq!(
        refused(b"{").0,
        ErrorCode::Dep,
        "text that is not JSON is refused by the strict reader"
    );
}

#[test]
fn a_reply_that_breaks_the_schema_is_a_contract_error() {
    let plan = r#"{"from": "A", "to": "B", "columns": {}, "audit": [], "effects": []}"#;
    let error = r#"{"code": "E-INPUT", "message": "m", "details": {}, "write_state": "final"}"#;
    let wrap = |key: &str, body: &str| format!(r#"{{"{key}": {body}}}"#);
    let table = [
        ("[]".to_string(), ""),
        ("{}".to_string(), "plan or error"),
        (
            format!(r#"{{"plan": {plan}, "error": {error}}}"#),
            "plan or error",
        ),
        (wrap("other", plan), "plan or error"),
        (
            wrap(
                "plan",
                &plan.replace(r#""columns": {}"#, r#""columns": []"#),
            ),
            "plan.columns",
        ),
        (
            wrap(
                "plan",
                &plan.replace(r#""audit": []"#, r#""audit": [{"from": "A"}]"#),
            ),
            "plan.audit",
        ),
        (
            wrap(
                "plan",
                &plan.replace(r#""effects": []"#, r#""effects": [{"kind": "x"}]"#),
            ),
            "plan.effects.kind",
        ),
        (wrap("plan", &plan.replace(r#""to": "B", "#, "")), "plan"),
        (
            wrap("error", &error.replace("E-INPUT", "E-NOPE")),
            "error.code",
        ),
        (
            wrap("error", &error.replace(r#""final""#, r#""maybe""#)),
            "error.write_state",
        ),
        (
            wrap(
                "error",
                &error.replace(r#""details": {}"#, r#""details": []"#),
            ),
            "error.details",
        ),
    ];
    for (reply, field) in table {
        let err = decode_reply(reply.as_bytes()).expect_err(&reply);
        assert_eq!(err.code(), ErrorCode::Contract, "{reply}");
        let expected = if field.is_empty() {
            "E-CONTRACT: reply must be a JSON object".to_string()
        } else {
            format!("E-CONTRACT: reply field {field} is missing or has the wrong type")
        };
        assert_eq!(err.message(), expected, "{reply}");
    }
    let err = decode_reply(b"\xff").expect_err("not UTF-8");
    assert_eq!(err.message(), "E-CONTRACT: reply is not valid UTF-8");
    assert_eq!(
        decode_reply(b"{").expect_err("not JSON").code(),
        ErrorCode::Dep
    );
}

#[test]
fn a_not_started_error_keeps_its_marker_through_the_wire() {
    let sent: Decision = Err(TransitionError {
        code: ErrorCode::Dep,
        message: "E-DEP: not started: x".into(),
        details: Vec::new(),
        write_state: WriteState::NotStarted,
    });
    let read = decode_reply(encode_reply(&sent).as_bytes()).expect("a reply");
    assert_eq!(read, sent);
    let Err(err) = read else { panic!("an error") };
    assert_eq!(err.write_state, WriteState::NotStarted);
}
