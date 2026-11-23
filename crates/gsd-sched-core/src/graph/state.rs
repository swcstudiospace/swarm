//! The single node state machine: [`State`], [`NodeState`], the
//! legal-transition table [`LEGAL_TRANSITIONS`], and the deterministic
//! ready order ([`order_ready`]).
//!
//! There is exactly one FSM for a node's lifecycle; every other name for a
//! node state is an alias of one of these nine states. Terminal states
//! (succeeded, failed, cancelled, timed_out, skipped) have no outgoing
//! transitions. A retry is a new scheduled attempt, not an FSM transition:
//! the host re-queues the node as a fresh `NodeState` rather than moving it
//! backwards through this table.

use std::error::Error;
use std::fmt;

use super::model::{Node, NodeId};

/// A node's lifecycle state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum State {
    /// Created, waiting for its dependencies to be scheduled.
    Queued,
    /// Dependencies satisfied; eligible for dispatch.
    Ready,
    /// Currently executing.
    Running,
    /// Execution suspended; may resume to ready or running.
    Paused,
    /// Finished successfully (terminal).
    Succeeded,
    /// Finished with an error (terminal).
    Failed,
    /// Cancelled by the host (terminal).
    Cancelled,
    /// Exceeded its per-attempt timeout (terminal).
    TimedOut,
    /// Skipped by the host without running (terminal).
    Skipped,
}

impl State {
    /// Whether this state is terminal (no outgoing transitions).
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            State::Succeeded | State::Failed | State::Cancelled | State::TimedOut | State::Skipped
        )
    }

    /// Whether the transition `self -> to` is in [`LEGAL_TRANSITIONS`].
    pub fn can_transition(self, to: State) -> bool {
        can_transition(self, to)
    }

    /// The transition `self -> to`, or [`TransitionError`] when illegal.
    pub fn transition(self, to: State) -> Result<State, TransitionError> {
        if can_transition(self, to) {
            Ok(to)
        } else {
            Err(TransitionError { from: self, to })
        }
    }
}

impl fmt::Display for State {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let name = match self {
            State::Queued => "queued",
            State::Ready => "ready",
            State::Running => "running",
            State::Paused => "paused",
            State::Succeeded => "succeeded",
            State::Failed => "failed",
            State::Cancelled => "cancelled",
            State::TimedOut => "timed_out",
            State::Skipped => "skipped",
        };
        f.write_str(name)
    }
}

/// Every legal transition of the node FSM, as `(from, to)` pairs.
///
/// The table is the single source of truth: [`State::can_transition`] and
/// [`State::transition`] consult it via [`can_transition`].
pub const LEGAL_TRANSITIONS: &[(State, State)] = &[
    (State::Queued, State::Ready),
    (State::Queued, State::Cancelled),
    (State::Queued, State::Skipped),
    (State::Ready, State::Running),
    (State::Ready, State::Paused),
    (State::Ready, State::Cancelled),
    (State::Ready, State::Skipped),
    (State::Running, State::Paused),
    (State::Running, State::Succeeded),
    (State::Running, State::Failed),
    (State::Running, State::Cancelled),
    (State::Running, State::TimedOut),
    (State::Paused, State::Ready),
    (State::Paused, State::Running),
    (State::Paused, State::Cancelled),
    (State::Paused, State::Skipped),
];

/// Whether the transition `from -> to` is legal (see [`LEGAL_TRANSITIONS`]).
pub fn can_transition(from: State, to: State) -> bool {
    LEGAL_TRANSITIONS.contains(&(from, to))
}

/// A rejected FSM transition.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TransitionError {
    /// The state the node was in.
    pub from: State,
    /// The state that was requested.
    pub to: State,
}

impl fmt::Display for TransitionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "illegal transition {} -> {}", self.from, self.to)
    }
}

impl Error for TransitionError {}

/// A node's current FSM state, as tracked by the scheduler host.
///
/// The only way to move is [`NodeState::transition`], which rejects illegal
/// transitions without changing the state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NodeState {
    /// The node's current state.
    pub state: State,
}

