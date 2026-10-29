//! The decision loop of the scheduler runtime (Phase 14.4, unit S2.4): [`run`].
//!
//! This is the Rust form of `Scheduler.loop`, `_schedule`, `_submit`, `_complete`, `_backpressure`, `_schedule_retry`, `_idle` and
//! `_cancel_drain` in `scripts/swarm_run.py`, which stay the oracle. Everything the loop needs from the host is a port of
//! [`SchedulerPorts`]; the selection itself is [`crate::select::plan_pass`]; the workers run on a [`crate::pool::Pool`].
//!
//! # What the loop does
//!
//! One iteration is Python's, in Python's order:
//!
//! 1. poll [`SchedulerPorts::cancel_state`]; a cancel request runs the drain (below) and ends the run;
//! 2. when a pass is due (at the start, and after every completion) and the run is not closed and, in the rounds barrier, nothing
//!    is in flight: run a pass. [`SchedulerPorts::begin_pass`], read the wall clock, [`crate::select::plan_pass`], one
//!    [`SchedulerPorts::submit`] per selected task (the worker then runs on a pool thread), [`SchedulerPorts::observe_pass`]. The run
//!    closes after a pass when `once` is set, an extension failure was latched, the rounds barrier reached `max_rounds`, or
//!    continuous mode reached its dispatch `budget`;
//! 3. a pass that dispatched nothing while nothing is in flight is an idle pass: nothing ready ends the run
//!    ([`StopReason::NothingReady`]), tasks waiting on a retry backoff make the loop sleep until the earliest one, tasks waiting on
//!    leases end the run ([`StopReason::LeaseWait`]), and [`RunConfig::internal_failure_limit`] consecutive idle passes that could
//!    not dispatch ready tasks end it as [`StopReason::Stalled`];
//! 4. otherwise block on the pool for at most [`RunConfig::wait_tick_s`] and handle every finished dispatch.
//!
//! A finished dispatch is handled as `Scheduler._complete` does: the claims of the task are released, [`SchedulerPorts::complete`]
//! answers with a [`Completion`], then the loop applies the AIMD feed (a rate-limited session halves the parallelism once per burst,
//! a success adds one back after the cooldown, `fail_streak` failures of distinct tasks halve it once), then the retry delay
//! ([`crate::backoff::backoff_s`] plus [`SchedulerPorts::set_retry_after`]), then the `extension_down` latch.
//!
//! # Threads, blocking and locks
//!
//! [`run`] runs the whole loop on the **calling thread**, which the Python binding makes the interpreter's main thread, and blocks it
//! until the run ends. The workers run [`SchedulerPorts::worker`] on pool threads. The loop thread blocks on the pool with a timeout
//! (a condition variable: no CPU while idle) and sleeps in ticks while a backoff cools down; every blocking call is bounded by
//! `wait_tick_s`, so a cancel request is seen within one tick, plus whatever a port call itself takes.
//!
//! **No Rust lock or guard is held across a port call.** The loop state is owned by the loop thread alone and is not behind a lock;
//! the pool's mutex is held only for a few queue operations inside [`crate::pool::Pool`], never around a callback; pool jobs capture
//! the ports and plain strings only.
//!
//! # Errors, cancellation and panics
//!
//! A loop-thread port that returns an `Err` stops the loop at once and [`run`] returns [`RunError::Port`] with the original
//! [`PortError`] (the binding re-raises the Python exception it wraps). An invalid [`RunConfig`] is [`RunError::Sched`] with
//! [`SchedCode::Input`]. A panic of the loop itself, or of a loop-thread port, is caught: [`run`] returns [`RunError::Sched`] with
//! [`SchedCode::Internal`] and a fixed message that carries no panic text, and never unwinds into the caller. Workers are isolated
//! as the pool isolates them: a worker port `Err` is turned into [`SchedulerPorts::internal_failure`] on the pool thread, and a
//! worker panic ([`crate::pool::Outcome::Crashed`]) into the same call on the loop thread, both with an `E-INTERNAL` text and never
//! with a panic payload.
//!
//! The cancel drain is `_cancel_drain`: [`SchedulerPorts::cancel_begin`] (flag, degraded record, log line), wait the cooperative
//! grace of [`crate::drain::plan`] counted from the request unless a force request arrives, [`SchedulerPorts::kill`] once, wait
//! the kill grace plus the settle time, then report what is still in flight as [`RunSummary::abandoned`]. Abandoned workers stay
//! detached on their threads; the pool never joins them.

