//! Task dependency graph and ready-frontier computation.
//!
//! Edges point from a dependency to its dependent. A task may only depend on
//! tasks that were already added, so insertion cannot close a cycle;
//! [`TaskDag::detect_cycles`] remains an independent structural check.

use std::collections::{HashMap, HashSet};

use gsd_proto::gsd::v1::{Task, TaskId};
use petgraph::algo::toposort;
use petgraph::graph::{DiGraph, NodeIndex};
use petgraph::Direction;

use crate::{SchedulerError, SchedulerResult};

/// Dependency graph of submitted tasks, in insertion order.
#[derive(Debug, Default)]
pub struct TaskDag {
    graph: DiGraph<Task, ()>,
    index: HashMap<String, NodeIndex>,
    completed: HashSet<String>,
}

impl TaskDag {
    /// Creates an empty DAG.
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds `task` and an edge from each of its dependencies.
    ///
    /// Returns `InvalidDag` when the task has no id or its id is already present,
    /// and `TaskNotFound` when a dependency has not been added yet.
    pub fn add_task(&mut self, task: Task) -> SchedulerResult<NodeIndex> {
        let id = task
            .id
            .as_ref()
            .map(|id| id.value.clone())
            .filter(|value| !value.is_empty())
            .ok_or_else(|| SchedulerError::InvalidDag("task has no id".to_string()))?;
        if self.index.contains_key(&id) {
            return Err(SchedulerError::InvalidDag(format!(
                "duplicate task id: {id}"
            )));
        }

        let mut parents = Vec::with_capacity(task.dependencies.len());
        for dep in &task.dependencies {
            match self.index.get(&dep.value) {
                Some(&parent) => parents.push(parent),
                None => return Err(SchedulerError::TaskNotFound(dep.value.clone())),
            }
        }

        let node = self.graph.add_node(task);
        for parent in parents {
            self.graph.add_edge(parent, node, ());
        }
        self.index.insert(id, node);
        Ok(node)
    }

    /// Returns the ids of a task on a cycle, or `None` when the graph is acyclic.
    pub fn detect_cycles(&self) -> Option<Vec<String>> {
        toposort(&self.graph, None)
            .err()
            .map(|cycle| vec![self.key_of(cycle.node_id()).to_string()])
    }

    /// Marks `id` complete so that its dependents become eligible.
    ///
    /// Returns `TaskNotFound` when `id` is not in the DAG.
    pub fn mark_complete(&mut self, id: &TaskId) -> SchedulerResult<()> {
        if !self.index.contains_key(&id.value) {
            return Err(SchedulerError::TaskNotFound(id.value.clone()));
        }
        self.completed.insert(id.value.clone());
        Ok(())
    }

    /// Returns tasks that are not complete and whose dependencies are all complete,
    /// in insertion order.
    pub fn get_ready_tasks(&self) -> Vec<TaskId> {
        self.graph
            .node_indices()
            .filter(|&node| {
                !self.completed.contains(self.key_of(node))
                    && self
                        .graph
                        .neighbors_directed(node, Direction::Incoming)
                        .all(|parent| self.completed.contains(self.key_of(parent)))
            })
            .filter_map(|node| self.graph[node].id.clone())
            .collect()
    }

    /// Returns the number of tasks on the longest chain that starts at `id` and
    /// follows dependents, counting `id` itself. Returns 0 for unknown ids.
    pub fn critical_path_len(&self, id: &str) -> usize {
        self.index
            .get(id)
            .and_then(|node| self.downstream_depths().get(node).copied())
            .unwrap_or(0)
    }

    /// Computes the longest downstream chain length for every node.
    fn downstream_depths(&self) -> HashMap<NodeIndex, usize> {
        let mut depth: HashMap<NodeIndex, usize> = HashMap::with_capacity(self.graph.node_count());
        let Ok(order) = toposort(&self.graph, None) else {
            return depth;
        };
        for &node in order.iter().rev() {
            let longest = self
                .graph
                .neighbors_directed(node, Direction::Outgoing)
                .filter_map(|next| depth.get(&next).copied())
                .max()
                .unwrap_or(0);
            depth.insert(node, longest + 1);
        }
        depth
    }

    /// Returns the task id stored at `node`, or an empty string for a node without one.
    fn key_of(&self, node: NodeIndex) -> &str {
        self.graph[node]
            .id
            .as_ref()
            .map_or("", |id| id.value.as_str())
    }
}
