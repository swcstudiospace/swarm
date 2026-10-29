//! A scripted host for the loop tests: a tiny in-memory task store behind the ports, with workers that block on conditions the test
//! controls (permits, the kill, the end of the test), a scripted wall clock, and a log of every port call.
//!
//! Every field of [`State`] is readable and writable by the test through [`Fake::lock`]; the ports lock it only for short sections and
//! wait on [`Fake::changed`], so a test thread and the loop never hold it across a blocking wait.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

use crate::ports::{
    AgentInfo, CancelState, ClaimSpec, ClaimsVerdict, Completion, LeaseVerdict, PassInfo,
    PortError, ReadyRow, SchedulerPorts, Submission, WorkerDone,
};

/// The longest a test waits for anything before it fails.
pub(super) const WAIT: Duration = Duration::from_secs(10);

/// A wall clock the test advances: seconds since the epoch as the bits of an `f64`.
pub(super) struct Clock(AtomicU64);

impl Clock {
    pub(super) fn new(start: f64) -> Self {
        Self(AtomicU64::new(start.to_bits()))
    }

    pub(super) fn now(&self) -> f64 {
        f64::from_bits(self.0.load(Ordering::SeqCst))
    }

    /// Only the loop thread advances it (from `cancel_state`), so the read-modify-write needs no loop.
    fn advance(&self, seconds: f64) {
        self.0
            .store((self.now() + seconds).to_bits(), Ordering::SeqCst);
    }
}

