//! Integration tests for the DAG core (`gsd_sched_core::graph`): validation,
//! the topological planner, bounded parallelism, cooperative cancellation,
//! typed aggregation and the graph dump.
//!
//! Covers the acceptance cases: cycles, missing/duplicate, empty/single,
//! fan-out/in, illegal transitions, bound, cancel.

use std::collections::BTreeMap;

use gsd_sched_core::graph::schedule::{
    dump, schedule_waves, topological_order, CancellationToken, NodeOutcome, Planner, Wave,
};
use gsd_sched_core::graph::validation::validate;
use gsd_sched_core::graph::{Contract, Graph, Node, NodeId, NodeState, State, ValidationError};

fn graph_with(nodes: Vec<Node>) -> Graph {
    Graph {
        nodes,
        contracts: vec![Contract::new("code.backend", 1)],
        roots: Vec::new(),
    }
}

fn node(id: &str) -> Node {
    Node::new(id, "code.backend")
}

fn node_with_deps(id: &str, deps: &[&str]) -> Node {
    node(id).with_deps(deps.iter().map(|dep| NodeId::from(*dep)).collect())
}

fn ids(words: &[&str]) -> Vec<NodeId> {
    words.iter().map(|word| NodeId::from(*word)).collect()
}

/// Diamond: a fans out to b and c; b and c fan in to d.
fn diamond() -> Graph {
    graph_with(vec![
        node("a"),
        node_with_deps("b", &["a"]),
        node_with_deps("c", &["a"]),
        node_with_deps("d", &["b", "c"]),
    ])
}

#[test]
fn cycle_is_rejected_by_validate_and_planner() {
    let g = graph_with(vec![
        node_with_deps("a", &["b"]),
        node_with_deps("b", &["a"]),
    ]);
    assert!(matches!(validate(&g), Err(ValidationError::Cycle(_))));
    assert!(matches!(
        topological_order(&g),
        Err(ValidationError::Cycle(_))
    ));
    assert!(matches!(
        Planner::new(&g, 2),
        Err(ValidationError::Cycle(_))
    ));
}

#[test]
fn self_dependency_is_a_cycle() {
    let g = graph_with(vec![node_with_deps("a", &["a"])]);
    assert!(matches!(validate(&g), Err(ValidationError::Cycle(_))));
}

#[test]
fn duplicate_ids_are_rejected() {
    let g = graph_with(vec![node("a"), node("a")]);
    assert_eq!(
        validate(&g),
        Err(ValidationError::DuplicateId(NodeId::new("a")))
    );
}

#[test]
fn missing_dep_is_rejected() {
    let g = graph_with(vec![node_with_deps("a", &["ghost"])]);
    assert_eq!(
        validate(&g),
        Err(ValidationError::DanglingDep {
            node: NodeId::new("a"),
            dep: NodeId::new("ghost")
        })
    );
}

#[test]
fn missing_contract_is_rejected() {
    let g = graph_with(vec![Node::new("a", "code.frontend")]);
    assert!(matches!(
        validate(&g),
        Err(ValidationError::MissingContract { .. })
    ));
}

#[test]
fn self_input_is_unproducible() {
    let g = graph_with(vec![node("a").with_inputs(vec![String::from("a")])]);
    assert_eq!(
        validate(&g),
        Err(ValidationError::UnproducibleInput {
            node: NodeId::new("a"),
            input: String::from("a")
        })
    );
}

#[test]
fn unreachable_required_node_is_rejected() {
    // "c" is required but not reachable from the explicit entry "a", so its
    // inputs can never be produced.
    let g = graph_with(vec![node("a"), node("c")]).with_roots(ids(&["a"]));
    assert_eq!(
        validate(&g),
        Err(ValidationError::UnreachableRequired(NodeId::new("c")))
    );
}

#[test]
fn empty_graph_schedules_to_nothing() {
    let g = graph_with(vec![]);
    assert_eq!(validate(&g), Ok(()));
    assert_eq!(topological_order(&g), Ok(vec![]));
    let token = CancellationToken::new();
    let mut planner = Planner::new(&g, 4).unwrap();
    assert_eq!(planner.next_wave(&token), Wave::Done);
    assert_eq!(planner.aggregate().total, 0);
    assert_eq!(planner.aggregate().waves, 0);
}

#[test]
fn single_node_graph_schedules_one_wave() {
    let g = graph_with(vec![node("only")]);
    assert_eq!(topological_order(&g), Ok(ids(&["only"])));
    assert_eq!(schedule_waves(&g, 4).unwrap(), vec![ids(&["only"])]);
}

#[test]
fn fan_out_and_fan_in_topological_order() {
    let g = diamond();
    // a first, d last, b and c between (deterministic tie-break by NodeId).
    assert_eq!(topological_order(&g), Ok(ids(&["a", "b", "c", "d"])));
    // One wave per level when the bound allows it.
    let waves = schedule_waves(&g, 8).unwrap();
    assert_eq!(waves, vec![ids(&["a"]), ids(&["b", "c"]), ids(&["d"])]);
}

#[test]
fn ready_tie_break_is_priority_then_node_id() {
    let g = graph_with(vec![
        node("b").with_priority(1),
        node("a").with_priority(1),
        node("c").with_priority(0),
    ]);
    assert_eq!(topological_order(&g), Ok(ids(&["c", "a", "b"])));
}

