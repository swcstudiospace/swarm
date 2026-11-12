//! The verified shadow of a second-batch write route on the sandbox (Phase 14.5, pin P2).
//!
//! The host computes its answer first, with its own pure function on the facts it read. The write guest is then asked to compute
//! the same answer from the encoded request, and its reply bytes must equal the host's encoded answer exactly. Equal bytes: the
//! guest verified the host, nothing changes, and the host's own value is used. Any other outcome is a fault of the sandbox and is
//! recorded with its wire code ([`Shadow::failover`]), and the host's value is still the answer. The one exception is a spent
//! deadline, which the caller reports as `E-TIMEOUT` with nothing written (OD-5), as the transition decider does.
//!
//! The clock rules are the transition decider's (`sandbox_engine::SandboxDecider`): the guest gets the budget left of the call
//! deadline less a reserve for what follows; a call that ends past the deadline is `E-TIMEOUT`; an `E-TIMEOUT` from the guest while
//! budget remains is fuel, which is a fault of the guest and fails over.

use std::sync::{Mutex, PoisonError};
use std::time::{Duration, Instant};

use gsd_failure::ErrorCode;
use gsd_sandbox::Limits;

use crate::sandbox_engine::{WriteGuest, WRITE_REPLY_CAP_BYTES};

/// One call's verification: the guest, its clock and the record of the fault it failed over on.
pub struct Shadow<'g> {
    guest: &'g dyn WriteGuest,
    make_limits: fn(Duration) -> Limits,
    started: Instant,
    /// The call deadline: nothing the guest returns after it is used.
    deadline: Duration,
    /// `deadline` less the reserve, counted from `started`.
    guest_deadline: Duration,
    /// What [`WriteGuest::prepare`] answered, read once before the write transaction opens.
    prepared: Result<(), ErrorCode>,
    failover: Mutex<Option<ErrorCode>>,
}

/// Why a verification did not agree with the host.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Disagreement {
    /// The sandbox stopped the guest after the deadline was spent: the call ends `E-TIMEOUT`, nothing is written, no failover.
    Timeout,
    /// The guest did not give the host's answer: a fault recorded with this code, and the host answers.
    Fault(ErrorCode),
}

impl<'g> Shadow<'g> {
    /// Starts a verification of a call that began at `started` with `deadline`. Calls [`WriteGuest::prepare`] once, so the identity
    /// check and the compile happen before any write lock is taken.
    pub fn new(
        guest: &'g dyn WriteGuest,
        make_limits: fn(Duration) -> Limits,
        started: Instant,
        deadline: Duration,
    ) -> Self {
        let reserve = (deadline / 10).min(Duration::from_secs(2));
        Self {
            guest,
            make_limits,
            started,
            deadline,
            guest_deadline: deadline.saturating_sub(reserve),
            prepared: guest.prepare(),
            failover: Mutex::new(None),
        }
    }

    /// The code of the fault the call was answered on by the host, or None when the guest agreed, the call timed out, or the
    /// verification never ran.
    pub fn failover(&self) -> Option<ErrorCode> {
        *self.failover.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Records `disagreement` for [`Self::failover`] unless it is a timeout, and returns it.
    pub fn settle(&self, disagreement: Option<Disagreement>) -> Result<(), Disagreement> {
        match disagreement {
            None => {
                *self.failover.lock().unwrap_or_else(PoisonError::into_inner) = None;
                Ok(())
            }
            Some(Disagreement::Fault(code)) => {
                *self.failover.lock().unwrap_or_else(PoisonError::into_inner) = Some(code);
                Err(Disagreement::Fault(code))
            }
            Some(Disagreement::Timeout) => {
                *self.failover.lock().unwrap_or_else(PoisonError::into_inner) = None;
                Err(Disagreement::Timeout)
            }
        }
    }

    /// Asks the guest for `request` and holds its reply against `expected`, the host's encoded answer. `Ok` only on equal bytes.
    pub fn verify(&self, request: &[u8], expected: &[u8]) -> Result<(), Disagreement> {
        let budget = self
            .guest_deadline
            .checked_sub(self.started.elapsed())
            .filter(|left| !left.is_zero())
            .ok_or(Disagreement::Timeout)?;
        self.prepared.map_err(Disagreement::Fault)?;
        let outcome = self.guest.call(request, &(self.make_limits)(budget));
        let elapsed = self.started.elapsed();
        // The guest call is synchronous: whatever it returns after the deadline is not used.
        if elapsed >= self.deadline {
            return Err(Disagreement::Timeout);
        }
        let bytes = match outcome {
            // The epoch deadline cannot trap before the budget it was made from is spent: an earlier `E-TIMEOUT` is fuel.
            Err(ErrorCode::Timeout) if elapsed < self.guest_deadline => {
                return Err(Disagreement::Fault(ErrorCode::Internal));
            }
            Err(ErrorCode::Timeout) => return Err(Disagreement::Timeout),
            Err(code) => return Err(Disagreement::Fault(code)),
            Ok(bytes) => bytes,
        };
        if bytes.len() > WRITE_REPLY_CAP_BYTES {
            return Err(Disagreement::Fault(ErrorCode::Capacity));
        }
        if bytes == expected {
            Ok(())
        } else {
            Err(Disagreement::Fault(ErrorCode::Contract))
        }
    }
}