/// What a worker does on one attempt.
#[derive(Clone)]
pub(super) enum Act {
    /// Return this outcome at once.
    Return(&'static str),
    /// Wait for a permit (see [`Fake::permit`]), then return the outcome.
    Permit(&'static str),
    /// Wait for the kill port, then return the outcome (a session that ends when its process group dies).
    UntilKill(&'static str),
    /// Never return until [`Fake::end`] (a stuck session).
    Hang,
    /// The worker port fails with this summary.
    Fail(&'static str),
    /// The worker panics with this text.
    Panic(&'static str),
}

/// Where a task is in the fake store.
#[derive(Clone, PartialEq, Debug)]
pub(super) enum Phase {
    Pending,
    Active,
    Final(String),
}

/// One task of the fake store and the script of what happens to it.
pub(super) struct Task {
    pub(super) id: String,
    agent: Option<(String, u32)>,
    claims: Vec<(String, bool)>,
    acts: Vec<Act>,
    comps: Vec<Completion>,
    max_attempts: usize,
    after: usize,
    pub(super) phase: Phase,
    /// Dispatches so far (counted at `submit`).
    pub(super) attempts: usize,
    completions: usize,
    retry_after: Option<f64>,
}

impl Task {
    /// A task of agent `A` (ceiling 1000) whose worker returns `DONE`, with one attempt.
    pub(super) fn new(id: &str) -> Self {
        Self {
            id: id.to_owned(),
            agent: Some(("A".to_owned(), 1000)),
            claims: Vec::new(),
            acts: Vec::new(),
            comps: Vec::new(),
            max_attempts: 1,
            after: 0,
            phase: Phase::Pending,
            attempts: 0,
            completions: 0,
            retry_after: None,
        }
    }

    pub(super) fn agent(mut self, agent: &str, max_parallel: u32) -> Self {
        self.agent = Some((agent.to_owned(), max_parallel));
        self
    }

    pub(super) fn no_agent(mut self) -> Self {
        self.agent = None;
        self
    }

    pub(super) fn claims(mut self, claims: &[(&str, bool)]) -> Self {
        self.claims = claims
            .iter()
            .map(|&(pattern, shared)| (pattern.to_owned(), shared))
            .collect();
        self
    }

    /// The worker's behaviour of the next attempt (the last one repeats).
    pub(super) fn act(mut self, act: Act) -> Self {
        self.acts.push(act);
        self
    }

    /// What [`SchedulerPorts::complete`] answers for the next completion (default: `failed` when the outcome is `FAILED`).
    pub(super) fn comp(mut self, completion: Completion) -> Self {
        self.comps.push(completion);
        self
    }

    /// The dispatches the task gets before a failure is final.
    pub(super) fn attempts(mut self, max_attempts: usize) -> Self {
        self.max_attempts = max_attempts;
        self
    }

    /// The worker returns only once this many completions were handled by the loop, which orders completions deterministically.
    pub(super) fn after(mut self, completions: usize) -> Self {
        self.after = completions;
        self
    }
}

/// A cancel request as the fake's `cancel_state` reports it.
pub(super) struct CancelRequest {
    pub(super) at: Instant,
    pub(super) force: bool,
    pub(super) reason: String,
}

/// One finished pass as `observe_pass` saw it.
#[derive(Clone, Debug)]
pub(super) struct PassSeen {
    pub(super) round: u64,
    pub(super) ready: usize,
    pub(super) submitted: usize,
    pub(super) effective_parallel: u32,
    pub(super) free: usize,
    pub(super) in_flight: Vec<String>,
    pub(super) skipped: Vec<(String, String)>,
}

/// What an injected fault does when its port is called.
#[derive(Clone, Copy)]
pub(super) enum Inject {
    /// The port returns an `Err` whose payload is the port name (a `&'static str`).
    Err,
    /// The port panics with a text the loop must never repeat.
    Panic,
}

/// The text a panicking port panics with, so a test can check that no run output repeats it.
pub(super) const PANIC_TEXT: &str = "SECRET-PANIC-PAYLOAD";

pub(super) struct State {
    pub(super) tasks: Vec<Task>,
    /// Every port call as `<port> <detail>`, in order; worker calls are `worker <task>`.
    pub(super) calls: Vec<String>,
    pub(super) logs: Vec<String>,
    pub(super) begin_args: Vec<(u64, Vec<String>)>,
    pub(super) passes: Vec<PassSeen>,
    pub(super) running: usize,
    pub(super) peak: usize,
    pub(super) agent_running: HashMap<String, (usize, usize)>,
    pub(super) started: Vec<String>,
    /// The wall clock when each worker started.
    pub(super) started_clock: Vec<f64>,
    pub(super) completed: Vec<String>,
    pub(super) dones: Vec<WorkerDone>,
    pub(super) permits: usize,
    pub(super) killed: bool,
    pub(super) ended: bool,
    /// `(when, grace_s, reason)` of the kill port.
    pub(super) kill: Option<(Instant, f64, String)>,
    pub(super) kills: usize,
    /// `(when, reason, grace_s, in_flight)` of `cancel_begin`.
    pub(super) begin: Option<(Instant, String, f64, usize)>,
    /// `(task, wall written, clock at the call)`.
    pub(super) retry_writes: Vec<(String, f64, f64)>,
    /// `(task, text, name of the calling thread)`.
    pub(super) internal_calls: Vec<(String, String, Option<String>)>,
    pub(super) unit: f64,
    /// `None`: leases are off. `Some(verdicts)`: the verdict per task; a task not listed is granted.
    pub(super) leases: Option<HashMap<String, LeaseVerdict>>,
    pub(super) cancel: Option<CancelRequest>,
    pub(super) cancel_on_submit: bool,
    pub(super) submit_failed: Vec<String>,
    /// The first `complete` call parks until this is cleared (or the end of the test).
    pub(super) hold_complete: bool,
    pub(super) in_complete: bool,
    pub(super) inject: Option<(&'static str, Inject)>,
    /// Seconds the wall clock advances on every `cancel_state` poll.
    pub(super) poll_advance: f64,
}

impl State {
    pub(super) fn task(&mut self, id: &str) -> &mut Task {
        self.tasks
            .iter_mut()
            .find(|task| task.id == id)
            .expect("the task is in the fake store")
    }

    pub(super) fn phase(&self, id: &str) -> Phase {
        self.tasks
            .iter()
            .find(|task| task.id == id)
            .expect("the task is in the fake store")
            .phase
            .clone()
    }

    /// Every call to port `name` (the calls that start with it).
    pub(super) fn calls_of(&self, name: &str) -> Vec<&str> {
        self.calls
            .iter()
            .map(String::as_str)
            .filter(|call| call.split(' ').next() == Some(name))
            .collect()
    }
}

pub(super) struct Fake {
    state: Mutex<State>,
    changed: Condvar,
    pub(super) clock: Arc<Clock>,
}

impl Fake {
    pub(super) fn new(tasks: Vec<Task>) -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(State {
                tasks,
                calls: Vec::new(),
                logs: Vec::new(),
                begin_args: Vec::new(),
                passes: Vec::new(),
                running: 0,
                peak: 0,
                agent_running: HashMap::new(),
                started: Vec::new(),
                started_clock: Vec::new(),
                completed: Vec::new(),
                dones: Vec::new(),
                permits: 0,
                killed: false,
                ended: false,
                kill: None,
                kills: 0,
                begin: None,
                retry_writes: Vec::new(),
                internal_calls: Vec::new(),
                unit: 0.0,
                leases: None,
                cancel: None,
                cancel_on_submit: false,
                submit_failed: Vec::new(),
                hold_complete: false,
                in_complete: false,
                inject: None,
                poll_advance: 0.0,
            }),
            changed: Condvar::new(),
            clock: Arc::new(Clock::new(1000.0)),
        })
    }

    pub(super) fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The scripted wall clock as the loop's [`super::WallClock`].
    pub(super) fn wall(&self) -> super::WallClock {
        let clock = Arc::clone(&self.clock);
        Arc::new(move || clock.now())
    }

    /// Parks the calling (test) thread until `condition` holds, failing the test after [`WAIT`].
    pub(super) fn wait_until(&self, what: &str, condition: impl Fn(&State) -> bool) {
        let state = self.lock();
        let (_state, timeout) = self
            .changed
            .wait_timeout_while(state, WAIT, |state| !condition(state))
            .unwrap_or_else(PoisonError::into_inner);
        assert!(!timeout.timed_out(), "timed out waiting for {what}");
    }

    /// Lets `count` blocked [`Act::Permit`] workers return.
    pub(super) fn permit(&self, count: usize) {
        self.lock().permits += count;
        self.changed.notify_all();
    }

    /// A cancel request now.
    pub(super) fn request_cancel(&self, reason: &str, force: bool) {
        self.lock().cancel = Some(CancelRequest {
            at: Instant::now(),
            force,
            reason: reason.to_owned(),
        });
        self.changed.notify_all();
    }

    /// Wakes every thread parked on a condition after a test changed the state directly.
    pub(super) fn wake(&self) {
        self.changed.notify_all();
    }

    /// Releases every parked worker and port: the end of a test.
    pub(super) fn end(&self) {
        self.lock().ended = true;
        self.changed.notify_all();
    }

    /// Records the call and applies an injected fault; returns the locked state for the port to work on.
    fn enter(&self, port: &'static str, detail: &str) -> Result<MutexGuard<'_, State>, PortError> {
        let mut state = self.lock();
        let line = if detail.is_empty() {
            port.to_owned()
        } else {
            format!("{port} {detail}")
        };
        state.calls.push(line);
        match state.inject {
            Some((name, Inject::Err)) if name == port => {
                drop(state);
                Err(PortError::new(format!("{port} failed"), Box::new(port)))
            }
            Some((name, Inject::Panic)) if name == port => {
                drop(state);
                panic!("{PANIC_TEXT}");
            }
            _ => Ok(state),
        }
    }
}

impl SchedulerPorts for Fake {
    fn begin_pass(&self, round: u64, in_flight: &[&str]) -> Result<Vec<ReadyRow>, PortError> {
        let mut state = self.enter("begin_pass", &round.to_string())?;
        state
            .begin_args
            .push((round, in_flight.iter().map(|id| (*id).to_owned()).collect()));
        Ok(state
            .tasks
            .iter()
            .enumerate()
            .filter(|(_, task)| {
                task.phase == Phase::Pending && !in_flight.contains(&task.id.as_str())
            })
            .map(|(index, task)| ReadyRow {
                task_id: task.id.clone(),
                priority: Some("P2".to_owned()),
                dag_depth: Some(0),
                // the creation order is the dispatch order
                created_at: Some(index as f64),
                retry_after: task.retry_after,
            })
            .collect())
    }

    fn agent_for(&self, task_id: &str) -> Result<Option<AgentInfo>, PortError> {
        let mut state = self.enter("agent_for", task_id)?;
        Ok(state
            .task(task_id)
            .agent
            .clone()
            .map(|(agent_id, max_parallel)| AgentInfo {
                slug: agent_id.to_lowercase(),
                agent_id,
                max_parallel,
            }))
    }

    fn claims_for(&self, task_id: &str) -> Result<ClaimsVerdict, PortError> {
        let mut state = self.enter("claims_for", task_id)?;
        let claims = state
            .task(task_id)
            .claims
            .iter()
            .map(|(pattern, shared)| ClaimSpec {
                pattern: pattern.clone(),
                shared: *shared,
            })
            .collect();
        Ok(ClaimsVerdict::Claims(claims))
    }

    fn block(&self, task_id: &str, reason: &str) -> Result<(), PortError> {
        let mut state = self.enter("block", &format!("{task_id} {reason}"))?;
        state.task(task_id).phase = Phase::Final("BLOCKED".to_owned());
        Ok(())
    }

    fn lease(&self, task_id: &str, agent_id: &str) -> Result<Option<LeaseVerdict>, PortError> {
        let state = self.enter("lease", &format!("{task_id} {agent_id}"))?;
        Ok(state.leases.as_ref().map(|verdicts| {
            verdicts.get(task_id).cloned().unwrap_or(LeaseVerdict {
                dispatch: true,
                status: "held".to_owned(),
                holder: None,
            })
        }))
    }

    fn observe_pass(&self, info: &PassInfo<'_>) -> Result<(), PortError> {
        let mut state = self.enter("observe_pass", &info.round.to_string())?;
        state.passes.push(PassSeen {
            round: info.round,
            ready: info.ready,
            submitted: info.submitted,
            effective_parallel: info.effective_parallel,
            free: info.free,
            in_flight: info.in_flight.iter().map(|id| (*id).to_owned()).collect(),
            skipped: info
                .skipped
                .iter()
                .map(|skip| (skip.task_id.clone(), skip.reason.clone()))
                .collect(),
        });
        Ok(())
    }

    fn submit(&self, task_id: &str, agent_id: &str) -> Result<Submission, PortError> {
        let mut state = self.enter("submit", task_id)?;
        if state.cancel_on_submit && state.cancel.is_none() {
            state.cancel = Some(CancelRequest {
                at: Instant::now(),
                force: false,
                reason: "signal SIGTERM".to_owned(),
            });
        }
        let task = state.task(task_id);
        task.phase = Phase::Active;
        task.attempts += 1;
        if let Some(position) = state.submit_failed.iter().position(|id| id == task_id) {
            state.submit_failed.remove(position);
            return Ok(Submission::Failed(WorkerDone {
                task_id: task_id.to_owned(),
                agent_id: agent_id.to_owned(),
                outcome: "FAILED".to_owned(),
                error: Some("E-INTERNAL: the dispatch could not be set up".to_owned()),
            }));
        }
        Ok(Submission::Started {
            sid: format!("S-{task_id}"),
        })
    }

    fn worker(&self, task_id: &str, agent_id: &str, _sid: &str) -> Result<WorkerDone, PortError> {
        let mut state = self.lock();
        state.calls.push(format!("worker {task_id}"));
        state.running += 1;
        state.peak = state.peak.max(state.running);
        let by_agent = state
            .agent_running
            .entry(agent_id.to_owned())
            .or_insert((0, 0));
        by_agent.0 += 1;
        by_agent.1 = by_agent.1.max(by_agent.0);
        state.started.push(task_id.to_owned());
        state.started_clock.push(self.clock.now());
        let task = state.task(task_id);
        let index = task
            .attempts
            .saturating_sub(1)
            .min(task.acts.len().saturating_sub(1));
        let act = task.acts.get(index).cloned().unwrap_or(Act::Return("DONE"));
        let after = task.after;
        self.changed.notify_all();
        state = self
            .changed
            .wait_while(state, |state| state.completed.len() < after && !state.ended)
            .unwrap_or_else(PoisonError::into_inner);
        let outcome = match act {
            Act::Return(outcome) => outcome,
            Act::Permit(outcome) => {
                state = self
                    .changed
                    .wait_while(state, |state| {
                        state.permits == 0 && !state.killed && !state.ended
                    })
                    .unwrap_or_else(PoisonError::into_inner);
                state.permits = state.permits.saturating_sub(1);
                outcome
            }
            Act::UntilKill(outcome) => {
                state = self
                    .changed
                    .wait_while(state, |state| !state.killed && !state.ended)
                    .unwrap_or_else(PoisonError::into_inner);
                outcome
            }
            Act::Hang => {
                state = self
                    .changed
                    .wait_while(state, |state| !state.ended)
                    .unwrap_or_else(PoisonError::into_inner);
                "DONE"
            }
            Act::Fail(_) | Act::Panic(_) => "FAILED",
        };
        state.running -= 1;
        if let Some(by_agent) = state.agent_running.get_mut(agent_id) {
            by_agent.0 -= 1;
        }
        self.changed.notify_all();
        drop(state);
        match act {
            Act::Fail(summary) => Err(PortError::new(summary, Box::new(summary))),
            Act::Panic(text) => panic!("{text}"),
            _ => Ok(WorkerDone {
                task_id: task_id.to_owned(),
                agent_id: agent_id.to_owned(),
                outcome: outcome.to_owned(),
                error: None,
            }),
        }
    }

    fn internal_failure(
        &self,
        task_id: &str,
        agent_id: &str,
        text: &str,
    ) -> Result<WorkerDone, PortError> {
        let mut state = self.enter("internal_failure", task_id)?;
        state.internal_calls.push((
            task_id.to_owned(),
            text.to_owned(),
            thread::current().name().map(str::to_owned),
        ));
        Ok(WorkerDone {
            task_id: task_id.to_owned(),
            agent_id: agent_id.to_owned(),
            outcome: "FAILED".to_owned(),
            error: Some(text.to_owned()),
        })
    }

    fn complete(&self, done: &WorkerDone) -> Result<Completion, PortError> {
        let mut state = self.enter("complete", &done.task_id)?;
        if state.hold_complete {
            state.in_complete = true;
            self.changed.notify_all();
            state = self
                .changed
                .wait_while(state, |state| state.hold_complete && !state.ended)
                .unwrap_or_else(PoisonError::into_inner);
        }
        state.completed.push(done.task_id.clone());
        state.dones.push(done.clone());
        let task = state.task(&done.task_id);
        let completion = task
            .comps
            .get(task.completions)
            .cloned()
            .unwrap_or_else(|| Completion {
                failed: done.error.is_some() || done.outcome == "FAILED",
                ..Completion::default()
            });
        task.completions += 1;
        task.phase = if completion.failed && task.attempts < task.max_attempts {
            Phase::Pending
        } else {
            Phase::Final(done.outcome.clone())
        };
        self.changed.notify_all();
        Ok(completion)
    }

    fn set_retry_after(&self, task_id: &str, wall: f64) -> Result<(), PortError> {
        let mut state = self.enter("set_retry_after", task_id)?;
        let now = self.clock.now();
        state.retry_writes.push((task_id.to_owned(), wall, now));
        state.task(task_id).retry_after = Some(wall);
        Ok(())
    }

    fn random_unit(&self) -> Result<f64, PortError> {
        Ok(self.enter("random_unit", "")?.unit)
    }

    fn cancel_state(&self) -> Result<CancelState, PortError> {
        let state = self.enter("cancel_state", "")?;
        let advance = state.poll_advance;
        let cancel = match &state.cancel {
            Some(request) => CancelState {
                is_set: true,
                force: request.force,
                reason: Some(request.reason.clone()),
                elapsed_s: request.at.elapsed().as_secs_f64(),
            },
            None => CancelState::default(),
        };
        drop(state);
        self.clock.advance(advance);
        Ok(cancel)
    }

    fn cancel_begin(&self, reason: &str, grace_s: f64, in_flight: usize) -> Result<(), PortError> {
        let mut state = self.enter("cancel_begin", reason)?;
        state.begin = Some((Instant::now(), reason.to_owned(), grace_s, in_flight));
        self.changed.notify_all();
        Ok(())
    }

    fn kill(&self, grace_s: f64, reason: &str) -> Result<(), PortError> {
        let mut state = self.enter("kill", reason)?;
        state.kill = Some((Instant::now(), grace_s, reason.to_owned()));
        state.kills += 1;
        state.killed = true;
        self.changed.notify_all();
        Ok(())
    }

    fn log(&self, line: &str) -> Result<(), PortError> {
        let mut state = self.enter("log", "")?;
        state.logs.push(line.to_owned());
        self.changed.notify_all();
        Ok(())
    }
}