use std::collections::{HashMap, VecDeque};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::aimd::AimdLimiter;
use crate::backoff::backoff_s;
use crate::claims::ClaimTable;
use crate::drain;
use crate::pool::{Completion as PoolCompletion, Outcome, Pool};
use crate::ports::{
    CancelState, Completion, PassInfo, PortError, RunConfig, RunError, RunSummary, SchedCode,
    SchedError, SchedulerPorts, StopReason, Submission, WorkerDone, BLOCK_REASON_MAX_CHARS,
};
use crate::select::{plan_pass, Dispatch, PassPlan, PassState};

#[cfg(test)]
mod fake;
#[cfg(test)]
mod tests;

/// What a pool job returns: the worker's answer, or the failure of `internal_failure` itself (an adapter fault that stops the run).
type WorkerResult = Result<WorkerDone, PortError>;

/// The source of wall-clock seconds since the epoch (`time.time()`): the system clock in [`run`], a scripted one in the tests.
type WallClock = Arc<dyn Fn() -> f64 + Send + Sync>;

/// The jitter of the retry delay: the default of `swarm.retry.backoff_s` (`jitter=0.25`).
const RETRY_JITTER: f64 = 0.25;

/// The attempt whose delay is the configured cap: `backoff_s(62, jitter=0)` in `Scheduler._schedule_retry`.
const CAP_ATTEMPT: i64 = 62;

/// The `internal_failure` text of a worker that panicked. The panic payload is never read.
const WORKER_PANICKED: &str = "E-INTERNAL: worker panicked";

/// The `internal_failure` text of a dispatch the pool could not start (no thread available).
const WORKER_NOT_STARTED: &str = "E-INTERNAL: the worker thread could not be started";

/// The message of the fault of a completion for a dispatch the loop does not know: an invariant of the host, never a user error.
const UNKNOWN_DISPATCH: &str = "a completion arrived for a dispatch that is not in flight";

/// The message of the fault a panic of the loop becomes.
const LOOP_PANICKED: &str = "the scheduler loop panicked";

/// One dispatch the loop is waiting for.
struct Inflight {
    task_id: String,
    agent_id: String,
    /// When the dispatch was set up (`RunHandle.submitted_at`): the once-per-burst gate compares it with the last decrease.
    submitted_at: Instant,
}

/// What one pass left for the loop (the Python `Pass`).
struct Pass {
    ready: usize,
    submitted: usize,
    waiting: Vec<String>,
    backoff_wall: Option<f64>,
}

/// What the loop does after an idle pass (the Python `_idle`).
enum Idle {
    Stop(StopReason),
    Sleep,
    Retry,
}

/// A dispatch that finished: already settled before it reached the pool, or read from the pool's completion queue.
enum Finished {
    Settled(WorkerDone),
    Pool(PoolCompletion<WorkerResult>),
}

/// Runs the scheduling loop to the end and returns how it ended; see the [module documentation](self).
///
/// `ports` is the host: the same object serves the loop thread and the pool threads. `config` is validated first
/// ([`RunConfig::validate`]) and copied; the environment is never read.
///
/// **Blocks the calling thread** until the run ends (nothing left to dispatch, a stop condition, or a cancel drain that ran its
/// course) and runs every loop-thread port on it. Workers run on `config.max_concurrent` detached pool threads at most; a worker
/// that never returns after a cancel is abandoned, not joined, and is named in [`RunSummary::abandoned`].
///
/// # Errors
///
/// [`RunError::Sched`] for an invalid configuration ([`SchedCode::Input`]) or a panic of the loop or of a loop-thread port
/// ([`SchedCode::Internal`], no panic text). [`RunError::Port`] with the original [`PortError`] when a loop-thread port, or
/// [`SchedulerPorts::internal_failure`], returned an `Err`: the loop stops at once, dispatches in flight stay detached.
pub fn run(ports: Arc<dyn SchedulerPorts>, config: &RunConfig) -> Result<RunSummary, RunError> {
    let pool = Arc::new(Pool::new(config.max_concurrent as usize));
    let wall: WallClock = Arc::new(|| {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |elapsed| elapsed.as_secs_f64())
    });
    run_with(ports, config, wall, pool)
}

