//! U3.1 session create: `SessionState` initialisation, per-thread capability bind and governor reservation.
//!
//! A new session has empty `threads` and `branches` and a [`BudgetSnapshot`] that starts at the full
//! workflow budget. Each thread is bound once to one workflow task and one [`CapabilityManifest`];
//! the bind reserves the thread's budget through [`gsd_governor::Governor`]. Recovery, resume,
//! snapshot and retention semantics are not implemented here: their answers are not in the repo.

use std::collections::HashMap;

use gsd_governor::Governor;
use gsd_pool::ThreadPhase;
use gsd_proto::gsd::v1::{
    Budget, BudgetGrant, BudgetRequest, BudgetSnapshot, CapabilityManifest, ChatThreadId,
    SessionId, SessionState, TaskId, ThreadState, ThreadStateKind, WorkflowDef,
};
use gsd_scheduler::build_dag;

/// Metadata key on a [`ThreadState`] holding its [`ThreadPhase`] name, such as `IDLE`.
///
/// The proto `ThreadStateKind` has no `QUEUED` or `BLOCKED`, so the finer phase lives here.
pub const PHASE_KEY: &str = "phase";
/// Metadata key on a [`ThreadState`] holding the workflow task id the thread serves.
pub const TASK_KEY: &str = "task_id";
/// Most threads one session may bind. Equal to the pool's `max_threads` default.
pub const MAX_THREADS_PER_SESSION: usize = 30;

/// Result type for session creation and thread binding.
pub type CreateResult<T> = Result<T, CreateError>;

/// Typed refusal from session creation or thread binding.
#[derive(Debug, thiserror::Error)]
pub enum CreateError {
    /// The session id is empty.
    #[error("session id is missing or empty")]
    MissingSessionId,
    /// The session carries no workflow.
    #[error("session has no workflow")]
    MissingWorkflow,
    /// The workflow carries no budget, so no snapshot can be initialised.
    #[error("workflow has no budget")]
    MissingBudget,
    /// The workflow failed DAG validation.
    #[error("invalid workflow: {0}")]
    InvalidWorkflow(String),
    /// The thread was already bound; a thread binds once.
    #[error("thread {0} is already bound")]
    AlreadyBound(String),
    /// The session already binds the maximum number of threads.
    #[error("session already binds {0} threads; the per-session cap is 30")]
    ThreadCapExceeded(usize),
    /// The bound task is not a task of the workflow.
    #[error("task {0} is not in the workflow")]
    UnknownTask(String),
    /// The task is already served by another thread.
    #[error("task {0} is already served by thread {1}")]
    TaskAlreadyBound(String, String),
    /// The manifest's subagent type differs from the task's subagent type.
    #[error("manifest subagent {manifest_agent:?} does not match the subagent of task {task}")]
    ManifestMismatch {
        /// Task the thread was asked to serve.
        task: String,
        /// Subagent type named by the manifest.
        manifest_agent: String,
    },
    /// A reservation amount is negative or not finite.
    #[error("invalid reservation: amounts must be finite and non-negative")]
    InvalidReservation,
    /// The reservation exceeds the budget, or the governor refused it.
    #[error("budget refused for thread {thread}: {reason}")]
    BudgetRefused {
        /// Thread whose reservation was refused.
        thread: String,
        /// Why the reservation was refused.
        reason: String,
    },
}

/// Binding of one thread: the workflow task it serves, its capability manifest and its budget request.
#[derive(Debug, Clone)]
pub struct ThreadBinding {
    /// Thread to bind. A thread binds once.
    pub thread_id: ChatThreadId,
    /// Workflow task the thread serves. A task is served by at most one thread.
    pub task_id: TaskId,
    /// Capability manifest. Its subagent type must equal the task's subagent type.
    pub manifest: CapabilityManifest,
    /// Amounts to reserve. `session_id` and `thread_id` are set by [`bind_thread`].
    pub reservation: BudgetRequest,
}

/// Returns the budget snapshot with nothing used and every limit of `budget` remaining.
pub fn initial_snapshot(budget: &Budget) -> BudgetSnapshot {
    BudgetSnapshot {
        tokens_remaining: budget.max_tokens,
        wall_time_ms_remaining: budget.max_wall_time_ms,
        tool_calls_remaining: budget.max_tool_calls,
        cost_usd_remaining: budget.max_cost_usd,
        ..Default::default()
    }
}

