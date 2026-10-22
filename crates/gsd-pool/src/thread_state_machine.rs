//! Thread state transition function.
//!
//! Legality is decided only by the transition table in `thread_state_transition_table`.

use crate::thread_state_transition_table::{is_legal_transition, ThreadPhase};
use crate::{PoolError, PoolResult};

/// Applies the transition `from -> to` and returns the new phase.
///
/// Returns `PoolError::InvalidState` for every edge absent from the table, including every
/// edge leaving a terminal phase.
pub fn transition(from: ThreadPhase, to: ThreadPhase) -> PoolResult<ThreadPhase> {
    if is_legal_transition(from, to) {
        Ok(to)
    } else {
        Err(PoolError::InvalidState(format!(
            "{} -> {} is not an allowed thread transition",
            from.as_str(),
            to.as_str()
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::thread_state_transition_table::{ALL_PHASES, LEGAL_EDGES};

    #[test]
    fn every_legal_edge_is_accepted() -> PoolResult<()> {
        for (from, to) in LEGAL_EDGES {
            assert_eq!(
                transition(from, to)?,
                to,
                "{} -> {}",
                from.as_str(),
                to.as_str()
            );
        }
        Ok(())
    }

    #[test]
    fn every_illegal_edge_is_rejected_with_invalid_state() {
        for from in ALL_PHASES {
            for to in ALL_PHASES {
                if LEGAL_EDGES.contains(&(from, to)) {
                    continue;
                }
                assert!(
                    matches!(transition(from, to), Err(PoolError::InvalidState(_))),
                    "{} -> {} must be rejected",
                    from.as_str(),
                    to.as_str()
                );
            }
        }
    }

    #[test]
    fn terminal_phases_are_sealed() {
        for from in ALL_PHASES.into_iter().filter(|p| p.is_terminal()) {
            for to in ALL_PHASES {
                assert!(
                    matches!(transition(from, to), Err(PoolError::InvalidState(_))),
                    "terminal {} must not reach {}",
                    from.as_str(),
                    to.as_str()
                );
            }
        }
    }

    #[test]
    fn failed_to_running_is_rejected_and_named_in_the_error() {
        let result = transition(ThreadPhase::Failed, ThreadPhase::Running);
        assert!(matches!(
            &result,
            Err(PoolError::InvalidState(msg)) if msg.contains("FAILED -> RUNNING")
        ));
    }

    #[test]
    fn self_transition_is_rejected() {
        for phase in ALL_PHASES {
            assert!(
                matches!(transition(phase, phase), Err(PoolError::InvalidState(_))),
                "{} -> {} must be rejected",
                phase.as_str(),
                phase.as_str()
            );
        }
    }
}
