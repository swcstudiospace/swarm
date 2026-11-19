//! Allowed-transition table for chat-thread lifecycle phases.
//! This table is the single source of truth for legal thread transitions. Nothing else
//! in the crate may encode transition legality.

use crate::{PoolError, PoolResult};

/// Thread metadata key that carries the reason a thread is `BLOCKED`.
pub const BLOCK_REASON_KEY: &str = "block_reason";

/// Spec-level lifecycle phase of a chat thread.
///
/// Finer-grained than the proto `ThreadStateKind`: `QUEUED`, `BLOCKED` and
/// `CANCELLED_TIMED_OUT` are refinements held in metadata per the plan.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ThreadPhase {
    /// Parked, not yet admitted to the scheduler.
    Idle,
    /// Admitted and awaiting a dispatcher slot.
    Queued,
    /// Executing.
    Running,
    /// Waiting on a dependency, HITL, budget or capability; see [`BLOCK_REASON_KEY`].
    Blocked,
    /// Terminal: completed successfully.
    Succeeded,
    /// Terminal: failed.
    Failed,
    /// Terminal: cancelled or timed out.
    CancelledTimedOut,
}

/// Every phase, in declaration order.
pub const ALL_PHASES: [ThreadPhase; 7] = [
    ThreadPhase::Idle,
    ThreadPhase::Queued,
    ThreadPhase::Running,
    ThreadPhase::Blocked,
    ThreadPhase::Succeeded,
    ThreadPhase::Failed,
    ThreadPhase::CancelledTimedOut,
];

/// The complete set of legal transitions, written out independently of [`allowed_successors`].
#[cfg(test)]
pub(crate) const LEGAL_EDGES: [(ThreadPhase, ThreadPhase); 7] = [
    (ThreadPhase::Idle, ThreadPhase::Queued),
    (ThreadPhase::Queued, ThreadPhase::Running),
    (ThreadPhase::Running, ThreadPhase::Blocked),
    (ThreadPhase::Running, ThreadPhase::Succeeded),
    (ThreadPhase::Running, ThreadPhase::Failed),
    (ThreadPhase::Running, ThreadPhase::CancelledTimedOut),
    (ThreadPhase::Blocked, ThreadPhase::Running),
];

impl ThreadPhase {
    /// Stable upper-case name used in logs and error messages.
    pub const fn as_str(self) -> &'static str {
        match self {
            ThreadPhase::Idle => "IDLE",
            ThreadPhase::Queued => "QUEUED",
            ThreadPhase::Running => "RUNNING",
            ThreadPhase::Blocked => "BLOCKED",
            ThreadPhase::Succeeded => "SUCCEEDED",
            ThreadPhase::Failed => "FAILED",
            ThreadPhase::CancelledTimedOut => "CANCELLED_TIMED_OUT",
        }
    }

    /// Returns `true` for phases with no outgoing transition.
    pub const fn is_terminal(self) -> bool {
        matches!(
            self,
            ThreadPhase::Succeeded | ThreadPhase::Failed | ThreadPhase::CancelledTimedOut
        )
    }
}

/// Phases reachable in exactly one step from `from`.
pub fn allowed_successors(from: ThreadPhase) -> &'static [ThreadPhase] {
    match from {
        ThreadPhase::Idle => &[ThreadPhase::Queued],
        ThreadPhase::Queued => &[ThreadPhase::Running],
        ThreadPhase::Running => &[
            ThreadPhase::Blocked,
            ThreadPhase::Succeeded,
            ThreadPhase::Failed,
            ThreadPhase::CancelledTimedOut,
        ],
        ThreadPhase::Blocked => &[ThreadPhase::Running],
        ThreadPhase::Succeeded | ThreadPhase::Failed | ThreadPhase::CancelledTimedOut => &[],
    }
}

/// Returns `true` when the table allows the edge `from -> to`.
pub fn is_legal_transition(from: ThreadPhase, to: ThreadPhase) -> bool {
    allowed_successors(from).contains(&to)
}

/// The retry reset: the one edge out of [`ThreadPhase::CancelledTimedOut`], back to [`ThreadPhase::Idle`].
///
/// It is kept out of [`allowed_successors`], so [`crate::transition`] still refuses every edge out of a
/// terminal phase. Only the join retry path in `gsd-state` applies it, through [`retry_reset`].
pub const RETRY_RESET: (ThreadPhase, ThreadPhase) =
    (ThreadPhase::CancelledTimedOut, ThreadPhase::Idle);

