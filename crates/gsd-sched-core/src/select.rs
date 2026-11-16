//! The selection pass of the scheduler: which ready tasks start now (Phase 14.4, unit S2.3).
//!
//! This module is the Rust form of three pieces of `scripts/swarm_run.py`, which stays the oracle: the eligibility filter and slot
//! arithmetic of `Scheduler._schedule`, `order_ready`, and `select_batch`. It owns no state. The loop (unit S2.4) keeps the in-flight
//! set, the per-class counts, the [`ClaimTable`], the internal-failure counts and the dispatch counter, and hands them in as plain
//! arguments on every pass.
//!
//! # What a pass does, in the order Python does it
//!
//! 1. [`plan_pass`] orders the rows with [`crate::priority::order_ready`] and walks them in that order. A row whose `retry_after`
//!    is later than the wall clock is left out with `backoff:<s>s` (one decimal, `until - now`; a `retry_after` equal to the clock
//!    is already eligible) and the earliest such time becomes `backoff_wall`; otherwise a task with at least
//!    `internal_failure_limit` internal failures is left out with `internal-failure`. The backoff test comes first.
//! 2. The effective parallelism is `max(1, min(max_concurrent, limiter))`; the free slots are `eff - in_flight` (never below 0),
//!    and in continuous mode also at most what is left of the dispatch budget. `max(1, ...)` is the whole of Python's
//!    `ErrorHandler.effective_parallel(cap)` term, because the runner never latches that handler's degrade flag.
//! 3. [`select_batch`] walks the eligible tasks, calling the ports for each task in exactly this order, and stops asking as soon as
//!    the task is decided:
//!    1. no free slot left: `no-free-slot`, no port call;
//!    2. [`SchedulerPorts::agent_for`]; `None` blocks the task with `no agent for capability` and uses up a slot;
//!    3. the class ceiling (`max(1, max_parallel)` replicas, those in flight plus those selected in this pass): `class-ceiling:<agent>`,
//!       the slot is kept for a later task of another class;
//!    4. [`SchedulerPorts::claims_for`]; an invalid verdict blocks the task with `E-INPUT: invalid notes.claims: <text>` cut to
//!       500 characters and uses up a slot; otherwise the claims are acquired all or nothing, a conflict leaves the task out with
//!       `claim-conflict:<holder>` (the first conflict's holder);
//!    5. [`SchedulerPorts::lease`] (unless the host already answered that leases are off): a verdict that does not dispatch releases
//!       the task's claims, adds the line `<task>: <status>[ (held by <holder>)]` to `waiting` and leaves the task out with
//!       `lease:<status>`, without taking a slot;
//!    6. the task joins the batch and takes a slot.
//!
//! Python's `select_batch` repeats the class-ceiling test after the lease (`take_agent_slot`). Nothing in between changes the counts,
//! so that second test can never fail and it is not reproduced here.
//!
//! # Threads and blocking
//!
//! Everything here runs on the loop thread. Every port call can block (the lease port makes a network round trip); no lock is held
//! across one, because the module holds none: the claim table is a `&mut` argument. The functions never allocate beyond the result
//! and a small per-pass map of the classes that were selected.
//!
//! # Cancellation checkpoints
//!
//! [`plan_pass`] and [`select_batch`] make the port calls described above and nothing else. The runtime's pass uses the same selection
//! through a private entry that takes an optional checkpoint, asked before each candidate (before its slot test), before each port
//! action on it, and before it joins the batch, leases on or off. A checkpoint that answers stop ends the selection at once, with
//! no further port call.

use std::collections::HashMap;

use crate::claim_pattern::ClaimMode;
use crate::claims::{AcquireError, ClaimTable};
use crate::ports::{
    AgentInfo, ClaimsVerdict, PortError, ReadyRow, RunConfig, SchedulerPorts, SkippedTask,
    BLOCK_REASON_MAX_CHARS, BLOCK_REASON_NO_AGENT,
};
use crate::priority;

/// The skip reason of a task that found no free global slot (`skip.setdefault(tid, "no-free-slot")` in `select_batch`).
const NO_FREE_SLOT: &str = "no-free-slot";

/// The skip reason of a task whose worker raised `internal_failure_limit` times (`skipped[...] = "internal-failure"` in `_schedule`).
const INTERNAL_FAILURE: &str = "internal-failure";

/// Asked before each port action of a controlled selection: `Ok(true)` stops the pass. Only the runtime's pass supplies one.
pub(crate) type Checkpoint<'a> = &'a mut dyn FnMut() -> Result<bool, PortError>;

/// What a controlled pass produced: the plan as far as it went, and whether a checkpoint stopped it.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ControlledPass {
    /// The plan. After a stop it holds only what was decided before the stop.
    pub(crate) plan: PassPlan,
    /// `true` when a checkpoint answered stop; the caller then admits nothing further.
    pub(crate) stopped: bool,
}

/// What a controlled pass has decided so far. The runtime keeps it across the pass, so the tasks left out and the leases granted
/// survive a port error that ends the pass.
#[derive(Debug, Default)]
pub(crate) struct PassProgress {
    /// `len(ready)`, written when the pass starts.
    pub(crate) ready: usize,
    /// The effective parallelism, written before the selection starts.
    pub(crate) effective_parallel: u32,
    /// The free slots the selection started from, written before the selection starts.
    pub(crate) free: usize,
    /// The tasks left out so far, in first-skipped order.
    pub(crate) skipped: Vec<SkippedTask>,
    /// The `(task_id, agent_id)` pairs whose lease was granted, in selection order.
    pub(crate) granted: Vec<(String, String)>,
}

/// Asks the checkpoint, when there is one, whether the pass must stop.
fn stop_requested(checkpoint: &mut Option<Checkpoint<'_>>) -> Result<bool, PortError> {
    match checkpoint {
        Some(check) => (**check)(),
        None => Ok(false),
    }
}

/// One task chosen to start: the id and the agent it runs as. The loop submits these in order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Dispatch {
    /// The task id.
    pub task_id: String,
    /// The agent [`SchedulerPorts::agent_for`] resolved for the task; its `agent_id` is the argument of `submit` and `worker`.
    pub agent: AgentInfo,
}

/// What [`select_batch`] decided: the tasks to start and the lines of the tasks left waiting on a lease.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Batch {
    /// The tasks to start, in selection order (the Python `batch`).
    pub dispatch: Vec<Dispatch>,
    /// `<task>: <status>` plus ` (held by <holder>)` when a non-empty holder is known, one per task a lease refused, in the order
    /// they were tried (the Python `waiting`).
    pub waiting: Vec<String>,
}

