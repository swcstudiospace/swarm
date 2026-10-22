//! U3.2 dispatch: hands the session's workflow to the B4 dispatcher and moves each thread
//! QUEUED to RUNNING through the B1 transition table in `gsd-pool`.
//!
//! The ready frontier is computed inside [`gsd_scheduler::run_force_parallel`]; this module adds no
//! frontier logic of its own. Each task runs on the thread bound to it. Before the worker starts,
//! the thread is admitted (IDLE to QUEUED, when the dispatcher hands it a task) and then handed off
//! (QUEUED to RUNNING). Terminal transitions and join belong to U3.4 and are not implemented here.

use std::future::Future;
use std::sync::Arc;

use gsd_pool::thread_state_transition_table::ALL_PHASES;
use gsd_pool::{transition, PoolError, ThreadPhase};
use gsd_proto::gsd::v1::{SessionState, Task, ThreadState, ThreadStateKind};
use gsd_scheduler::{
    build_dag, run_force_parallel, DispatchConfig, DispatchReport, SchedulerError, TaskFailure,
};
use parking_lot::Mutex;

use crate::session_create::{thread_for_task, PHASE_KEY};

/// Result type for dispatch operations.
pub type DispatchResult<T> = Result<T, DispatchError>;

/// Typed refusal or failure from dispatch.
#[derive(Debug, thiserror::Error)]
pub enum DispatchError {
    /// The session carries no workflow.
    #[error("session has no workflow")]
    MissingWorkflow,
    /// A workflow task has no bound thread. No thread is dispatched without a capability bind.
    #[error("task {0} has no bound thread; no thread is dispatched without a capability bind")]
    Unbound(String),
    /// The named thread is not in the session.
    #[error("thread {0} is not in the session")]
    UnknownThread(String),
    /// The thread has no capability manifest bound.
    #[error("thread {0} has no capability manifest")]
    MissingManifest(String),
    /// The thread's stored phase is not a known [`ThreadPhase`] name.
    #[error("thread {thread} carries unknown phase {value:?}")]
    UnknownPhase {
        /// Thread whose phase could not be read.
        thread: String,
        /// Raw metadata value.
        value: String,
    },
    /// A requested phase change is not in the B1 transition table.
    #[error("thread phase: {0}")]
    Phase(#[from] PoolError),
    /// The B3 DAG builder or the B4 dispatcher refused the workflow.
    #[error("scheduler: {0}")]
    Scheduler(#[from] SchedulerError),
}

/// Admits thread `thread_id` from IDLE to QUEUED: its task is admitted to the dispatcher.
///
/// Legality is decided by [`gsd_pool::transition`]; any other starting phase is refused.
pub fn admit_thread(state: &mut SessionState, thread_id: &str) -> DispatchResult<()> {
    let thread = thread_mut(state, thread_id)?;
    move_phase(thread, thread_id, ThreadPhase::Queued).map(|_| ())
}

/// Moves a QUEUED thread to RUNNING through the B1 transition table and records the `RUNNING` kind.
///
/// Refuses a thread that is not QUEUED (IDLE, RUNNING, BLOCKED or terminal) and a thread without a
/// capability manifest. A refused call leaves the thread unchanged.
pub fn handoff_thread(state: &mut SessionState, thread_id: &str) -> DispatchResult<()> {
    let thread = thread_mut(state, thread_id)?;
    if thread.capability_manifest.is_none() {
        return Err(DispatchError::MissingManifest(thread_id.to_string()));
    }
    move_phase(thread, thread_id, ThreadPhase::Running)?;
    thread.state = ThreadStateKind::Running as i32;
    Ok(())
}

/// Dispatches every task of the session's workflow through the B4 dispatcher and returns its report.
///
/// Before any task runs, every workflow task must have a bound thread; otherwise the call fails with
/// [`DispatchError::Unbound`] and no worker is started. `worker` runs each task after its thread has
/// been admitted and handed off to RUNNING. A start failure fails only that task, as
/// [`TaskFailure::Exception`], and the worker is not called. Each phase change is written to
/// `session` under its lock as it happens.
pub async fn dispatch_session<W, Fut>(
    session: Arc<Mutex<SessionState>>,
    config: DispatchConfig,
    worker: W,
) -> DispatchResult<DispatchReport>
where
    W: Fn(Task) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = Result<(), TaskFailure>> + Send + 'static,
{
    let built = {
        let guard = session.lock();
        let workflow = guard
            .workflow
            .as_ref()
            .ok_or(DispatchError::MissingWorkflow)?;
        let built = build_dag(workflow)?;
        for task in &built.tasks {
            let id = task_id_of(task);
            if thread_for_task(&guard, id).is_none() {
                return Err(DispatchError::Unbound(id.to_string()));
            }
        }
        built
    };

    let worker = Arc::new(worker);
    let wrapped = move |task: Task| {
        let session = Arc::clone(&session);
        let worker = Arc::clone(&worker);
        async move {
            if let Err(failure) = start_task(&session, task_id_of(&task)) {
                return Err(failure);
            }
            (*worker)(task).await
        }
    };
    let report = run_force_parallel(built, config, wrapped).await?;
    Ok(report)
}

/// Admits and hands off the thread serving `task_id`, as the dispatcher's worker wrapper does.
fn start_task(session: &Mutex<SessionState>, task_id: &str) -> Result<(), TaskFailure> {
    let mut guard = session.lock();
    start_thread(&mut guard, task_id).map_err(|e| TaskFailure::Exception(e.to_string()))
}

fn start_thread(state: &mut SessionState, task_id: &str) -> DispatchResult<()> {
    let thread_id = thread_for_task(state, task_id)
        .ok_or_else(|| DispatchError::Unbound(task_id.to_string()))?;
    let phase = phase_of(thread_ref(state, &thread_id)?, &thread_id)?;
    if phase == ThreadPhase::Idle {
        admit_thread(state, &thread_id)?;
    }
    handoff_thread(state, &thread_id)
}

fn move_phase(
    thread: &mut ThreadState,
    thread_id: &str,
    to: ThreadPhase,
) -> DispatchResult<ThreadPhase> {
    let from = phase_of(thread, thread_id)?;
    let next = transition(from, to)?;
    thread
        .metadata
        .insert(PHASE_KEY.to_string(), next.as_str().to_string());
    Ok(next)
}

fn phase_of(thread: &ThreadState, thread_id: &str) -> DispatchResult<ThreadPhase> {
    let raw = thread.metadata.get(PHASE_KEY).map_or("", String::as_str);
    ALL_PHASES
        .into_iter()
        .find(|phase| phase.as_str() == raw)
        .ok_or_else(|| DispatchError::UnknownPhase {
            thread: thread_id.to_string(),
            value: raw.to_string(),
        })
}

fn thread_ref<'a>(state: &'a SessionState, thread_id: &str) -> DispatchResult<&'a ThreadState> {
    state
        .threads
        .get(thread_id)
        .ok_or_else(|| DispatchError::UnknownThread(thread_id.to_string()))
}

