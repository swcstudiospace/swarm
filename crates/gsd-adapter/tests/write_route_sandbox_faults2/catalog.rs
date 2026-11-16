//! Fault tests of the `catalog` mode on the sandbox (Phase 14.5, pin P2). The mode has no host route yet (`swarm_tool` answers `no_route`),
//! so these tests exercise the verification a route would run: the host's answer from [`gsd_route::catalog_wire::answer`], the request
//! and expected bytes, and [`InProcessGuest`], whose honest reply is [`gsd_route::core_invoke`] on the request.

use std::sync::atomic::Ordering;
use std::time::Duration;

use gsd_adapter::sandbox_engine::WRITE_REPLY_CAP_BYTES;
use gsd_adapter::sandbox_shadow::Disagreement;
use gsd_failure::ErrorCode;
use gsd_route::catalog::{CatalogArgs, Kind, NotStarted, Registry, SkillRow, MAX_REPLY_BYTES};
use gsd_route::catalog_wire::{
    answer, decode_outcome, encode_outcome, encode_request, MAX_WIRE_REPLY_BYTES,
};
use gsd_route::{jstr, obj_get, parse_json, Json};

use super::{shadow, verify_once, Behaviour, InProcessGuest};

const VECTORS: &str = include_str!("../../../gsd-route/tests/vectors/catalog.json");

/// The real registry and skill rows of the vector file.
fn corpus() -> (Registry, Vec<SkillRow>) {
    let document = parse_json(VECTORS).expect("the vector file parses");
    let Json::Obj(top) = &document else {
        panic!("the vector file is an object")
    };
    let registry = Registry::from_json(obj_get(top, "registry").expect("registry"))
        .expect("the real slice is the documented shape");
    let skills = SkillRow::from_json(obj_get(top, "skills").expect("skills"))
        .expect("the real skill rows are the documented shape");
    (registry, skills)
}

fn args(caller: &str, kind: Kind) -> CatalogArgs {
    CatalogArgs {
        json: true,
        dry_run: false,
        caller: Some(caller.to_string()),
        kind,
        query: None,
        task_id: None,
        correlation_id: None,
    }
}

/// The request the host would send for `args` on `registry`, and the bytes the host expects back.
fn request_and_expected(
    args: &CatalogArgs,
    registry: &Registry,
    skills: &[SkillRow],
) -> (Vec<u8>, Vec<u8>) {
    (
        encode_request(args, registry, skills).into_bytes(),
        encode_outcome(&answer(args, registry, skills)).into_bytes(),
    )
}

/// One verification against `guest`, with the shadow's failover record.
fn settle_once(
    guest: &InProcessGuest,
    request: &[u8],
    expected: &[u8],
) -> (Result<(), Disagreement>, Option<ErrorCode>) {
    let check = shadow(guest, Duration::from_secs(30));
    let outcome = check.verify(request, expected);
    let settled = check.settle(outcome.err());
    (settled, check.failover())
}

#[test]
fn an_honest_guest_agrees_with_the_host_answer() {
    let (registry, skills) = corpus();
    let (request, expected) = request_and_expected(&args("A01", Kind::All), &registry, &skills);
    let guest = InProcessGuest::new(Behaviour::Honest);
    assert_eq!(verify_once(&guest, &request, &expected), Ok(()));
    assert_eq!(guest.calls(), 1);
    let (settled, failover) = settle_once(&guest, &request, &expected);
    assert_eq!(settled, Ok(()));
    assert_eq!(failover, None);
}

#[test]
fn a_guest_error_is_a_fault_with_its_code_and_the_host_answers() {
    let (registry, skills) = corpus();
    let (request, expected) = request_and_expected(&args("A01", Kind::Tools), &registry, &skills);
    for code in [ErrorCode::Dep, ErrorCode::Internal, ErrorCode::Capacity] {
        let guest = InProcessGuest::new(Behaviour::Fail(code));
        let (settled, failover) = settle_once(&guest, &request, &expected);
        assert_eq!(settled, Err(Disagreement::Fault(code)), "{code:?}");
        assert_eq!(failover, Some(code), "{code:?}");
    }
}

#[test]
fn a_spent_deadline_is_a_timeout_with_no_failover() {
    let (registry, skills) = corpus();
    let (request, expected) = request_and_expected(&args("A01", Kind::Tools), &registry, &skills);
    let guest = InProcessGuest::new(Behaviour::Spent);
    // The guest sleeps 50 ms: a call deadline of 20 ms is spent by then.
    let check = shadow(&guest, Duration::from_millis(20));
    let outcome = check.verify(&request, &expected);
    assert_eq!(check.settle(outcome.err()), Err(Disagreement::Timeout));
    assert_eq!(check.failover(), None);
    assert_eq!(guest.calls(), 1);
}

