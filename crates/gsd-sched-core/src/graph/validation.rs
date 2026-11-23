//! Graph validation: the pure function [`validate`] (and
//! [`validate_with_limits`]) over a [`Graph`], returning typed
//! [`ValidationError`]s.
//!
//! Checks run in a fixed order and the first failure is returned:
//! size, duplicate ids, dangling deps, missing contracts, unproducible
//! inputs (a node listing its own id as an input: it can never produce its
//! own input before running), fan-out, cycles, depth, unreachable required
//! nodes. Every iteration is over sorted [`NodeId`]s, so the error reported
//! for a given graph is deterministic and input-order independent.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::error::Error;
use std::fmt;

use super::model::{Graph, NodeId};

/// Conservative bounded caps for graph validation.
///
/// These are design constants, not operator policy: a graph larger than the
/// caps is refused before scheduling, and the host lowers them (never raises
/// them silently) when its own budget is tighter.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ValidationLimits {
    /// Maximum number of nodes in one graph.
    pub max_nodes: usize,
    /// Maximum dependency depth (longest root-to-node path, in edges).
    pub max_depth: usize,
    /// Maximum fan-out: dependents of a single node.
    pub max_fan_out: usize,
}

impl Default for ValidationLimits {
    fn default() -> Self {
        Self {
            max_nodes: 1024,
            max_depth: 64,
            max_fan_out: 64,
        }
    }
}

/// Why a graph failed validation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ValidationError {
    /// The graph has more nodes than the size limit.
    SizeExceeded {
        /// The number of nodes in the graph.
        nodes: usize,
        /// The configured limit.
        limit: usize,
    },
    /// Two or more nodes share this id.
    DuplicateId(NodeId),
    /// A node depends on an id that is not in the graph.
    DanglingDep {
        /// The node carrying the dangling dependency.
        node: NodeId,
        /// The missing dependency id.
        dep: NodeId,
    },
    /// A node's capability has no covering contract.
    MissingContract {
        /// The uncovered node.
        node: NodeId,
        /// The capability with no contract.
        capability: String,
    },
    /// A node lists its own id as an input, so it can never be fed.
    UnproducibleInput {
        /// The node with the self-input.
        node: NodeId,
        /// The unproducible input (the node's own id).
        input: String,
    },
    /// A node has more dependents than the fan-out limit.
    FanOutExceeded {
        /// The node with the excessive fan-out.
        node: NodeId,
        /// Its dependent count.
        fan_out: usize,
        /// The configured limit.
        limit: usize,
    },
    /// The dependency graph contains a cycle (path of ids, entry repeated
    /// at the end).
    Cycle(Vec<NodeId>),
    /// The longest root-to-node dependency path exceeds the depth limit.
    DepthExceeded {
        /// The deepest node's id.
        node: NodeId,
        /// Its depth in edges.
        depth: usize,
        /// The configured limit.
        limit: usize,
    },
    /// A required node is not reachable from the graph's entry set.
    UnreachableRequired(NodeId),
}

impl fmt::Display for ValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ValidationError::SizeExceeded { nodes, limit } => {
                write!(f, "graph has {nodes} nodes, over the limit of {limit}")
            }
            ValidationError::DuplicateId(id) => write!(f, "duplicate node id {id}"),
            ValidationError::DanglingDep { node, dep } => {
                write!(f, "node {node} depends on missing node {dep}")
            }
            ValidationError::MissingContract { node, capability } => {
                write!(f, "node {node} has no contract for capability {capability}")
            }
            ValidationError::UnproducibleInput { node, input } => {
                write!(f, "node {node} has unproducible input {input}")
            }
            ValidationError::FanOutExceeded {
                node,
                fan_out,
                limit,
            } => {
                write!(
                    f,
                    "node {node} has fan-out {fan_out}, over the limit of {limit}"
                )
            }
            ValidationError::Cycle(path) => {
                let names: Vec<String> = path.iter().map(ToString::to_string).collect();
                write!(f, "dependency cycle {}", names.join(" -> "))
            }
            ValidationError::DepthExceeded { node, depth, limit } => {
                write!(
                    f,
                    "node {node} is at depth {depth}, over the limit of {limit}"
                )
            }
            ValidationError::UnreachableRequired(id) => {
                write!(f, "required node {id} is unreachable from the entry set")
            }
        }
    }
}