/// Initialises a session for `workflow`: empty threads and branches, and a budget snapshot that
/// starts at the workflow budget. The workflow is validated with the B3 DAG builder.
pub fn create_session(session_id: SessionId, workflow: WorkflowDef) -> CreateResult<SessionState> {
    if session_id.value.is_empty() {
        return Err(CreateError::MissingSessionId);
    }
    build_dag(&workflow).map_err(|e| CreateError::InvalidWorkflow(e.to_string()))?;
    let snapshot = initial_snapshot(
        workflow
            .budgets
            .as_ref()
            .ok_or(CreateError::MissingBudget)?,
    );
    Ok(SessionState {
        session_id: Some(session_id),
        workflow: Some(workflow),
        budget_snapshot: Some(snapshot),
        ..Default::default()
    })
}

/// Binds `binding` to `state`: checks the bind, reserves the thread's budget through `governor`,
/// charges the session snapshot, and inserts the thread in phase `IDLE`.
///
/// Refuses, without reserving and without changing `state`, a second bind of the same thread, a
/// thirty-first thread, a task that is absent or already served, a manifest whose subagent type
/// differs from the task's, and a reservation the snapshot or the governor refuses.
pub async fn bind_thread(
    state: &mut SessionState,
    governor: &Governor,
    binding: ThreadBinding,
) -> CreateResult<()> {
    check_bind_allowed(state, &binding)?;
    let session_id = state
        .session_id
        .clone()
        .ok_or(CreateError::MissingSessionId)?;
    let thread_key = binding.thread_id.value.clone();
    let snapshot = state
        .budget_snapshot
        .clone()
        .ok_or(CreateError::MissingBudget)?;

    let mut request = binding.reservation.clone();
    request.session_id = Some(session_id);
    request.thread_id = Some(binding.thread_id.clone());
    validate_reservation(&request)?;
    check_within_snapshot(&snapshot, &request, &thread_key)?;

    let grant = governor.reserve_budget(request).await;
    if !grant.granted {
        return Err(CreateError::BudgetRefused {
            thread: thread_key,
            reason: "the governor refused the reservation".to_string(),
        });
    }

    let mut charged = snapshot;
    charge(&mut charged, &grant);
    state.budget_snapshot = Some(charged);
    state.threads.insert(thread_key, new_thread(binding));
    Ok(())
}

/// Returns the key of the thread that serves `task_id`, if one is bound.
pub(crate) fn thread_for_task(state: &SessionState, task_id: &str) -> Option<String> {
    state
        .threads
        .iter()
        .find(|(_, thread)| thread.metadata.get(TASK_KEY).map(String::as_str) == Some(task_id))
        .map(|(key, _)| key.clone())
}

fn check_bind_allowed(state: &SessionState, binding: &ThreadBinding) -> CreateResult<()> {
    let thread = binding.thread_id.value.as_str();
    if state.threads.contains_key(thread) {
        return Err(CreateError::AlreadyBound(thread.to_string()));
    }
    if state.threads.len() >= MAX_THREADS_PER_SESSION {
        return Err(CreateError::ThreadCapExceeded(state.threads.len()));
    }

    let workflow = state
        .workflow
        .as_ref()
        .ok_or(CreateError::MissingWorkflow)?;
    let task_id = binding.task_id.value.as_str();
    let task = workflow
        .tasks
        .iter()
        .find(|task| task.id.as_ref().is_some_and(|id| id.value == task_id))
        .ok_or_else(|| CreateError::UnknownTask(task_id.to_string()))?;

    let task_agent = task.subagent_type.as_ref().map_or("", |s| s.value.as_str());
    let manifest_agent = binding
        .manifest
        .subagent_type
        .as_ref()
        .map_or("", |s| s.value.as_str());
    if manifest_agent.is_empty() || manifest_agent != task_agent {
        return Err(CreateError::ManifestMismatch {
            task: task_id.to_string(),
            manifest_agent: manifest_agent.to_string(),
        });
    }

    if let Some(other) = thread_for_task(state, task_id) {
        return Err(CreateError::TaskAlreadyBound(task_id.to_string(), other));
    }
    Ok(())
}

fn validate_reservation(request: &BudgetRequest) -> CreateResult<()> {
    let negative = request.tokens < 0
        || request.wall_time_ms < 0
        || request.tool_calls < 0
        || request.cost_usd < 0.0;
    if negative || !request.cost_usd.is_finite() {
        return Err(CreateError::InvalidReservation);
    }
    Ok(())
}