/// The loop state one pass reads, owned by the loop and borrowed here.
#[derive(Debug, Clone, Copy)]
pub struct PassState<'a> {
    /// The run configuration; read: `max_concurrent`, `barrier`, `budget` and `internal_failure_limit`.
    pub config: &'a RunConfig,
    /// The AIMD limiter's current value (`limiter.current()`).
    pub limiter_current: u32,
    /// How many tasks the run has submitted so far (`Scheduler.dispatched`), counted against `config.budget` in continuous mode.
    pub dispatched: u64,
    /// How many dispatches are in flight (`len(self.inflight)`).
    pub in_flight: usize,
    /// In-flight dispatches per agent id (`Counter(i.agent["id"] for i in inflight.values())`); an absent id counts 0.
    pub per_class: &'a HashMap<String, u32>,
    /// Internal failures per task id (`Scheduler._internal`); an absent id counts 0.
    pub internal_failures: &'a HashMap<String, u32>,
    /// The wall clock in seconds since the epoch, read after [`SchedulerPorts::begin_pass`] returned (Python reads `time.time()`
    /// after `dispatchable`). A row is held back while its `retry_after` is strictly greater than this.
    pub now_wall: f64,
    /// `true` once the lease port answered `None` (leases off for the run): the lease port is then not called again.
    pub leases_off: bool,
}

/// The outcome of one selection pass (the numbers of the Python `Pass` and `_tick`).
#[derive(Debug, Clone, PartialEq)]
pub struct PassPlan {
    /// `len(ready)`: the rows the pass saw, before the backoff and internal-failure filters.
    pub ready: usize,
    /// The effective parallelism of the pass, at least 1.
    pub effective_parallel: u32,
    /// The free slots the selection started from.
    pub free: usize,
    /// The tasks to submit, in order, and the lease waiting lines.
    pub batch: Batch,
    /// Every task left out, once, with its reason, in first-skipped order: the backoff and internal-failure skips in dispatch
    /// order first, then the selection's skips in selection order (the insertion order of the Python `skipped` dict).
    pub skipped: Vec<SkippedTask>,
    /// The earliest `retry_after` among the tasks held back by the backoff gate; the loop sleeps until it (`Pass.backoff_wall`).
    pub backoff_wall: Option<f64>,
    /// `true` once the lease port has answered `None`; the loop passes it back as [`PassState::leases_off`].
    pub leases_off: bool,
}

/// Runs the selection of one pass over the rows [`SchedulerPorts::begin_pass`] returned: eligibility, effective parallelism, ordering
/// and [`select_batch`]. Python: the middle of `Scheduler._schedule`.
///
/// The caller (unit S2.4) has already called `begin_pass` and reads the wall clock into `state.now_wall`; after this returns it
/// submits the batch, tells [`SchedulerPorts::observe_pass`] about the pass and writes `plan.leases_off` back to its state. The
/// tasks a pass selects keep their claims in `claims` until the loop releases them when the dispatch completes.
///
/// # Errors
///
/// The first failing port call ends the pass and its [`PortError`] is returned unchanged. Claims and slots decided before it stay as
/// they were, as they do in Python when `select_batch` raises (the run stops).
pub fn plan_pass(
    ports: &dyn SchedulerPorts,
    rows: &[ReadyRow],
    state: &PassState<'_>,
    claims: &mut ClaimTable,
) -> Result<PassPlan, PortError> {
    plan_pass_controlled(ports, rows, state, claims, None).map(|pass| pass.plan)
}

/// [`plan_pass`] with the optional checkpoint of the runtime path. Without one it makes exactly the calls of [`plan_pass`]. Python:
/// the middle of `Scheduler._schedule`.
pub(crate) fn plan_pass_controlled(
    ports: &dyn SchedulerPorts,
    rows: &[ReadyRow],
    state: &PassState<'_>,
    claims: &mut ClaimTable,
    checkpoint: Option<Checkpoint<'_>>,
) -> Result<ControlledPass, PortError> {
    plan_pass_recorded(
        ports,
        rows,
        state,
        claims,
        checkpoint,
        &mut PassProgress::default(),
    )
}

/// [`plan_pass_controlled`] that also writes what the pass decides into `progress` as it goes. The runtime's pass uses this entry:
/// a port error ends the pass with `Err`, and `progress` still holds the tasks left out and the leases granted before it.
pub(crate) fn plan_pass_recorded(
    ports: &dyn SchedulerPorts,
    rows: &[ReadyRow],
    state: &PassState<'_>,
    claims: &mut ClaimTable,
    mut checkpoint: Option<Checkpoint<'_>>,
    progress: &mut PassProgress,
) -> Result<ControlledPass, PortError> {
    let order = priority::order_ready(
        rows.iter()
            .map(|row| (row.priority.as_deref(), row.dag_depth, row.created_at)),
    );
    progress.ready = rows.len();
    let mut eligible: Vec<&str> = Vec::with_capacity(rows.len());
    let mut backoff_wall: Option<f64> = None;
    for index in order {
        let row = &rows[index];
        let failures = state
            .internal_failures
            .get(row.task_id.as_str())
            .copied()
            .unwrap_or(0);
        match row.retry_after {
            Some(until) if until > state.now_wall => {
                progress.skipped.push(SkippedTask {
                    task_id: row.task_id.clone(),
                    reason: format!("backoff:{:.1}s", until - state.now_wall),
                });
                backoff_wall = Some(backoff_wall.map_or(until, |wall| wall.min(until)));
            }
            _ if failures >= state.config.internal_failure_limit => {
                progress.skipped.push(SkippedTask {
                    task_id: row.task_id.clone(),
                    reason: INTERNAL_FAILURE.to_owned(),
                });
            }
            _ => eligible.push(row.task_id.as_str()),
        }
    }
    let effective_parallel = state
        .config
        .max_concurrent
        .min(state.limiter_current)
        .max(1);
    let mut free = usize::try_from(effective_parallel)
        .unwrap_or(usize::MAX)
        .saturating_sub(state.in_flight);
    if !state.config.barrier {
        // `--max-rounds` is a total: never start more than what is left of it in one pass
        let left = state.config.budget.saturating_sub(state.dispatched);
        free = free.min(usize::try_from(left).unwrap_or(usize::MAX));
    }
    progress.effective_parallel = effective_parallel;
    progress.free = free;
    let mut leases_off = state.leases_off;
    let (batch, stopped) = select_controlled(
        ports,
        &eligible,
        free,
        state.per_class,
        claims,
        &mut leases_off,
        &mut progress.skipped,
        &mut progress.granted,
        &mut checkpoint,
    )?;
    Ok(ControlledPass {
        plan: PassPlan {
            ready: rows.len(),
            effective_parallel,
            free,
            batch,
            skipped: std::mem::take(&mut progress.skipped),
            backoff_wall,
            leases_off,
        },
        stopped,
    })
}

/// Chooses the tasks to start from `ready` (already eligible and in dispatch order) with `free` global slots. Python: `select_batch`.
///
/// `running` holds the in-flight dispatches per agent id; `claims` is the run's claim table, which this call fills with the claims
/// of the tasks it selects (and of nothing else: a task that is left out or blocked holds no claim afterwards). `leases_off` says
/// whether the lease port is known to be off; it is set when the port answers `None`, and then the port is not called again.
/// Every task left out is appended to `skipped` with its reason, in the order they were decided; tasks that were blocked are not.
/// Task ids in `ready` must be unique.
///
/// See the module documentation for the order of the port calls per task.
///
/// # Errors
///
/// The first failing port call ends the selection; its [`PortError`] is returned unchanged.
pub fn select_batch(
    ports: &dyn SchedulerPorts,
    ready: &[&str],
    free: usize,
    running: &HashMap<String, u32>,
    claims: &mut ClaimTable,
    leases_off: &mut bool,
    skipped: &mut Vec<SkippedTask>,
) -> Result<Batch, PortError> {
    select_controlled(
        ports,
        ready,
        free,
        running,
        claims,
        leases_off,
        skipped,
        &mut Vec::new(),
        &mut None,
    )
    .map(|(batch, _)| batch)
}