fn thread_mut<'a>(
    state: &'a mut SessionState,
    thread_id: &str,
) -> DispatchResult<&'a mut ThreadState> {
    state
        .threads
        .get_mut(thread_id)
        .ok_or_else(|| DispatchError::UnknownThread(thread_id.to_string()))
}

fn task_id_of(task: &Task) -> &str {
    task.id.as_ref().map_or("", |id| id.value.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::session_create::{bind_thread, create_session, ThreadBinding};
    use gsd_governor::{Governor, GovernorConfig};
    use gsd_proto::gsd::v1::{
        Budget, BudgetRequest, CapabilityManifest, ChatThreadId, SessionId, SubagentType, TaskId,
        WorkflowDef,
    };
    use std::sync::atomic::{AtomicUsize, Ordering};

    type TestResult = Result<(), Box<dyn std::error::Error>>;

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

    fn workflow(tasks: Vec<Task>) -> WorkflowDef {
        WorkflowDef {
            name: "wf".to_string(),
            tasks,
            budgets: Some(Budget {
                max_tokens: 10_000,
                max_wall_time_ms: 60_000,
                max_tool_calls: 100,
                max_cost_usd: 10.0,
            }),
            ..Default::default()
        }
    }

    /// Creates a session over `tasks` and binds each `(thread, task)` pair with agent `w`.
    async fn bound_session(
        tasks: Vec<Task>,
        pairs: &[(&str, &str)],
    ) -> Result<SessionState, Box<dyn std::error::Error>> {
        let mut state = create_session(
            SessionId {
                value: "s1".to_string(),
            },
            workflow(tasks),
        )?;
        let governor = Governor::new(GovernorConfig::default());
        for (thread, task_id) in pairs.iter() {
            bind_thread(
                &mut state,
                &governor,
                ThreadBinding {
                    thread_id: ChatThreadId {
                        value: thread.to_string(),
                    },
                    task_id: TaskId {
                        value: task_id.to_string(),
                    },
                    manifest: CapabilityManifest {
                        subagent_type: Some(SubagentType {
                            value: "w".to_string(),
                        }),
                        ..Default::default()
                    },
                    reservation: BudgetRequest {
                        tokens: 1,
                        ..Default::default()
                    },
                },
            )
            .await?;
        }
        Ok(state)
    }

    fn phase_of_task(state: &SessionState, task_id: &str) -> String {
        thread_for_task(state, task_id)
            .and_then(|key| state.threads.get(&key))
            .and_then(|thread| thread.metadata.get(PHASE_KEY).cloned())
            .unwrap_or_default()
    }

    #[tokio::test]
    async fn idle_thread_cannot_skip_straight_to_running() -> TestResult {
        let mut state = bound_session(vec![task("a", "w", &[])], &[("t1", "a")]).await?;
        let refused = handoff_thread(&mut state, "t1");
        assert!(matches!(
            refused,
            Err(DispatchError::Phase(PoolError::InvalidState(_)))
        ));
        assert_eq!(phase_of_task(&state, "a"), "IDLE");
        Ok(())
    }

    #[tokio::test]
    async fn queued_thread_hands_off_to_running_once() -> TestResult {
        let mut state = bound_session(vec![task("a", "w", &[])], &[("t1", "a")]).await?;
        admit_thread(&mut state, "t1")?;
        assert_eq!(phase_of_task(&state, "a"), "QUEUED");
        handoff_thread(&mut state, "t1")?;
        assert_eq!(phase_of_task(&state, "a"), "RUNNING");
        assert_eq!(
            state.threads.get("t1").map(|t| t.state),
            Some(ThreadStateKind::Running as i32)
        );
        let again = handoff_thread(&mut state, "t1");
        assert!(matches!(
            again,
            Err(DispatchError::Phase(PoolError::InvalidState(_)))
        ));
        assert_eq!(phase_of_task(&state, "a"), "RUNNING");
        Ok(())
    }

    #[tokio::test]
    async fn terminal_thread_cannot_be_handed_off() -> TestResult {
        let mut state = bound_session(vec![task("a", "w", &[])], &[("t1", "a")]).await?;
        if let Some(thread) = state.threads.get_mut("t1") {
            thread
                .metadata
                .insert(PHASE_KEY.to_string(), "FAILED".to_string());
        }
        let refused = handoff_thread(&mut state, "t1");
        assert!(matches!(
            &refused,
            Err(DispatchError::Phase(PoolError::InvalidState(msg))) if msg.contains("FAILED -> RUNNING")
        ));
        assert_eq!(phase_of_task(&state, "a"), "FAILED");
        Ok(())
    }

    #[tokio::test]
    async fn dispatch_runs_each_task_only_after_its_thread_is_running() -> TestResult {
        let state = bound_session(
            vec![task("a", "w", &[]), task("b", "w", &["a"])],
            &[("t1", "a"), ("t2", "b")],
        )
        .await?;
        let session = Arc::new(Mutex::new(state));
        let observed: Arc<Mutex<Vec<(String, String)>>> = Arc::new(Mutex::new(Vec::new()));
        let seen_session = Arc::clone(&session);
        let seen = Arc::clone(&observed);

        let report = dispatch_session(
            Arc::clone(&session),
            DispatchConfig::default(),
            move |task: Task| {
                let session = Arc::clone(&seen_session);
                let seen = Arc::clone(&seen);
                async move {
                    let id = task_id_of(&task).to_string();
                    let phase = phase_of_task(&session.lock(), &id);
                    seen.lock().push((id, phase));
                    Ok::<(), TaskFailure>(())
                }
            },
        )
        .await?;

        assert_eq!(
            report.dispatch_order,
            vec!["a".to_string(), "b".to_string()]
        );
        assert_eq!(
            *observed.lock(),
            vec![
                ("a".to_string(), "RUNNING".to_string()),
                ("b".to_string(), "RUNNING".to_string()),
            ]
        );
        assert_eq!(phase_of_task(&session.lock(), "a"), "RUNNING");
        assert_eq!(phase_of_task(&session.lock(), "b"), "RUNNING");
        Ok(())
    }

    #[tokio::test]
    async fn unbound_task_stops_dispatch_before_any_worker_runs() -> TestResult {
        let state = bound_session(
            vec![task("a", "w", &[]), task("b", "w", &[])],
            &[("t1", "a")],
        )
        .await?;
        let session = Arc::new(Mutex::new(state));
        let calls = Arc::new(AtomicUsize::new(0));
        let counter = Arc::clone(&calls);

        let refused = dispatch_session(session, DispatchConfig::default(), move |_task: Task| {
            let counter = Arc::clone(&counter);
            async move {
                counter.fetch_add(1, Ordering::SeqCst);
                Ok::<(), TaskFailure>(())
            }
        })
        .await;

        assert!(matches!(&refused, Err(DispatchError::Unbound(id)) if id == "b"));
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        Ok(())
    }
}