/// [`run`] with the wall clock and the pool supplied by the caller, so the tests can script the clock and look at the pool.
fn run_with(
    ports: Arc<dyn SchedulerPorts>,
    config: &RunConfig,
    wall: WallClock,
    pool: Arc<Pool<WorkerResult>>,
) -> Result<RunSummary, RunError> {
    let mut runtime = Runtime::new(ports, config, wall, pool)?;
    match catch_unwind(AssertUnwindSafe(|| runtime.drive())) {
        Ok(result) => result,
        Err(payload) => {
            // dropping a payload can itself panic: contain that too, the run is over either way
            let _ = catch_unwind(AssertUnwindSafe(|| drop(payload)));
            Err(RunError::Sched(SchedError::new(
                SchedCode::Internal,
                LOOP_PANICKED,
            )))
        }
    }
}

/// The state of one run, owned by the loop thread.
struct Runtime {
    ports: Arc<dyn SchedulerPorts>,
    config: RunConfig,
    wall: WallClock,
    pool: Arc<Pool<WorkerResult>>,
    /// `RunConfig::wait_tick_s` as a duration.
    tick: Duration,
    limiter: AimdLimiter,
    /// When the limiter last dropped (`Scheduler._last_decrease`): a rate-limit hit from a dispatch set up before it belongs to
    /// the same provider burst.
    last_decrease: Option<Instant>,
    claims: ClaimTable,
    /// The dispatches in flight, in submission order (`Scheduler.inflight`).
    inflight: Vec<Inflight>,
    /// In-flight dispatches per agent id (the class counts of `select_batch`).
    per_class: HashMap<String, u32>,
    /// Internal failures per task id (`Scheduler._internal`).
    internal_failures: HashMap<String, u32>,
    /// Dispatches that finished without ever reaching a pool thread; read before the pool by the next wait.
    settled: VecDeque<WorkerDone>,
    rounds: u64,
    dispatched: u64,
    closed: bool,
    extension_down: bool,
    leases_off: bool,
    /// The earliest backoff end of the last pass (`Scheduler._wake_wall`).
    wake_wall: Option<f64>,
    /// The waiting lines of the pass that ended the run as a lease wait.
    waiting: Vec<String>,
}

impl Runtime {
    fn new(
        ports: Arc<dyn SchedulerPorts>,
        config: &RunConfig,
        wall: WallClock,
        pool: Arc<Pool<WorkerResult>>,
    ) -> Result<Self, SchedError> {
        config.validate()?;
        let seconds = |value: f64| {
            Duration::try_from_secs_f64(value).map_err(|_| {
                SchedError::new(
                    SchedCode::Input,
                    "a duration of the configuration is out of range",
                )
            })
        };
        let limiter = AimdLimiter::new(
            config.max_concurrent as usize,
            1,
            seconds(config.cooldown_s)?,
        )
        .map_err(|_| SchedError::new(SchedCode::Input, "max_concurrent must be >= 1"))?
        .with_streak_threshold(config.fail_streak as usize)
        .with_retry_cap(seconds(config.retry_cap_s)?);
        Ok(Self {
            ports,
            tick: seconds(config.wait_tick_s)?,
            config: config.clone(),
            wall,
            pool,
            limiter,
            last_decrease: None,
            claims: ClaimTable::new(),
            inflight: Vec::new(),
            per_class: HashMap::new(),
            internal_failures: HashMap::new(),
            settled: VecDeque::new(),
            rounds: 0,
            dispatched: 0,
            closed: false,
            extension_down: false,
            leases_off: false,
            wake_wall: None,
            waiting: Vec::new(),
        })
    }

