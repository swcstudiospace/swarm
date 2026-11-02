//! Builds a validated [`TaskDag`] from workflow definitions.
//!
//! Tasks and edges are ingested as values. Edges are merged into their dependent task's
//! dependencies, the graph is ordered topologically, and every task is inserted through
//! [`TaskDag::add_task`]. A cycle is reported as [`SchedulerError::CycleDetected`], which maps to
//! `E-DEP` in the `gsd-failure` taxonomy.

use std::collections::{HashMap, HashSet, VecDeque};

use gsd_proto::gsd::v1::{Edge, Task, TaskId, WorkflowDef};

use crate::dag::TaskDag;
use crate::{SchedulerError, SchedulerResult};

/// A validated task graph together with its tasks, in workflow order.
#[derive(Debug, Default)]
pub struct BuiltDag {
    /// The dependency graph, with every task inserted through `add_task`.
    pub dag: TaskDag,
    /// The tasks in workflow order, with edge dependencies merged into `dependencies`.
    pub tasks: Vec<Task>,
}

/// Builds the DAG for `workflow`, merging its edges into task dependencies.
///
/// Returns `CycleDetected` on a dependency cycle, `TaskNotFound` for an unknown dependency or
/// edge endpoint, and `InvalidDag` for a missing or duplicate task id.
pub fn build_dag(workflow: &WorkflowDef) -> SchedulerResult<BuiltDag> {
    build_dag_from(&workflow.tasks, &workflow.edges)
}

/// Builds the DAG for `tasks` with additional `edges`, where an edge `from -> to` makes `to`
/// depend on `from`.
///
/// Returns the same errors as [`build_dag`].
pub fn build_dag_from(tasks: &[Task], edges: &[Edge]) -> SchedulerResult<BuiltDag> {
    let ids = task_ids(tasks)?;
    let index: HashMap<&str, usize> = ids.iter().enumerate().map(|(i, id)| (*id, i)).collect();

    let mut deps: Vec<Vec<usize>> = Vec::with_capacity(tasks.len());
    for task in tasks {
        let mut parents = Vec::with_capacity(task.dependencies.len());
        for dep in &task.dependencies {
            push_unique(&mut parents, lookup(&index, &dep.value)?);
        }
        deps.push(parents);
    }
    for edge in edges {
        let from = lookup(&index, endpoint(edge.from.as_ref()))?;
        let to = lookup(&index, endpoint(edge.to.as_ref()))?;
        push_unique(&mut deps[to], from);
    }

    let order = topological_order(&deps, &ids)?;

    let merged: Vec<Task> = tasks
        .iter()
        .enumerate()
        .map(|(i, task)| Task {
            dependencies: deps[i]
                .iter()
                .map(|&parent| TaskId {
                    value: ids[parent].to_string(),
                })
                .collect(),
            ..task.clone()
        })
        .collect();

    let mut dag = TaskDag::new();
    for &node in &order {
        dag.add_task(merged[node].clone())?;
    }
    Ok(BuiltDag { dag, tasks: merged })
}

/// Returns each task's id, rejecting missing or duplicate ids.
fn task_ids(tasks: &[Task]) -> SchedulerResult<Vec<&str>> {
    let mut seen: HashSet<&str> = HashSet::with_capacity(tasks.len());
    let mut ids = Vec::with_capacity(tasks.len());
    for task in tasks {
        let id = task
            .id
            .as_ref()
            .map(|id| id.value.as_str())
            .filter(|value| !value.is_empty())
            .ok_or_else(|| SchedulerError::InvalidDag("task has no id".to_string()))?;
        if !seen.insert(id) {
            return Err(SchedulerError::InvalidDag(format!(
                "duplicate task id: {id}"
            )));
        }
        ids.push(id);
    }
    Ok(ids)
}

/// Returns the endpoint id of an edge, or an empty id when the endpoint is absent.
fn endpoint(id: Option<&TaskId>) -> &str {
    id.map_or("", |id| id.value.as_str())
}

fn lookup(index: &HashMap<&str, usize>, id: &str) -> SchedulerResult<usize> {
    index
        .get(id)
        .copied()
        .ok_or_else(|| SchedulerError::TaskNotFound(id.to_string()))
}

fn push_unique(list: &mut Vec<usize>, value: usize) {
    if !list.contains(&value) {
        list.push(value);
    }
}

