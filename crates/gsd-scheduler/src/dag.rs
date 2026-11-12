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
    /// Tasks that ended as failed or cancelled: never ready again; their dependents stay blocked.
    failed: HashSet<String>,
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

    /// Removes the task `id`, every edge into or out of it, and its completion and failure marks.
    ///
    /// The other tasks keep their insertion order and their edges. Returns `Ok(false)` and changes
    /// nothing when `id` is not in the DAG. A task that still has dependents is refused with
    /// `InvalidDag` and nothing changes: removing it would leave those dependents with a parent
    /// that is gone.
    pub fn remove_task(&mut self, id: &str) -> SchedulerResult<bool> {
        let Some(&removed) = self.index.get(id) else {
            return Ok(false);
        };
        if self
            .graph
            .neighbors_directed(removed, Direction::Outgoing)
            .next()
            .is_some()
        {
            return Err(SchedulerError::InvalidDag(format!(
                "task {id} still has dependents"
            )));
        }
        self.index.remove(id);
        self.completed.remove(id);
        self.failed.remove(id);

        // Rebuild the graph without the node. `petgraph` moves the last node into a freed slot,
        // which would reorder `get_ready_tasks`, so the surviving nodes keep their existing order.
        let (nodes, edges) = std::mem::take(&mut self.graph).into_nodes_edges();
        let mut remap: Vec<Option<NodeIndex>> = vec![None; nodes.len()];
        let mut graph = DiGraph::with_capacity(nodes.len() - 1, edges.len());
        for (old, node) in nodes.into_iter().enumerate() {
            if old != removed.index() {
                remap[old] = Some(graph.add_node(node.weight));
            }
        }
        for edge in edges {
            let source = remap[edge.source().index()];
            let target = remap[edge.target().index()];
            if let (Some(source), Some(target)) = (source, target) {
                graph.add_edge(source, target, ());
            }
        }
        for node in self.index.values_mut() {
            if let Some(new) = remap[node.index()] {
                *node = new;
            }
        }
        self.graph = graph;
        Ok(true)
    }

    /// Returns the ids of a task on a cycle, or `None` when the graph is acyclic.
    pub fn detect_cycles(&self) -> Option<Vec<String>> {
        toposort(&self.graph, None)
            .err()
            .map(|cycle| vec![self.key_of(cycle.node_id()).to_string()])
    }

    /// Marks `id` complete so that its dependents become eligible.
    ///
    /// Returns `TaskNotFound` when `id` is not in the DAG, and `InvalidDag` when `id` is already
    /// marked failed.
    pub fn mark_complete(&mut self, id: &TaskId) -> SchedulerResult<()> {
        if !self.index.contains_key(&id.value) {
            return Err(SchedulerError::TaskNotFound(id.value.clone()));
        }
        if self.failed.contains(&id.value) {
            return Err(SchedulerError::InvalidDag(format!(
                "task {} is already failed",
                id.value
            )));
        }
        self.completed.insert(id.value.clone());
        Ok(())
    }

    /// Marks `id` as failed. A failed task is never ready again, and its dependents stay blocked,
    /// because a task becomes ready only when every dependency is complete.
    ///
    /// Returns `TaskNotFound` when `id` is not in the DAG, and `InvalidDag` when `id` is already
    /// marked complete.
    pub fn mark_failed(&mut self, id: &TaskId) -> SchedulerResult<()> {
        if !self.index.contains_key(&id.value) {
            return Err(SchedulerError::TaskNotFound(id.value.clone()));
        }
        if self.completed.contains(&id.value) {
            return Err(SchedulerError::InvalidDag(format!(
                "task {} is already complete",
                id.value
            )));
        }
        self.failed.insert(id.value.clone());
        Ok(())
    }

    /// Returns tasks that are neither complete nor failed and whose dependencies are all complete,
    /// in insertion order.
    pub fn get_ready_tasks(&self) -> Vec<TaskId> {
        self.graph
            .node_indices()
            .filter(|&node| {
                let key = self.key_of(node);
                !self.completed.contains(key)
                    && !self.failed.contains(key)
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

#[cfg(test)]
mod tests {
    use super::*;

    fn task(id: &str, deps: &[&str]) -> Task {
        Task {
            id: Some(TaskId {
                value: id.to_string(),
            }),
            dependencies: deps
                .iter()
                .map(|dep| TaskId {
                    value: dep.to_string(),
                })
                .collect(),
            ..Default::default()
        }
    }

    fn ready_ids(dag: &TaskDag) -> Vec<String> {
        dag.get_ready_tasks()
            .into_iter()
            .map(|id| id.value)
            .collect()
    }

    /// Removing a task that is not last moves every later node to a new index. The surviving
    /// nodes keep their ids, their edges and their readiness.
    #[test]
    fn removing_an_early_task_keeps_the_later_tasks_usable() -> SchedulerResult<()> {
        let mut dag = TaskDag::new();
        dag.add_task(task("A", &[]))?;
        dag.add_task(task("B", &[]))?;
        dag.add_task(task("C", &["B"]))?;
        assert!(dag.remove_task("A")?);
        assert_eq!(ready_ids(&dag), vec!["B".to_string()]);
        dag.mark_complete(&TaskId {
            value: "B".to_string(),
        })?;
        assert_eq!(ready_ids(&dag), vec!["C".to_string()]);
        assert_eq!(dag.critical_path_len("B"), 2);
        assert_eq!(dag.critical_path_len("C"), 1);
        assert!(!dag.remove_task("A")?, "a second removal changes nothing");
        Ok(())
    }

    /// `remove_task` drops the removed task's edges and keeps every other edge across the index
    /// remap. C is removed first, which leaves B with no dependents, so B can go. D keeps its parent A.
    #[test]
    fn removing_a_task_drops_its_edges_and_keeps_the_others() -> SchedulerResult<()> {
        let mut dag = TaskDag::new();
        dag.add_task(task("A", &[]))?;
        dag.add_task(task("B", &[]))?;
        dag.add_task(task("C", &["B"]))?;
        dag.add_task(task("D", &["A"]))?;
        assert!(dag.remove_task("C")?);
        assert!(dag.remove_task("B")?);
        assert_eq!(ready_ids(&dag), vec!["A".to_string()]);
        dag.mark_complete(&TaskId {
            value: "A".to_string(),
        })?;
        assert_eq!(ready_ids(&dag), vec!["D".to_string()]);
        assert_eq!(dag.critical_path_len("A"), 2);
        // D's only parent A stays complete, so removing D leaves no task ready.
        assert!(dag.remove_task("D")?);
        assert!(ready_ids(&dag).is_empty());
        Ok(())
    }

    /// A failed task is never ready again, and its dependents stay blocked until it completes.
    #[test]
    fn a_failed_task_is_never_ready_and_keeps_its_dependents_blocked() -> SchedulerResult<()> {
        let mut dag = TaskDag::new();
        dag.add_task(task("A", &[]))?;
        dag.add_task(task("B", &["A"]))?;
        assert_eq!(ready_ids(&dag), vec!["A".to_string()]);
        dag.mark_failed(&TaskId {
            value: "A".to_string(),
        })?;
        assert!(ready_ids(&dag).is_empty());
        let unknown = TaskId {
            value: "nope".to_string(),
        };
        assert!(matches!(
            dag.mark_failed(&unknown),
            Err(SchedulerError::TaskNotFound(_))
        ));
        Ok(())
    }

    #[test]
    fn a_task_cannot_be_both_completed_and_failed() -> SchedulerResult<()> {
        let mut dag = TaskDag::new();
        dag.add_task(task("A", &[]))?;
        dag.add_task(task("B", &[]))?;
        let a = TaskId {
            value: "A".to_string(),
        };
        dag.mark_complete(&a)?;
        assert!(matches!(
            dag.mark_failed(&a),
            Err(SchedulerError::InvalidDag(_))
        ));
        let b = TaskId {
            value: "B".to_string(),
        };
        dag.mark_failed(&b)?;
        assert!(matches!(
            dag.mark_complete(&b),
            Err(SchedulerError::InvalidDag(_))
        ));
        assert!(ready_ids(&dag).is_empty());
        Ok(())
    }

    /// `remove_task` forgets both outcome marks, so an id added again starts clean.
    #[test]
    fn a_removed_task_forgets_its_completion_and_failure_marks() -> SchedulerResult<()> {
        let mut dag = TaskDag::new();
        dag.add_task(task("A", &[]))?;
        dag.add_task(task("B", &[]))?;
        dag.mark_complete(&TaskId {
            value: "A".to_string(),
        })?;
        dag.mark_failed(&TaskId {
            value: "B".to_string(),
        })?;
        assert!(dag.remove_task("A")?);
        assert!(dag.remove_task("B")?);
        dag.add_task(task("A", &[]))?;
        dag.add_task(task("B", &[]))?;
        assert_eq!(ready_ids(&dag), vec!["A".to_string(), "B".to_string()]);
        Ok(())
    }

    /// `remove_task` refuses a task that still has dependents: it stays, and its dependents keep their parent.
    #[test]
    fn a_task_with_dependents_is_not_removed() -> SchedulerResult<()> {
        let mut dag = TaskDag::new();
        dag.add_task(task("A", &[]))?;
        dag.add_task(task("B", &["A"]))?;
        assert!(matches!(
            dag.remove_task("A"),
            Err(SchedulerError::InvalidDag(_))
        ));
        assert_eq!(ready_ids(&dag), vec!["A".to_string()]);
        dag.mark_complete(&TaskId {
            value: "A".to_string(),
        })?;
        assert_eq!(ready_ids(&dag), vec!["B".to_string()]);
        Ok(())
    }
}