fn check_within_snapshot(
    snapshot: &BudgetSnapshot,
    request: &BudgetRequest,
    thread: &str,
) -> CreateResult<()> {
    let exceeds = request.tokens > snapshot.tokens_remaining
        || request.wall_time_ms > snapshot.wall_time_ms_remaining
        || request.tool_calls > snapshot.tool_calls_remaining
        || request.cost_usd > snapshot.cost_usd_remaining;
    if exceeds {
        return Err(CreateError::BudgetRefused {
            thread: thread.to_string(),
            reason: "the reservation exceeds the session budget remaining".to_string(),
        });
    }
    Ok(())
}

fn charge(snapshot: &mut BudgetSnapshot, grant: &BudgetGrant) {
    snapshot.tokens_used = snapshot.tokens_used.saturating_add(grant.tokens_granted);
    snapshot.tokens_remaining = snapshot
        .tokens_remaining
        .saturating_sub(grant.tokens_granted);
    snapshot.wall_time_ms_used = snapshot
        .wall_time_ms_used
        .saturating_add(grant.wall_time_ms_granted);
    snapshot.wall_time_ms_remaining = snapshot
        .wall_time_ms_remaining
        .saturating_sub(grant.wall_time_ms_granted);
    snapshot.tool_calls_used = snapshot
        .tool_calls_used
        .saturating_add(grant.tool_calls_granted);
    snapshot.tool_calls_remaining = snapshot
        .tool_calls_remaining
        .saturating_sub(grant.tool_calls_granted);
    snapshot.cost_usd_used += grant.cost_usd_granted;
    snapshot.cost_usd_remaining -= grant.cost_usd_granted;
}

