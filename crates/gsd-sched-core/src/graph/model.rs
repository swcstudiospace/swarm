//! Graph model: [`NodeId`], [`Node`], [`Graph`] and [`Contract`].
//!
//! Pure data only; no validation, no state transitions. Validation lives in
//! [`super::validation`], the node FSM in [`super::state`].

use std::fmt;

/// An opaque, non-empty-by-convention node identifier.
///
/// Ordering is plain lexicographic string order ([`Ord`]); the scheduler's
/// deterministic ready order breaks priority ties by `NodeId`, so ids that
/// should dispatch in a stable order should sort the way a reader expects.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct NodeId(String);

impl NodeId {
    /// Create an id from any string-like value.
    pub fn new(id: impl Into<String>) -> Self {
        Self(id.into())
    }

    /// The id as a string slice.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for NodeId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl From<String> for NodeId {
    fn from(id: String) -> Self {
        Self(id)
    }
}

impl From<&str> for NodeId {
    fn from(id: &str) -> Self {
        Self(id.to_owned())
    }
}

/// One schedulable unit of work in a [`Graph`].
///
/// `priority` is a rank where lower dispatches first (`0` is the most urgent),
/// matching the rank convention of the existing `priority` module
/// (`rank_or_p2`: P0 = 0 first). `timeout` is the per-attempt wall-clock
/// budget; `retry` is the maximum retry count for the node (bounded by the
/// caller's policy, not re-decided here); `idempotency_key`, when present, is
/// the key the executor must deduplicate attempts by. `required` marks nodes
/// that must be reachable from the graph's entry set (see
/// [`super::validation`]); new nodes default to `required: true`, the
/// conservative choice.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Node {
    /// The node's unique id (unique within its graph).
    pub id: NodeId,
    /// Input references (artifact ids, task ids, …) the node consumes.
    pub inputs: Vec<String>,
    /// Ids of nodes that must finish before this node can run.
    pub deps: Vec<NodeId>,
    /// The capability the node needs (for example `code.backend`).
    pub capability: String,
    /// Dispatch rank: lower dispatches first.
    pub priority: u8,
    /// Cost estimate in opaque units, for the caller's budget arithmetic.
    pub cost: u64,
    /// Per-attempt wall-clock timeout.
    pub timeout: std::time::Duration,
    /// Maximum retries for this node.
    pub retry: u32,
    /// Deduplication key for attempts, when the node is idempotent.
    pub idempotency_key: Option<String>,
    /// Whether the node must be reachable from the graph entry set.
    pub required: bool,
}

impl Node {
    /// A node with the given id and capability, and conservative defaults:
    /// no inputs or deps, `priority` 0, `cost` 1, no timeout, no retries, no
    /// idempotency key, and `required: true`.
    pub fn new(id: impl Into<NodeId>, capability: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            inputs: Vec::new(),
            deps: Vec::new(),
            capability: capability.into(),
            priority: 0,
            cost: 1,
            timeout: std::time::Duration::ZERO,
            retry: 0,
            idempotency_key: None,
            required: true,
        }
    }

    /// Set the input references. Builder-style; consumes and returns `self`.
    pub fn with_inputs(mut self, inputs: Vec<String>) -> Self {
        self.inputs = inputs;
        self
    }

    /// Set the dependency ids. Builder-style; consumes and returns `self`.
    pub fn with_deps(mut self, deps: Vec<NodeId>) -> Self {
        self.deps = deps;
        self
    }

    /// Set the dispatch rank (lower first). Builder-style.
    pub fn with_priority(mut self, priority: u8) -> Self {
        self.priority = priority;
        self
    }

    /// Set the cost estimate. Builder-style.
    pub fn with_cost(mut self, cost: u64) -> Self {
        self.cost = cost;
        self
    }

    /// Set the per-attempt timeout. Builder-style.
    pub fn with_timeout(mut self, timeout: std::time::Duration) -> Self {
        self.timeout = timeout;
        self
    }

    /// Set the maximum retry count. Builder-style.
    pub fn with_retry(mut self, retry: u32) -> Self {
        self.retry = retry;
        self
    }

    /// Set the idempotency key. Builder-style.
    pub fn with_idempotency_key(mut self, key: impl Into<String>) -> Self {
        self.idempotency_key = Some(key.into());
        self
    }

    /// Set whether the node is required (reachable). Builder-style.
    pub fn with_required(mut self, required: bool) -> Self {
        self.required = required;
        self
    }
}