/// [`select_batch`] with the optional checkpoint of the runtime path, answering `(batch, stopped)`. The checkpoint is asked before each
/// candidate (before its slot test), before each port action on it (`agent_for`, `claims_for`, `block`, `lease`), and before it joins
/// the batch. When it answers stop, the selection returns at once. The candidate it stopped on gives back the claims it acquired for
/// itself; the tasks already in the batch keep theirs, for the caller to release, because none of them was submitted.
/// Each lease granted is appended to `granted` in selection order, so the caller still has the grants when a later port call fails.
#[allow(clippy::too_many_arguments)]
fn select_controlled(
    ports: &dyn SchedulerPorts,
    ready: &[&str],
    free: usize,
    running: &HashMap<String, u32>,
    claims: &mut ClaimTable,
    leases_off: &mut bool,
    skipped: &mut Vec<SkippedTask>,
    granted: &mut Vec<(String, String)>,
    checkpoint: &mut Option<Checkpoint<'_>>,
) -> Result<(Batch, bool), PortError> {
    let mut batch = Batch::default();
    let mut slots = free;
    // replicas selected in this pass, on top of `running` (Python copies `running` and increments it)
    let mut added: HashMap<String, u32> = HashMap::new();
    for &tid in ready {
        // asked before anything about the candidate, the slot test included, as Python's `select_batch` asks at the top of its loop
        if stop_requested(checkpoint)? {
            return Ok((batch, true));
        }
        if slots == 0 {
            skipped.push(SkippedTask {
                task_id: tid.to_owned(),
                reason: NO_FREE_SLOT.to_owned(),
            });
            continue;
        }
        let Some(agent) = ports.agent_for(tid)? else {
            if stop_requested(checkpoint)? {
                return Ok((batch, true));
            }
            ports.block(tid, BLOCK_REASON_NO_AGENT)?;
            slots -= 1;
            continue;
        };
        let ceiling = agent.max_parallel.max(1);
        let replicas = running
            .get(&agent.agent_id)
            .copied()
            .unwrap_or(0)
            .saturating_add(added.get(&agent.agent_id).copied().unwrap_or(0));
        if replicas >= ceiling {
            // a later task of another class can still take a free global slot
            skipped.push(SkippedTask {
                task_id: tid.to_owned(),
                reason: format!("class-ceiling:{}", agent.agent_id),
            });
            continue;
        }
        if stop_requested(checkpoint)? {
            return Ok((batch, true));
        }
        let invalid = match ports.claims_for(tid)? {
            ClaimsVerdict::Invalid(text) => Some(text),
            ClaimsVerdict::Claims(wanted) => {
                let pairs: Vec<(&str, ClaimMode)> = wanted
                    .iter()
                    .map(|claim| {
                        let mode = if claim.shared {
                            ClaimMode::Shared
                        } else {
                            ClaimMode::Exclusive
                        };
                        (claim.pattern.as_str(), mode)
                    })
                    .collect();
                match claims.acquire(tid, &pairs) {
                    Ok(()) => None,
                    // the port's parser normalizes every pattern, so this is a guard: it is a malformed claim all the same
                    Err(AcquireError::InvalidPattern(error)) => Some(error.to_string()),
                    Err(AcquireError::Conflicts(conflicts)) => {
                        let holder = conflicts.first().map_or("", |first| first.holder.as_str());
                        skipped.push(SkippedTask {
                            task_id: tid.to_owned(),
                            reason: format!("claim-conflict:{holder}"),
                        });
                        continue;
                    }
                }
            }
        };
        if let Some(text) = invalid {
            // a malformed claim blocks the task rather than running it unclaimed
            if stop_requested(checkpoint)? {
                return Ok((batch, true));
            }
            let reason: String = format!("E-INPUT: invalid notes.claims: {text}")
                .chars()
                .take(BLOCK_REASON_MAX_CHARS)
                .collect();
            ports.block(tid, &reason)?;
            slots -= 1;
            continue;
        }
        // asked once the claims are taken and before any lease is requested, leases on or off, as Python does
        if stop_requested(checkpoint)? {
            // acquired for this candidate and not selected: its claims go back here
            claims.release(tid);
            return Ok((batch, true));
        }
        let mut leased = false;
        if !*leases_off {
            match ports.lease(tid, &agent.agent_id)? {
                None => *leases_off = true,
                Some(verdict) if !verdict.dispatch => {
                    claims.release(tid);
                    let line = match verdict
                        .holder
                        .as_deref()
                        .filter(|holder| !holder.is_empty())
                    {
                        Some(holder) => format!("{tid}: {} (held by {holder})", verdict.status),
                        None => format!("{tid}: {}", verdict.status),
                    };
                    batch.waiting.push(line);
                    skipped.push(SkippedTask {
                        task_id: tid.to_owned(),
                        reason: format!("lease:{}", verdict.status),
                    });
                    continue;
                }
                Some(_) => leased = true,
            }
        }
        if leased {
            granted.push((tid.to_owned(), agent.agent_id.clone()));
        }
        *added.entry(agent.agent_id.clone()).or_insert(0) += 1;
        slots -= 1;
        batch.dispatch.push(Dispatch {
            task_id: tid.to_owned(),
            agent,
        });
    }
    Ok((batch, false))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ports::{
        CancelState, ClaimSpec, Completion, LeaseVerdict, PassInfo, Submission, WorkerDone,
    };
    use std::sync::Mutex;

    /// How the fake answers the lease port.
    enum Leases {
        /// `Ok(None)`: leases are off for the run.
        Off,
        /// Per-task verdicts; a task that is not listed is granted.
        On(HashMap<String, LeaseVerdict>),
    }

    /// A scripted host that records every call as one line, so a test can compare the exact call sequence with the one Python makes.
    /// Ports the selection must never use answer with an error (and are recorded), so a stray call fails the test twice.
    struct RecordingPorts {
        calls: Mutex<Vec<String>>,
        agents: HashMap<String, AgentInfo>,
        claims: HashMap<String, ClaimsVerdict>,
        leases: Leases,
        fail_lease_for: Option<&'static str>,
    }

    impl RecordingPorts {
        fn new() -> Self {
            Self {
                calls: Mutex::new(Vec::new()),
                agents: HashMap::new(),
                claims: HashMap::new(),
                leases: Leases::Off,
                fail_lease_for: None,
            }
        }

        fn agent(mut self, task: &str, agent_id: &str, max_parallel: u32) -> Self {
            self.agents.insert(
                task.to_owned(),
                AgentInfo {
                    agent_id: agent_id.to_owned(),
                    slug: agent_id.to_lowercase(),
                    max_parallel,
                },
            );
            self
        }

        fn claim(mut self, task: &str, claims: &[(&str, bool)]) -> Self {
            let specs = claims
                .iter()
                .map(|&(pattern, shared)| ClaimSpec {
                    pattern: pattern.to_owned(),
                    shared,
                })
                .collect();
            self.claims
                .insert(task.to_owned(), ClaimsVerdict::Claims(specs));
            self
        }

        fn invalid_claims(mut self, task: &str, text: &str) -> Self {
            self.claims
                .insert(task.to_owned(), ClaimsVerdict::Invalid(text.to_owned()));
            self
        }

        fn lease_verdicts(mut self, verdicts: &[(&str, bool, &str, Option<&str>)]) -> Self {
            self.leases = Leases::On(
                verdicts
                    .iter()
                    .map(|&(task, dispatch, status, holder)| {
                        (
                            task.to_owned(),
                            LeaseVerdict {
                                dispatch,
                                status: status.to_owned(),
                                holder: holder.map(str::to_owned),
                            },
                        )
                    })
                    .collect(),
            );
            self
        }

        fn record(&self, line: String) {
            self.calls.lock().expect("calls mutex").push(line);
        }

        fn calls(&self) -> Vec<String> {
            self.calls.lock().expect("calls mutex").clone()
        }

        fn unexpected(&self, name: &str) -> PortError {
            self.record(format!("UNEXPECTED {name}"));
            PortError::new(format!("{name} is not a selection port"), Box::new(()))
        }
    }

    impl SchedulerPorts for RecordingPorts {
        fn begin_pass(&self, _: u64, _: &[&str]) -> Result<Vec<ReadyRow>, PortError> {
            Err(self.unexpected("begin_pass"))
        }

        fn agent_for(&self, task_id: &str) -> Result<Option<AgentInfo>, PortError> {
            self.record(format!("agent_for {task_id}"));
            Ok(self.agents.get(task_id).cloned())
        }

        fn claims_for(&self, task_id: &str) -> Result<ClaimsVerdict, PortError> {
            self.record(format!("claims_for {task_id}"));
            Ok(self
                .claims
                .get(task_id)
                .cloned()
                .unwrap_or(ClaimsVerdict::Claims(Vec::new())))
        }

        fn block(&self, task_id: &str, reason: &str) -> Result<(), PortError> {
            self.record(format!("block {task_id} {reason}"));
            Ok(())
        }

        fn lease(&self, task_id: &str, agent_id: &str) -> Result<Option<LeaseVerdict>, PortError> {
            self.record(format!("lease {task_id} {agent_id}"));
            if self.fail_lease_for == Some(task_id) {
                return Err(PortError::new(
                    "RuntimeError: substrate unreachable",
                    Box::new(()),
                ));
            }
            match &self.leases {
                Leases::Off => Ok(None),
                Leases::On(verdicts) => Ok(Some(verdicts.get(task_id).cloned().unwrap_or(
                    LeaseVerdict {
                        dispatch: true,
                        status: "held".to_owned(),
                        holder: None,
                    },
                ))),
            }
        }

        fn observe_pass(&self, _: &PassInfo<'_>) -> Result<(), PortError> {
            Err(self.unexpected("observe_pass"))
        }

        fn submit(&self, _: &str, _: &str) -> Result<Submission, PortError> {
            Err(self.unexpected("submit"))
        }

        fn worker(&self, _: &str, _: &str, _: &str) -> Result<WorkerDone, PortError> {
            Err(self.unexpected("worker"))
        }

        fn internal_failure(&self, _: &str, _: &str, _: &str) -> Result<WorkerDone, PortError> {
            Err(self.unexpected("internal_failure"))
        }

        fn complete(&self, _: &WorkerDone) -> Result<Completion, PortError> {
            Err(self.unexpected("complete"))
        }

        fn set_retry_after(&self, _: &str, _: f64) -> Result<(), PortError> {
            Err(self.unexpected("set_retry_after"))
        }

        fn random_unit(&self) -> Result<f64, PortError> {
            Err(self.unexpected("random_unit"))
        }

        fn cancel_state(&self) -> Result<CancelState, PortError> {
            Err(self.unexpected("cancel_state"))
        }

        fn cancel_begin(&self, _: &str, _: f64, _: usize) -> Result<(), PortError> {
            Err(self.unexpected("cancel_begin"))
        }

        fn kill(&self, _: f64, _: &str) -> Result<(), PortError> {
            Err(self.unexpected("kill"))
        }

        fn log(&self, _: &str) -> Result<(), PortError> {
            Err(self.unexpected("log"))
        }
    }

    fn pairs(skipped: &[SkippedTask]) -> Vec<(&str, &str)> {
        skipped
            .iter()
            .map(|s| (s.task_id.as_str(), s.reason.as_str()))
            .collect()
    }

    fn ids(batch: &Batch) -> Vec<&str> {
        batch.dispatch.iter().map(|d| d.task_id.as_str()).collect()
    }

    fn counts(entries: &[(&str, u32)]) -> HashMap<String, u32> {
        entries.iter().map(|&(k, v)| (k.to_owned(), v)).collect()
    }

    fn row(task_id: &str) -> ReadyRow {
        ReadyRow {
            task_id: task_id.to_owned(),
            priority: Some("P2".to_owned()),
            dag_depth: Some(0),
            created_at: Some(0.0),
            retry_after: None,
        }
    }

    /// `select_batch` with leases off, an empty claim table and nothing running: the common start of a test.
    fn select(
        ports: &RecordingPorts,
        ready: &[&str],
        free: usize,
        running: &[(&str, u32)],
    ) -> (Batch, Vec<SkippedTask>, ClaimTable) {
        let mut claims = ClaimTable::new();
        let mut skipped = Vec::new();
        let batch = select_batch(
            ports,
            ready,
            free,
            &counts(running),
            &mut claims,
            &mut true,
            &mut skipped,
        )
        .expect("selection succeeds");
        (batch, skipped, claims)
    }

    fn owners(claims: &ClaimTable) -> Vec<&str> {
        claims.held().map(|(owner, _)| owner).collect()
    }

    /// Replays `test_select_batch_reports_a_reason_for_every_skipped_task` (tests/test_scheduler_continuous.py:595): A12 allows two
    /// replicas, A05 and A06 six, and the call order is the one Python's `select_batch` makes.
    #[test]
    fn replays_the_python_reason_for_every_skipped_task_scenario() {
        let ports = RecordingPorts::new()
            .agent("T-1", "A12", 2)
            .agent("T-2", "A12", 2)
            .agent("T-3", "A12", 2)
            .agent("T-4", "A05", 6)
            .agent("T-5", "A05", 6)
            .agent("T-6", "A06", 6)
            .claim("T-4", &[("a/**", false)])
            .claim("T-5", &[("a/b.py", false)]);
        let all = ["T-1", "T-2", "T-3", "T-4", "T-5", "T-6"];
        let (batch, skipped, claims) = select(&ports, &all, 4, &[("A12", 1)]);
        // python: assert [t["task_id"] for t, _ in batch] == ["T-1", "T-4", "T-6"] and waiting == []
        assert_eq!(ids(&batch), ["T-1", "T-4", "T-6"]);
        assert!(batch.waiting.is_empty());
        // python: assert skipped == {"T-2": "class-ceiling:A12", "T-3": "class-ceiling:A12", "T-5": "claim-conflict:T-4"}
        assert_eq!(
            pairs(&skipped),
            [
                ("T-2", "class-ceiling:A12"),
                ("T-3", "class-ceiling:A12"),
                ("T-5", "claim-conflict:T-4")
            ]
        );
        assert_eq!(owners(&claims), ["T-4"]);
        assert_eq!(
            ports.calls(),
            [
                "agent_for T-1",
                "claims_for T-1",
                "agent_for T-2",
                "agent_for T-3",
                "agent_for T-4",
                "claims_for T-4",
                "agent_for T-5",
                "claims_for T-5",
                "agent_for T-6",
                "claims_for T-6",
            ]
        );

        // python: select_batch(store, tasks[3:], 1, None, skipped=skipped) -> batch ["T-4"], skipped {"T-5": "no-free-slot",
        // "T-6": "no-free-slot"}. Python passes claims=None there, which the scheduler never does, so claims_for is asked here.
        let before = ports.calls().len();
        let (batch, skipped, _) = select(&ports, &all[3..], 1, &[]);
        assert_eq!(ids(&batch), ["T-4"]);
        assert_eq!(
            pairs(&skipped),
            [("T-5", "no-free-slot"), ("T-6", "no-free-slot")]
        );
        assert_eq!(ports.calls()[before..], ["agent_for T-4", "claims_for T-4"]);
    }

    #[test]
    fn no_free_slot_asks_no_port_and_the_last_slot_goes_to_the_first_task() {
        let ports = RecordingPorts::new()
            .agent("T-a", "A05", 6)
            .agent("T-b", "A05", 6);
        let (batch, skipped, _) = select(&ports, &["T-a", "T-b"], 0, &[]);
        assert!(batch.dispatch.is_empty());
        assert_eq!(
            pairs(&skipped),
            [("T-a", "no-free-slot"), ("T-b", "no-free-slot")]
        );
        assert!(ports.calls().is_empty());

        let (batch, skipped, _) = select(&ports, &["T-a", "T-b"], 1, &[]);
        assert_eq!(ids(&batch), ["T-a"]);
        assert_eq!(pairs(&skipped), [("T-b", "no-free-slot")]);
    }

    /// The class ceiling is `max(1, max_parallel)` replicas, counting those in flight and those selected in the same pass; a task
    /// left out for it keeps the global slot for the next class.
    #[test]
    fn class_ceiling_counts_running_and_selected_replicas() {
        // (agent max_parallel, running replicas, replicas this pass takes of 3 tasks of the class)
        for (max_parallel, running, taken) in [
            (0, 0, 1),
            (0, 1, 0),
            (1, 0, 1),
            (2, 1, 1),
            (2, 2, 0),
            (6, 0, 3),
        ] {
            let ports = RecordingPorts::new()
                .agent("T-1", "A", max_parallel)
                .agent("T-2", "A", max_parallel)
                .agent("T-3", "A", max_parallel);
            let (batch, skipped, _) = select(&ports, &["T-1", "T-2", "T-3"], 9, &[("A", running)]);
            assert_eq!(
                batch.dispatch.len(),
                taken,
                "max_parallel {max_parallel} running {running}"
            );
            assert!(skipped.iter().all(|s| s.reason == "class-ceiling:A"));
        }
    }

    #[test]
    fn a_class_at_its_ceiling_does_not_use_a_slot_for_a_later_class() {
        let ports = RecordingPorts::new()
            .agent("R-0", "A12", 2)
            .agent("R-1", "A12", 2)
            .agent("R-2", "A12", 2)
            .agent("R-be", "A05", 6);
        // python tests/test_blast_radius.py:168: select_batch(store, mixed, 5, None) -> ["R-0", "R-1", "R-be"]
        let (batch, skipped, _) = select(&ports, &["R-0", "R-1", "R-2", "R-be"], 3, &[]);
        assert_eq!(ids(&batch), ["R-0", "R-1", "R-be"]);
        assert_eq!(pairs(&skipped), [("R-2", "class-ceiling:A12")]);
    }

    /// `agent_for` answering `None` blocks the task once with Python's reason, uses up a slot, and puts the task in neither the batch
    /// nor the skipped list.
    #[test]
    fn a_task_without_an_agent_is_blocked_once_and_takes_a_slot() {
        let ports = RecordingPorts::new().agent("T-y", "A05", 6);
        let (batch, skipped, _) = select(&ports, &["T-x", "T-y"], 1, &[]);
        assert!(batch.dispatch.is_empty());
        assert_eq!(pairs(&skipped), [("T-y", "no-free-slot")]);
        // python: store.transition(tid, S.BLOCKED, reason="no agent for capability")
        assert_eq!(
            ports.calls(),
            ["agent_for T-x", "block T-x no agent for capability"]
        );
    }

    #[test]
    fn a_malformed_claim_blocks_with_the_exact_reason_and_takes_a_slot() {
        let ports = RecordingPorts::new()
            .agent("T-a", "A05", 6)
            .agent("T-b", "A05", 6)
            .invalid_claims("T-a", "notes.claims must be a list");
        let (batch, skipped, claims) = select(&ports, &["T-a", "T-b"], 1, &[]);
        assert!(batch.dispatch.is_empty());
        assert_eq!(pairs(&skipped), [("T-b", "no-free-slot")]);
        assert!(owners(&claims).is_empty());
        // python: reason=f"E-INPUT: invalid notes.claims: {e}"[:500]; no lease is asked for a blocked task
        assert_eq!(
            ports.calls(),
            [
                "agent_for T-a",
                "claims_for T-a",
                "block T-a E-INPUT: invalid notes.claims: notes.claims must be a list"
            ]
        );
    }

    #[test]
    fn the_block_reason_is_cut_to_500_characters_not_bytes() {
        let long = "é".repeat(600);
        let ports = RecordingPorts::new()
            .agent("T-a", "A05", 6)
            .invalid_claims("T-a", &long);
        let _ = select(&ports, &["T-a"], 1, &[]);
        let calls = ports.calls();
        let reason = calls[2].strip_prefix("block T-a ").expect("block call");
        let prefix = "E-INPUT: invalid notes.claims: ";
        assert_eq!(reason.chars().count(), 500);
        assert_eq!(
            reason,
            format!("{prefix}{}", "é".repeat(500 - prefix.chars().count()))
        );
    }

    /// The port's parser has normalized every pattern, so an unparsable one reaching the table is a guard: it blocks like a
    /// malformed claim, leaves the table unchanged and uses a slot.
    #[test]
    fn a_pattern_the_table_refuses_blocks_the_task_and_holds_nothing() {
        let ports = RecordingPorts::new()
            .agent("T-a", "A05", 6)
            .claim("T-a", &[("src/**", false), ("../escape", false)]);
        let (batch, skipped, claims) = select(&ports, &["T-a"], 1, &[]);
        assert!(batch.dispatch.is_empty() && skipped.is_empty());
        assert!(owners(&claims).is_empty());
        let calls = ports.calls();
        assert_eq!(calls.len(), 3);
        assert!(calls[2].starts_with("block T-a E-INPUT: invalid notes.claims: "));
    }

    /// Mode mapping and the holder named in the reason: shared claims coexist, an exclusive claim waits for the first holder.
    #[test]
    fn claim_modes_decide_the_conflict_and_the_first_holder_is_named() {
        let ports = RecordingPorts::new()
            .agent("T-1", "A05", 6)
            .agent("T-2", "A05", 6)
            .agent("T-3", "A05", 6)
            .agent("T-4", "A05", 6)
            .claim("T-1", &[("docs/**", true)])
            .claim("T-2", &[("docs/**", true)])
            .claim("T-3", &[("docs/guide.md", false)])
            .claim("T-4", &[("src/**", false)]);
        let (batch, skipped, claims) = select(&ports, &["T-1", "T-2", "T-3", "T-4"], 9, &[]);
        assert_eq!(ids(&batch), ["T-1", "T-2", "T-4"]);
        assert_eq!(pairs(&skipped), [("T-3", "claim-conflict:T-1")]);
        assert_eq!(owners(&claims), ["T-1", "T-2", "T-4"]);
    }

    #[test]
    fn a_claim_held_by_an_in_flight_task_keeps_a_later_task_out() {
        let ports = RecordingPorts::new()
            .agent("T-b", "A05", 6)
            .claim("T-b", &[("src/**", false)]);
        let mut claims = ClaimTable::new();
        assert!(claims
            .acquire("T-a", &[("src/**", ClaimMode::Exclusive)])
            .is_ok());
        let mut skipped = Vec::new();
        let batch = select_batch(
            &ports,
            &["T-b"],
            3,
            &HashMap::new(),
            &mut claims,
            &mut true,
            &mut skipped,
        )
        .expect("selection succeeds");
        assert!(batch.dispatch.is_empty());
        assert_eq!(pairs(&skipped), [("T-b", "claim-conflict:T-a")]);
    }

    /// python tests/test_substrate_lease.py:377 and :392: a refused claim waits with its status and holder, takes no slot, and
    /// releases the task's claims so a later task can take them.
    #[test]
    fn a_refused_lease_waits_without_a_slot_and_releases_the_claims() {
        let ports = RecordingPorts::new()
            .agent("T-a", "A05", 6)
            .agent("T-b", "A05", 6)
            .claim("T-a", &[("src/**", false)])
            .claim("T-b", &[("src/x.py", false)])
            .lease_verdicts(&[("T-a", false, "denied", Some("A05@r9:G-1"))]);
        let mut claims = ClaimTable::new();
        let mut skipped = Vec::new();
        let batch = select_batch(
            &ports,
            &["T-a", "T-b"],
            1,
            &HashMap::new(),
            &mut claims,
            &mut false,
            &mut skipped,
        )
        .expect("selection succeeds");
        assert_eq!(ids(&batch), ["T-b"]);
        assert_eq!(batch.waiting, ["T-a: denied (held by A05@r9:G-1)"]);
        assert_eq!(pairs(&skipped), [("T-a", "lease:denied")]);
        assert_eq!(owners(&claims), ["T-b"]);
        assert_eq!(
            ports.calls(),
            [
                "agent_for T-a",
                "claims_for T-a",
                "lease T-a A05",
                "agent_for T-b",
                "claims_for T-b",
                "lease T-b A05"
            ]
        );
    }

    #[test]
    fn the_waiting_line_names_a_holder_only_when_one_is_known() {
        // python: f"{tid}: {claim.status}" + (f" (held by {holder})" if holder else "")
        for (holder, line) in [
            (None, "T-be: refused"),
            (Some(""), "T-be: refused"),
            (Some("s-9"), "T-be: refused (held by s-9)"),
        ] {
            let ports = RecordingPorts::new()
                .agent("T-be", "A05", 6)
                .lease_verdicts(&[("T-be", false, "refused", holder)]);
            let mut skipped = Vec::new();
            let batch = select_batch(
                &ports,
                &["T-be"],
                3,
                &HashMap::new(),
                &mut ClaimTable::new(),
                &mut false,
                &mut skipped,
            )
            .expect("selection succeeds");
            assert_eq!(batch.waiting, [line]);
            assert_eq!(pairs(&skipped), [("T-be", "lease:refused")]);
        }
    }

    /// `Ok(None)` means leases are off for the run: the port is asked once, then never again, and the flag comes back.
    #[test]
    fn a_lease_port_that_is_off_is_asked_once() {
        let ports = RecordingPorts::new()
            .agent("T-1", "A05", 6)
            .agent("T-2", "A05", 6)
            .agent("T-3", "A05", 6);
        let mut leases_off = false;
        let batch = select_batch(
            &ports,
            &["T-1", "T-2", "T-3"],
            3,
            &HashMap::new(),
            &mut ClaimTable::new(),
            &mut leases_off,
            &mut Vec::new(),
        )
        .expect("selection succeeds");
        assert_eq!(ids(&batch), ["T-1", "T-2", "T-3"]);
        assert!(leases_off);
        let leases: Vec<String> = ports
            .calls()
            .into_iter()
            .filter(|call| call.starts_with("lease"))
            .collect();
        assert_eq!(leases, ["lease T-1 A05"]);
    }

    #[test]
    fn a_granted_lease_keeps_the_claims_and_takes_the_slot() {
        let ports = RecordingPorts::new()
            .agent("T-a", "A05", 6)
            .claim("T-a", &[("src/**", false)])
            .lease_verdicts(&[]);
        let mut claims = ClaimTable::new();
        let batch = select_batch(
            &ports,
            &["T-a"],
            1,
            &HashMap::new(),
            &mut claims,
            &mut false,
            &mut Vec::new(),
        )
        .expect("selection succeeds");
        assert_eq!(ids(&batch), ["T-a"]);
        assert_eq!(batch.dispatch[0].agent.agent_id, "A05");
        assert_eq!(owners(&claims), ["T-a"]);
        assert!(batch.waiting.is_empty());
    }

    #[test]
    fn a_port_failure_ends_the_selection_with_the_original_error() {
        let mut ports = RecordingPorts::new()
            .agent("T-a", "A05", 6)
            .agent("T-b", "A05", 6)
            .agent("T-c", "A05", 6)
            .lease_verdicts(&[]);
        ports.fail_lease_for = Some("T-b");
        let mut skipped = Vec::new();
        let Err(error) = select_batch(
            &ports,
            &["T-a", "T-b", "T-c"],
            3,
            &HashMap::new(),
            &mut ClaimTable::new(),
            &mut false,
            &mut skipped,
        ) else {
            panic!("the lease failure must surface");
        };
        assert_eq!(error.summary(), "RuntimeError: substrate unreachable");
        assert!(!ports.calls().iter().any(|call| call.contains("T-c")));
    }

    // ---- plan_pass -------------------------------------------------------------------------------------------------------

    struct World {
        config: RunConfig,
        limiter_current: u32,
        dispatched: u64,
        in_flight: usize,
        per_class: HashMap<String, u32>,
        internal_failures: HashMap<String, u32>,
        now_wall: f64,
        leases_off: bool,
    }

    impl World {
        fn new() -> Self {
            Self {
                config: RunConfig::default(),
                limiter_current: 32,
                dispatched: 0,
                in_flight: 0,
                per_class: HashMap::new(),
                internal_failures: HashMap::new(),
                now_wall: 1000.0,
                leases_off: true,
            }
        }

        fn state(&self) -> PassState<'_> {
            PassState {
                config: &self.config,
                limiter_current: self.limiter_current,
                dispatched: self.dispatched,
                in_flight: self.in_flight,
                per_class: &self.per_class,
                internal_failures: &self.internal_failures,
                now_wall: self.now_wall,
                leases_off: self.leases_off,
            }
        }

        fn plan(&self, ports: &RecordingPorts, rows: &[ReadyRow]) -> PassPlan {
            plan_pass(ports, rows, &self.state(), &mut ClaimTable::new()).expect("pass succeeds")
        }
    }

    fn ports_for(tasks: &[&str]) -> RecordingPorts {
        tasks.iter().fold(RecordingPorts::new(), |ports, task| {
            ports.agent(task, "A05", 64)
        })
    }

    /// python `test_order_ready_is_stable_for_ties` (tests/test_scheduler_continuous.py:318): `z, y, w, x`; the order the tasks
    /// are asked for an agent is the dispatch order.
    #[test]
    fn rows_are_selected_in_order_ready_order() {
        let rows = [
            ("x", "P2", 1, 5.0),
            ("y", "P2", 0, 9.0),
            ("z", "P1", 3, 1.0),
            ("w", "P2", 0, 9.0),
        ]
        .map(|(id, priority, depth, created)| ReadyRow {
            task_id: id.to_owned(),
            priority: Some(priority.to_owned()),
            dag_depth: Some(depth),
            created_at: Some(created),
            retry_after: None,
        });
        let ports = ports_for(&["x", "y", "z", "w"]);
        let plan = World::new().plan(&ports, &rows);
        assert_eq!(ids(&plan.batch), ["z", "y", "w", "x"]);
        assert_eq!(
            ports
                .calls()
                .iter()
                .filter(|c| c.starts_with("agent_for"))
                .collect::<Vec<_>>(),
            ["agent_for z", "agent_for y", "agent_for w", "agent_for x"]
        );
    }

    #[test]
    fn an_unknown_or_missing_priority_ranks_as_p2() {
        let make = |id: &str, priority: Option<&str>| ReadyRow {
            priority: priority.map(str::to_owned),
            ..row(id)
        };
        let rows = [
            make("low", Some("P3")),
            make("bogus", Some("P9")),
            make("none", None),
            make("top", Some("P0")),
            make("p2", Some("P2")),
        ];
        let ports = ports_for(&["low", "bogus", "none", "top", "p2"]);
        let mut world = World::new();
        world.config.max_concurrent = 8;
        let plan = world.plan(&ports, &rows);
        assert_eq!(ids(&plan.batch), ["top", "bogus", "none", "p2", "low"]);
    }

    /// python `until > now` (swarm_run.py:1938): equal is eligible; the reason is `until - now` with one decimal, rounded the way
    /// Python's `:.1f` rounds (exact binary value, ties to even), and the wake time is the earliest gated `retry_after`.
    #[test]
    fn the_backoff_gate_is_strict_and_formats_like_python() {
        let held = |id: &str, until: f64| ReadyRow {
            retry_after: Some(until),
            ..row(id)
        };
        // (retry_after - 1000.0, eligible, python f"{until - now:.1f}")
        for (offset, eligible, shown) in [
            (0.0, true, ""),
            (-0.5, true, ""),
            (0.04, false, "0.0"),
            (0.25, false, "0.2"),
            (0.5, false, "0.5"),
            (2.75, false, "2.8"),
            (1.25, false, "1.2"),
            (30.0, false, "30.0"),
        ] {
            let ports = ports_for(&["t"]);
            let plan = World::new().plan(&ports, &[held("t", 1000.0 + offset)]);
            if eligible {
                assert_eq!(ids(&plan.batch), ["t"], "offset {offset}");
                assert!(plan.skipped.is_empty() && plan.backoff_wall.is_none());
            } else {
                assert!(plan.batch.dispatch.is_empty(), "offset {offset}");
                assert_eq!(
                    pairs(&plan.skipped),
                    [("t", format!("backoff:{shown}s").as_str())]
                );
                assert_eq!(plan.backoff_wall, Some(1000.0 + offset));
                assert!(ports.calls().is_empty(), "a gated task is never asked for");
            }
        }
    }

    #[test]
    fn the_wake_time_is_the_earliest_gated_retry_after() {
        let held = |id: &str, until: f64| ReadyRow {
            retry_after: Some(until),
            ..row(id)
        };
        let ports = ports_for(&["a", "b", "c"]);
        let plan = World::new().plan(
            &ports,
            &[held("a", 1040.0), held("b", 1010.0), held("c", 1020.0)],
        );
        assert_eq!(plan.backoff_wall, Some(1010.0));
        assert_eq!(plan.ready, 3);
        assert!(plan.batch.dispatch.is_empty());
    }

    #[test]
    fn a_nan_retry_after_never_holds_a_task_back() {
        let rows = [ReadyRow {
            retry_after: Some(f64::NAN),
            ..row("t")
        }];
        let plan = World::new().plan(&ports_for(&["t"]), &rows);
        assert_eq!(ids(&plan.batch), ["t"]);
        assert!(plan.backoff_wall.is_none());
    }

    /// python `self._internal.get(tid, 0) >= INTERNAL_FAILURE_LIMIT`; the backoff test comes first.
    #[test]
    fn the_internal_failure_limit_is_inclusive_and_yields_to_the_backoff_gate() {
        let mut world = World::new();
        world.internal_failures = counts(&[("two", 2), ("three", 3), ("many", 9), ("both", 3)]);
        let rows = [
            row("none"),
            row("two"),
            row("three"),
            row("many"),
            ReadyRow {
                retry_after: Some(1005.0),
                ..row("both")
            },
        ];
        let ports = ports_for(&["none", "two", "three", "many", "both"]);
        let plan = world.plan(&ports, &rows);
        assert_eq!(ids(&plan.batch), ["none", "two"]);
        assert_eq!(
            pairs(&plan.skipped),
            [
                ("three", "internal-failure"),
                ("many", "internal-failure"),
                ("both", "backoff:5.0s")
            ]
        );
        assert_eq!(plan.backoff_wall, Some(1005.0));
        world.config.internal_failure_limit = 10;
        assert_eq!(
            world
                .plan(&ports_for(&["many"]), &[row("many")])
                .batch
                .dispatch
                .len(),
            1
        );
    }

    /// The Python `skipped` dict lists the backoff and internal-failure skips (added while the rows are filtered, in dispatch order)
    /// before the selection's skips.
    #[test]
    fn skips_are_listed_filter_first_then_selection_in_dispatch_order() {
        let mut world = World::new();
        world.internal_failures = counts(&[("sick", 3)]);
        world.config.max_concurrent = 1;
        let make = |id: &str, priority: &str, retry_after: Option<f64>| ReadyRow {
            priority: Some(priority.to_owned()),
            retry_after,
            ..row(id)
        };
        // dispatch order: sick (P0), first (P1), waiting (P2), late (P3)
        let rows = [
            make("late", "P3", None),
            make("waiting", "P2", Some(1500.0)),
            make("first", "P1", None),
            make("sick", "P0", None),
        ];
        let plan = world.plan(&ports_for(&["late", "first"]), &rows);
        assert_eq!(ids(&plan.batch), ["first"]);
        assert_eq!(
            pairs(&plan.skipped),
            [
                ("sick", "internal-failure"),
                ("waiting", "backoff:500.0s"),
                ("late", "no-free-slot")
            ]
        );
        assert_eq!(plan.ready, 4);
    }

    /// `eff = max(1, min(max_concurrent, limiter))`, `free = max(0, eff - in_flight)`, and in continuous mode
    /// `free = min(free, max(0, budget - dispatched))`; the rounds barrier ignores the budget.
    #[test]
    fn effective_parallelism_and_free_slots_follow_python() {
        // (max_concurrent, limiter, in_flight, barrier, budget, dispatched) -> (eff, free)
        let cases = [
            (4, 32, 0, false, 200, 0, 4, 4),
            (4, 2, 1, false, 200, 0, 2, 1),
            (4, 0, 0, false, 200, 0, 1, 1),
            (2, 32, 5, false, 200, 0, 2, 0),
            (4, 32, 1, false, 10, 9, 4, 1),
            (4, 32, 0, false, 10, 10, 4, 0),
            (4, 32, 0, false, 10, 99, 4, 0),
            (4, 32, 0, true, 10, 99, 4, 4),
            (4, 32, 3, true, 1, 0, 4, 1),
        ];
        for (max_concurrent, limiter, in_flight, barrier, budget, dispatched, eff, free) in cases {
            let mut world = World::new();
            world.config.max_concurrent = max_concurrent;
            world.config.barrier = barrier;
            world.config.budget = budget;
            world.limiter_current = limiter;
            world.in_flight = in_flight;
            world.dispatched = dispatched;
            let plan = world.plan(&RecordingPorts::new(), &[]);
            assert_eq!(
                (plan.effective_parallel, plan.free),
                (eff, free),
                "case {max_concurrent} {limiter} {in_flight} {barrier} {budget} {dispatched}"
            );
        }
    }

    #[test]
    fn the_free_slots_bound_the_batch_and_the_running_classes_bound_each_agent() {
        let mut world = World::new();
        world.config.max_concurrent = 3;
        world.in_flight = 1;
        world.per_class = counts(&[("A12", 1)]);
        let rows = [row("a"), row("b"), row("c")];
        let ports = RecordingPorts::new()
            .agent("a", "A12", 2)
            .agent("b", "A12", 2)
            .agent("c", "A05", 6);
        let plan = world.plan(&ports, &rows);
        assert_eq!(plan.free, 2);
        assert_eq!(ids(&plan.batch), ["a", "c"]);
        assert_eq!(pairs(&plan.skipped), [("b", "class-ceiling:A12")]);
    }

    #[test]
    fn the_lease_flag_goes_in_and_comes_back_out() {
        let rows = [row("a"), row("b")];
        let mut world = World::new();
        world.leases_off = false;
        let ports = ports_for(&["a", "b"]);
        let plan = world.plan(&ports, &rows);
        assert!(plan.leases_off);
        assert_eq!(
            ports
                .calls()
                .iter()
                .filter(|c| c.starts_with("lease"))
                .count(),
            1
        );

        world.leases_off = true;
        let ports = ports_for(&["a", "b"]);
        assert!(world.plan(&ports, &rows).leases_off);
        assert!(!ports.calls().iter().any(|c| c.starts_with("lease")));
    }

    /// Two tasks of one agent with one claim each: the common start of the checkpoint tests.
    fn two_claimed_tasks() -> RecordingPorts {
        RecordingPorts::new()
            .agent("T-1", "A05", 6)
            .claim("T-1", &[("src/a", false)])
            .agent("T-2", "A05", 6)
            .claim("T-2", &[("src/b", false)])
    }

    /// A checkpoint that never stops leaves the plan and the port calls exactly as the public path leaves them.
    #[test]
    fn a_checkpoint_that_never_stops_changes_neither_the_plan_nor_the_calls() {
        let rows = [row("T-1"), row("T-2")];
        let mut world = World::new();
        world.leases_off = false;
        let public_ports = two_claimed_tasks().lease_verdicts(&[("T-2", false, "busy", None)]);
        let public = world.plan(&public_ports, &rows);
        let checked_ports = two_claimed_tasks().lease_verdicts(&[("T-2", false, "busy", None)]);
        let mut never_stop = || -> Result<bool, PortError> { Ok(false) };
        let controlled = plan_pass_controlled(
            &checked_ports,
            &rows,
            &world.state(),
            &mut ClaimTable::new(),
            Some(&mut never_stop),
        )
        .expect("the pass succeeds");
        assert!(!controlled.stopped);
        assert_eq!(controlled.plan, public);
        assert_eq!(checked_ports.calls(), public_ports.calls());
        assert_eq!(
            public_ports.calls(),
            vec![
                "agent_for T-1",
                "claims_for T-1",
                "lease T-1 A05",
                "agent_for T-2",
                "claims_for T-2",
                "lease T-2 A05"
            ]
        );
    }

    /// A stop asked before a lease gives back the claim of that candidate and asks nothing else: no lease, no other task.
    #[test]
    fn a_stop_before_a_lease_gives_back_the_claim_of_that_candidate_and_asks_no_more() {
        let ports = two_claimed_tasks();
        let mut world = World::new();
        world.leases_off = false;
        let mut claims = ClaimTable::new();
        let mut stop_before_lease = || -> Result<bool, PortError> {
            Ok(ports
                .calls()
                .last()
                .is_some_and(|call| call == "claims_for T-1"))
        };
        let controlled = plan_pass_controlled(
            &ports,
            &[row("T-1"), row("T-2")],
            &world.state(),
            &mut claims,
            Some(&mut stop_before_lease),
        )
        .expect("the pass succeeds");
        assert!(controlled.stopped);
        assert!(controlled.plan.batch.dispatch.is_empty());
        assert!(owners(&claims).is_empty(), "the claim of T-1 is given back");
        assert_eq!(ports.calls(), vec!["agent_for T-1", "claims_for T-1"]);
    }

    /// A stop after a selection leaves that selection's claims held: the caller releases them, since nothing was submitted.
    #[test]
    fn a_stop_after_a_selection_keeps_that_selections_claims_for_the_caller() {
        let ports = two_claimed_tasks();
        let mut world = World::new();
        world.leases_off = false;
        let mut claims = ClaimTable::new();
        let mut stop_at_second_agent = || -> Result<bool, PortError> {
            Ok(ports
                .calls()
                .last()
                .is_some_and(|call| call == "agent_for T-2"))
        };
        let controlled = plan_pass_controlled(
            &ports,
            &[row("T-1"), row("T-2")],
            &world.state(),
            &mut claims,
            Some(&mut stop_at_second_agent),
        )
        .expect("the pass succeeds");
        assert!(controlled.stopped);
        let selected: Vec<&str> = controlled
            .plan
            .batch
            .dispatch
            .iter()
            .map(|dispatch| dispatch.task_id.as_str())
            .collect();
        assert_eq!(selected, vec!["T-1"]);
        assert_eq!(owners(&claims), vec!["T-1"]);
        assert_eq!(
            ports.calls(),
            vec![
                "agent_for T-1",
                "claims_for T-1",
                "lease T-1 A05",
                "agent_for T-2"
            ]
        );
    }
}