    /// Runs the loop, then closes the pool whatever happened. Python: the body of `loop` and `_cancel_drain`.
    fn drive(&mut self) -> Result<RunSummary, RunError> {
        let stopped = self.run_loop();
        let shutdown = self.pool.shutdown(Duration::ZERO);
        let stopped = stopped?;
        let abandoned = if stopped == StopReason::Cancelled {
            let mut left = shutdown.abandoned;
            left.extend(shutdown.dropped);
            left.sort();
            left
        } else {
            Vec::new()
        };
        let parallel = self
            .config
            .max_concurrent
            .min(u32::try_from(self.limiter.current()).unwrap_or(u32::MAX))
            .max(1);
        Ok(RunSummary {
            rounds: self.rounds,
            dispatched: self.dispatched,
            effective_parallel: parallel,
            closed: self.closed,
            extension_down: self.extension_down,
            abandoned,
            stopped,
            waiting: std::mem::take(&mut self.waiting),
        })
    }

    /// Python: `Scheduler.loop`.
    fn run_loop(&mut self) -> Result<StopReason, RunError> {
        let ports = Arc::clone(&self.ports);
        let mut need_pass = true;
        let mut idle: u32 = 0;
        loop {
            let cancel = ports.cancel_state()?;
            let polled_at = Instant::now();
            if cancel.is_set {
                self.cancel_drain(&cancel, polled_at)?;
                return Ok(StopReason::Cancelled);
            }
            let barrier = self.config.barrier;
            if need_pass && !self.closed && !(barrier && !self.inflight.is_empty()) {
                need_pass = false;
                let pass = self.schedule()?;
                if self.config.once
                    || self.extension_down
                    || (barrier && self.rounds >= self.config.max_rounds)
                    || (!barrier && self.dispatched >= self.config.budget)
                {
                    self.closed = true;
                }
                if self.inflight.is_empty() && pass.submitted == 0 {
                    let verdict = self.idle(&pass);
                    idle = if matches!(verdict, Idle::Retry) {
                        idle + 1
                    } else {
                        0
                    };
                    match verdict {
                        Idle::Stop(reason) => return Ok(reason),
                        Idle::Retry if idle >= self.config.internal_failure_limit => {
                            return Ok(StopReason::Stalled);
                        }
                        Idle::Sleep => {
                            if let Some(wake) = pass.backoff_wall {
                                self.sleep_until(wake)?;
                            }
                        }
                        Idle::Retry => {}
                    }
                    need_pass = true;
                    continue;
                }
            }
            if self.inflight.is_empty() {
                if self.closed {
                    return Ok(StopReason::Closed);
                }
                need_pass = true;
                continue;
            }
            let finished = self.wait_finished(self.tick);
            if finished.is_empty() {
                // nothing finished within the tick: a backoff that has cooled down makes a pass due
                if self.wake_wall.is_some_and(|wake| (self.wall)() >= wake) {
                    need_pass = true;
                }
                continue;
            }
            for item in finished {
                self.finish(item)?;
            }
            need_pass = true;
        }
    }

    /// One scheduling pass. Python: `Scheduler._schedule` (the host does `reconcile` and the telemetry registration inside
    /// `begin_pass`, and `_tick` inside `observe_pass`).
    fn schedule(&mut self) -> Result<Pass, RunError> {
        self.rounds += 1;
        let ports = Arc::clone(&self.ports);
        let rows = {
            let ids: Vec<&str> = self
                .inflight
                .iter()
                .map(|item| item.task_id.as_str())
                .collect();
            ports.begin_pass(self.rounds, &ids)?
        };
        // Python reads `time.time()` after `dispatchable` returned
        let now_wall = (self.wall)();
        let state = PassState {
            config: &self.config,
            limiter_current: u32::try_from(self.limiter.current()).unwrap_or(u32::MAX),
            dispatched: self.dispatched,
            in_flight: self.inflight.len(),
            per_class: &self.per_class,
            internal_failures: &self.internal_failures,
            now_wall,
            leases_off: self.leases_off,
        };
        let plan: PassPlan = plan_pass(&*ports, &rows, &state, &mut self.claims)?;
        self.leases_off = plan.leases_off;
        for dispatch in &plan.batch.dispatch {
            self.submit(dispatch)?;
        }
        let ids: Vec<&str> = self
            .inflight
            .iter()
            .map(|item| item.task_id.as_str())
            .collect();
        ports.observe_pass(&PassInfo {
            round: self.rounds,
            ready: plan.ready,
            submitted: plan.batch.dispatch.len(),
            effective_parallel: plan.effective_parallel,
            free: plan.free,
            in_flight: &ids,
            skipped: &plan.skipped,
        })?;
        self.wake_wall = plan.backoff_wall;
        Ok(Pass {
            ready: plan.ready,
            submitted: plan.batch.dispatch.len(),
            waiting: plan.batch.waiting,
            backoff_wall: plan.backoff_wall,
        })
    }

