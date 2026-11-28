//! Topological scheduling over a validated [`Graph`]: the pure planner
//! ([`Planner`]), a cooperative [`CancellationToken`], typed outcome
//! aggregation ([`Aggregate`], [`NodeOutcome`]) and a deterministic graph
//! dump ([`GraphDump`], [`dump`]).
//!
//! The planner is a decision core, not an executor: it emits ready waves of
//! [`NodeId`]s in topological order and the host runs them. Every wave is
//! bounded by `max_parallel` (bounded parallelism), its nodes are ordered by
//! `(priority, NodeId)` via [`order_ready`], and a node enters a wave only
//! once every dependency was emitted in an earlier wave — so fan-out (one
//! node feeding many later dependents) and fan-in (one node waiting on many
//! predecessors) fall out of the dependency edges. The host polls
//! [`Planner::next_wave`] with the shared [`CancellationToken`] between
//! waves; if the token is cancelled, every not-yet-emitted node is marked
//! [`NodeOutcome::Cancelled`] and no further wave is emitted (cooperative
//! cancellation).
//!
//! Threads and blocking: std only; the token is an [`AtomicBool`]. No async
//! runtime, no I/O, no globals. No Python/PyO3 dependency, no new deps, no
//! unsafe code.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::sync::atomic::{AtomicBool, Ordering};

use super::model::{Contract, Graph, NodeId};
use super::state::order_ready;
use super::validation::{validate, ValidationError};

/// A cooperative cancellation token shared by the host and the [`Planner`].
///
/// The host cancels it (for example on shutdown) and the planner checks it
/// before every wave; workers may also check it between awaits. Cancellation
/// is sticky: once cancelled, a token never un-cancels.
#[derive(Debug, Default)]
pub struct CancellationToken {
    cancelled: AtomicBool,
}

impl CancellationToken {
    /// A token that is not cancelled.
    pub fn new() -> Self {
        Self::default()
    }

    /// Mark the token cancelled. Idempotent.
    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
    }

    /// Whether the token has been cancelled.
    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }
}

/// What happened to one node in a [`Planner`] run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeOutcome {
    /// The node was emitted in a wave for the host to execute.
    Emitted,
    /// The run was cancelled before the node could be emitted.
    Cancelled,
    /// The node has not been emitted yet (run still in progress).
    Pending,
}

/// Typed aggregate of a planner run: per-outcome counts plus wave shape.
///
/// Every field is a count or a width; no stringly-typed statuses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Aggregate {
    /// Total nodes in the graph.
    pub total: usize,
    /// Nodes emitted in a wave.
    pub emitted: usize,
    /// Nodes marked cancelled (run cancelled before they were emitted).
    pub cancelled: usize,
    /// Nodes not yet emitted (run still in progress).
    pub pending: usize,
    /// Waves emitted so far.
    pub waves: usize,
    /// Width of the widest wave emitted so far (never exceeds the bound).
    pub max_wave_width: usize,
}

/// The result of one [`Planner::next_wave`] call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Wave {
    /// A wave of ready nodes, at most `max_parallel` wide.
    Batch(Vec<NodeId>),
    /// Every node was emitted; the run is complete.
    Done,
    /// The token was cancelled; all remaining nodes are
    /// [`NodeOutcome::Cancelled`]. Sticky: later calls return this again.
    Cancelled,
}

/// The pure topological planner over one [`Graph`].
///
/// Construct with [`Planner::new`] (validates the graph), then pull waves
/// with [`Planner::next_wave`]. Re-running on the same graph yields the
/// same waves: ready order is `(priority, NodeId)` and every internal map
/// is a [`BTreeMap`]/[`BTreeSet`], so input order never matters.
#[derive(Debug)]
pub struct Planner<'a> {
    /// Nodes by id, in sorted-id order.
    by_id: BTreeMap<NodeId, &'a super::model::Node>,
    /// For each node, the nodes that depend on it (unique deps only).
    dependents: BTreeMap<NodeId, Vec<NodeId>>,
    /// How many unique deps each node still waits on.
    remaining_deps: BTreeMap<NodeId, usize>,
    /// Node ids whose deps all were emitted and that are not yet emitted.
    ready: BTreeSet<NodeId>,
    /// The parallelism bound; at least 1.
    max_parallel: usize,
    /// Per-node outcome, in sorted-id order.
    outcomes: BTreeMap<NodeId, NodeOutcome>,
    /// Waves emitted so far, in emission order.
    waves: Vec<Vec<NodeId>>,
    /// Whether the run finished (done or cancelled); sticky outcome.
    finished: Option<Wave>,
}