impl Error for ValidationError {}

/// Validate `graph` against the default [`ValidationLimits`].
///
/// Pure: no I/O, no globals, same graph in, same result out.
pub fn validate(graph: &Graph) -> Result<(), ValidationError> {
    validate_with_limits(graph, &ValidationLimits::default())
}

/// Validate `graph` against `limits`. See the module docs for check order.
pub fn validate_with_limits(
    graph: &Graph,
    limits: &ValidationLimits,
) -> Result<(), ValidationError> {
    // Size.
    if graph.nodes.len() > limits.max_nodes {
        return Err(ValidationError::SizeExceeded {
            nodes: graph.nodes.len(),
            limit: limits.max_nodes,
        });
    }

    // Index nodes by id, in sorted-id order for deterministic iteration.
    let mut by_id: BTreeMap<&NodeId, &super::model::Node> = BTreeMap::new();
    for node in &graph.nodes {
        if by_id.contains_key(&node.id) {
            return Err(ValidationError::DuplicateId(node.id.clone()));
        }
        by_id.insert(&node.id, node);
    }

    // Dangling deps, then missing contracts, then fan-out.
    let mut dependents: BTreeMap<&NodeId, BTreeSet<&NodeId>> = BTreeMap::new();
    for &id in by_id.keys() {
        dependents.insert(id, BTreeSet::new());
    }
    for (&id, &node) in &by_id {
        for dep in &node.deps {
            if !by_id.contains_key(dep) {
                return Err(ValidationError::DanglingDep {
                    node: id.clone(),
                    dep: dep.clone(),
                });
            }
            dependents.get_mut(dep).expect("dep is indexed").insert(id);
        }
    }
    for (&id, &node) in &by_id {
        if !graph.has_contract(&node.capability) {
            return Err(ValidationError::MissingContract {
                node: id.clone(),
                capability: node.capability.clone(),
            });
        }
    }
    for (&id, &node) in &by_id {
        if node.inputs.iter().any(|input| input == id.as_str()) {
            return Err(ValidationError::UnproducibleInput {
                node: id.clone(),
                input: id.to_string(),
            });
        }
    }
    for (&id, fans) in &dependents {
        if fans.len() > limits.max_fan_out {
            return Err(ValidationError::FanOutExceeded {
                node: id.clone(),
                fan_out: fans.len(),
                limit: limits.max_fan_out,
            });
        }
    }

    // Cycles (before depth, which assumes an acyclic graph).
    if let Some(cycle) = find_cycle(&by_id) {
        return Err(ValidationError::Cycle(cycle));
    }

    // Depth: longest path from any node with no deps, in edges.
    let mut depths: HashMap<&NodeId, usize> = HashMap::new();
    for &id in by_id.keys() {
        depth_of(id, &by_id, &mut depths);
    }
    for &id in by_id.keys() {
        let depth = depths[id];
        if depth > limits.max_depth {
            return Err(ValidationError::DepthExceeded {
                node: id.clone(),
                depth,
                limit: limits.max_depth,
            });
        }
    }

    // Reachability of required nodes from the entry set: the explicit roots,
    // or the dep-less nodes when no roots are given.
    let entries: Vec<&NodeId> = if graph.roots.is_empty() {
        by_id
            .iter()
            .filter(|(_, node)| node.deps.is_empty())
            .map(|(&id, _)| id)
            .collect()
    } else {
        graph
            .roots
            .iter()
            .filter(|id| by_id.contains_key(id))
            .collect()
    };
    let mut reachable: HashSet<&NodeId> = HashSet::new();
    let mut stack: Vec<&NodeId> = entries;
    while let Some(id) = stack.pop() {
        if reachable.insert(id) {
            if let Some(fans) = dependents.get(id) {
                stack.extend(fans.iter().copied());
            }
        }
    }
    for (&id, &node) in &by_id {
        if node.required && !reachable.contains(id) {
            return Err(ValidationError::UnreachableRequired(id.clone()));
        }
    }

    Ok(())
}

