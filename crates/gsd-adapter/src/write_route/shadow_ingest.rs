//! The verified shadow of the `ingest` route on the sandbox (Phase 14.5, pin P2): [`ShadowIngest`] is the [`IngestCheck`] the sandbox
//! engine hands to [`TaskStoreWriter::ingest_with`](gsd_taskwriter::TaskStoreWriter::ingest_with).
//!
//! The writer has planned the call on the host and holds the answer. The check asks the write guest to plan the same facts
//! ([`ingest_wire::encode_request`]) and holds its reply against the host's answer encoded ([`ingest_wire::encode_outcome`]): equal bytes
//! mean the guest verified the plan. For an ingest the reconcile stage is verified on its own too: the host plans [`plan_reconcile`] on
//! the facts of the correlation and the guest must reply with that answer encoded ([`reconcile_wire`]). The two checks share one
//! [`Shadow`], so one clock and one failover record cover the call.
//!
//! The answer is always the host's. A fault is recorded with its wire code for the binary's stderr notice; a spent deadline is
//! `E-TIMEOUT`, which the writer answers with nothing written (the transaction is rolled back). The check runs inside the write
//! transaction, after planning and before the first write, so a disagreement never reaches the store.

use std::time::{Duration, Instant};

use gsd_failure::ErrorCode;
use gsd_route::ingest_plan::{IngestError, IngestFacts, IngestPlan, Mode};
use gsd_route::reconcile_plan::plan_reconcile;
use gsd_route::{ingest_wire, reconcile_wire};
use gsd_sandbox::Limits;
use gsd_taskwriter::ingest::IngestCheck;

use crate::sandbox_engine::WriteGuest;
use crate::sandbox_shadow::{Disagreement, Shadow};

/// One call's verification of the ingest plan and its reconcile stage on the sandbox guest.
pub struct ShadowIngest<'g> {
    shadow: Shadow<'g>,
}

impl<'g> ShadowIngest<'g> {
    /// Starts the verification of a call that began at `started` with `deadline`. Prepares the guest once (see [`Shadow::new`]), before
    /// the caller opens its write transaction.
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

    /// The code of the fault the call was answered on by the host, or None when the guest agreed, the call timed out, or the
    /// verification never ran.
    pub fn failover(&self) -> Option<ErrorCode> {
        self.shadow.failover()
    }

    /// The two requests of the call and their expected replies: the ingest plan, then (for an ingest, not a headless run) the reconcile
    /// stage on the facts of the correlation. The second one is asked only when the first agrees.
    fn verify(
        &self,
        facts: &IngestFacts,
        now: f64,
        host: &Result<IngestPlan, IngestError>,
    ) -> Result<(), Disagreement> {
        let request = ingest_wire::encode_request(facts, now);
        let expected = ingest_wire::encode_outcome(host);
        self.shadow
            .verify(request.as_bytes(), expected.as_bytes())?;
        if facts.mode != Mode::Ingest {
            return Ok(());
        }
        // `reconcile` runs with `approve = not --advisory`, as the planner runs it.
        let approve = !facts.advisory;
        let request = reconcile_wire::encode_request(&facts.reconcile, approve, now);
        let expected =
            reconcile_wire::encode_outcome(&plan_reconcile(&facts.reconcile, approve, now));
        self.shadow.verify(request.as_bytes(), expected.as_bytes())
    }
}

impl IngestCheck for ShadowIngest<'_> {
    fn check(
        &self,
        facts: &IngestFacts,
        now: f64,
        host: &Result<IngestPlan, IngestError>,
    ) -> Result<(), ErrorCode> {
        match self.shadow.settle(self.verify(facts, now, host).err()) {
            Ok(()) | Err(Disagreement::Fault(_)) => Ok(()),
            Err(Disagreement::Timeout) => Err(ErrorCode::Timeout),
        }
    }
}
