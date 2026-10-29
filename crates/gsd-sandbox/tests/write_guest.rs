//! The guest's `transition` mode through the sandbox, against the host decision function.
//!
//! The default guest (feature off) must refuse a transition request with the read route's own error; the `write-routes` guest must
//! answer every vector case of `decide_transition` with the plan or refusal the host function gives, under the production limits
//! (fuel 1e9, 16 MiB, an epoch deadline) and with the empty linker the loader gives every guest.

#[cfg(feature = "write-routes")]
#[path = "../../gsd-route/tests/support/transition_vectors.rs"]
mod transition_vectors;

use gsd_failure::ErrorCode;
use gsd_sandbox::wasmtime_limits::deadline_ticks_for;
use gsd_sandbox::wasmtime_module_build::{describe, MODULE_NAME};
use gsd_sandbox::{GuestInstance, GuestInvokeError, GuestLoader, Limits, LoadedGuest};
use std::time::Duration;

const GUEST: &[u8] = include_bytes!(env!("GSD_GUEST_WASM"));
const SEMVER: &str = "0.1.0";

/// A transition request for an unknown task: the smallest request the decision answers.
const UNKNOWN_TASK: &str = r#"{"mode": "transition", "now": 1.5, "task": null, "request": {"task_id": "T-1", "to": "VALIDATED", "actor": "A01", "reason": "r", "manual": true}, "facts": {"approval": null}}"#;

/// The production limits of the sandbox engine (OD-6), with the deadline of a 30 s call.
fn production_limits() -> Limits {
    Limits {
        fuel: 1_000_000_000,
        memory_bytes: 16 * 1024 * 1024,
        deadline_ticks: Some(deadline_ticks_for(Duration::from_secs(30))),
    }
}

fn load(loader: &GuestLoader) -> LoadedGuest {
    let manifest = describe(GUEST, MODULE_NAME, SEMVER);
    loader
        .load_verified(GUEST, &manifest, SEMVER)
        .expect("the built guest verifies and compiles")
}

fn instance(loader: &GuestLoader, guest: &LoadedGuest, limits: &Limits) -> GuestInstance {
    loader
        .instantiate(guest, limits)
        .expect("the guest instantiates with no imports")
}

#[cfg(not(feature = "write-routes"))]
mod read_only_guest {
    use super::*;

    #[test]
    fn a_transition_request_is_refused_with_the_read_route_error() {
        let loader = GuestLoader::new().expect("loader builds");
        let guest = load(&loader);
        // `UNKNOWN_TASK` has no read-route headers, so the read route refuses its first missing field; with the headers it refuses
        // the mode itself.
        let with_headers = UNKNOWN_TASK.replacen(
            r#""mode": "transition","#,
            r#""mode": "transition", "agent": "A01", "script": "orch_status", "correlation": null, "db_path": "/x", "swarm_dir": "/x", "events": 0, "verdicts": [],"#,
            1,
        );
        for (request, refusal) in [
            (
                UNKNOWN_TASK,
                "request field agent is missing or has the wrong type",
            ),
            (
                with_headers.as_str(),
                r#"request field mode must be "list" or "history""#,
            ),
        ] {
            let native = gsd_route::orch_status_invoke(request.as_bytes())
                .expect_err("the read route refuses a transition request");
            assert!(native.message().ends_with(refusal), "{}", native.message());
            let reply = instance(&loader, &guest, &production_limits()).invoke(request.as_bytes());
            match reply {
                Err(GuestInvokeError::Guest { code, message }) => {
                    assert_eq!(code, ErrorCode::Input);
                    assert_eq!(message, native.message());
                }
                other => panic!("the default guest must refuse the mode, got {other:?}"),
            }
        }
    }
}

#[cfg(feature = "write-routes")]
mod write_guest {
    use super::*;
    use gsd_route::transition_plan::decide_transition;
    use gsd_route::transition_wire::{decode_reply, encode_reply, encode_request};
    use std::time::Instant;
    use transition_vectors::{cases, Case, CASES};

    fn guest_decision(
        loader: &GuestLoader,
        guest: &LoadedGuest,
        case: &Case,
    ) -> Result<Vec<u8>, GuestInvokeError> {
        let request = encode_request(&case.facts, &case.request, case.now);
        instance(loader, guest, &production_limits()).invoke(request.as_bytes())
    }

