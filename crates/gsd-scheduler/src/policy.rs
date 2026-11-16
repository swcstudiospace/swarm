//! Ordering policies that rank ready tasks for dispatch.

use gsd_proto::gsd::v1::Task;

use crate::dag::TaskDag;

/// Dispatch rank of a task. Larger values dispatch first.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct PriorityKey {
    /// Task priority field; a higher value is more urgent.
    pub priority: i32,
    /// Length of the longest dependent chain below the task, counting the task.
    pub critical_path: usize,
}

/// Ranks tasks for dispatch.
pub trait SchedulingPolicyTrait: Send + Sync {
    /// Returns the dispatch rank of `task` within `dag`.
    fn priority(&self, task: &Task, dag: &TaskDag) -> PriorityKey;
}

/// Default policy: task priority first, then critical-path length.
#[derive(Debug, Default, Clone, Copy)]
pub struct CriticalPathThroughputHybridPolicy;

impl CriticalPathThroughputHybridPolicy {
    /// Creates the default policy.
    pub fn new() -> Self {
        Self
    }
}

impl SchedulingPolicyTrait for CriticalPathThroughputHybridPolicy {
    fn priority(&self, task: &Task, dag: &TaskDag) -> PriorityKey {
        let critical_path = task
            .id
            .as_ref()
            .map_or(0, |id| dag.critical_path_len(&id.value));
        PriorityKey {
            priority: task.priority,
            critical_path,
        }
    }
}