impl NodeState {
    /// A node state starting in [`State::Queued`].
    pub fn queued() -> Self {
        Self {
            state: State::Queued,
        }
    }

    /// Move to `to` if the transition is legal; on rejection the state is
    /// unchanged and the error names both states.
    pub fn transition(&mut self, to: State) -> Result<State, TransitionError> {
        self.state = self.state.transition(to)?;
        Ok(self.state)
    }

    /// Whether the node is in a terminal state.
    pub fn is_terminal(&self) -> bool {
        self.state.is_terminal()
    }
}

/// The deterministic dispatch order of `nodes`: by ascending `priority`
/// (lower first), then by ascending [`NodeId`] (lexicographic).
///
/// Input order never affects the result: the sort key is total, so equal
/// inputs in any permutation yield the same output.
pub fn order_ready<'a>(nodes: impl IntoIterator<Item = &'a Node>) -> Vec<NodeId> {
    let mut keyed: Vec<(u8, NodeId)> = nodes
        .into_iter()
        .map(|node| (node.priority, node.id.clone()))
        .collect();
    keyed.sort();
    keyed.into_iter().map(|(_, id)| id).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn node(id: &str, priority: u8) -> Node {
        Node::new(id, "code.backend").with_priority(priority)
    }

    #[test]
    fn legal_transitions_are_accepted() {
        let mut state = NodeState::queued();
        assert_eq!(state.transition(State::Ready), Ok(State::Ready));
        assert_eq!(state.transition(State::Running), Ok(State::Running));
        assert_eq!(state.transition(State::Paused), Ok(State::Paused));
        assert_eq!(state.transition(State::Running), Ok(State::Running));
        assert_eq!(state.transition(State::Succeeded), Ok(State::Succeeded));
        assert!(state.is_terminal());
    }

    #[test]
    fn illegal_transitions_are_rejected_and_keep_state() {
        let mut state = NodeState::queued();
        // queued cannot jump straight to a result.
        assert_eq!(
            state.transition(State::Running),
            Err(TransitionError {
                from: State::Queued,
                to: State::Running
            })
        );
        assert_eq!(
            state.transition(State::Succeeded),
            Err(TransitionError {
                from: State::Queued,
                to: State::Succeeded
            })
        );
        assert_eq!(state.state, State::Queued);

        state.transition(State::Ready).unwrap();
        assert!(state.transition(State::Succeeded).is_err());
        assert_eq!(state.state, State::Ready);
    }

    #[test]
    fn terminal_states_have_no_outgoing_transitions() {
        for terminal in [
            State::Succeeded,
            State::Failed,
            State::Cancelled,
            State::TimedOut,
            State::Skipped,
        ] {
            assert!(terminal.is_terminal());
            assert_eq!(
                terminal.transition(State::Queued),
                Err(TransitionError {
                    from: terminal,
                    to: State::Queued
                })
            );
            // Even a self-transition is illegal: terminal means done.
            assert!(!terminal.can_transition(terminal));
        }
    }

    #[test]
    fn transition_table_matches_can_transition() {
        for (from, to) in LEGAL_TRANSITIONS {
            assert!(can_transition(*from, *to), "{from} -> {to} should be legal");
        }
        assert!(!can_transition(State::Queued, State::TimedOut));
        assert!(!can_transition(State::Running, State::Ready));
    }

    #[test]
    fn ready_order_is_priority_then_node_id() {
        let nodes = [node("b", 1), node("a", 1), node("c", 0), node("d", 2)];
        let order = order_ready(nodes.iter());
        assert_eq!(
            order,
            vec![
                NodeId::new("c"),
                NodeId::new("a"),
                NodeId::new("b"),
                NodeId::new("d")
            ]
        );
    }

    #[test]
    fn ready_order_is_input_order_independent() {
        let forward = [node("b", 1), node("a", 1), node("c", 0)];
        let backward = [node("c", 0), node("a", 1), node("b", 1)];
        assert_eq!(order_ready(forward.iter()), order_ready(backward.iter()));
    }
}