    #[test]
    fn the_guest_decides_every_vector_case_like_the_host_function() {
        let loader = GuestLoader::new().expect("loader builds");
        let guest = load(&loader);
        let all = cases();
        assert_eq!(all.len(), CASES);
        for case in &all {
            let reply = guest_decision(&loader, &guest, case)
                .unwrap_or_else(|err| panic!("{}: the guest failed: {err:?}", case.id));
            let from_guest = decode_reply(&reply)
                .unwrap_or_else(|err| panic!("{}: the reply does not decode: {err:?}", case.id));
            let from_host = decide_transition(&case.facts, &case.request, case.now);
            assert_eq!(from_guest, from_host, "{}", case.id);
            assert_eq!(
                String::from_utf8(reply).expect("UTF-8"),
                encode_reply(&from_host),
                "{}: bytes",
                case.id
            );
        }
    }

    #[test]
    fn the_guest_answers_a_refusal_of_the_decision_as_a_reply_not_an_invoke_error() {
        let loader = GuestLoader::new().expect("loader builds");
        let guest = load(&loader);
        let reply = instance(&loader, &guest, &production_limits())
            .invoke(UNKNOWN_TASK.as_bytes())
            .expect("a refusal of the decision is a reply");
        let Err(error) = decode_reply(&reply).expect("a reply") else {
            panic!("an unknown task is refused");
        };
        assert_eq!(error.message, "E-INPUT: unknown task T-1");
    }

    #[test]
    fn a_transition_request_the_guest_cannot_read_is_an_input_invoke_error() {
        let loader = GuestLoader::new().expect("loader builds");
        let guest = load(&loader);
        let unreadable = UNKNOWN_TASK.replacen(r#""manual": true"#, r#""manual": 1"#, 1);
        let native = gsd_route::core_invoke(unreadable.as_bytes()).expect_err("native refuses");
        match instance(&loader, &guest, &production_limits()).invoke(unreadable.as_bytes()) {
            Err(GuestInvokeError::Guest { code, message }) => {
                assert_eq!(code, ErrorCode::Input);
                assert_eq!(message, native.message());
                assert_eq!(
                    message,
                    "E-INPUT: field request.request.manual is missing or has the wrong type"
                );
            }
            other => panic!("an unreadable request must be an invoke error, got {other:?}"),
        }
    }

    /// The fewest fuel units that complete the call, by bisection (fuel use of a fixed request is deterministic).
    fn fuel_of(loader: &GuestLoader, guest: &LoadedGuest, request: &[u8]) -> u64 {
        let completes = |fuel: u64| {
            // No deadline: with one, running out of fuel is classified as a timeout, not as fuel exhaustion.
            let limits = Limits {
                fuel,
                deadline_ticks: None,
                ..production_limits()
            };
            match instance(loader, guest, &limits).invoke(request) {
                Ok(_) => true,
                Err(GuestInvokeError::Trap(ErrorCode::Internal)) => false,
                Err(other) => panic!("not fuel exhaustion: {other:?}"),
            }
        };
        let (mut lacks, mut fits) = (0u64, production_limits().fuel);
        assert!(completes(fits), "the production fuel covers one call");
        while fits - lacks > 1 {
            let middle = lacks + (fits - lacks) / 2;
            if completes(middle) {
                fits = middle;
            } else {
                lacks = middle;
            }
        }
        fits
    }

    /// Fuel and wall time of the cases that exercise each branch of the decision; printed for the record (`--nocapture`).
    #[test]
    fn a_transition_costs_a_tiny_part_of_the_fuel_limit() {
        let loader = GuestLoader::new().expect("loader builds");
        let guest = load(&loader);
        let probes = [
            "matrix-CREATED-VALIDATED",
            "matrix-CREATED-CREATED",
            "rework-plain",
            "rework-cap-no-prior",
            "approved-ok",
            "notes-corrupt-text",
        ];
        let all = cases();
        let mut worst = 0;
        for id in probes {
            let case = all
                .iter()
                .find(|case| case.id == id)
                .expect("probe case exists");
            let request = encode_request(&case.facts, &case.request, case.now);
            let fuel = fuel_of(&loader, &guest, request.as_bytes());
            let started = Instant::now();
            instance(&loader, &guest, &production_limits())
                .invoke(request.as_bytes())
                .expect("the probe completes");
            println!(
                "fuel {fuel:>9}  {:>9?} instantiate+invoke  {id}",
                started.elapsed()
            );
            worst = worst.max(fuel);
        }
        assert!(
            worst < production_limits().fuel / 1000,
            "worst probe: {worst} fuel"
        );
    }
}
