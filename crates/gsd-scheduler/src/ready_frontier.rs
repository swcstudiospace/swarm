//! Ready-frontier computation: which planned tasks may be dispatched now.
//!
//! [`ready_frontier`] is a pure function. It reads a snapshot of the workflow, the completed and
//! in-flight tasks, their file claims and per-agent session counts, and returns the task ids that
//! may start. It holds no state, performs no I/O, and never dispatches anything.
//!
//! A task is ready when all its dependencies are completed, it is neither completed nor in flight,
//! its agent has a free `max_parallel` slot, and its requested claims do not conflict with any
//! active claim or with a claim of a task admitted earlier in the same frontier.

use std::collections::{HashMap, HashSet};

use gsd_proto::gsd::v1::TaskId;

use crate::dag_build::BuiltDag;
use gsd_sched_core::claim_pattern::claims_conflict;
pub use gsd_sched_core::claim_pattern::{ClaimMode, PathClaim};

/// Snapshot the ready frontier is computed from. Every field is borrowed and never modified.
#[derive(Debug, Clone, Copy)]
pub struct FrontierInput<'a> {
    /// The validated workflow; tasks are read in workflow order.
    pub dag: &'a BuiltDag,
    /// Task ids that have finished. Their dependents become eligible.
    pub completed: &'a HashSet<String>,
    /// In-flight task id to the claims it holds. Every in-flight task must appear, even with no claims.
    pub active_claims: &'a HashMap<String, Vec<PathClaim>>,
    /// Task id to the claims it requests before it may start.
    pub requested_claims: &'a HashMap<String, Vec<PathClaim>>,
    /// Agent id to the number of its sessions already in flight.
    pub running_per_agent: &'a HashMap<String, usize>,
    /// Agent id to its `max_parallel`. An agent without an entry is limited to one session.
    pub agent_max_parallel: &'a HashMap<String, usize>,
    /// Global slots still free for new dispatches.
    pub free_slots: usize,
}