/// Applies the retry reset to `from` and returns the phase it reaches, `IDLE`.
/// Every other phase is refused with `PoolError::InvalidState`.
pub fn retry_reset(from: ThreadPhase) -> PoolResult<ThreadPhase> {
    let (edge_from, to) = RETRY_RESET;
    if from == edge_from {
        Ok(to)
    } else {
        Err(PoolError::InvalidState(format!(
            "{} has no retry reset to IDLE",
            from.as_str()
        )))
    }
}

/// The failed-thread retry reset: the edge out of [`ThreadPhase::Failed`] back to [`ThreadPhase::Idle`].
///
/// The join path applies it only to a thread that ended by a join timeout (`terminal_reason` `timed_out`). It is
/// kept out of [`allowed_successors`] for the same reason as [`RETRY_RESET`].
pub const RETRY_RESET_FAILED: (ThreadPhase, ThreadPhase) = (ThreadPhase::Failed, ThreadPhase::Idle);

/// Applies the failed-thread retry reset to `from` and returns `IDLE`.
/// Every other phase is refused with `PoolError::InvalidState`.
pub fn retry_reset_failed(from: ThreadPhase) -> PoolResult<ThreadPhase> {
    let (edge_from, to) = RETRY_RESET_FAILED;
    if from == edge_from {
        Ok(to)
    } else {
        Err(PoolError::InvalidState(format!(
            "{} has no failed retry reset to IDLE",
            from.as_str()
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_matches_explicit_legal_edges_exactly() {
        for from in ALL_PHASES {
            for to in ALL_PHASES {
                assert_eq!(
                    is_legal_transition(from, to),
                    LEGAL_EDGES.contains(&(from, to)),
                    "{} -> {}",
                    from.as_str(),
                    to.as_str()
                );
            }
        }
    }

    #[test]
    fn exactly_seven_edges_are_legal() {
        let mut count = 0;
        for from in ALL_PHASES {
            for to in ALL_PHASES {
                if is_legal_transition(from, to) {
                    count += 1;
                }
            }
        }
        assert_eq!(count, 7);
    }

    #[test]
    fn running_successors_are_blocked_and_three_terminals() {
        assert_eq!(
            allowed_successors(ThreadPhase::Running),
            [
                ThreadPhase::Blocked,
                ThreadPhase::Succeeded,
                ThreadPhase::Failed,
                ThreadPhase::CancelledTimedOut,
            ]
            .as_slice()
        );
    }

    #[test]
    fn terminal_phases_have_no_successors() {
        for phase in ALL_PHASES.into_iter().filter(|p| p.is_terminal()) {
            assert!(allowed_successors(phase).is_empty(), "{}", phase.as_str());
        }
    }

    #[test]
    fn exactly_three_phases_are_terminal() {
        let terminal: Vec<&str> = ALL_PHASES
            .iter()
            .filter(|p| p.is_terminal())
            .map(|p| p.as_str())
            .collect();
        assert_eq!(terminal, ["SUCCEEDED", "FAILED", "CANCELLED_TIMED_OUT"]);
    }

    #[test]
    fn block_reason_metadata_key_is_stable() {
        assert_eq!(BLOCK_REASON_KEY, "block_reason");
    }

    #[test]
    fn retry_reset_only_returns_cancelled_timed_out_to_idle() {
        for from in ALL_PHASES {
            let result = retry_reset(from);
            if from == ThreadPhase::CancelledTimedOut {
                assert_eq!(result.ok(), Some(ThreadPhase::Idle));
            } else {
                assert!(
                    matches!(result, Err(PoolError::InvalidState(_))),
                    "{} must have no retry reset",
                    from.as_str()
                );
            }
        }
    }

    #[test]
    fn retry_reset_stays_out_of_the_general_table() {
        assert!(!is_legal_transition(
            ThreadPhase::CancelledTimedOut,
            ThreadPhase::Idle
        ));
        assert!(allowed_successors(ThreadPhase::CancelledTimedOut).is_empty());
    }

    #[test]
    fn failed_retry_reset_is_the_only_edge_from_failed_to_idle() {
        for from in ALL_PHASES {
            let result = retry_reset_failed(from);
            if from == ThreadPhase::Failed {
                assert_eq!(result.ok(), Some(ThreadPhase::Idle));
            } else {
                assert!(
                    matches!(result, Err(PoolError::InvalidState(_))),
                    "{} must have no failed retry reset",
                    from.as_str()
                );
            }
        }
    }

    #[test]
    fn failed_retry_reset_stays_out_of_the_general_table() {
        assert!(!is_legal_transition(ThreadPhase::Failed, ThreadPhase::Idle));
        assert!(allowed_successors(ThreadPhase::Failed).is_empty());
    }
}