    /// Sets one selected task up and queues its worker. Python: `Scheduler._submit`.
    fn submit(&mut self, dispatch: &Dispatch) -> Result<(), RunError> {
        let submitted_at = Instant::now();
        let task_id = &dispatch.task_id;
        let agent_id = &dispatch.agent.agent_id;
        let submission = self.ports.submit(task_id, agent_id)?;
        self.dispatched += 1;
        self.inflight.push(Inflight {
            task_id: task_id.clone(),
            agent_id: agent_id.clone(),
            submitted_at,
        });
        *self.per_class.entry(agent_id.clone()).or_insert(0) += 1;
        let sid = match submission {
            Submission::Started { sid } => sid,
            Submission::Failed(done) => {
                self.settled.push_back(done);
                return Ok(());
            }
        };
        let ports = Arc::clone(&self.ports);
        let (task, agent) = (task_id.clone(), agent_id.clone());
        let queued = self.pool.submit(task_id.clone(), move || {
            match ports.worker(&task, &agent, &sid) {
                Ok(done) => Ok(done),
                // `_worker`'s own `except` path: this task fails with E-INTERNAL, on the pool thread
                Err(error) => {
                    let text: String = format!("E-INTERNAL: {}", error.summary())
                        .chars()
                        .take(BLOCK_REASON_MAX_CHARS)
                        .collect();
                    ports.internal_failure(&task, &agent, &text)
                }
            }
        });
        if queued.is_err() {
            // the `except Exception` branch of `_submit`: a dispatch that cannot be queued fails that task only
            let done = self
                .ports
                .internal_failure(task_id, agent_id, WORKER_NOT_STARTED)?;
            self.settled.push_back(done);
        }
        Ok(())
    }

    /// The verdict after a pass that dispatched nothing with nothing in flight. Python: `Scheduler._idle` (the `stalled: [...]`
    /// line needs the store and is the host's).
    fn idle(&mut self, pass: &Pass) -> Idle {
        if pass.ready == 0 {
            return Idle::Stop(StopReason::NothingReady);
        }
        if pass.backoff_wall.is_some() && !self.closed {
            return Idle::Sleep;
        }
        if !pass.waiting.is_empty() {
            self.waiting.clone_from(&pass.waiting);
            return Idle::Stop(StopReason::LeaseWait);
        }
        Idle::Retry
    }

    /// Waits for a retry's backoff to pass, waking early on a cancel. Python: `Scheduler._sleep_until`.
    fn sleep_until(&self, wake: f64) -> Result<(), RunError> {
        loop {
            let left = wake - (self.wall)();
            if left <= 0.0 || self.ports.cancel_state()?.is_set {
                return Ok(());
            }
            thread::sleep(
                Duration::try_from_secs_f64(left).map_or(self.tick, |left| left.min(self.tick)),
            );
        }
    }