/// Returns the ids of tasks that may be dispatched now, in workflow order.
///
/// Admission is greedy in workflow order. Each admitted task reserves its agent slot and its
/// requested claims for the tasks after it, so two conflicting tasks are never both returned.
pub fn ready_frontier(input: &FrontierInput<'_>) -> Vec<TaskId> {
    let mut held: Vec<&PathClaim> = input.active_claims.values().flatten().collect();
    let mut running: HashMap<&str, usize> = input
        .running_per_agent
        .iter()
        .map(|(agent, count)| (agent.as_str(), *count))
        .collect();
    let mut slots = input.free_slots;
    let mut ready = Vec::new();

    for task in &input.dag.tasks {
        if slots == 0 {
            break;
        }
        let Some(tid) = task.id.as_ref() else {
            continue;
        };
        let id = tid.value.as_str();
        if id.is_empty()
            || input.completed.contains(id)
            || input.active_claims.contains_key(id)
            || !task
                .dependencies
                .iter()
                .all(|dep| input.completed.contains(&dep.value))
        {
            continue;
        }

        let agent = task.subagent_type.as_ref().map_or("", |s| s.value.as_str());
        let cap = input
            .agent_max_parallel
            .get(agent)
            .copied()
            .unwrap_or(1)
            .max(1);
        let used = running.get(agent).copied().unwrap_or(0);
        if used >= cap {
            continue;
        }

        let wanted: &[PathClaim] = input
            .requested_claims
            .get(id)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        if wanted
            .iter()
            .any(|claim| held.iter().any(|other| claims_conflict(claim, other)))
        {
            continue;
        }

        held.extend(wanted.iter());
        *running.entry(agent).or_insert(0) += 1;
        slots -= 1;
        ready.push(tid.clone());
    }
    ready
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dag_build::build_dag_from;
    use gsd_proto::gsd::v1::{SubagentType, Task};

    fn task(id: &str, agent: &str, deps: &[&str]) -> Task {
        Task {
            id: Some(TaskId {
                value: id.to_string(),
            }),
            subagent_type: Some(SubagentType {
                value: agent.to_string(),
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

    fn graph(tasks: &[Task]) -> BuiltDag {
        let Ok(built) = build_dag_from(tasks, &[]) else {
            panic!("fixture graph must build");
        };
        built
    }

    fn excl(pattern: &str) -> PathClaim {
        let Ok(claim) = PathClaim::new(pattern, ClaimMode::Exclusive) else {
            panic!("fixture claim must parse");
        };
        claim
    }

    fn ids(tasks: &[TaskId]) -> Vec<&str> {
        tasks.iter().map(|t| t.value.as_str()).collect()
    }

    #[test]
    fn overlapping_claims_are_not_both_ready() {
        let tasks = [
            task("a", "worker", &[]),
            task("b", "worker", &[]),
            task("c", "worker", &[]),
        ];
        let built = graph(&tasks);
        let completed = HashSet::new();
        let active = HashMap::new();
        let running = HashMap::new();
        let caps = HashMap::from([("worker".to_string(), 8)]);
        let requested = HashMap::from([
            ("a".to_string(), vec![excl("src/lib.rs")]),
            ("b".to_string(), vec![excl("src/lib.rs")]),
            ("c".to_string(), vec![excl("docs/")]),
        ]);
        let input = FrontierInput {
            dag: &built,
            completed: &completed,
            active_claims: &active,
            requested_claims: &requested,
            running_per_agent: &running,
            agent_max_parallel: &caps,
            free_slots: 10,
        };
        assert_eq!(ids(&ready_frontier(&input)), vec!["a", "c"]);
    }

    #[test]
    fn active_claims_exclude_overlapping_tasks() {
        let tasks = [task("a", "worker", &[]), task("b", "worker", &[])];
        let built = graph(&tasks);
        let completed = HashSet::new();
        let active = HashMap::from([("in-flight".to_string(), vec![excl("src/")])]);
        let running = HashMap::new();
        let caps = HashMap::from([("worker".to_string(), 8)]);
        let requested = HashMap::from([
            ("a".to_string(), vec![excl("src/lib.rs")]),
            ("b".to_string(), vec![excl("docs/readme.md")]),
        ]);
        let input = FrontierInput {
            dag: &built,
            completed: &completed,
            active_claims: &active,
            requested_claims: &requested,
            running_per_agent: &running,
            agent_max_parallel: &caps,
            free_slots: 10,
        };
        assert_eq!(ids(&ready_frontier(&input)), vec!["b"]);
    }

    #[test]
    fn dependencies_gate_readiness() {
        let tasks = [task("a", "worker", &[]), task("b", "worker", &["a"])];
        let built = graph(&tasks);
        let active = HashMap::new();
        let running = HashMap::new();
        let caps = HashMap::from([("worker".to_string(), 8)]);
        let requested = HashMap::new();
        let done: HashSet<String> = HashSet::new();
        let before = FrontierInput {
            dag: &built,
            completed: &done,
            active_claims: &active,
            requested_claims: &requested,
            running_per_agent: &running,
            agent_max_parallel: &caps,
            free_slots: 10,
        };
        assert_eq!(ids(&ready_frontier(&before)), vec!["a"]);
        let finished: HashSet<String> = ["a".to_string()].into_iter().collect();
        let after = FrontierInput {
            completed: &finished,
            ..before
        };
        assert_eq!(ids(&ready_frontier(&after)), vec!["b"]);
    }

    #[test]
    fn max_parallel_is_honoured() {
        let tasks = [
            task("a1", "worker", &[]),
            task("a2", "worker", &[]),
            task("a3", "worker", &[]),
            task("o1", "other", &[]),
            task("o2", "other", &[]),
        ];
        let built = graph(&tasks);
        let completed = HashSet::new();
        let active = HashMap::new();
        let requested = HashMap::new();
        let caps = HashMap::from([("worker".to_string(), 2)]);

        // Agent without a cap entry is limited to one session.
        let running = HashMap::new();
        let input = FrontierInput {
            dag: &built,
            completed: &completed,
            active_claims: &active,
            requested_claims: &requested,
            running_per_agent: &running,
            agent_max_parallel: &caps,
            free_slots: 10,
        };
        assert_eq!(ids(&ready_frontier(&input)), vec!["a1", "a2", "o1"]);

        // Sessions already in flight count against the cap.
        let running = HashMap::from([("worker".to_string(), 1)]);
        let input = FrontierInput {
            running_per_agent: &running,
            ..input
        };
        assert_eq!(ids(&ready_frontier(&input)), vec!["a1", "o1"]);

        // Global free slots bound the frontier.
        let running = HashMap::new();
        let input = FrontierInput {
            running_per_agent: &running,
            free_slots: 1,
            ..input
        };
        assert_eq!(ids(&ready_frontier(&input)), vec!["a1"]);
    }

    #[test]
    fn repeated_calls_are_identical_and_leave_input_unchanged() {
        let tasks = [
            task("a", "worker", &[]),
            task("b", "worker", &["a"]),
            task("c", "worker", &[]),
        ];
        let built = graph(&tasks);
        let completed: HashSet<String> = ["a".to_string()].into_iter().collect();
        let active = HashMap::from([("x".to_string(), vec![excl("ops/")])]);
        let running = HashMap::from([("worker".to_string(), 1)]);
        let caps = HashMap::from([("worker".to_string(), 3)]);
        let requested = HashMap::from([("c".to_string(), vec![excl("src/")])]);
        let input = FrontierInput {
            dag: &built,
            completed: &completed,
            active_claims: &active,
            requested_claims: &requested,
            running_per_agent: &running,
            agent_max_parallel: &caps,
            free_slots: 4,
        };
        let snapshot = (
            completed.clone(),
            active.clone(),
            running.clone(),
            requested.clone(),
        );
        let first = ready_frontier(&input);
        let second = ready_frontier(&input);
        assert_eq!(first, second);
        assert_eq!(ids(&first), vec!["b", "c"]);
        assert_eq!(snapshot, (completed, active, running, requested));
    }

    #[test]
    fn core_errors_surface_as_other_with_the_original_text() {
        fn bad_claim() -> crate::SchedulerResult<PathClaim> {
            Ok(PathClaim::new("../escape", ClaimMode::Exclusive)?)
        }
        fn bad_limiter() -> crate::SchedulerResult<crate::AimdLimiter> {
            Ok(crate::AimdLimiter::new(2, 3, std::time::Duration::ZERO)?)
        }
        let claim = bad_claim().expect_err("a parent-directory pattern is rejected");
        assert_eq!(
            claim.to_string(),
            r#"Other: invalid claim pattern: "../escape""#
        );
        let limiter = bad_limiter().expect_err("min above max is rejected");
        assert_eq!(
            limiter.to_string(),
            "Other: need 1 <= min <= max, got min=3 max=2"
        );
    }
}
