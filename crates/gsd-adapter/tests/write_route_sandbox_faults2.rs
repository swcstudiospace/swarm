//! Faults of the second-batch write routes on the sandbox (Phase 14.5, pin P2): guest error, guest timeout, malformed guest output
//! and digest mismatch, per mode. Each mode's cases live in `write_route_sandbox_faults2/<mode>.rs`.
//!
//! The guest in these tests is [`InProcessGuest`]: it answers with [`gsd_route::core_invoke`], the same function the wasm guest runs,
//! so the wire and the verification are the real ones and the pin is not needed. The pinned wasm guest is exercised by the sandbox
//! tests of `gsd-sandbox` and by the parity harness on the release binary.

#![cfg(feature = "sandbox-write")]

use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use gsd_adapter::sandbox_engine::WriteGuest;
use gsd_adapter::sandbox_shadow::{Disagreement, Shadow};
use gsd_failure::ErrorCode;
use gsd_sandbox::Limits;

#[path = "write_route_sandbox_faults2/approval.rs"]
mod approval;
#[path = "write_route_sandbox_faults2/catalog.rs"]
mod catalog;
#[path = "write_route_sandbox_faults2/ingest.rs"]
mod ingest;
#[path = "write_route_sandbox_faults2/plan.rs"]
mod plan;

/// How the in-process guest answers a call.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Behaviour {
    /// The honest guest: [`gsd_route::core_invoke`] on the request.
    Honest,
    /// The guest's invoke fails with this code (an `invoke-error`, a trap, or a sandbox refusal).
    Fail(ErrorCode),
    /// The sandbox stops the guest at its deadline: `E-TIMEOUT` after the guest's budget is spent.
    Spent,
    /// The guest runs out of fuel: `E-TIMEOUT` at once, before the budget is spent.
    Fuel,
    /// The guest answers bytes that are not a reply.
    Garbage,
    /// The guest answers the honest reply with one byte changed.
    Mangled,
    /// The guest answers an honest reply larger than the reply cap.
    Oversized,
    /// The guest does not pass the identity check (its digest is not the pin): `prepare` fails with this code.
    Prepare(ErrorCode),
}

/// The write guest of these tests. Counts its calls, so a test can show the guest was or was not asked.
#[derive(Debug)]
pub struct InProcessGuest {
    pub behaviour: Behaviour,
    pub calls: AtomicUsize,
}

impl InProcessGuest {
    pub fn new(behaviour: Behaviour) -> Self {
        Self {
            behaviour,
            calls: AtomicUsize::new(0),
        }
    }

    pub fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

fn honest(request: &[u8]) -> Result<Vec<u8>, ErrorCode> {
    gsd_route::core_invoke(request).map_err(|error| error.code())
}

impl WriteGuest for InProcessGuest {
    fn prepare(&self) -> Result<(), ErrorCode> {
        match self.behaviour {
            Behaviour::Prepare(code) => Err(code),
            _ => Ok(()),
        }
    }

    fn call(&self, request: &[u8], _limits: &Limits) -> Result<Vec<u8>, ErrorCode> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        match self.behaviour {
            Behaviour::Honest | Behaviour::Prepare(_) => honest(request),
            Behaviour::Fail(code) => Err(code),
            Behaviour::Spent => {
                std::thread::sleep(Duration::from_millis(50));
                Err(ErrorCode::Timeout)
            }
            Behaviour::Fuel => Err(ErrorCode::Timeout),
            Behaviour::Garbage => Ok(b"\x00 not a reply".to_vec()),
            Behaviour::Mangled => {
                let mut bytes = honest(request)?;
                if let Some(last) = bytes.last_mut() {
                    *last = if *last == b'}' { b']' } else { b'}' };
                }
                Ok(bytes)
            }
            Behaviour::Oversized => Ok(vec![
                b' ';
                gsd_adapter::sandbox_engine::WRITE_REPLY_CAP_BYTES + 1
            ]),
        }
    }
}

/// The limits the tests give the guest. The sandbox's own limits are not exercised here.
pub fn test_limits(_budget: Duration) -> Limits {
    Limits {
        fuel: 1_000_000_000,
        memory_bytes: 16 * 1024 * 1024,
        deadline_ticks: None,
    }
}

/// A verification of one call against `guest` with a call deadline of `deadline`.
pub fn shadow(guest: &dyn WriteGuest, deadline: Duration) -> Shadow<'_> {
    Shadow::new(guest, test_limits, std::time::Instant::now(), deadline)
}

/// Runs `verify` of one request against `expected` on `guest` and returns the disagreement, if any, as the shadow settles it.
pub fn verify_once(
    guest: &dyn WriteGuest,
    request: &[u8],
    expected: &[u8],
) -> Result<(), Disagreement> {
    let shadow = shadow(guest, Duration::from_secs(30));
    let outcome = shadow.verify(request, expected);
    shadow.settle(outcome.err())
}