/// Find one dependency cycle by iterative DFS over sorted ids, returning the
/// cycle path with the entry id repeated at the end, or `None` if acyclic.
fn find_cycle<'a>(by_id: &BTreeMap<&'a NodeId, &'a super::model::Node>) -> Option<Vec<NodeId>> {
    #[derive(Clone, Copy, PartialEq)]
    enum Mark {
        White,
        Grey,
        Black,
    }

    let mut marks: HashMap<&NodeId, Mark> = by_id.keys().map(|&id| (id, Mark::White)).collect();
    for &start in by_id.keys() {
        if marks[start] != Mark::White {
            continue;
        }
        // Each frame is (node, next unvisited dep index).
        let mut path: Vec<&NodeId> = Vec::new();
        let mut stack: Vec<(&NodeId, usize)> = Vec::new();
        marks.insert(start, Mark::Grey);
        path.push(start);
        stack.push((start, 0));
        while let Some(&(id, index)) = stack.last() {
            let deps = &by_id[id].deps;
            if index < deps.len() {
                stack.last_mut().expect("stack is non-empty").1 += 1;
                let dep = &deps[index];
                match marks[dep] {
                    Mark::Grey => {
                        let at = path
                            .iter()
                            .position(|seen| **seen == *dep)
                            .expect("grey node is on the path");
                        let mut cycle: Vec<NodeId> =
                            path[at..].iter().map(|seen| (*seen).clone()).collect();
                        cycle.push(dep.clone());
                        return Some(cycle);
                    }
                    Mark::White => {
                        marks.insert(dep, Mark::Grey);
                        path.push(dep);
                        stack.push((dep, 0));
                    }
                    Mark::Black => {}
                }
            } else {
                marks.insert(id, Mark::Black);
                path.pop();
                stack.pop();
            }
        }
    }
    None
}

