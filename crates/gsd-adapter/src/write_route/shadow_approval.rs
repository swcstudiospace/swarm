//! The sandbox verifier of the approval facts of an APPROVED transition (Phase 14.5, pin P2).
//!
//! [`SandboxApproval`] is the [`ApprovalVerifier`] of the sandboxed transition route. The writer calls it once per APPROVED call inside
//! its write transaction, after the verdict rows were read and before the decider runs, with the facts it read and the host's answer to
//! them. The facts are encoded with `approval_wire::encode_request`; the guest's reply must equal `encode_outcome` of the host's answer,
//! byte for byte, through [`Shadow`]. The host's answer always stands, except for a spent deadline: that aborts the call with `E-TIMEOUT`
//! before any write, as the decider's does (OD-5). A fault of the guest is recorded for the binary's stderr notice
//! ([`SandboxApproval::failover`]); the verification never writes and never keys: the request carries booleans and the host's answers.

use std::fmt;
use std::time::{Duration, Instant};

use gsd_failure::ErrorCode;
use gsd_route::approval_wire::{encode_outcome, encode_request, Answer, Inputs};
use gsd_sandbox::Limits;
use gsd_taskwriter::ApprovalVerifier;

use crate::sandbox_engine::WriteGuest;
use crate::sandbox_shadow::{Disagreement, Shadow};

/// The approval verifier of one sandboxed call: a [`Shadow`] over the guest, on the call's clock.
pub struct SandboxApproval<'g> {
    shadow: Shadow<'g>,
}

impl<'g> SandboxApproval<'g> {
    /// A verifier for a call that began at `started` and must end within `deadline`, asking `guest` under the limits `make_limits` gives
    /// from the budget left. Prepares the guest now (see [`Shadow::new`]): call it before the write transaction opens.
    pub fn new(
        guest: &'g dyn WriteGuest,
        make_limits: fn(Duration) -> Limits,
        started: Instant,
        deadline: Duration,
    ) -> Self {
        Self {
            shadow: Shadow::new(guest, make_limits, started, deadline),
        }
    }

    /// The code of the guest fault this call failed over on, or None when the guest agreed, timed out, or never ran.
    pub fn failover(&self) -> Option<ErrorCode> {
        self.shadow.failover()
    }
}

impl fmt::Debug for SandboxApproval<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SandboxApproval").finish_non_exhaustive()
    }
}

impl ApprovalVerifier for SandboxApproval<'_> {
    /// Asks the guest for `inputs` and holds its reply against the host's `answer`. `Err(E-TIMEOUT)` only for a spent deadline; every
    /// other outcome is `Ok` and the host's answer stands.
    fn verify(&self, inputs: &Inputs, answer: &Answer) -> Result<(), ErrorCode> {
        let request = encode_request(inputs);
        let expected = encode_outcome(answer);
        let outcome = self
            .shadow
            .verify(request.as_bytes(), expected.as_bytes())
            .err();
        match self.shadow.settle(outcome) {
            Err(Disagreement::Timeout) => Err(ErrorCode::Timeout),
            // `settle` has recorded the fault; the host's answer is the one the writer uses.
            Ok(()) | Err(Disagreement::Fault(_)) => Ok(()),
        }
    }
}

/// The guest with its `prepare` answered by the decider's, which already prepared it: a call prepares the guest once, and a failed prepare
/// is never followed by a call. `call` is the guest's own.
pub(crate) struct PreparedGuest<'g> {
    guest: &'g dyn WriteGuest,
    prepared: Result<(), ErrorCode>,
}

impl<'g> PreparedGuest<'g> {
    /// `guest`, prepared with the outcome `prepared`.
    pub(crate) fn new(guest: &'g dyn WriteGuest, prepared: Result<(), ErrorCode>) -> Self {
        Self { guest, prepared }
    }
}

impl WriteGuest for PreparedGuest<'_> {
    fn prepare(&self) -> Result<(), ErrorCode> {
        self.prepared
    }

    fn call(&self, request: &[u8], limits: &Limits) -> Result<Vec<u8>, ErrorCode> {
        self.guest.call(request, limits)
    }
}