#[test]
fn fuel_before_the_budget_is_spent_is_internal() {
    let (registry, skills) = corpus();
    let (request, expected) = request_and_expected(&args("A01", Kind::Tools), &registry, &skills);
    let guest = InProcessGuest::new(Behaviour::Fuel);
    let (settled, failover) = settle_once(&guest, &request, &expected);
    assert_eq!(settled, Err(Disagreement::Fault(ErrorCode::Internal)));
    assert_eq!(failover, Some(ErrorCode::Internal));
}

#[test]
fn bytes_that_are_not_a_reply_are_a_contract_fault() {
    let (registry, skills) = corpus();
    let (request, expected) = request_and_expected(&args("A01", Kind::Tools), &registry, &skills);
    for behaviour in [Behaviour::Garbage, Behaviour::Mangled] {
        let guest = InProcessGuest::new(behaviour);
        assert_eq!(
            verify_once(&guest, &request, &expected),
            Err(Disagreement::Fault(ErrorCode::Contract)),
            "{behaviour:?}"
        );
    }
}

#[test]
fn an_oversized_reply_is_a_capacity_fault() {
    let (registry, skills) = corpus();
    let (request, expected) = request_and_expected(&args("A01", Kind::Tools), &registry, &skills);
    let guest = InProcessGuest::new(Behaviour::Oversized);
    assert_eq!(
        verify_once(&guest, &request, &expected),
        Err(Disagreement::Fault(ErrorCode::Capacity))
    );
}

#[test]
fn a_guest_that_fails_the_identity_check_is_never_asked() {
    let (registry, skills) = corpus();
    let (request, expected) = request_and_expected(&args("A01", Kind::Tools), &registry, &skills);
    let guest = InProcessGuest::new(Behaviour::Prepare(ErrorCode::Dep));
    assert_eq!(
        verify_once(&guest, &request, &expected),
        Err(Disagreement::Fault(ErrorCode::Dep))
    );
    assert_eq!(guest.calls.load(Ordering::SeqCst), 0);
}

#[test]
fn facts_the_guest_does_not_get_from_the_request_are_a_contract_fault() {
    // The host decided on A01 but encoded A05's request: the guest answers what the request says, which is not the host's answer.
    let (registry, skills) = corpus();
    let (request, _) = request_and_expected(&args("A05", Kind::All), &registry, &skills);
    let (_, expected) = request_and_expected(&args("A01", Kind::All), &registry, &skills);
    let guest = InProcessGuest::new(Behaviour::Honest);
    assert_eq!(
        verify_once(&guest, &request, &expected),
        Err(Disagreement::Fault(ErrorCode::Contract))
    );
    assert_eq!(guest.calls(), 1);
}

#[test]
fn the_wire_reply_cap_is_the_write_reply_cap() {
    assert_eq!(MAX_WIRE_REPLY_BYTES, WRITE_REPLY_CAP_BYTES);
}

#[test]
fn a_reply_above_the_stdout_cap_is_the_same_refusal_on_both_sides() {
    // The catalog declines a stdout longer than MAX_REPLY_BYTES. The host answer is that decline, the guest gives the same bytes, and
    // the shadow holds them equal: no capacity fault for a decline that is small on the wire.
    let (registry, skills) = corpus();
    let mut tool = registry.tools[0].clone();
    tool.purpose = jstr(&"x".repeat(MAX_REPLY_BYTES));
    tool.permitted_callers = Json::Arr(vec![jstr("A01")]);
    let registry = Registry {
        agents: registry.agents.clone(),
        tools: vec![tool],
    };
    let call = args("A01", Kind::Tools);
    let host = answer(&call, &registry, &skills);
    let why = host.clone().expect_err("the stdout is over the cap").0;
    assert!(why.starts_with("reply-too-large:"), "{why}");
    let (request, expected) = request_and_expected(&call, &registry, &skills);
    let guest = InProcessGuest::new(Behaviour::Honest);
    assert_eq!(verify_once(&guest, &request, &expected), Ok(()));
    let reply = decode_outcome(&expected).expect("the expected bytes read");
    assert_eq!(reply, Err(NotStarted(why)));
}

#[test]
fn a_reply_under_the_cap_is_answered_and_fits_the_guest_cap() {
    // A purpose that leaves the reply just under the stdout cap, and its wire form under the reply cap: the guest answers, no fault.
    let (registry, skills) = corpus();
    let mut tool = registry.tools[0].clone();
    tool.purpose = jstr(&"x".repeat(MAX_REPLY_BYTES - 4096));
    tool.permitted_callers = Json::Arr(vec![jstr("A01")]);
    let registry = Registry {
        agents: registry.agents.clone(),
        tools: vec![tool],
    };
    let call = args("A01", Kind::Tools);
    let (request, expected) = request_and_expected(&call, &registry, &skills);
    assert!(
        expected.len() <= WRITE_REPLY_CAP_BYTES,
        "{}",
        expected.len()
    );
    let decoded = decode_outcome(&expected).expect("the expected bytes read");
    assert_eq!(decoded.expect("answered").exit_code, 0);
    let guest = InProcessGuest::new(Behaviour::Honest);
    assert_eq!(verify_once(&guest, &request, &expected), Ok(()));
}