/// Depth of `id` (longest deps chain, in edges); the graph is acyclic here.
/// Memoised over `depths`.
fn depth_of<'a>(
    id: &'a NodeId,
    by_id: &BTreeMap<&'a NodeId, &'a super::model::Node>,
    depths: &mut HashMap<&'a NodeId, usize>,
) -> usize {
    if let Some(&depth) = depths.get(id) {
        return depth;
    }
    let depth = by_id[id]
        .deps
        .iter()
        .map(|dep| depth_of(dep, by_id, depths))
        .max()
        .map_or(0, |deepest| deepest + 1);
    depths.insert(id, depth);
    depth
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::model::{Contract, Graph, Node};

    fn graph(nodes: Vec<Node>) -> Graph {
        Graph {
            nodes,
            contracts: vec![Contract::new("code.backend", 1)],
            roots: Vec::new(),
        }
    }
    #[test]
    fn valid_chain_passes() {
        let g = graph(vec![
            Node::new("a", "code.backend"),
            Node::new("b", "code.backend").with_deps(vec![NodeId::new("a")]),
        ]);
        assert_eq!(validate(&g), Ok(()));
    }

    #[test]
    fn duplicate_ids_are_rejected() {
        let g = graph(vec![
            Node::new("a", "code.backend"),
            Node::new("a", "code.backend"),
        ]);
        assert_eq!(
            validate(&g),
            Err(ValidationError::DuplicateId(NodeId::new("a")))
        );
    }

    #[test]
    fn dangling_deps_are_rejected() {
        let g = graph(vec![
            Node::new("a", "code.backend").with_deps(vec![NodeId::new("ghost")])
        ]);
        assert_eq!(
            validate(&g),
            Err(ValidationError::DanglingDep {
                node: NodeId::new("a"),
                dep: NodeId::new("ghost")
            })
        );
    }

    #[test]
    fn cycles_are_rejected_with_path() {
        let g = graph(vec![
            Node::new("a", "code.backend").with_deps(vec![NodeId::new("b")]),
            Node::new("b", "code.backend").with_deps(vec![NodeId::new("a")]),
        ]);
        match validate(&g) {
            Err(ValidationError::Cycle(path)) => {
                assert_eq!(
                    path,
                    vec![NodeId::new("a"), NodeId::new("b"), NodeId::new("a")]
                );
            }
            other => panic!("expected Cycle, got {other:?}"),
        }
    }

    #[test]
    fn self_dependency_is_a_cycle() {
        let g = graph(vec![
            Node::new("a", "code.backend").with_deps(vec![NodeId::new("a")])
        ]);
        assert!(matches!(validate(&g), Err(ValidationError::Cycle(_))));
    }

    #[test]
    fn missing_contracts_are_rejected() {
        let g = graph(vec![Node::new("a", "code.frontend")]);
        assert_eq!(
            validate(&g),
            Err(ValidationError::MissingContract {
                node: NodeId::new("a"),
                capability: String::from("code.frontend"),
            })
        );
    }

    #[test]
    fn unreachable_required_nodes_are_rejected() {
        // Explicit roots: only "a" is an entry, so required "c" (in its own
        // dep-less component) is unreachable.
        let g = graph(vec![
            Node::new("a", "code.backend"),
            Node::new("c", "code.backend"),
        ])
        .with_roots(vec![NodeId::new("a")]);
        assert_eq!(
            validate(&g),
            Err(ValidationError::UnreachableRequired(NodeId::new("c")))
        );
    }

    #[test]
    fn optional_unreachable_nodes_pass() {
        let g = graph(vec![
            Node::new("a", "code.backend"),
            Node::new("c", "code.backend").with_required(false),
        ])
        .with_roots(vec![NodeId::new("a")]);
        assert_eq!(validate(&g), Ok(()));
    }

    #[test]
    fn depth_limit_is_enforced() {
        let g = graph(vec![
            Node::new("a", "code.backend"),
            Node::new("b", "code.backend").with_deps(vec![NodeId::new("a")]),
            Node::new("c", "code.backend").with_deps(vec![NodeId::new("b")]),
        ]);
        let limits = ValidationLimits {
            max_nodes: 8,
            max_depth: 1,
            max_fan_out: 8,
        };
        assert_eq!(
            validate_with_limits(&g, &limits),
            Err(ValidationError::DepthExceeded {
                node: NodeId::new("c"),
                depth: 2,
                limit: 1
            })
        );
    }

    #[test]
    fn fan_out_limit_is_enforced() {
        let g = graph(vec![
            Node::new("a", "code.backend"),
            Node::new("b", "code.backend").with_deps(vec![NodeId::new("a")]),
            Node::new("c", "code.backend").with_deps(vec![NodeId::new("a")]),
        ]);
        let limits = ValidationLimits {
            max_nodes: 8,
            max_depth: 8,
            max_fan_out: 1,
        };
        assert_eq!(
            validate_with_limits(&g, &limits),
            Err(ValidationError::FanOutExceeded {
                node: NodeId::new("a"),
                fan_out: 2,
                limit: 1
            })
        );
    }

    #[test]
    fn size_limit_is_enforced() {
        let g = graph(vec![
            Node::new("a", "code.backend"),
            Node::new("b", "code.backend"),
        ]);
        let limits = ValidationLimits {
            max_nodes: 1,
            max_depth: 8,
            max_fan_out: 8,
        };
        assert_eq!(
            validate_with_limits(&g, &limits),
            Err(ValidationError::SizeExceeded { nodes: 2, limit: 1 })
        );
    }

    #[test]
    fn validation_is_input_order_independent() {
        let forward = graph(vec![
            Node::new("a", "code.backend").with_deps(vec![NodeId::new("b")]),
            Node::new("b", "code.backend").with_deps(vec![NodeId::new("a")]),
        ]);
        let backward = graph(vec![
            Node::new("b", "code.backend").with_deps(vec![NodeId::new("a")]),
            Node::new("a", "code.backend").with_deps(vec![NodeId::new("b")]),
        ]);
        assert_eq!(validate(&forward), validate(&backward));
    }
}