impl<'a> Planner<'a> {
    /// A planner for `graph`, failing on the same validation errors as
    /// [`validate`]. `max_parallel` of 0 is clamped to 1.
    pub fn new(graph: &'a Graph, max_parallel: usize) -> Result<Self, ValidationError> {
        validate(graph)?;
        let mut by_id: BTreeMap<NodeId, &'a super::model::Node> = BTreeMap::new();
        let mut dependents: BTreeMap<NodeId, Vec<NodeId>> = BTreeMap::new();
        let mut remaining_deps: BTreeMap<NodeId, usize> = BTreeMap::new();
        let mut ready: BTreeSet<NodeId> = BTreeSet::new();
        let mut outcomes: BTreeMap<NodeId, NodeOutcome> = BTreeMap::new();
        for node in &graph.nodes {
            by_id.insert(node.id.clone(), node);
            dependents.insert(node.id.clone(), Vec::new());
            outcomes.insert(node.id.clone(), NodeOutcome::Pending);
        }
        for node in &graph.nodes {
            // Unique deps: a duplicated dep would otherwise be counted
            // twice and could never be satisfied.
            let unique_deps: BTreeSet<&NodeId> = node.deps.iter().collect();
            remaining_deps.insert(node.id.clone(), unique_deps.len());
            if unique_deps.is_empty() {
                ready.insert(node.id.clone());
            }
            for dep in unique_deps {
                dependents
                    .get_mut(dep)
                    .expect("validated graph: dep exists")
                    .push(node.id.clone());
            }
        }
        for fans in dependents.values_mut() {
            fans.sort();
        }
        Ok(Self {
            by_id,
            dependents,
            remaining_deps,
            ready,
            max_parallel: max_parallel.max(1),
            outcomes,
            waves: Vec::new(),
            finished: None,
        })
    }

    /// The parallelism bound waves are limited to (at least 1).
    pub fn max_parallel(&self) -> usize {
        self.max_parallel
    }

    /// Pull the next wave of ready nodes.
    ///
    /// The token is checked first: a cancelled token marks every
    /// not-yet-emitted node [`NodeOutcome::Cancelled`] and returns
    /// [`Wave::Cancelled`] (sticky). Otherwise the ready set is ordered by
    /// [`order_ready`] and up to `max_parallel` nodes are emitted as one
    /// wave; emitting a node decrements its dependents' remaining-dep
    /// counts, so a fan-in node becomes ready only after *all* its
    /// predecessors were emitted. When nothing is ready and nothing
    /// remains, returns [`Wave::Done`] (sticky).
    pub fn next_wave(&mut self, token: &CancellationToken) -> Wave {
        if let Some(finished) = &self.finished {
            return finished.clone();
        }
        if token.is_cancelled() {
            for outcome in self.outcomes.values_mut() {
                if *outcome == NodeOutcome::Pending {
                    *outcome = NodeOutcome::Cancelled;
                }
            }
            self.finished = Some(Wave::Cancelled);
            return Wave::Cancelled;
        }
        if self.ready.is_empty() {
            self.finished = Some(Wave::Done);
            return Wave::Done;
        }
        let ordered = order_ready(self.ready.iter().map(|id| self.by_id[id]));
        let batch: Vec<NodeId> = ordered.into_iter().take(self.max_parallel).collect();
        for id in &batch {
            self.ready.remove(id);
            self.outcomes.insert(id.clone(), NodeOutcome::Emitted);
        }
        for id in &batch {
            for fan in &self.dependents[id] {
                let left = self.remaining_deps.get_mut(fan).expect("indexed node");
                *left -= 1;
                if *left == 0 {
                    self.ready.insert(fan.clone());
                }
            }
        }
        self.waves.push(batch.clone());
        Wave::Batch(batch)
    }

    /// All waves emitted so far, in emission order.
    pub fn waves(&self) -> &[Vec<NodeId>] {
        &self.waves
    }

    /// The per-node outcome of every node, in sorted-id order.
    pub fn outcomes(&self) -> &BTreeMap<NodeId, NodeOutcome> {
        &self.outcomes
    }

    /// The typed aggregate of the run so far.
    pub fn aggregate(&self) -> Aggregate {
        let mut aggregate = Aggregate {
            total: self.outcomes.len(),
            emitted: 0,
            cancelled: 0,
            pending: 0,
            waves: self.waves.len(),
            max_wave_width: self.waves.iter().map(Vec::len).max().unwrap_or(0),
        };
        for outcome in self.outcomes.values() {
            match outcome {
                NodeOutcome::Emitted => aggregate.emitted += 1,
                NodeOutcome::Cancelled => aggregate.cancelled += 1,
                NodeOutcome::Pending => aggregate.pending += 1,
            }
        }
        aggregate
    }

    /// Whether the run reached [`Wave::Done`] or [`Wave::Cancelled`].
    pub fn is_finished(&self) -> bool {
        self.finished.is_some()
    }
}