/// A capability contract: the versioned promise that a capability exists.
///
/// A node is covered when a contract with the same `capability` is present in
/// its graph. `version` is the contract's own semver-major marker for the
/// caller's pinning; this crate does not interpret it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Contract {
    /// The capability this contract covers (e.g. `code.backend`).
    pub capability: String,
    /// Contract version marker (major version).
    pub version: u32,
}

impl Contract {
    /// A contract for `capability` at `version`.
    pub fn new(capability: impl Into<String>, version: u32) -> Self {
        Self {
            capability: capability.into(),
            version,
        }
    }
}

/// A task graph: nodes, the contracts covering their capabilities, and an
/// optional explicit entry (`roots`) set.
///
/// When `roots` is empty, the entry set is derived as the nodes with no
/// dependencies. When non-empty, only the listed nodes are entry points, so a
/// required node in a disconnected component is unreachable (see
/// [`super::validation`]).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Graph {
    /// The graph's nodes, in caller-supplied order.
    pub nodes: Vec<Node>,
    /// The capability contracts covering the graph's nodes.
    pub contracts: Vec<Contract>,
    /// Explicit entry node ids; empty means "derive from dep-less nodes".
    pub roots: Vec<NodeId>,
}

impl Graph {
    /// An empty graph.
    pub fn new() -> Self {
        Self::default()
    }

    /// Add a node. Duplicate ids are not rejected here; [`super::validation`]
    /// reports them. Builder-style; consumes and returns `self`.
    pub fn with_node(mut self, node: Node) -> Self {
        self.nodes.push(node);
        self
    }

    /// Add a contract. Builder-style; consumes and returns `self`.
    pub fn with_contract(mut self, contract: Contract) -> Self {
        self.contracts.push(contract);
        self
    }

    /// Set the explicit entry set. Builder-style; consumes and returns `self`.
    pub fn with_roots(mut self, roots: Vec<NodeId>) -> Self {
        self.roots = roots;
        self
    }

    /// The node with `id`, or `None`. The first match wins when duplicates
    /// exist (validation reports duplicates separately).
    pub fn node(&self, id: &NodeId) -> Option<&Node> {
        self.nodes.iter().find(|node| &node.id == id)
    }

    /// Whether a contract covers `capability`.
    pub fn has_contract(&self, capability: &str) -> bool {
        self.contracts
            .iter()
            .any(|contract| contract.capability == capability)
    }

    /// The number of nodes in the graph.
    pub fn len(&self) -> usize {
        self.nodes.len()
    }

    /// Whether the graph has no nodes.
    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn node_id_orders_lexicographically() {
        assert!(NodeId::new("a") < NodeId::new("b"));
        assert_eq!(NodeId::from("x").as_str(), "x");
        assert_eq!(NodeId::from(String::from("y")).to_string(), "y");
    }

    #[test]
    fn node_defaults_are_conservative() {
        let node = Node::new("n1", "code.backend");
        assert_eq!(node.id.as_str(), "n1");
        assert!(node.inputs.is_empty());
        assert!(node.deps.is_empty());
        assert_eq!(node.priority, 0);
        assert_eq!(node.cost, 1);
        assert_eq!(node.timeout, std::time::Duration::ZERO);
        assert_eq!(node.retry, 0);
        assert_eq!(node.idempotency_key, None);
        assert!(node.required);
    }

    #[test]
    fn graph_lookup_and_contract_cover() {
        let graph = Graph::new()
            .with_node(Node::new("a", "code.backend"))
            .with_contract(Contract::new("code.backend", 1));
        assert_eq!(graph.len(), 1);
        assert!(!graph.is_empty());
        assert!(graph.node(&NodeId::new("a")).is_some());
        assert!(graph.node(&NodeId::new("missing")).is_none());
        assert!(graph.has_contract("code.backend"));
        assert!(!graph.has_contract("code.frontend"));
    }
}
