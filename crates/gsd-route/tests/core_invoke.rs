//! `core_invoke`: the transition mode goes to the transition decision, every other request is answered by the read route unchanged.
#![cfg(feature = "write-routes")]

#[path = "support/transition_vectors.rs"]
mod transition_vectors;

use gsd_route::transition_plan::decide_transition;
use gsd_route::transition_wire::{decode_reply, encode_request, transition_invoke};
use gsd_route::{core_invoke, orch_status_invoke, SeamError};
use transition_vectors::{cases, CASES};

/// A list request: two tasks, the second with one verdict row.
const LIST: &str = r#"{"mode":"list","agent":"A01","script":"orch_status","correlation":null,"db_path":"/x/tasks.db","swarm_dir":"/x","events":3,"tasks":[{"task_id":"T-A","state":"DONE","agent_id":"A05","dag_depth":0,"attempt":0,"rework_loops":0,"risk_class":"low","depends_on":"[]","notes":"{}"},{"task_id":"T-B","state":"IN_REVIEW","agent_id":"A05","dag_depth":1,"attempt":0,"rework_loops":0,"risk_class":"low","depends_on":"[\"T-A\"]","notes":"{}"}],"verdicts":[{"task_id":"T-B","rows":[{"id":1,"task_id":"T-B","gate":"review","verdict":"pass","findings":"[]","ts":1.5,"envelope_json":null}]}],"history":null}"#;
const HISTORY_UNKNOWN: &str = r#"{"mode":"history","agent":"A01","script":"orch_status","correlation":null,"db_path":"/x/tasks.db","swarm_dir":"/x","events":0,"tasks":[],"verdicts":[],"history":{"task_id":"T-X","task":null,"transitions":[]}}"#;

fn same(left: &Result<Vec<u8>, SeamError>, right: &Result<Vec<u8>, SeamError>, what: &str) {
    match (left, right) {
        (Ok(left), Ok(right)) => assert_eq!(left, right, "{what}"),
        (Err(left), Err(right)) => assert_eq!(left, right, "{what}"),
        other => panic!("{what}: the two answers differ in kind: {other:?}"),
    }
}

#[test]
fn every_request_that_is_not_a_transition_gets_the_read_route_answer_unchanged() {
    let listed_as_transition_header = LIST.replace(r#""mode":"list""#, r#""mode":"Transition""#);
    let table: Vec<(&str, Vec<u8>)> = vec![
        ("list", LIST.as_bytes().to_vec()),
        ("history refusal", HISTORY_UNKNOWN.as_bytes().to_vec()),
        (
            "mode in another case",
            listed_as_transition_header.into_bytes(),
        ),
        ("unknown mode", br#"{"mode":"bogus"}"#.to_vec()),
        ("mode of another type", br#"{"mode":5}"#.to_vec()),
        ("no mode", b"{}".to_vec()),
        ("not an object", b"[]".to_vec()),
        ("not JSON", b"{".to_vec()),
        ("not UTF-8", b"\xff\xfe".to_vec()),
    ];
    for (what, input) in table {
        same(&core_invoke(&input), &orch_status_invoke(&input), what);
    }
}

#[test]
fn a_list_request_is_still_answered() {
    core_invoke(LIST.as_bytes()).expect("the list request is answered");
}

#[test]
fn a_transition_request_is_decided_and_not_read_as_a_list() {
    // Carries every header of a read request on purpose: only the mode may decide where it goes.
    let as_read = LIST.replace(r#""mode":"list""#, r#""mode":"transition""#);
    let read_route =
        orch_status_invoke(as_read.as_bytes()).expect_err("the read route refuses the mode");
    assert!(
        read_route
            .message()
            .ends_with(r#"must be "list" or "history""#),
        "{}",
        read_route.message()
    );
    let err = core_invoke(as_read.as_bytes())
        .expect_err("the transition reader refuses the missing fields");
    assert_eq!(
        err.message(),
        "E-INPUT: field request is missing or has the wrong type"
    );

    let all = cases();
    assert_eq!(all.len(), CASES);
    for case in all {
        let request = encode_request(&case.facts, &case.request, case.now);
        let routed = core_invoke(request.as_bytes());
        same(&routed, &transition_invoke(request.as_bytes()), &case.id);
        let decision = decide_transition(&case.facts, &case.request, case.now);
        assert_eq!(
            decode_reply(&routed.expect("answered")).expect("decoded"),
            decision,
            "{}",
            case.id
        );
    }
}