/// The node ids of `graph` in topological order, or the [`ValidationError`]
/// that rejects it. Deterministic: priority, then [`NodeId`], for ready
/// nodes, with unbounded waves (every ready node in one wave).
pub fn topological_order(graph: &Graph) -> Result<Vec<NodeId>, ValidationError> {
    let token = CancellationToken::new();
    let mut planner = Planner::new(graph, usize::MAX)?;
    let mut order = Vec::with_capacity(graph.len());
    loop {
        match planner.next_wave(&token) {
            Wave::Batch(batch) => order.extend(batch),
            Wave::Done => return Ok(order),
            Wave::Cancelled => unreachable!("fresh token is never cancelled"),
        }
    }
}

/// All waves for `graph` under `max_parallel`, or the [`ValidationError`]
/// that rejects it.
///
/// Convenience over [`Planner`] for hosts that want the whole plan at once;
/// hosts executing wave-by-wave (and honouring cancellation between waves)
/// should use [`Planner`] directly.
pub fn schedule_waves(
    graph: &Graph,
    max_parallel: usize,
) -> Result<Vec<Vec<NodeId>>, ValidationError> {
    let token = CancellationToken::new();
    let mut planner = Planner::new(graph, max_parallel)?;
    loop {
        match planner.next_wave(&token) {
            Wave::Batch(_) => {}
            Wave::Done | Wave::Cancelled => return Ok(planner.waves().to_vec()),
        }
    }
}

/// One node of a [`GraphDump`]: its scheduling facts, deps sorted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DumpedNode {
    /// The node's id.
    pub id: NodeId,
    /// The capability the node needs.
    pub capability: String,
    /// Dispatch rank (lower first).
    pub priority: u8,
    /// Cost estimate in opaque units.
    pub cost: u64,
    /// Dependency ids, sorted.
    pub deps: Vec<NodeId>,
    /// Input references, as listed.
    pub inputs: Vec<String>,
    /// Whether the node must be reachable from the entry set.
    pub required: bool,
}

/// A deterministic, input-order-independent dump of a [`Graph`]:
/// nodes sorted by id, edges sorted, contracts and roots as listed.
///
/// Pure data plus [`fmt::Display`]; no JSON dependency and no I/O.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GraphDump {
    /// The graph's nodes, sorted by id.
    pub nodes: Vec<DumpedNode>,
    /// Dependency edges `(dep, node)`, sorted.
    pub edges: Vec<(NodeId, NodeId)>,
    /// The capability contracts covering the graph.
    pub contracts: Vec<Contract>,
    /// The explicit entry set (empty means derived from dep-less nodes).
    pub roots: Vec<NodeId>,
}

impl fmt::Display for GraphDump {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(
            f,
            "graph nodes={} edges={} contracts={} roots={}",
            self.nodes.len(),
            self.edges.len(),
            self.contracts.len(),
            self.roots.len()
        )?;
        for node in &self.nodes {
            let deps: Vec<String> = node.deps.iter().map(ToString::to_string).collect();
            writeln!(
                f,
                "node {} cap={} pri={} cost={} required={} deps=[{}] inputs=[{}]",
                node.id,
                node.capability,
                node.priority,
                node.cost,
                node.required,
                deps.join(","),
                node.inputs.join(",")
            )?;
        }
        for (dep, node) in &self.edges {
            writeln!(f, "edge {dep} -> {node}")?;
        }
        for contract in &self.contracts {
            writeln!(f, "contract {} v{}", contract.capability, contract.version)?;
        }
        Ok(())
    }
}

/// Dump `graph` deterministically: same graph in, same dump out, whatever
/// order its nodes were supplied in.
pub fn dump(graph: &Graph) -> GraphDump {
    let mut nodes: Vec<DumpedNode> = graph
        .nodes
        .iter()
        .map(|node| {
            let mut deps = node.deps.clone();
            deps.sort();
            deps.dedup();
            DumpedNode {
                id: node.id.clone(),
                capability: node.capability.clone(),
                priority: node.priority,
                cost: node.cost,
                deps,
                inputs: node.inputs.clone(),
                required: node.required,
            }
        })
        .collect();
    nodes.sort_by(|a, b| a.id.cmp(&b.id));
    let mut edges: Vec<(NodeId, NodeId)> = graph
        .nodes
        .iter()
        .flat_map(|node| {
            let unique_deps: BTreeSet<&NodeId> = node.deps.iter().collect();
            unique_deps
                .into_iter()
                .map(move |dep| (dep.clone(), node.id.clone()))
        })
        .collect();
    edges.sort();
    edges.dedup();
    GraphDump {
        nodes,
        edges,
        contracts: graph.contracts.clone(),
        roots: graph.roots.clone(),
    }
}