/// Kahn's algorithm over the dependency lists. Fails with `CycleDetected` naming every task
/// that cannot be ordered, which includes all tasks on or downstream of a cycle.
fn topological_order(deps: &[Vec<usize>], ids: &[&str]) -> SchedulerResult<Vec<usize>> {
    let n = deps.len();
    let mut indegree: Vec<usize> = deps.iter().map(Vec::len).collect();
    let mut children: Vec<Vec<usize>> = vec![Vec::new(); n];
    for (node, parents) in deps.iter().enumerate() {
        for &parent in parents {
            children[parent].push(node);
        }
    }

    let mut queue: VecDeque<usize> = (0..n).filter(|&i| indegree[i] == 0).collect();
    let mut order = Vec::with_capacity(n);
    while let Some(node) = queue.pop_front() {
        order.push(node);
        for &child in &children[node] {
            indegree[child] -= 1;
            if indegree[child] == 0 {
                queue.push_back(child);
            }
        }
    }

    if order.len() < n {
        let blocked: Vec<&str> = (0..n)
            .filter(|&i| indegree[i] > 0)
            .map(|i| ids[i])
            .collect();
        return Err(SchedulerError::CycleDetected(blocked.join(", ")));
    }
    Ok(order)
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_failure::ErrorKind;
    use gsd_proto::gsd::v1::SubagentType;

    fn task(id: &str, deps: &[&str]) -> Task {
        Task {
            id: Some(TaskId {
                value: id.to_string(),
            }),
            subagent_type: Some(SubagentType {
                value: "worker".to_string(),
            }),
            dependencies: deps
                .iter()
                .map(|d| TaskId {
                    value: d.to_string(),
                })
                .collect(),
            ..Default::default()
        }
    }

    fn edge(from: &str, to: &str) -> Edge {
        Edge {
            from: Some(TaskId {
                value: from.to_string(),
            }),
            to: Some(TaskId {
                value: to.to_string(),
            }),
            condition: String::new(),
        }
    }

    #[test]
    fn cycle_maps_to_scheduler_cycle_and_e_dep() {
        // A two-task cycle declared through dependencies.
        let tasks = [task("a", &["b"]), task("b", &["a"])];
        let Err(err) = build_dag_from(&tasks, &[]) else {
            panic!("a dependency cycle must be rejected");
        };
        assert!(matches!(err, SchedulerError::CycleDetected(_)));
        assert_eq!(ErrorKind::CycleDetected.as_python_code(), "E-DEP");

        // A cycle formed only by edges, plus a self-loop.
        let tasks = [task("a", &[]), task("b", &[])];
        let edges = [edge("a", "b"), edge("b", "a")];
        let Err(err) = build_dag_from(&tasks, &edges) else {
            panic!("an edge cycle must be rejected");
        };
        assert!(matches!(err, SchedulerError::CycleDetected(_)));

        let Err(err) = build_dag_from(&[task("s", &[])], &[edge("s", "s")]) else {
            panic!("a self-loop must be rejected");
        };
        assert!(matches!(err, SchedulerError::CycleDetected(_)));
    }

    #[test]
    fn edges_become_dependencies_and_acyclic_graph_builds() {
        // Declared out of order: `c` depends on `b` via an edge, `b` on `a` via dependencies.
        let tasks = [task("c", &[]), task("b", &["a"]), task("a", &[])];
        let Ok(built) = build_dag_from(&tasks, &[edge("b", "c")]) else {
            panic!("an acyclic workflow must build");
        };
        assert_eq!(built.tasks.len(), 3);
        let Some(c) = built
            .tasks
            .iter()
            .find(|t| t.id.as_ref().is_some_and(|id| id.value == "c"))
        else {
            panic!("task c must be kept");
        };
        assert_eq!(c.dependencies.len(), 1);
        assert_eq!(c.dependencies[0].value, "b");
        assert_eq!(built.dag.detect_cycles(), None);
        let ready: Vec<String> = built
            .dag
            .get_ready_tasks()
            .into_iter()
            .map(|t| t.value)
            .collect();
        assert_eq!(ready, vec!["a".to_string()]);
    }

    #[test]
    fn unknown_and_missing_ids_are_rejected() {
        let Err(err) = build_dag_from(&[task("a", &["ghost"])], &[]) else {
            panic!("unknown dependency must be rejected");
        };
        assert!(matches!(err, SchedulerError::TaskNotFound(_)));

        let Err(err) = build_dag_from(&[task("a", &[]), task("a", &[])], &[]) else {
            panic!("duplicate ids must be rejected");
        };
        assert!(matches!(err, SchedulerError::InvalidDag(_)));

        let no_id = Task {
            id: None,
            ..Default::default()
        };
        let Err(err) = build_dag_from(&[no_id], &[]) else {
            panic!("a task without an id must be rejected");
        };
        assert!(matches!(err, SchedulerError::InvalidDag(_)));
    }

    #[test]
    fn workflow_def_ingests_tasks_and_edges() {
        let workflow = WorkflowDef {
            name: "wf".to_string(),
            tasks: vec![task("x", &[]), task("y", &[])],
            edges: vec![edge("x", "y")],
            ..Default::default()
        };
        let Ok(built) = build_dag(&workflow) else {
            panic!("workflow must build");
        };
        assert_eq!(built.dag.get_ready_tasks().len(), 1);
    }
}
