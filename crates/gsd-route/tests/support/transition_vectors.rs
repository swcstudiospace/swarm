//! Loads `tests/vectors/transition.json` as the inputs of `decide_transition`, for every test that replays the whole vector set.
//!
//! Shared by path: `gsd-route`'s tests declare `mod support;`, and the sandbox crate includes this file with `#[path]`. Only the
//! inputs are read; each test supplies its own expectation (the decision itself, or the guest's answer).

use gsd_route::transition_plan::{ApprovalFacts, Facts, Request};
use gsd_route::{obj_get, parse_json, Fields, Json};

const VECTORS: &str = include_str!("../vectors/transition.json");

/// The number of cases the committed vector file holds; a truncated file fails the tests that use it.
pub const CASES: usize = 351;

/// One vector case: its id, the clock reading and the inputs of the decision.
pub struct Case {
    pub id: String,
    pub now: f64,
    pub facts: Facts,
    pub request: Request,
}

fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
    match value {
        Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
        other => panic!("not an object looking for {key}: {other:?}"),
    }
}

fn text(value: &Json) -> String {
    match value {
        Json::Str(text) => text.clone(),
        other => panic!("not text: {other:?}"),
    }
}

fn flag(value: &Json) -> bool {
    match value {
        Json::Bool(flag) => *flag,
        other => panic!("not a bool: {other:?}"),
    }
}

fn names(value: &Json) -> Vec<String> {
    match value {
        Json::Arr(items) => items.iter().map(text).collect(),
        other => panic!("not a list: {other:?}"),
    }
}

fn row(value: &Json) -> Fields {
    match value {
        Json::Obj(fields) => fields.clone(),
        other => panic!("not an object: {other:?}"),
    }
}

fn approval(value: &Json) -> Option<ApprovalFacts> {
    match value {
        Json::Null => None,
        facts => Some(ApprovalFacts {
            require_key: flag(member(facts, "require_key")),
            real_key: flag(member(facts, "real_key")),
            dev_key: flag(member(facts, "dev_key")),
            missing_gates: names(member(facts, "missing_gates")),
            unaccepted_gates: names(member(facts, "unaccepted_gates")),
        }),
    }
}

/// Every case of the vector file, in file order.
pub fn cases() -> Vec<Case> {
    let doc = parse_json(VECTORS).expect("the vector file parses");
    let Json::Arr(cases) = member(&doc, "cases") else {
        panic!("cases is a list");
    };
    cases
        .iter()
        .map(|case| {
            let request = member(case, "request");
            Case {
                id: text(member(case, "id")),
                now: match member(case, "now") {
                    Json::Float(now) => *now,
                    other => panic!("now is not a float: {other:?}"),
                },
                facts: Facts {
                    task: match member(case, "task") {
                        Json::Null => None,
                        task => Some(row(task)),
                    },
                    approval: approval(member(case, "approval")),
                },
                request: Request {
                    task_id: text(member(request, "task_id")),
                    to: text(member(request, "to")),
                    actor: text(member(request, "actor")),
                    reason: text(member(request, "reason")),
                    manual: flag(member(request, "manual")),
                },
            }
        })
        .collect()
}
