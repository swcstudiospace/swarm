//! Scheduling-policy selection for the matrix scheduler.
//!
//! [`SchedulingPolicyTrait`] and the `CriticalPathThroughputHybrid` implementation live in
//! [`crate::policy`]. This module maps the wire-level [`SchedulingPolicy`] to an implementation and
//! fixes the default. Unimplemented wire policies are rejected, not silently replaced.

use gsd_failure::{ErrorCode, FailureError};
use gsd_proto::gsd::v1::SchedulingPolicy;

use crate::policy::{CriticalPathThroughputHybridPolicy, SchedulingPolicyTrait};
use crate::SchedulerResult;

/// Policy used when a workflow does not name one.
pub const DEFAULT_SCHEDULING_POLICY: SchedulingPolicy =
    SchedulingPolicy::CriticalPathThroughputHybrid;

/// Returns the default policy, `CriticalPathThroughputHybrid`.
pub fn default_policy() -> Box<dyn SchedulingPolicyTrait> {
    Box::new(CriticalPathThroughputHybridPolicy::new())
}

/// Returns the policy implementation for `kind`.
///
/// Returns `E-INPUT` for a policy that has no implementation yet.
pub fn policy_for(kind: SchedulingPolicy) -> SchedulerResult<Box<dyn SchedulingPolicyTrait>> {
    match kind {
        SchedulingPolicy::CriticalPathThroughputHybrid => {
            let policy: Box<dyn SchedulingPolicyTrait> =
                Box::new(CriticalPathThroughputHybridPolicy::new());
            Ok(policy)
        }
        other => Err(FailureError::new(
            ErrorCode::Input,
            format!("scheduling policy {other:?} is not implemented"),
        )
        .into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_policy_is_critical_path_throughput_hybrid() {
        assert_eq!(
            DEFAULT_SCHEDULING_POLICY,
            SchedulingPolicy::CriticalPathThroughputHybrid
        );
        assert!(policy_for(DEFAULT_SCHEDULING_POLICY).is_ok());
        let _ = default_policy();
    }

    #[test]
    fn unimplemented_policy_is_rejected_with_e_input() {
        let Err(error) = policy_for(SchedulingPolicy::MaxThroughput) else {
            panic!("MAX_THROUGHPUT has no implementation and must be rejected");
        };
        assert!(error.to_string().contains("E-INPUT"));
    }
}