fn new_thread(binding: ThreadBinding) -> ThreadState {
    let mut metadata = HashMap::new();
    metadata.insert(
        PHASE_KEY.to_string(),
        ThreadPhase::Idle.as_str().to_string(),
    );
    metadata.insert(TASK_KEY.to_string(), binding.task_id.value.clone());
    ThreadState {
        id: Some(binding.thread_id),
        subagent_type: binding.manifest.subagent_type.clone(),
        state: ThreadStateKind::Idle as i32,
        capability_manifest: Some(binding.manifest),
        metadata,
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_governor::GovernorConfig;
    use gsd_pool::PoolConfig;
    use gsd_proto::gsd::v1::{SubagentType, Task};

    fn task(id: &str, agent: &str) -> Task {
        Task {
            id: Some(TaskId {
                value: id.to_string(),
            }),
            subagent_type: Some(SubagentType {
                value: agent.to_string(),
            }),
            ..Default::default()
        }
    }

    fn workflow(tasks: Vec<Task>, max_tokens: i64) -> WorkflowDef {
        WorkflowDef {
            name: "wf".to_string(),
            tasks,
            budgets: Some(Budget {
                max_tokens,
                max_wall_time_ms: 60_000,
                max_tool_calls: 10,
                max_cost_usd: 5.0,
            }),
            ..Default::default()
        }
    }

    fn session(tasks: Vec<Task>, max_tokens: i64) -> CreateResult<SessionState> {
        create_session(
            SessionId {
                value: "s1".to_string(),
            },
            workflow(tasks, max_tokens),
        )
    }

    fn manifest(agent: &str) -> CapabilityManifest {
        CapabilityManifest {
            subagent_type: Some(SubagentType {
                value: agent.to_string(),
            }),
            ..Default::default()
        }
    }

    fn binding(thread: &str, task: &str, agent: &str, tokens: i64) -> ThreadBinding {
        ThreadBinding {
            thread_id: ChatThreadId {
                value: thread.to_string(),
            },
            task_id: TaskId {
                value: task.to_string(),
            },
            manifest: manifest(agent),
            reservation: BudgetRequest {
                tokens,
                ..Default::default()
            },
        }
    }

    #[test]
    fn new_session_starts_with_no_threads_and_the_budget_snapshot() -> CreateResult<()> {
        let state = session(vec![task("a", "w")], 500)?;
        assert_eq!(
            state.session_id.as_ref().map(|s| s.value.as_str()),
            Some("s1")
        );
        assert_eq!(state.workflow.as_ref().map(|w| w.name.as_str()), Some("wf"));
        assert!(state.threads.is_empty());
        assert!(state.branches.is_empty());
        assert_eq!(
            state.budget_snapshot.as_ref().map(|s| s.tokens_used),
            Some(0)
        );
        assert_eq!(
            state.budget_snapshot.as_ref().map(|s| s.tokens_remaining),
            Some(500)
        );
        assert_eq!(
            state
                .budget_snapshot
                .as_ref()
                .map(|s| s.tool_calls_remaining),
            Some(10)
        );
        Ok(())
    }

    #[test]
    fn create_rejects_a_workflow_without_budget() {
        let mut wf = workflow(vec![task("a", "w")], 1);
        wf.budgets = None;
        let refused = create_session(
            SessionId {
                value: "s1".to_string(),
            },
            wf,
        );
        assert!(matches!(refused, Err(CreateError::MissingBudget)));
    }

    #[test]
    fn create_rejects_an_empty_session_id() {
        let refused = create_session(
            SessionId {
                value: String::new(),
            },
            workflow(vec![task("a", "w")], 1),
        );
        assert!(matches!(refused, Err(CreateError::MissingSessionId)));
    }

    #[tokio::test]
    async fn manifest_binds_once_per_thread() -> CreateResult<()> {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = session(vec![task("a", "w"), task("b", "w")], 1_000)?;
        bind_thread(&mut state, &governor, binding("t1", "a", "w", 1)).await?;
        assert!(state
            .threads
            .get("t1")
            .is_some_and(|t| t.capability_manifest.is_some()));

        let again = bind_thread(&mut state, &governor, binding("t1", "b", "w", 1)).await;
        assert!(matches!(again, Err(CreateError::AlreadyBound(id)) if id == "t1"));

        let second_thread_same_task =
            bind_thread(&mut state, &governor, binding("t2", "a", "w", 1)).await;
        assert!(matches!(
            second_thread_same_task,
            Err(CreateError::TaskAlreadyBound(_, _))
        ));

        let wrong_agent = bind_thread(&mut state, &governor, binding("t3", "b", "other", 1)).await;
        assert!(matches!(
            wrong_agent,
            Err(CreateError::ManifestMismatch { .. })
        ));

        assert_eq!(state.threads.len(), 1);
        Ok(())
    }

    #[tokio::test]
    async fn granted_reservation_charges_the_snapshot() -> CreateResult<()> {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = session(vec![task("a", "w")], 100)?;
        bind_thread(&mut state, &governor, binding("t1", "a", "w", 40)).await?;
        let snapshot = state.budget_snapshot.clone().unwrap_or_default();
        assert_eq!(snapshot.tokens_used, 40);
        assert_eq!(snapshot.tokens_remaining, 60);
        Ok(())
    }

    #[tokio::test]
    async fn reservation_over_budget_is_refused_with_a_typed_error() -> CreateResult<()> {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = session(vec![task("a", "w")], 100)?;
        let refused = bind_thread(&mut state, &governor, binding("t1", "a", "w", 500)).await;
        assert!(matches!(refused, Err(CreateError::BudgetRefused { .. })));
        assert!(state.threads.is_empty());
        assert_eq!(
            state.budget_snapshot.as_ref().map(|s| s.tokens_used),
            Some(0)
        );
        Ok(())
    }

    #[tokio::test]
    async fn governor_refusal_is_reported_as_budget_refused() -> CreateResult<()> {
        // The workflow budget admits 200 000 tokens, but the governor's session cap is 100 000
        // (and its per-minute token rate limit is 100 000), so the governor refuses the grant.
        let governor = Governor::new(GovernorConfig::default());
        let mut state = session(vec![task("a", "w")], 1_000_000)?;
        let refused = bind_thread(&mut state, &governor, binding("t1", "a", "w", 200_000)).await;
        assert!(matches!(refused, Err(CreateError::BudgetRefused { .. })));
        assert!(state.threads.is_empty());
        assert_eq!(
            state.budget_snapshot.as_ref().map(|s| s.tokens_used),
            Some(0)
        );
        Ok(())
    }

    #[tokio::test]
    async fn thirty_first_thread_is_refused() -> CreateResult<()> {
        let tasks: Vec<Task> = (0..=MAX_THREADS_PER_SESSION)
            .map(|i| task(&format!("t{i}"), "w"))
            .collect();
        let governor = Governor::new(GovernorConfig::default());
        let mut state = session(tasks, 1_000)?;
        for i in 0..MAX_THREADS_PER_SESSION {
            bind_thread(
                &mut state,
                &governor,
                binding(&format!("c{i}"), &format!("t{i}"), "w", 0),
            )
            .await?;
        }
        let refused = bind_thread(&mut state, &governor, binding("c30", "t30", "w", 0)).await;
        assert!(matches!(refused, Err(CreateError::ThreadCapExceeded(30))));
        assert_eq!(state.threads.len(), MAX_THREADS_PER_SESSION);
        Ok(())
    }

    #[test]
    fn session_cap_matches_the_pool_default() {
        assert_eq!(MAX_THREADS_PER_SESSION, PoolConfig::default().max_threads);
    }
}