#[test]
fn schedule_is_input_order_independent() {
    let forward = diamond();
    let backward = graph_with(vec![
        node_with_deps("d", &["b", "c"]),
        node_with_deps("c", &["a"]),
        node_with_deps("b", &["a"]),
        node("a"),
    ]);
    assert_eq!(topological_order(&forward), topological_order(&backward));
    assert_eq!(dump(&forward).to_string(), dump(&backward).to_string());
}

#[test]
fn duplicated_deps_do_not_deadlock() {
    let g = graph_with(vec![node("a"), node("b").with_deps(ids(&["a", "a"]))]);
    assert_eq!(topological_order(&g), Ok(ids(&["a", "b"])));
}

#[test]
fn parallelism_bound_is_respected() {
    let g = graph_with(vec![node("a"), node("b"), node("c"), node("d")]);
    let waves = schedule_waves(&g, 2).unwrap();
    assert_eq!(waves.len(), 2);
    for wave in &waves {
        assert!(wave.len() <= 2, "wave {wave:?} exceeds bound 2");
    }
    assert!(waves.iter().all(|wave| wave.len() == 2));
    // A bound of 0 clamps to 1: fully serial.
    let serial = schedule_waves(&g, 0).unwrap();
    assert_eq!(serial.len(), 4);
    assert!(serial.iter().all(|wave| wave.len() == 1));
}

#[test]
fn aggregate_counts_outcomes_and_wave_shape() {
    let g = diamond();
    let token = CancellationToken::new();
    let mut planner = Planner::new(&g, 8).unwrap();
    while !planner.is_finished() {
        let _ = planner.next_wave(&token);
    }
    let aggregate = planner.aggregate();
    assert_eq!(aggregate.total, 4);
    assert_eq!(aggregate.emitted, 4);
    assert_eq!(aggregate.cancelled, 0);
    assert_eq!(aggregate.pending, 0);
    assert_eq!(aggregate.waves, 3);
    assert_eq!(aggregate.max_wave_width, 2);
    let expected: BTreeMap<NodeId, NodeOutcome> = ids(&["a", "b", "c", "d"])
        .into_iter()
        .map(|id| (id, NodeOutcome::Emitted))
        .collect();
    assert_eq!(planner.outcomes(), &expected);
}

#[test]
fn precancelled_token_cancels_everything() {
    let g = diamond();
    let token = CancellationToken::new();
    token.cancel();
    let mut planner = Planner::new(&g, 8).unwrap();
    assert_eq!(planner.next_wave(&token), Wave::Cancelled);
    // Sticky.
    assert_eq!(planner.next_wave(&token), Wave::Cancelled);
    assert!(planner.waves().is_empty());
    let aggregate = planner.aggregate();
    assert_eq!(aggregate.emitted, 0);
    assert_eq!(aggregate.cancelled, 4);
    assert_eq!(aggregate.pending, 0);
    assert!(planner
        .outcomes()
        .values()
        .all(|outcome| *outcome == NodeOutcome::Cancelled));
}

#[test]
fn mid_run_cancel_stops_at_wave_boundary() {
    let g = diamond();
    let token = CancellationToken::new();
    let mut planner = Planner::new(&g, 8).unwrap();
    assert_eq!(planner.next_wave(&token), Wave::Batch(ids(&["a"])));
    token.cancel();
    assert_eq!(planner.next_wave(&token), Wave::Cancelled);
    let aggregate = planner.aggregate();
    assert_eq!(aggregate.emitted, 1);
    assert_eq!(aggregate.cancelled, 3);
    assert_eq!(planner.outcomes()[&NodeId::new("a")], NodeOutcome::Emitted);
    assert_eq!(
        planner.outcomes()[&NodeId::new("d")],
        NodeOutcome::Cancelled
    );
}

#[test]
fn illegal_transitions_are_rejected_and_keep_state() {
    let mut state = NodeState::queued();
    assert!(state.transition(State::Running).is_err());
    assert!(state.transition(State::Succeeded).is_err());
    assert_eq!(state.state, State::Queued);
    state.transition(State::Ready).unwrap();
    state.transition(State::Running).unwrap();
    assert!(state.transition(State::Ready).is_err());
    assert_eq!(state.state, State::Running);
    state.transition(State::Succeeded).unwrap();
    // Terminal: nothing leaves, not even to itself.
    assert!(state.transition(State::Queued).is_err());
    assert!(state.transition(State::Succeeded).is_err());
}

#[test]
fn graph_dump_is_deterministic_and_sorted() {
    let g = diamond();
    let dump = dump(&g);
    let text = dump.to_string();
    assert!(text.starts_with("graph nodes=4 edges=4 contracts=1 roots=0"));
    // Nodes sorted by id, edges sorted: a appears before b before c before d.
    let node_lines: Vec<&str> = text
        .lines()
        .filter(|line| line.starts_with("node "))
        .collect();
    assert_eq!(node_lines.len(), 4);
    assert!(node_lines[0].starts_with("node a "));
    assert!(node_lines[3].starts_with("node d "));
    assert!(text.contains("edge a -> b"));
    assert!(text.contains("edge b -> d"));
    assert!(text.contains("edge c -> d"));
    assert!(text.contains("contract code.backend v1"));
    assert_eq!(dump.edges.len(), 4);
    assert_eq!(dump.nodes.len(), 4);
}