    /// The dispatches that finished: those settled before they reached the pool, else the first the pool delivers within `timeout`,
    /// then everything else it already has. Python: `Scheduler._wait` (`futures_wait(..., FIRST_COMPLETED).done`).
    fn wait_finished(&mut self, timeout: Duration) -> Vec<Finished> {
        let mut finished: Vec<Finished> = self.settled.drain(..).map(Finished::Settled).collect();
        if finished.is_empty() {
            if let Some(first) = self.pool.next_completion(timeout) {
                finished.push(Finished::Pool(first));
            }
        }
        while let Some(next) = self.pool.try_next_completion() {
            finished.push(Finished::Pool(next));
        }
        finished
    }

    /// Handles one finished dispatch. Python: `Scheduler._complete`, from the crash path on.
    fn finish(&mut self, finished: Finished) -> Result<(), RunError> {
        let done = match finished {
            Finished::Settled(done) => done,
            Finished::Pool(completion) => match completion.outcome {
                Outcome::Done(result) => result?,
                // a panic escaped the worker's own guard: still this task's failure only (`future.exception()` branch)
                Outcome::Crashed => {
                    let agent_id = self
                        .inflight
                        .iter()
                        .find(|item| item.task_id == completion.task_id)
                        .map(|item| item.agent_id.clone())
                        .ok_or_else(|| SchedError::new(SchedCode::Internal, UNKNOWN_DISPATCH))?;
                    self.ports
                        .internal_failure(&completion.task_id, &agent_id, WORKER_PANICKED)?
                }
            },
        };
        let position = self
            .inflight
            .iter()
            .position(|item| item.task_id == done.task_id)
            .ok_or_else(|| SchedError::new(SchedCode::Internal, UNKNOWN_DISPATCH))?;
        let item = self.inflight.remove(position);
        if let Some(count) = self.per_class.get_mut(&item.agent_id) {
            *count = count.saturating_sub(1);
        }
        self.claims.release(&item.task_id);
        if done.error.is_some() {
            *self
                .internal_failures
                .entry(item.task_id.clone())
                .or_insert(0) += 1;
        }
        let completion = self.ports.complete(&done)?;
        self.backpressure(&item, &completion, Instant::now())?;
        self.schedule_retry(&item, &completion)?;
        if completion.extension_down && !self.extension_down {
            self.extension_down = true;
            self.closed = true;
            // swarm_run.py:2108
            self.ports
                .log("stopped: omp extension failed to load; no further sessions this run")?;
        }
        Ok(())
    }

    /// The AIMD feed of one completion. Python: `Scheduler._backpressure` (`swarm_run.py:2110`); the `swarm.degraded` records it
    /// writes are not part of the contract (the host derives them from the effective parallelism `observe_pass` reports).
    fn backpressure(
        &mut self,
        item: &Inflight,
        completion: &Completion,
        now: Instant,
    ) -> Result<(), RunError> {
        let task = &item.task_id;
        let before = self.limiter.current();
        if let Some(hit) = completion.rate_limited {
            if self
                .last_decrease
                .is_some_and(|last| item.submitted_at < last)
            {
                // swarm_run.py:2120: one provider burst hits every session already running: one decrease, not k
                self.ports.log(&format!(
                    "rate limited at {task}: same burst, effective parallelism stays {before}"
                ))?;
                return Ok(());
            }
            // Python `on_rate_limit(run.rate_limited or None)`: zero, a negative and a NaN hold nothing extra
            let retry_after = (hit > 0.0)
                .then(|| Duration::try_from_secs_f64(hit.min(self.config.retry_cap_s)).ok())
                .flatten();
            self.limiter.on_rate_limit(now, retry_after);
            let after = self.limiter.current();
            if after < before {
                self.last_decrease = Some(now);
            }
            // swarm_run.py:2126
            self.ports.log(&format!(
                "rate limited at {task}: effective parallelism {before} → {after}"
            ))?;
        } else if completion.failed {
            // circuit off, or a shutdown killing sessions: nothing counts
            if self.config.fail_streak == 0 || completion.cancelled {
                return Ok(());
            }
            if self.limiter.on_failure(now, task) {
                let after = self.limiter.current();
                // swarm_run.py:2139
                self.ports.log(&format!(
                    "{} failures in a row: effective parallelism {before} → {after}",
                    self.limiter.streak_len()
                ))?;
            }
        } else {
            self.limiter.on_success(now);
            let after = self.limiter.current();
            if after > before {
                // swarm_run.py:2149
                self.ports.log(&format!(
                    "rate limit cooled: effective parallelism {before} → {after}"
                ))?;
            }
        }
        Ok(())
    }

