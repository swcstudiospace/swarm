//! The sandbox verifier of the `orch_plan` route (Phase 14.5, pin P2): the verified shadow of the host derivation.
//!
//! [`PlanShadow`] is the [`PlanCheck`] of the sandbox path. For each derivation the host made, it encodes the request with
//! [`encode_request`], takes the host's answer as [`encode_outcome`] renders it, and holds the guest's reply against those bytes. Equal
//! bytes: the guest verified the host and nothing changes. Any other outcome is a fault of the sandbox: it is recorded with its wire code
//! for the binary's stderr notice, and the host's answer is still the answer. The one exception is a spent deadline, which ends the call
//! as `E-TIMEOUT` with nothing written (OD-5), as the transition decider does. The clock rules are those of [`Shadow`].

use std::time::{Duration, Instant};

use gsd_failure::ErrorCode;
use gsd_route::jstr;
use gsd_route::plan_derive::{PlanDerived, PlanError, PlanFacts, PlanRequest};
use gsd_route::plan_wire::{encode_outcome, encode_request};
use gsd_route::SeamError;
use gsd_sandbox::Limits;

use crate::sandbox_engine::WriteGuest;
use crate::sandbox_shadow::{Disagreement, Shadow};
use crate::write_route::plan::PlanCheck;

/// The verified shadow of one `orch_plan` call on the sandbox.
pub(crate) struct PlanShadow<'g> {
    shadow: Shadow<'g>,
    deadline: Duration,
}

impl<'g> PlanShadow<'g> {
    /// A verification of a call that began at `started` and must end within `deadline`, against `guest`. Prepares the guest now, so
    /// the identity check and the compile happen before the call takes the write lock.
    pub(crate) fn new(
        guest: &'g dyn WriteGuest,
        make_limits: fn(Duration) -> Limits,
        started: Instant,
        deadline: Duration,
    ) -> Self {
        Self {
            shadow: Shadow::new(guest, make_limits, started, deadline),
            deadline,
        }
    }

    /// The code of the fault the call was answered on by the host, or None when the guest agreed, the call timed out, or nothing was
    /// checked.
    pub(crate) fn failover(&self) -> Option<ErrorCode> {
        self.shadow.failover()
    }
}

impl PlanCheck for PlanShadow<'_> {
    fn check(
        &self,
        facts: &PlanFacts,
        request: &PlanRequest,
        now: f64,
        host: &Result<PlanDerived, PlanError>,
    ) -> Result<(), SeamError> {
        let wire = encode_request(facts, request, now);
        let expected = encode_outcome(host);
        let outcome = self.shadow.verify(wire.as_bytes(), expected.as_bytes());
        match self.shadow.settle(outcome.err()) {
            // A fault is recorded by `settle` for the notice; the host's answer stands.
            Ok(()) | Err(Disagreement::Fault(_)) => Ok(()),
            Err(Disagreement::Timeout) => Err(timeout(self.deadline)),
        }
    }
}

/// The reply of a spent deadline: `E-TIMEOUT`, final, nothing written.
fn timeout(deadline: Duration) -> SeamError {
    SeamError::new(
        ErrorCode::Timeout,
        format!("guest call exceeded its {deadline:?} deadline"),
    )
    .with_details(vec![("write_state".to_string(), jstr("final"))])
}
