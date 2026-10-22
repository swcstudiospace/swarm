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
use crate::{SchedulerError, SchedulerResult};

/// Whether a claim reserves its paths for one task or only reads them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClaimMode {
    /// The claimed paths are reserved; any overlapping claim conflicts.
    Exclusive,
    /// The claimed paths may be shared with other shared claims.
    Shared,
}

/// A repo-relative path glob claimed by a task, normalized into segments.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PathClaim {
    segments: Vec<String>,
    mode: ClaimMode,
}

impl PathClaim {
    /// Parses `pattern` into a claim. A trailing `/` means the whole directory.
    ///
    /// Returns `Other` for an empty, absolute, drive-qualified, or `..`-containing pattern.
    pub fn new(pattern: &str, mode: ClaimMode) -> SchedulerResult<Self> {
        let normalized = pattern.trim().replace('\\', "/");
        let invalid = || SchedulerError::Other(format!("invalid claim pattern: {pattern:?}"));
        if normalized.is_empty() || normalized.starts_with('/') || is_drive_qualified(&normalized) {
            return Err(invalid());
        }
        let directory = normalized.ends_with('/');
        let mut segments: Vec<String> = Vec::new();
        for segment in normalized.split('/') {
            match segment {
                "" | "." => {}
                ".." => return Err(invalid()),
                "**" if segments.last().is_some_and(|last| last == "**") => {}
                other => segments.push(other.to_string()),
            }
        }
        if segments.is_empty() {
            return Err(invalid());
        }
        if directory && segments.last().is_some_and(|last| last != "**") {
            segments.push("**".to_string());
        }
        Ok(Self { segments, mode })
    }

    /// Returns the claim mode.
    pub fn mode(&self) -> ClaimMode {
        self.mode
    }
}

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

fn is_drive_qualified(pattern: &str) -> bool {
    let mut chars = pattern.chars();
    matches!((chars.next(), chars.next()), (Some(d), Some(':')) if d.is_ascii_alphabetic())
}

/// Segment lists a claim really reserves. An exclusive bare name also reserves everything below it.
fn footprints(claim: &PathClaim) -> Vec<Vec<&str>> {
    let own: Vec<&str> = claim.segments.iter().map(String::as_str).collect();
    let bare = claim
        .segments
        .last()
        .is_some_and(|last| !last.contains(['*', '?', '[']));
    if claim.mode == ClaimMode::Exclusive && bare {
        let mut below = own.clone();
        below.push("**");
        vec![own, below]
    } else {
        vec![own]
    }
}

/// Two claims conflict when they can match a common path and at least one is exclusive.
fn claims_conflict(a: &PathClaim, b: &PathClaim) -> bool {
    if a.mode == ClaimMode::Shared && b.mode == ClaimMode::Shared {
        return false;
    }
    let left = footprints(a);
    let right = footprints(b);
    left.iter()
        .any(|x| right.iter().any(|y| path_segments_overlap(x, y)))
}

/// Decides whether two segment lists can match a common path. `**` matches any number of segments.
fn path_segments_overlap(a: &[&str], b: &[&str]) -> bool {
    glob_overlap(
        a,
        b,
        |segment| *segment == "**",
        |x, y| segment_overlap(x, y),
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Token {
    Star,
    Any,
    Literal(char),
}

fn tokenize(segment: &str) -> Vec<Token> {
    let chars: Vec<char> = segment.chars().collect();
    let mut tokens = Vec::with_capacity(chars.len());
    let mut i = 0;
    while i < chars.len() {
        match chars[i] {
            '*' => {
                if tokens.last() != Some(&Token::Star) {
                    tokens.push(Token::Star);
                }
            }
            '?' => tokens.push(Token::Any),
            '[' => {
                if let Some(offset) = chars[i + 1..].iter().position(|&c| c == ']') {
                    tokens.push(Token::Any);
                    i += offset + 2;
                    continue;
                }
                tokens.push(Token::Literal('['));
            }
            other => tokens.push(Token::Literal(other)),
        }
        i += 1;
    }
    tokens
}

/// Decides whether two single-segment globs can match a common name. Character classes count as `?`.
fn segment_overlap(a: &str, b: &str) -> bool {
    if a == b {
        return true;
    }
    glob_overlap(
        &tokenize(a),
        &tokenize(b),
        |token| *token == Token::Star,
        |x, y| match (x, y) {
            (Token::Any, _) | (_, Token::Any) => true,
            (Token::Literal(p), Token::Literal(q)) => p == q,
            _ => false,
        },
    )
}

/// Bottom-up table: `ok[i][j]` is whether `a[i..]` and `b[j..]` can match a common sequence.
fn glob_overlap<T: Copy>(
    a: &[T],
    b: &[T],
    is_star: impl Fn(&T) -> bool,
    can_match: impl Fn(&T, &T) -> bool,
) -> bool {
    let (n, m) = (a.len(), b.len());
    let width = m + 1;
    let mut ok = vec![false; (n + 1) * width];
    ok[n * width + m] = true;
    for i in (0..=n).rev() {
        for j in (0..=m).rev() {
            if i == n && j == m {
                continue;
            }
            let a_star = i < n && is_star(&a[i]);
            let b_star = j < m && is_star(&b[j]);
            let mut hit = false;
            if a_star {
                hit |= ok[(i + 1) * width + j] || (j < m && ok[i * width + j + 1]);
            }
            if b_star {
                hit |= ok[i * width + j + 1] || (i < n && ok[(i + 1) * width + j]);
            }
            if i < n && j < m && !a_star && !b_star && can_match(&a[i], &b[j]) {
                hit |= ok[(i + 1) * width + j + 1];
            }
            ok[i * width + j] = hit;
        }
    }
    ok[0]
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

    fn shared(pattern: &str) -> PathClaim {
        let Ok(claim) = PathClaim::new(pattern, ClaimMode::Shared) else {
            panic!("fixture claim must parse");
        };
        claim
    }

    fn ids(tasks: &[TaskId]) -> Vec<&str> {
        tasks.iter().map(|t| t.value.as_str()).collect()
    }

    #[test]
    fn claim_patterns_are_validated() {
        assert!(PathClaim::new("../escape", ClaimMode::Exclusive).is_err());
        assert!(PathClaim::new("/abs/path", ClaimMode::Exclusive).is_err());
        assert!(PathClaim::new("C:/win", ClaimMode::Exclusive).is_err());
        assert!(PathClaim::new("  ", ClaimMode::Exclusive).is_err());
        assert!(PathClaim::new("src/", ClaimMode::Exclusive).is_ok());
    }

    #[test]
    fn overlap_rules_follow_claim_modes() {
        // Two shared claims never conflict.
        assert!(!claims_conflict(
            &shared("src/lib.rs"),
            &shared("src/lib.rs")
        ));
        // A shared claim against an exclusive one conflicts when the paths overlap.
        assert!(claims_conflict(&shared("src/lib.rs"), &excl("src/*.rs")));
        // An exclusive bare name reserves everything beneath it.
        assert!(claims_conflict(&excl("src"), &excl("src/a.rs")));
        // Directory claims cover their contents.
        assert!(claims_conflict(&excl("src/"), &excl("src/deep/b.rs")));
        // Disjoint paths do not conflict.
        assert!(!claims_conflict(&excl("docs/"), &excl("src/")));
        assert!(!claims_conflict(&excl("src/a.rs"), &excl("src/b.rs")));
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
}