    /// The retry delay of a transient failure. Python: `Scheduler._schedule_retry` (`swarm_run.py:2153`); the store lookup and the
    /// transient classification are the host's and arrive as [`Completion::retry_attempt`].
    fn schedule_retry(&self, item: &Inflight, completion: &Completion) -> Result<(), RunError> {
        let Some(attempt) = completion.retry_attempt else {
            return Ok(());
        };
        let (base, cap) = (self.config.backoff_base_s, self.config.backoff_cap_s);
        let unit = self.ports.random_unit()?;
        let mut delay = backoff_s(i64::from(attempt.max(1)), base, cap, RETRY_JITTER, unit)
            .map_err(|_| {
                SchedError::new(SchedCode::Input, "the backoff configuration is invalid")
            })?;
        // a provider Retry-After lengthens the delay, up to the configured cap; a bare marker (0.0) does not
        if let Some(hit) = completion.rate_limited.filter(|hit| *hit != 0.0) {
            let ceiling = backoff_s(CAP_ATTEMPT, base, cap, 0.0, 0.0).map_err(|_| {
                SchedError::new(SchedCode::Input, "the backoff configuration is invalid")
            })?;
            // Python `max(delay, min(hit, ceiling))`: the first argument wins unless the second is strictly better
            let held = if ceiling < hit { ceiling } else { hit };
            if held > delay {
                delay = held;
            }
        }
        self.ports
            .set_retry_after(&item.task_id, (self.wall)() + delay)?;
        // swarm_run.py:2163
        self.ports.log(&format!(
            "{}: transient failure, re-dispatch in {delay:.1}s",
            item.task_id
        ))?;
        Ok(())
    }

    /// The cancel flow. Python: `Scheduler._cancel_drain` (`swarm_run.py:2226`); its log line, the degraded record and the flag are
    /// [`SchedulerPorts::cancel_begin`], the kill is [`SchedulerPorts::kill`], and `abandoned after kill: [...]` is the host's.
    fn cancel_drain(&mut self, cancel: &CancelState, polled_at: Instant) -> Result<(), RunError> {
        let config = &self.config;
        let plan = drain::plan(
            config.cancel_grace_s,
            config.shutdown_budget_s,
            config.kill_grace_s,
            drain::STOP_GRACE_S,
            config.kill_settle_s,
        );
        let reason = cancel.reason.as_deref().unwrap_or("cancelled").to_owned();
        // the grace is counted from the request, which was `elapsed_s` before the poll
        let left = Duration::try_from_secs_f64((plan.grace_s - cancel.elapsed_s).max(0.0))
            .unwrap_or(Duration::ZERO);
        let deadline = polled_at.checked_add(left).unwrap_or(polled_at);
        let ports = Arc::clone(&self.ports);
        ports.cancel_begin(&reason, plan.grace_s, self.inflight.len())?;
        while !self.inflight.is_empty() && Instant::now() < deadline && !ports.cancel_state()?.force
        {
            self.reap(deadline)?;
        }
        if !self.inflight.is_empty() {
            ports.kill(plan.kill_grace_s, &reason)?;
            let window = Duration::try_from_secs_f64(plan.kill_grace_s + plan.settle_s)
                .unwrap_or(Duration::ZERO);
            let hard = Instant::now().checked_add(window).unwrap_or(polled_at);
            while !self.inflight.is_empty() && Instant::now() < hard {
                self.reap(hard)?;
            }
        }
        Ok(())
    }

    /// Waits for finished dispatches until `deadline` at most one tick, and handles them.
    fn reap(&mut self, deadline: Instant) -> Result<(), RunError> {
        let timeout = self
            .tick
            .min(deadline.saturating_duration_since(Instant::now()));
        for item in self.wait_finished(timeout) {
            self.finish(item)?;
        }
        Ok(())
    }
}
