//! Tests of the loop against a scripted host ([`super::fake`]): real threads for the pool, gates and permits instead of sleeps, a
//! scripted wall clock for the backoff. The Python scenarios they replay are named in the comments.

use std::collections::HashMap;
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use super::fake::{Act, Cleanup, Fake, Inject, Phase, Task, PANIC_TEXT, WAIT};
use super::{run, run_with, Runtime, LOOP_PANICKED, WORKER_PANICKED};
use super::{WorkerResult, WORKER_NOT_STARTED};
use crate::pool::Pool;
use crate::ports::{
    Completion, LeaseVerdict, PortError, RunConfig, RunError, RunSummary, SchedCode,
    SchedulerPorts, StopReason,
};

/// A configuration for tests: a short tick and no AIMD cooldown, everything else the Python default.
fn config(max_concurrent: u32) -> RunConfig {
    RunConfig {
        max_concurrent,
        wait_tick_s: 0.005,
        cooldown_s: 0.0,
        ..RunConfig::default()
    }
}

/// A run on its own thread, so the test thread can steer the fake while the loop runs.
struct Running {
    result: mpsc::Receiver<Result<RunSummary, RunError>>,
    pool: Arc<Pool<WorkerResult>>,
}

impl Running {
    fn outcome(&self) -> Result<RunSummary, RunError> {
        self.result
            .recv_timeout(WAIT)
            .expect("the run ended within the test's time limit")
    }

    fn summary(&self) -> RunSummary {
        self.outcome().expect("the run succeeded")
    }
}

fn start(fake: &Arc<Fake>, config: RunConfig) -> Running {
    let pool = Arc::new(Pool::new(config.max_concurrent as usize));
    let (sender, result) = mpsc::channel();
    let ports: Arc<dyn SchedulerPorts> = Arc::clone(fake) as Arc<dyn SchedulerPorts>;
    let (wall, loop_pool) = (fake.wall(), Arc::clone(&pool));
    thread::Builder::new()
        .name("loop-under-test".to_owned())
        .spawn(move || {
            let _ = sender.send(run_with(ports, &config, wall, loop_pool));
        })
        .expect("the loop thread starts");
    Running { result, pool }
}

/// Runs `job` on its own thread and waits for its result, so a loop that hangs fails the test instead of hanging it.
fn on_loop_thread<T: Send + 'static>(job: impl FnOnce() -> T + Send + 'static) -> T {
    let (sender, result) = mpsc::channel();
    thread::Builder::new()
        .name("loop-under-test".to_owned())
        .spawn(move || {
            let _ = sender.send(job());
        })
        .expect("the loop thread starts");
    result
        .recv_timeout(WAIT)
        .expect("the run ended within the test's time limit")
}

fn run_to_end(fake: &Arc<Fake>, config: RunConfig) -> RunSummary {
    start(fake, config).summary()
}

fn tasks(count: usize) -> Vec<Task> {
    (1..=count).map(|n| Task::new(&format!("T{n}"))).collect()
}

fn logs(fake: &Fake) -> Vec<String> {
    fake.lock().logs.clone()
}

fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|item| (*item).to_owned()).collect()
}

fn failed(rate_limited: Option<f64>) -> Completion {
    Completion {
        failed: true,
        rate_limited,
        ..Completion::default()
    }
}

// ---- overlap, caps, refill, the rounds barrier, --once, the budget -------------------------------------------------------------

/// Overlap and the global cap (`test_scheduler_continuous.py`: the overlap and cap tests): three of six tasks run at once, a fourth
/// never starts while three run, and a finished worker's slot is refilled by the next pass.
#[test]
fn slots_overlap_up_to_the_global_cap_and_refill_when_a_worker_finishes() {
    let fake = Fake::new(
        (1..=6)
            .map(|n| Task::new(&format!("T{n}")).act(Act::Permit("DONE")))
            .collect(),
    );
    let run = start(&fake, config(3));
    fake.wait_until("three workers running at once", |s| s.started.len() == 3);
    thread::sleep(Duration::from_millis(30));
    assert_eq!(fake.lock().started.len(), 3, "the cap holds");
    fake.permit(1);
    fake.wait_until("the freed slot refilled", |s| s.started.len() == 4);
    {
        let state = fake.lock();
        assert!(
            !state.completed.is_empty(),
            "a slot is refilled by a completion, not before"
        );
        assert!(
            state
                .begin_args
                .iter()
                .any(|(_, in_flight)| in_flight.len() == 2),
            "the refill pass names the two tasks still running"
        );
    }
    fake.permit(5);
    let summary = run.summary();
    assert_eq!(summary.dispatched, 6);
    assert_eq!(summary.stopped, StopReason::NothingReady);
    assert_eq!(fake.lock().peak, 3, "never more than the cap at once");
}

/// The per-class ceiling: agent `A12` allows one session, so its three tasks never overlap, while another agent's task runs beside
/// the first of them.
#[test]
fn the_class_ceiling_keeps_an_agents_tasks_from_overlapping() {
    let fake = Fake::new(vec![
        Task::new("A1").agent("A12", 1).act(Act::Permit("DONE")),
        Task::new("A2").agent("A12", 1).act(Act::Permit("DONE")),
        Task::new("A3").agent("A12", 1).act(Act::Permit("DONE")),
        Task::new("B1").agent("B", 5).act(Act::Permit("DONE")),
    ]);
    let run = start(&fake, config(4));
    fake.wait_until("A1 and B1 running", |s| s.started.len() == 2);
    thread::sleep(Duration::from_millis(30));
    {
        let state = fake.lock();
        let mut started = state.started.clone();
        started.sort();
        assert_eq!(started, strings(&["A1", "B1"]));
        assert_eq!(
            state.passes[0].skipped,
            [
                ("A2".to_owned(), "class-ceiling:A12".to_owned()),
                ("A3".to_owned(), "class-ceiling:A12".to_owned())
            ]
        );
    }
    fake.permit(4);
    let summary = run.summary();
    assert_eq!(summary.dispatched, 4);
    assert_eq!(fake.lock().agent_running["A12"].1, 1);
}

/// The rounds barrier: no pass while anything is in flight, and at most `max_rounds` passes.
#[test]
fn the_rounds_barrier_never_starts_a_pass_while_anything_runs_and_stops_at_max_rounds() {
    let fake = Fake::new(tasks(5));
    let summary = run_to_end(
        &fake,
        RunConfig {
            barrier: true,
            max_rounds: 2,
            ..config(2)
        },
    );
    let state = fake.lock();
    assert!(
        state
            .begin_args
            .iter()
            .all(|(_, in_flight)| in_flight.is_empty()),
        "every pass started with nothing in flight: {:?}",
        state.begin_args
    );
    assert_eq!((summary.rounds, summary.dispatched), (2, 4));
    let rounds: Vec<u64> = state.passes.iter().map(|pass| pass.round).collect();
    assert_eq!(rounds, [1, 2]);
    assert!(summary.closed);
    assert_eq!(summary.stopped, StopReason::Closed);
    assert_eq!(state.phase("T5"), Phase::Pending);
}

/// `--once`: one pass, then the run closes and drains what that pass started.
#[test]
fn once_runs_a_single_pass_and_drains_it() {
    let fake = Fake::new(tasks(5));
    let summary = run_to_end(
        &fake,
        RunConfig {
            once: true,
            ..config(2)
        },
    );
    assert_eq!((summary.rounds, summary.dispatched), (1, 2));
    assert!(summary.closed);
    assert_eq!(summary.stopped, StopReason::Closed);
    let state = fake.lock();
    assert_eq!(state.completed.len(), 2, "both dispatches were completed");
    assert_eq!(state.phase("T3"), Phase::Pending);
}

/// Continuous mode's dispatch budget (`--max-rounds` times the cap) is a total: a pass never starts more than what is left of it.
#[test]
fn the_dispatch_budget_caps_the_total_and_closes_the_run() {
    let fake = Fake::new(tasks(6));
    let summary = run_to_end(
        &fake,
        RunConfig {
            budget: 3,
            ..config(2)
        },
    );
    assert_eq!(summary.dispatched, 3);
    assert!(summary.closed);
    assert_eq!(summary.stopped, StopReason::Closed);
    let state = fake.lock();
    let free: Vec<usize> = state.passes.iter().map(|pass| pass.free).collect();
    assert_eq!(free, [2, 1], "the second pass only had one dispatch left");
}

/// The port calls of one pass come in the order of Python's `_schedule`: `begin_pass`, the selection of every task, then one
/// `submit` per selected task, then `observe_pass`.
#[test]
fn a_pass_calls_the_ports_in_pythons_order() {
    let fake = Fake::new(tasks(2));
    fake.lock().leases = Some(HashMap::new());
    run_to_end(&fake, config(2));
    let calls: Vec<String> = fake
        .lock()
        .calls
        .iter()
        .filter(|call| *call != "cancel_state" && !call.starts_with("worker"))
        .take(10)
        .cloned()
        .collect();
    assert_eq!(
        calls,
        strings(&[
            "begin_pass 1",
            "agent_for T1",
            "claims_for T1",
            "lease T1 A",
            "agent_for T2",
            "claims_for T2",
            "lease T2 A",
            "submit T1",
            "submit T2",
            "observe_pass 1",
        ])
    );
}

/// A task's claims are released when its dispatch completes: the task held back by `claim-conflict:T1` starts in the next pass.
#[test]
fn claims_are_released_when_a_dispatch_completes() {
    let fake = Fake::new(vec![
        Task::new("T1").claims(&[("src/a.py", false)]),
        Task::new("T2").claims(&[("src/a.py", false)]),
    ]);
    let summary = run_to_end(&fake, config(2));
    let state = fake.lock();
    assert_eq!(
        state.passes[0].skipped,
        [("T2".to_owned(), "claim-conflict:T1".to_owned())]
    );
    assert_eq!(state.phase("T2"), Phase::Final("DONE".to_owned()));
    assert_eq!(summary.dispatched, 2);
    assert_eq!(summary.stopped, StopReason::NothingReady);
}

// ---- AIMD -------------------------------------------------------------------------------------------------------------------------

/// A burst of rate-limited completions is one decrease (`_backpressure`: "same burst"), however many sessions it hit.
#[test]
fn a_burst_of_rate_limited_completions_halves_the_parallelism_once() {
    let fake = Fake::new(
        (1..=4)
            .map(|n| {
                Task::new(&format!("T{n}"))
                    .act(Act::Return("FAILED"))
                    .comp(failed(Some(0.0)))
            })
            .collect(),
    );
    let summary = run_to_end(&fake, config(4));
    let logs = logs(&fake);
    let halved = logs
        .iter()
        .filter(|line| line.ends_with("effective parallelism 4 → 2"))
        .count();
    let same = logs
        .iter()
        .filter(|line| line.ends_with("same burst, effective parallelism stays 2"))
        .count();
    assert_eq!((halved, same, logs.len()), (1, 3, 4), "{logs:?}");
    assert_eq!(summary.effective_parallel, 2);
}

/// A session submitted after the last decrease starts a new burst, even when the limiter is already at its floor and cannot drop
/// (the gate is armed by a decrease only).
#[test]
fn a_session_submitted_after_the_decrease_is_a_new_burst() {
    let fake = Fake::new(
        (1..=3)
            .map(|n| {
                Task::new(&format!("T{n}"))
                    .act(Act::Return("FAILED"))
                    .comp(failed(Some(0.0)))
            })
            .collect(),
    );
    let summary = run_to_end(&fake, config(2));
    let logs = logs(&fake);
    assert_eq!(logs.len(), 3, "{logs:?}");
    assert!(logs[0].ends_with("effective parallelism 2 → 1"), "{logs:?}");
    assert!(
        logs[1].ends_with("same burst, effective parallelism stays 1"),
        "{logs:?}"
    );
    assert_eq!(logs[2], "rate limited at T3: effective parallelism 1 → 1");
    assert_eq!(summary.effective_parallel, 1);
}

/// Successes add one slot back each once the cooldown has passed, and stop at the cap.
#[test]
fn successes_after_a_rate_limit_grow_the_parallelism_back_to_the_cap() {
    let mut all = vec![Task::new("T1")
        .act(Act::Return("FAILED"))
        .comp(failed(Some(0.0)))];
    all.extend((2..=4).map(|n| Task::new(&format!("T{n}")).after(1)));
    let fake = Fake::new(all);
    let summary = run_to_end(&fake, config(4));
    assert_eq!(
        logs(&fake),
        strings(&[
            "rate limited at T1: effective parallelism 4 → 2",
            "rate limit cooled: effective parallelism 2 → 3",
            "rate limit cooled: effective parallelism 3 → 4",
        ])
    );
    assert_eq!(summary.effective_parallel, 4);
}

/// How one task of a streak scenario ends.
#[derive(Clone, Copy)]
enum Kind {
    Fail,
    Succeed,
    RateLimited,
    /// A failure handled while a cancel is already under way.
    Cancelled,
}

/// One scenario of the streak test: the outcomes of tasks that finish in order, the log lines and the parallelism it must end with.
struct StreakCase {
    name: &'static str,
    fail_streak: u32,
    kinds: &'static [Kind],
    logs: &'static [&'static str],
    parallel: u32,
}

/// The `SWARM_FAIL_STREAK` circuit: consecutive failures of distinct tasks halve the parallelism once; a success resets it; a
/// rate-limited failure neither counts nor resets; a failure of a session killed by a shutdown counts nothing; 0 turns it off.
#[test]
fn the_failure_streak_halves_once_and_a_success_resets_it() {
    use Kind::{Cancelled, Fail, RateLimited, Succeed};
    let case = |name, fail_streak, kinds, logs, parallel| StreakCase {
        name,
        fail_streak,
        kinds,
        logs,
        parallel,
    };
    let cases = [
        case(
            "three failures halve once",
            3,
            &[Fail; 6][..],
            &["3 failures in a row: effective parallelism 8 → 4"][..],
            4,
        ),
        case(
            "a success resets",
            3,
            &[Fail, Fail, Succeed, Fail, Fail],
            &[],
            8,
        ),
        case(
            "a full streak after a reset",
            3,
            &[Fail, Fail, Succeed, Fail, Fail, Fail],
            &["3 failures in a row: effective parallelism 8 → 4"],
            4,
        ),
        case("off", 0, &[Fail; 6], &[], 8),
        case(
            "a rate-limited failure neither counts nor resets",
            3,
            &[Fail, Fail, RateLimited, Fail],
            &[
                "rate limited at T3: effective parallelism 8 → 4",
                "3 failures in a row: effective parallelism 4 → 2",
            ],
            2,
        ),
        case(
            "a shutdown's failure counts nothing",
            3,
            &[Fail, Fail, Cancelled],
            &[],
            8,
        ),
        case(
            "a threshold of two",
            2,
            &[Fail, Fail],
            &["2 failures in a row: effective parallelism 8 → 4"],
            4,
        ),
    ];
    for StreakCase {
        name,
        fail_streak,
        kinds,
        logs: expected,
        parallel,
    } in cases
    {
        let all = kinds
            .iter()
            .enumerate()
            .map(|(index, kind)| {
                let task = Task::new(&format!("T{}", index + 1)).after(index);
                match kind {
                    Kind::Fail => task.act(Act::Return("FAILED")),
                    Kind::Succeed => task,
                    Kind::RateLimited => task.act(Act::Return("FAILED")).comp(failed(Some(0.0))),
                    Kind::Cancelled => task.act(Act::Return("FAILED")).comp(Completion {
                        failed: true,
                        cancelled: true,
                        ..Completion::default()
                    }),
                }
            })
            .collect();
        let fake = Fake::new(all);
        let summary = run_to_end(
            &fake,
            RunConfig {
                fail_streak,
                ..config(8)
            },
        );
        assert_eq!(logs(&fake), strings(expected), "{name}");
        assert_eq!(summary.effective_parallel, parallel, "{name}");
        let last = fake
            .lock()
            .passes
            .last()
            .map(|pass| pass.effective_parallel);
        assert_eq!(
            last,
            Some(parallel),
            "{name}: the last pass reports the parallelism"
        );
    }
}

/// The streak counts distinct tasks: one task failing over and over is one entry (`Scheduler._streak` is a set).
#[test]
fn one_task_failing_repeatedly_does_not_trip_the_streak() {
    let fake = Fake::new(vec![Task::new("T1").attempts(5).act(Act::Return("FAILED"))]);
    let summary = run_to_end(&fake, config(8));
    assert_eq!(summary.dispatched, 5);
    assert!(logs(&fake).is_empty(), "{:?}", logs(&fake));
    assert_eq!(summary.effective_parallel, 8);
}

// ---- retry delay and the backoff gate --------------------------------------------------------------------------------------------

/// The retry delay: `backoff_s(attempt)` with the 0.25 jitter, lengthened by a provider hint up to the configured cap, written as an
/// absolute wall time and logged with one decimal.
#[test]
fn a_transient_failure_waits_the_backoff_and_the_provider_hint() {
    // (attempt, jitter unit, rate-limit hint, expected delay)
    let cases = [
        (1, 0.0, None, 0.1),
        (0, 0.0, None, 0.1),
        (3, 1.0, None, 0.5),
        (3, -1.0, None, 0.3),
        (70, 0.0, None, 5.0),
        (1, 0.0, Some(3.0), 3.0),
        (1, 0.0, Some(10.0), 5.0),
        (1, 0.0, Some(0.0), 0.1),
    ];
    for (attempt, unit, hint, delay) in cases {
        let fake = Fake::new(vec![Task::new("T1")
            .attempts(2)
            .act(Act::Return("FAILED"))
            .act(Act::Return("DONE"))
            .comp(Completion {
                failed: true,
                rate_limited: hint,
                retry_attempt: Some(attempt),
                ..Completion::default()
            })]);
        {
            let mut state = fake.lock();
            state.unit = unit;
            state.poll_advance = 1.0;
        }
        let summary = run_to_end(&fake, config(2));
        let state = fake.lock();
        let case = format!("attempt {attempt}, unit {unit}, hint {hint:?}");
        let retry_line = state
            .logs
            .iter()
            .find(|line| line.contains("transient failure"));
        assert_eq!(
            retry_line.map(String::as_str),
            Some(format!("T1: transient failure, re-dispatch in {delay:.1}s").as_str()),
            "{case}"
        );
        let (task, wall, written_at) = &state.retry_writes[0];
        assert_eq!(task, "T1");
        assert!(
            (wall - written_at - delay).abs() < 1e-9,
            "{case}: {wall} {written_at}"
        );
        assert_eq!(summary.dispatched, 2, "{case}");
        assert_eq!(summary.stopped, StopReason::NothingReady, "{case}");
    }
}

/// Backoff gating: a task with `retry_after` in the future is left out of every pass until the (injected) wall clock reaches it, and
/// the loop sleeps for it instead of stopping or spinning.
#[test]
fn a_task_waiting_on_its_backoff_is_not_dispatched_until_the_wall_clock_reaches_it() {
    let fake = Fake::new(vec![Task::new("T1")
        .attempts(2)
        .act(Act::Return("FAILED"))
        .act(Act::Return("DONE"))
        .comp(Completion {
            failed: true,
            retry_attempt: Some(3),
            ..Completion::default()
        })]);
    {
        let mut state = fake.lock();
        state.unit = 1.0;
        state.poll_advance = 0.02;
    }
    let summary = run_to_end(&fake, config(2));
    let state = fake.lock();
    let wall = state.retry_writes[0].1;
    assert!(
        state.started_clock[1] >= wall,
        "the retry started at {} before its time {wall}",
        state.started_clock[1]
    );
    assert!(
        state.passes.iter().any(|pass| pass.submitted == 0
            && pass
                .skipped
                .iter()
                .any(|(task, why)| task == "T1" && why.starts_with("backoff:"))),
        "an idle pass reported the backoff: {:?}",
        state.passes
    );
    assert_eq!(summary.dispatched, 2);
    assert_eq!(summary.stopped, StopReason::NothingReady);
}

/// Replays `test_a_full_manager_refuses_instead_of_waiting_forever` (tests/test_scheduler_continuous.py:698): a worker that answers
/// `FAILED` (the `E-CAPACITY` of a full manager) is a normal failed completion: a streak input, a retry delay, and the loop goes on
/// without waiting.
#[test]
fn a_capacity_failure_is_a_normal_failed_completion() {
    let fake = Fake::new(vec![Task::new("T1")
        .attempts(2)
        .act(Act::Return("FAILED"))
        .act(Act::Return("DONE"))
        .comp(Completion {
            failed: true,
            retry_attempt: Some(1),
            ..Completion::default()
        })]);
    fake.lock().poll_advance = 1.0;
    let summary = run_to_end(
        &fake,
        RunConfig {
            fail_streak: 1,
            ..config(4)
        },
    );
    assert_eq!(
        logs(&fake),
        strings(&[
            "1 failures in a row: effective parallelism 4 → 2",
            "T1: transient failure, re-dispatch in 0.1s",
            "rate limit cooled: effective parallelism 2 → 3",
        ])
    );
    assert_eq!(summary.dispatched, 2);
}

// ---- internal failures --------------------------------------------------------------------------------------------------------------

/// Replays `test_a_worker_exception_is_that_tasks_structured_failure` (tests/test_scheduler_continuous.py:344): a worker that raises
/// fails only its task with `E-INTERNAL`, three times in a row, while its sibling is not delayed and ends `DONE`.
#[test]
fn a_worker_error_is_that_tasks_structured_failure_and_its_sibling_is_not_delayed() {
    let fake = Fake::new(vec![
        // BAD's workers wait for one completion, which can only be GOOD's: a loop that BAD's failures held up would never get there
        Task::new("BAD")
            .attempts(3)
            .after(1)
            .act(Act::Fail("RuntimeError: boom")),
        Task::new("GOOD"),
    ]);
    let summary = run_to_end(&fake, config(2));
    let state = fake.lock();
    assert_eq!(state.internal_calls.len(), 3);
    for (task, text, thread) in &state.internal_calls {
        assert_eq!(task, "BAD");
        assert_eq!(text, "E-INTERNAL: RuntimeError: boom");
        assert!(
            thread
                .as_deref()
                .is_some_and(|name| name.starts_with("sched-worker")),
            "a worker port failure is recorded on the pool thread: {thread:?}"
        );
    }
    assert_eq!(state.phase("GOOD"), Phase::Final("DONE".to_owned()));
    assert_eq!(state.phase("BAD"), Phase::Final("FAILED".to_owned()));
    let position = |task: &str, nth: usize| {
        state
            .completed
            .iter()
            .enumerate()
            .filter(|(_, done)| *done == task)
            .nth(nth)
            .map(|(index, _)| index)
    };
    assert_eq!(position("GOOD", 0), Some(0));
    let errors: Vec<_> = state
        .dones
        .iter()
        .filter(|done| done.task_id == "BAD")
        .map(|done| done.error.as_deref())
        .collect();
    assert_eq!(errors, [Some("E-INTERNAL: RuntimeError: boom"); 3]);
    assert_eq!(summary.dispatched, 4);
    assert_eq!(summary.stopped, StopReason::NothingReady);
}

/// `INTERNAL_FAILURE_LIMIT` consecutive internal failures leave a task alone for the rest of the run (`internal-failure`), and the
/// run stops as stalled after that many idle passes.
#[test]
fn the_internal_failure_limit_leaves_the_task_alone_and_the_idle_passes_stall_the_run() {
    let fake = Fake::new(vec![
        Task::new("BAD")
            .attempts(100)
            .act(Act::Fail("RuntimeError: boom")),
        Task::new("GOOD"),
    ]);
    let summary = run_to_end(&fake, config(2));
    let state = fake.lock();
    assert_eq!(
        state.started.iter().filter(|task| *task == "BAD").count(),
        3
    );
    let idle = state
        .passes
        .iter()
        .rev()
        .take_while(|pass| pass.submitted == 0 && pass.in_flight.is_empty())
        .count();
    assert_eq!(idle, 3, "{:?}", state.passes);
    let last = state.passes.last().expect("passes ran");
    assert!(last
        .skipped
        .contains(&("BAD".to_owned(), "internal-failure".to_owned())));
    assert_eq!(summary.stopped, StopReason::Stalled);
}

/// A worker panic is that task's failure only: `internal_failure` is asked with the fixed `E-INTERNAL` text on the loop thread, and
/// nothing the run says repeats the panic payload.
#[test]
fn a_worker_panic_becomes_an_internal_failure_without_the_payload() {
    let fake = Fake::new(vec![
        Task::new("BAD").act(Act::Panic(PANIC_TEXT)),
        Task::new("GOOD"),
    ]);
    let summary = run_to_end(&fake, config(2));
    let state = fake.lock();
    assert_eq!(
        state.internal_calls,
        [(
            "BAD".to_owned(),
            WORKER_PANICKED.to_owned(),
            Some("loop-under-test".to_owned())
        )]
    );
    assert_eq!(WORKER_PANICKED, "E-INTERNAL: worker panicked");
    let said: String = format!("{:?} {:?}", state.dones, state.logs);
    assert!(!said.contains(PANIC_TEXT), "{said}");
    assert_eq!(state.phase("GOOD"), Phase::Final("DONE".to_owned()));
    assert_eq!(summary.dispatched, 2);
}

/// A dispatch that cannot even be set up (`Submission::Failed`) is dispatched and in flight, its worker never runs, and its
/// result goes through `complete` like any other.
#[test]
fn a_dispatch_that_could_not_be_set_up_still_completes_without_a_worker() {
    let fake = Fake::new(tasks(1));
    fake.lock().submit_failed = strings(&["T1"]);
    let summary = run_to_end(&fake, config(2));
    let state = fake.lock();
    assert!(state.started.is_empty());
    assert_eq!(summary.dispatched, 1);
    assert_eq!(
        state.dones[0].error.as_deref(),
        Some("E-INTERNAL: the dispatch could not be set up")
    );
    assert_eq!(state.phase("T1"), Phase::Final("FAILED".to_owned()));
}

/// A pool that refuses a job (closed) fails that task through `internal_failure` with the fixed text instead of losing it.
#[test]
fn a_pool_that_refuses_a_job_fails_that_task_instead_of_losing_it() {
    let fake = Fake::new(tasks(1));
    let pool = Arc::new(Pool::new(1));
    pool.shutdown(Duration::ZERO);
    let ports: Arc<dyn SchedulerPorts> = Arc::clone(&fake) as Arc<dyn SchedulerPorts>;
    let wall = fake.wall();
    let summary =
        on_loop_thread(move || run_with(ports, &config(1), wall, pool)).expect("the run succeeded");
    let state = fake.lock();
    assert_eq!(summary.dispatched, 1);
    assert_eq!(state.internal_calls.len(), 1);
    assert_eq!(state.internal_calls[0].1, WORKER_NOT_STARTED);
    assert!(state.started.is_empty());
}

// ---- idle verdicts and the extension stop -------------------------------------------------------------------------------------------

/// Nothing ready and nothing in flight ends the run after one pass.
#[test]
fn an_idle_pass_with_nothing_ready_stops_the_run() {
    let fake = Fake::new(Vec::new());
    let summary = run_to_end(&fake, config(2));
    assert_eq!(summary.stopped, StopReason::NothingReady);
    assert_eq!((summary.rounds, summary.dispatched), (1, 0));
    let state = fake.lock();
    assert!(state.calls_of("submit").is_empty());
    assert_eq!(state.passes[0].ready, 0);
}

/// Everything ready waits on a lease: the run ends with the waiting lines (Python logs them as `waiting on leases: [...]`).
#[test]
fn tasks_waiting_on_leases_end_the_run_with_their_waiting_lines() {
    let fake = Fake::new(tasks(1));
    fake.lock().leases = Some(HashMap::from([(
        "T1".to_owned(),
        LeaseVerdict {
            dispatch: false,
            status: "busy".to_owned(),
            holder: Some("S-9".to_owned()),
        },
    )]));
    let summary = run_to_end(&fake, config(2));
    assert_eq!(summary.stopped, StopReason::LeaseWait);
    assert_eq!(summary.waiting, strings(&["T1: busy (held by S-9)"]));
    assert_eq!(summary.dispatched, 0);
}

/// `extension_down` latches, logs once, and closes the run to new dispatches while the dispatches in flight finish.
#[test]
fn extension_down_stops_new_dispatches_while_in_flight_tasks_finish() {
    let fake = Fake::new(vec![
        Task::new("T1").comp(Completion {
            extension_down: true,
            ..Completion::default()
        }),
        Task::new("T2").act(Act::Permit("DONE")),
        Task::new("T3"),
        Task::new("T4"),
    ]);
    let run = start(&fake, config(2));
    let line = "stopped: omp extension failed to load; no further sessions this run";
    fake.wait_until("both workers started", |s| s.started.len() == 2);
    fake.wait_until("the stop line", |s| s.logs.iter().any(|log| log == line));
    thread::sleep(Duration::from_millis(30));
    let mut started = fake.lock().started.clone();
    started.sort();
    assert_eq!(
        started,
        strings(&["T1", "T2"]),
        "no dispatch after the stop"
    );
    fake.permit(1);
    let summary = run.summary();
    assert!(summary.extension_down && summary.closed);
    assert_eq!(summary.stopped, StopReason::Closed);
    assert_eq!(summary.dispatched, 2);
    let state = fake.lock();
    assert_eq!(state.phase("T2"), Phase::Final("DONE".to_owned()));
    assert_eq!(state.logs, strings(&[line]), "logged once");
}

// ---- cancel ------------------------------------------------------------------------------------------------------------------------

/// A configuration whose drain schedule is short: `cancel_grace_s`, a roomy budget and a small settle time.
fn cancel_config(max_concurrent: u32, cancel_grace_s: f64) -> RunConfig {
    RunConfig {
        cancel_grace_s,
        shutdown_budget_s: 10.0,
        kill_settle_s: 0.0,
        ..config(max_concurrent)
    }
}

/// Dispatch real workers before cancellation, then drive the existing loop with the test-only monotonic clock.
/// `stage_completion` transfers an actual pool completion to the runtime's settled queue before the cancel drain starts.
fn start_scripted_cancel(
    fake: &Arc<Fake>,
    config: RunConfig,
    stage_completion: bool,
    force: bool,
) -> Running {
    let pool = Arc::new(Pool::new(config.max_concurrent as usize));
    let ports: Arc<dyn SchedulerPorts> = Arc::clone(fake) as Arc<dyn SchedulerPorts>;
    let mut runtime = Runtime::new(ports, &config, fake.wall(), Arc::clone(&pool))
        .expect("the scripted configuration is valid");
    runtime.drain_clock = Some(Arc::clone(&fake.drain_clock));
    let count = fake.lock().tasks.len();
    runtime.schedule().expect("the initial pass dispatched");
    fake.wait_until("all scripted workers started", |s| s.started.len() == count);
    if stage_completion {
        let completion = pool
            .next_completion(WAIT)
            .expect("the returning worker finished");
        match completion.outcome {
            crate::pool::Outcome::Done(result) => {
                runtime
                    .settled
                    .push_back(result.expect("the worker succeeded"));
            }
            crate::pool::Outcome::Crashed => panic!("the scripted worker unexpectedly panicked"),
        }
    }
    let request_during_poll = fake.lock().cancel_on_poll.is_some();
    if !request_during_poll {
        fake.request_cancel("signal SIGTERM", force);
    }
    let (sender, result) = mpsc::channel();
    thread::Builder::new()
        .name("scripted-cancel-loop".to_owned())
        .spawn(move || {
            let _ = sender.send(runtime.drive());
        })
        .expect("the scripted loop starts");
    Running { result, pool }
}

fn hard_drain_config() -> RunConfig {
    RunConfig {
        cancel_grace_s: 0.0,
        shutdown_budget_s: 2.0,
        kill_grace_s: 0.5,
        kill_settle_s: 0.25,
        wait_tick_s: 0.125,
        ..config(2)
    }
}

/// The cancel flow: the grace elapses with sessions still running, the sessions are killed once with the plan's kill grace, and the
/// sessions that end on the kill complete normally.
#[test]
fn a_cancel_waits_the_grace_then_kills_once() {
    let fake = Fake::new(
        (1..=2)
            .map(|n| Task::new(&format!("T{n}")).act(Act::UntilKill("FAILED")))
            .collect(),
    );
    let run = start(&fake, cancel_config(2, 0.05));
    fake.wait_until("both sessions running", |s| s.started.len() == 2);
    let requested = Instant::now();
    fake.request_cancel("swarm-timeout", false);
    let summary = run.summary();
    let state = fake.lock();
    let (_, reason, _, in_flight) = state.begin.clone().expect("cancel_begin was called");
    // The begin argument is the residual grace, not the nominal 0.05; scripted age tests below assert its exact value.
    assert_eq!((reason.as_str(), in_flight), ("swarm-timeout", 2));
    let (killed_at, kill_grace, kill_reason) =
        state.kill.clone().expect("the sessions were killed");
    assert_eq!((kill_grace, kill_reason.as_str()), (5.0, "swarm-timeout"));
    assert!(
        killed_at.duration_since(requested) >= Duration::from_millis(50),
        "the kill waited for the grace"
    );
    assert_eq!(state.kills, 1);
    assert_eq!(
        state.completed.len(),
        2,
        "the killed sessions were completed"
    );
    assert_eq!(summary.stopped, StopReason::Cancelled);
    assert!(summary.abandoned.is_empty());
}

/// Sessions that leave inside the grace are not killed.
#[test]
fn sessions_that_leave_inside_the_grace_are_not_killed() {
    let fake = Fake::new(
        (1..=2)
            .map(|n| Task::new(&format!("T{n}")).act(Act::Permit("SKIPPED")))
            .collect(),
    );
    let run = start(&fake, cancel_config(2, 5.0));
    fake.wait_until("both sessions running", |s| s.started.len() == 2);
    fake.request_cancel("signal SIGTERM", false);
    fake.wait_until("cancel_begin", |s| s.begin.is_some());
    fake.permit(2);
    let summary = run.summary();
    let state = fake.lock();
    assert_eq!(state.kills, 0);
    assert_eq!(state.completed.len(), 2);
    assert_eq!(summary.stopped, StopReason::Cancelled);
    assert!(summary.abandoned.is_empty());
}

/// A force request (the second signal) skips the grace: the kill comes at once, not after the 30 seconds of grace.
#[test]
fn a_force_request_skips_the_grace() {
    let fake = Fake::new(vec![Task::new("T1").act(Act::UntilKill("FAILED"))]);
    let run = start(
        &fake,
        RunConfig {
            shutdown_budget_s: 100.0,
            ..cancel_config(1, 30.0)
        },
    );
    fake.wait_until("the session running", |s| s.started.len() == 1);
    fake.request_cancel("signal SIGINT", true);
    let summary = run.summary();
    let state = fake.lock();
    let (begun_at, ..) = state.begin.clone().expect("cancel_begin was called");
    let (killed_at, ..) = state.kill.clone().expect("the session was killed");
    assert!(killed_at.duration_since(begun_at) < Duration::from_secs(5));
    assert_eq!(summary.stopped, StopReason::Cancelled);
}

/// A session that never returns is still in flight after the kill window: it is reported as abandoned, the run does not wait for
/// it forever, and its thread is left detached.
#[test]
fn a_worker_that_never_returns_is_abandoned_after_the_kill_window() {
    let fake = Fake::new(vec![Task::new("T1").act(Act::Hang), Task::new("T2")]);
    let run = start(
        &fake,
        RunConfig {
            cancel_grace_s: 0.02,
            shutdown_budget_s: 2.0,
            kill_grace_s: 0.5,
            kill_settle_s: 0.1,
            ..config(2)
        },
    );
    fake.wait_until("both workers started", |s| s.started.len() == 2);
    fake.request_cancel("swarm-timeout", false);
    let summary = run.summary();
    let ended = Instant::now();
    let state = fake.lock();
    let (killed_at, kill_grace, _) = state.kill.clone().expect("the session was killed");
    assert_eq!(kill_grace, 0.5);
    assert_eq!(state.kills, 1);
    let waited = ended.duration_since(killed_at);
    assert!(
        waited >= Duration::from_millis(500) && waited < Duration::from_secs(5),
        "the run waited out the kill window and then gave up: {waited:?}"
    );
    assert_eq!(summary.abandoned, strings(&["T1"]));
    assert_eq!(summary.stopped, StopReason::Cancelled);
    drop(state);
    fake.end();
}

/// A cancel that is already set before the first pass dispatches nothing.
#[test]
fn a_cancel_before_the_first_pass_dispatches_nothing() {
    let fake = Fake::new(tasks(2));
    fake.request_cancel("signal SIGTERM", false);
    let summary = run_to_end(&fake, cancel_config(2, 1.0));
    let state = fake.lock();
    assert!(state.calls_of("begin_pass").is_empty());
    assert_eq!(state.begin.as_ref().map(|begin| begin.3), Some(0));
    assert_eq!(state.kills, 0);
    assert_eq!((summary.rounds, summary.dispatched), (0, 0));
    assert_eq!(summary.stopped, StopReason::Cancelled);
}

/// Replays the new obligation of `test_the_lifecycle_slot_is_released_on_every_worker_exit_path` (tests/test_scheduler_continuous.py:963):
/// a task submitted to the pool while the cancel is already set still gets its `worker` call, so the host can release its slot.
#[test]
fn a_task_submitted_while_the_cancel_is_set_still_gets_its_worker_call() {
    let fake = Fake::new(
        (1..=2)
            .map(|n| Task::new(&format!("T{n}")).act(Act::Return("SKIPPED")))
            .collect(),
    );
    fake.lock().cancel_on_submit = true;
    let summary = run_to_end(&fake, cancel_config(2, 5.0));
    let state = fake.lock();
    let mut started = state.started.clone();
    started.sort();
    assert_eq!(started, strings(&["T1", "T2"]));
    assert_eq!(state.completed.len(), 2);
    assert_eq!(summary.dispatched, 2);
    assert_eq!(summary.stopped, StopReason::Cancelled);
}

/// The hard drain must poll on every idle tick, not only when a worker finishes. Repeated force requests do not renew it.
#[test]
fn hard_drain_polls_cancel_state_without_completions() {
    let fake = Fake::new(vec![
        Task::new("T2").act(Act::Hang),
        Task::new("T1").act(Act::Hang),
    ]);
    let _cleanup = Cleanup(Arc::clone(&fake));
    {
        let mut state = fake.lock();
        state.cancel_elapsed_s = Some(0.0);
        state.hold_post_kill_poll = true;
    }
    let origin = fake.drain_clock.now();
    let run = start_scripted_cancel(&fake, hard_drain_config(), false, false);
    fake.wait_until("the first final-hard-drain poll", |s| {
        s.post_kill_polls == 1
    });
    let first_request_at = fake
        .lock()
        .cancel
        .as_ref()
        .expect("cancel was requested")
        .at;
    fake.request_cancel("second signal", true);
    fake.request_cancel("third signal", true);
    {
        let mut state = fake.lock();
        assert!(
            state.completed.is_empty(),
            "no completion can satisfy the poll observation"
        );
        assert_eq!(state.cancel.as_ref().unwrap().at, first_request_at);
        state.hold_post_kill_poll = false;
    }
    fake.wake();
    fake.wait_until("multiple idle hard-drain polls", |s| s.post_kill_polls >= 3);
    let summary = run.summary();
    {
        let state = fake.lock();
        assert_eq!(state.post_kill_polls, 6, "one poll per bounded idle tick");
        assert_eq!(state.calls_of("cancel_begin").len(), 1);
        assert_eq!(state.kills, 1);
        let (_, kill_grace, reason) = state.kill.as_ref().unwrap();
        assert_eq!((*kill_grace, reason.as_str()), (0.5, "signal SIGTERM"));
        let begin = state
            .calls
            .iter()
            .position(|call| call.starts_with("cancel_begin "))
            .unwrap();
        let kill = state
            .calls
            .iter()
            .position(|call| call.starts_with("kill "))
            .unwrap();
        assert!(begin < kill);
        assert!(state.calls[kill + 1..]
            .iter()
            .all(|call| call == "cancel_state"));
        assert!(state.completed.is_empty());
    }
    let waits = fake.drain_clock.waits();
    assert_eq!(waits.len(), 6);
    for (index, (deadline, now, timeout)) in waits.into_iter().enumerate() {
        assert_eq!(deadline, origin + Duration::from_millis(750));
        assert_eq!(now, origin + Duration::from_millis(125 * index as u64));
        assert_eq!(timeout, Duration::from_millis(125));
    }
    assert_eq!(fake.drain_clock.now(), origin + Duration::from_millis(750));
    assert_eq!(summary.stopped, StopReason::Cancelled);
    assert_eq!(
        summary.abandoned,
        strings(&["T1", "T2"]),
        "sorted, not submission order"
    );
    fake.end();
    fake.wait_until("hanging workers released in cleanup", |s| s.running == 0);
}

/// Inject only after a post-kill poll has parked: neither the initial nor cooperative-grace poll can satisfy this test.
#[test]
fn hard_drain_preserves_cancel_port_error_payload() {
    struct Opaque(u8);
    let original = Arc::new(Opaque(19));
    let payload = Box::new(Arc::clone(&original));
    let payload_address = (&*payload as *const Arc<Opaque>) as usize;
    let fake = Fake::new(vec![
        Task::new("T1").act(Act::Hang),
        Task::new("T2").act(Act::Hang),
    ]);
    let _cleanup = Cleanup(Arc::clone(&fake));
    {
        let mut state = fake.lock();
        state.cancel_elapsed_s = Some(0.0);
        state.hold_post_kill_poll = true;
    }
    let run = start_scripted_cancel(&fake, hard_drain_config(), false, false);
    fake.wait_until("hard-drain entry before injecting the error", |s| {
        s.post_kill_polls == 1
    });
    {
        let mut state = fake.lock();
        assert_eq!(state.kills, 1);
        assert!(state.completed.is_empty());
        state.post_kill_error = Some(PortError::new("opaque scripted failure", payload));
        state.hold_post_kill_poll = false;
    }
    fake.wake();
    match run.outcome() {
        Err(RunError::Port(error)) => {
            assert_eq!(error.summary(), "opaque scripted failure");
            let returned = error
                .into_payload()
                .downcast::<Arc<Opaque>>()
                .expect("original payload type");
            assert_eq!((&*returned as *const Arc<Opaque>) as usize, payload_address);
            assert!(
                Arc::ptr_eq(&original, &returned),
                "the same opaque object, not a reconstruction"
            );
            assert_eq!(returned.0, 19);
        }
        other => panic!("expected the hard-drain port error, got {other:?}"),
    }
    {
        let state = fake.lock();
        assert_eq!(state.post_kill_polls, 1);
        assert_eq!(state.calls_of("cancel_begin").len(), 1);
        assert_eq!(state.calls_of("kill").len(), 1);
        assert_eq!(state.calls.last().map(String::as_str), Some("cancel_state"));
        assert!(state.completed.is_empty());
    }
    assert!(
        fake.drain_clock.waits().is_empty(),
        "the error stops the drain before another wait"
    );
    fake.end();
    fake.wait_until("error-path hanging workers released", |s| s.running == 0);
}

/// Exact scripted seconds: returned request age charges the initial delay once; later callbacks consume the fixed deadlines.
#[test]
fn cancel_drain_charges_callback_elapsed_to_remaining_budget() {
    let fake = Fake::new(vec![
        Task::new("T1").act(Act::Return("DONE")),
        Task::new("T2").act(Act::Hang),
    ]);
    let _cleanup = Cleanup(Arc::clone(&fake));
    {
        let mut state = fake.lock();
        // The request is already one second old at poll entry; the near-return sample includes the next two seconds too.
        state.cancel_elapsed_s = Some(1.0);
        for (port, seconds) in [
            ("cancel_state", 2),
            ("cancel_begin", 3),
            ("complete", 5),
            ("kill", 1),
        ] {
            state
                .callback_advance
                .insert(port, Duration::from_secs(seconds));
        }
    }
    let origin = fake.drain_clock.now();
    let run = start_scripted_cancel(
        &fake,
        RunConfig {
            cancel_grace_s: 10.0,
            shutdown_budget_s: 15.0,
            kill_grace_s: 5.0,
            kill_settle_s: 3.0,
            wait_tick_s: 4.0,
            ..config(2)
        },
        true,
        false,
    );
    let summary = run.summary();
    {
        let state = fake.lock();
        assert_eq!(state.completed, strings(&["T1"]));
        assert_eq!(state.calls_of("cancel_begin").len(), 1);
        assert_eq!(state.kills, 1);
        assert_eq!(
            state.begin.as_ref().unwrap().2,
            4.0,
            "three seconds of the seven-second grace already elapsed"
        );
        assert_eq!(
            state.kill.as_ref().unwrap().1,
            4.0,
            "only four seconds remain at kill entry"
        );
        assert!(
            state.callback_advance.is_empty(),
            "all four callback delays were exercised"
        );
        let begin = state
            .calls
            .iter()
            .position(|call| call.starts_with("cancel_begin "))
            .unwrap();
        let complete = state
            .calls
            .iter()
            .position(|call| call == "complete T1")
            .unwrap();
        let kill = state
            .calls
            .iter()
            .position(|call| call.starts_with("kill "))
            .unwrap();
        assert!(begin < complete && complete < kill);
    }
    assert_eq!(
        fake.drain_clock.waits(),
        vec![
            // Poll returns at two with age three: grace ends at six; cancel_begin consumes another three seconds.
            (
                origin + Duration::from_secs(6),
                origin + Duration::from_secs(5),
                Duration::from_secs(1)
            ),
            // Complete consumed five more seconds, kill one: the hard wait has only three seconds, not 5+3.
            (
                origin + Duration::from_secs(14),
                origin + Duration::from_secs(11),
                Duration::from_secs(3)
            ),
        ],
    );
    assert_eq!(fake.drain_clock.now(), origin + Duration::from_secs(14));
    assert_eq!(summary.stopped, StopReason::Cancelled);
    assert_eq!(summary.abandoned, strings(&["T2"]));
    fake.end();
    fake.wait_until("callback-budget worker released", |s| s.running == 0);
}

/// Poll entry 100, first Python observation 129, return/sample 130 with age one: shutdown is 144, not 114.
/// A later hard-drain callback may consume or overrun that deadline, but cannot renew it.
#[test]
fn cancel_drain_request_first_observed_during_initial_poll_preserves_request_budget() {
    let cases = [
        (0, vec![(44, 36, 4), (44, 40, 4)], 44),
        (6, vec![(44, 42, 2)], 44),
        (20, vec![], 56),
    ];
    for (later_poll_delay, hard_waits, ended_s) in cases {
        let fake = Fake::new(vec![
            Task::new("T2").act(Act::Hang),
            Task::new("T1").act(Act::Hang),
        ]);
        let _cleanup = Cleanup(Arc::clone(&fake));
        fake.drain_clock.advance(Duration::from_secs(100));
        let entry = fake.drain_clock.now();
        {
            let mut state = fake.lock();
            state.cancel_elapsed_s = Some(0.0);
            state.cancel_on_poll = Some(Duration::from_secs(29));
            state
                .callback_advance
                .insert("cancel_state", Duration::from_secs(30));
            state.hold_post_kill_poll = true;
        }
        let run = start_scripted_cancel(
            &fake,
            RunConfig {
                cancel_grace_s: 10.0,
                shutdown_budget_s: 15.0,
                kill_grace_s: 5.0,
                kill_settle_s: 3.0,
                wait_tick_s: 4.0,
                ..config(2)
            },
            false,
            false,
        );
        fake.wait_until("hard-drain entry after the request-relative grace", |s| {
            s.post_kill_polls == 1
        });
        {
            let state = fake.lock();
            assert_eq!(
                state.cancel.as_ref().unwrap().at,
                entry + Duration::from_secs(29)
            );
            let (at, reason, grace, in_flight) = state.begin.as_ref().unwrap();
            assert_eq!(
                (*at, reason.as_str(), *grace, *in_flight),
                (entry + Duration::from_secs(30), "signal SIGTERM", 6.0, 2)
            );
            let (at, grace, reason) = state.kill.as_ref().unwrap();
            assert_eq!(
                (*at, *grace, reason.as_str()),
                (entry + Duration::from_secs(36), 5.0, "signal SIGTERM")
            );
        }
        fake.request_cancel("second signal", true);
        fake.request_cancel("third signal", true);
        {
            let mut state = fake.lock();
            state
                .callback_advance
                .insert("cancel_state", Duration::from_secs(later_poll_delay));
            state.hold_post_kill_poll = false;
        }
        fake.wake();
        let summary = run.summary();
        let mut expected = vec![(36, 30, 4), (36, 34, 2)];
        expected.extend(hard_waits);
        let expected: Vec<_> = expected
            .into_iter()
            .map(|(deadline, now, timeout)| {
                (
                    entry + Duration::from_secs(deadline),
                    entry + Duration::from_secs(now),
                    Duration::from_secs(timeout),
                )
            })
            .collect();
        assert_eq!(
            fake.drain_clock.waits(),
            expected,
            "later poll delay {later_poll_delay}"
        );
        assert_eq!(fake.drain_clock.now(), entry + Duration::from_secs(ended_s));
        {
            let state = fake.lock();
            assert_eq!(
                state.cancel.as_ref().unwrap().at,
                entry + Duration::from_secs(29)
            );
            assert_eq!(state.calls_of("cancel_begin").len(), 1);
            assert_eq!(state.kills, 1);
            assert!(
                state.callback_advance.is_empty(),
                "both initial and later poll delays were consumed"
            );
            assert!(state.completed.is_empty());
        }
        assert_eq!(summary.stopped, StopReason::Cancelled);
        assert_eq!(summary.abandoned, strings(&["T1", "T2"]));
        fake.end();
        fake.wait_until("during-poll request workers released", |s| s.running == 0);
    }
}

/// A request recorded at 99 is age five when the blocked 100→104 poll returns, not age one plus another four-second charge.
#[test]
fn cancel_drain_request_recorded_before_initial_poll_charges_delay_once() {
    let fake = Fake::new(vec![
        Task::new("T2").act(Act::Hang),
        Task::new("T1").act(Act::Hang),
    ]);
    let _cleanup = Cleanup(Arc::clone(&fake));
    fake.drain_clock.advance(Duration::from_secs(100));
    let entry = fake.drain_clock.now();
    {
        let mut state = fake.lock();
        state.cancel_elapsed_s = Some(1.0);
        state
            .callback_advance
            .insert("cancel_state", Duration::from_secs(4));
        state
            .callback_advance
            .insert("cancel_begin", Duration::from_secs(1));
    }
    let run = start_scripted_cancel(
        &fake,
        RunConfig {
            cancel_grace_s: 10.0,
            shutdown_budget_s: 15.0,
            kill_grace_s: 5.0,
            kill_settle_s: 3.0,
            wait_tick_s: 4.0,
            ..config(2)
        },
        false,
        false,
    );
    let summary = run.summary();
    {
        let state = fake.lock();
        assert_eq!(
            state.cancel.as_ref().unwrap().at,
            entry - Duration::from_secs(1)
        );
        let (at, reason, grace, in_flight) = state.begin.as_ref().unwrap();
        assert_eq!(
            (*at, reason.as_str(), *grace, *in_flight),
            (entry + Duration::from_secs(4), "signal SIGTERM", 2.0, 2)
        );
        let (at, grace, reason) = state.kill.as_ref().unwrap();
        assert_eq!(
            (*at, *grace, reason.as_str()),
            (entry + Duration::from_secs(6), 5.0, "signal SIGTERM")
        );
        assert_eq!(state.calls_of("cancel_begin").len(), 1);
        assert_eq!(state.kills, 1);
        assert!(state.callback_advance.is_empty());
    }
    assert_eq!(
        fake.drain_clock.waits(),
        vec![
            // cancel_begin received the residual two seconds, and consumed one without renewing the grace.
            (
                entry + Duration::from_secs(6),
                entry + Duration::from_secs(5),
                Duration::from_secs(1)
            ),
            (
                entry + Duration::from_secs(14),
                entry + Duration::from_secs(6),
                Duration::from_secs(4)
            ),
            (
                entry + Duration::from_secs(14),
                entry + Duration::from_secs(10),
                Duration::from_secs(4)
            ),
        ]
    );
    assert_eq!(fake.drain_clock.now(), entry + Duration::from_secs(14));
    assert_eq!(summary.stopped, StopReason::Cancelled);
    assert_eq!(summary.abandoned, strings(&["T1", "T2"]));
    fake.end();
    fake.wait_until("pre-poll request workers released", |s| s.running == 0);
}

/// A callback may cooperatively overrun, but its return never starts a new grace or kill/settle window.
#[test]
fn cancel_drain_exhausted_budget_does_not_restart_grace() {
    let cases = [
        // label, already observed elapsed, delayed port, queued completion, force, kill argument, final clock, wait count
        ("already exhausted", 20.0, None, false, false, 0.0, 0, 0),
        (
            "initial poll/GIL",
            0.0,
            Some("cancel_state"),
            false,
            false,
            0.0,
            20,
            0,
        ),
        (
            "cancel_begin",
            0.0,
            Some("cancel_begin"),
            false,
            false,
            0.0,
            20,
            0,
        ),
        ("complete", 0.0, Some("complete"), true, false, 0.0, 20, 1),
        ("kill return", 0.0, Some("kill"), false, true, 5.0, 20, 0),
    ];
    for (label, elapsed, delayed, stage_completion, force, kill_grace, ended_s, wait_count) in cases
    {
        let fake = Fake::new(vec![
            Task::new("T1").act(if stage_completion {
                Act::Return("DONE")
            } else {
                Act::Hang
            }),
            Task::new("T2").act(Act::Hang),
        ]);
        let _cleanup = Cleanup(Arc::clone(&fake));
        {
            let mut state = fake.lock();
            state.cancel_elapsed_s = Some(elapsed);
            if let Some(port) = delayed {
                state.callback_advance.insert(port, Duration::from_secs(20));
            }
        }
        let origin = fake.drain_clock.now();
        let run = start_scripted_cancel(
            &fake,
            RunConfig {
                cancel_grace_s: 10.0,
                shutdown_budget_s: 15.0,
                kill_grace_s: 5.0,
                kill_settle_s: 3.0,
                wait_tick_s: 4.0,
                ..config(2)
            },
            stage_completion,
            force,
        );
        let summary = run.summary();
        {
            let state = fake.lock();
            assert_eq!(state.calls_of("cancel_begin").len(), 1, "{label}");
            assert_eq!(state.kills, 1, "{label}");
            assert_eq!(state.kill.as_ref().unwrap().1, kill_grace, "{label}");
            if label == "already exhausted" || label == "initial poll/GIL" {
                assert_eq!(state.begin.as_ref().unwrap().2, 0.0, "{label}");
            }
        }
        let shutdown = origin + Duration::from_secs_f64((15.0 - elapsed).max(0.0));
        let waits = fake.drain_clock.waits();
        assert_eq!(
            waits.len(),
            wait_count,
            "{label}: no wait after the callback exhausted the budget"
        );
        for (deadline, now, timeout) in waits {
            assert!(deadline <= shutdown && now < deadline, "{label}");
            assert!(
                timeout <= deadline.saturating_duration_since(now),
                "{label}"
            );
        }
        assert_eq!(
            fake.drain_clock.now(),
            origin + Duration::from_secs(ended_s),
            "{label}"
        );
        assert_eq!(summary.stopped, StopReason::Cancelled, "{label}");
        let abandoned = if stage_completion {
            strings(&["T2"])
        } else {
            strings(&["T1", "T2"])
        };
        assert_eq!(summary.abandoned, abandoned, "{label}");
        fake.end();
        fake.wait_until("exhausted-budget workers released", |s| s.running == 0);
    }
}

// ---- errors, panics, configuration, locks -----------------------------------------------------------------------------------------

/// A scenario that calls every loop-thread port before it ends: a task without an agent (`block`), a task on a lease whose first
/// attempt fails rate-limited and transient (`log`, `random_unit`, `set_retry_after`) and succeeds on the retry.
fn every_port_scenario() -> Arc<Fake> {
    let fake = Fake::new(vec![
        Task::new("N").no_agent(),
        Task::new("T")
            .attempts(2)
            .act(Act::Return("FAILED"))
            .act(Act::Return("DONE"))
            .comp(Completion {
                failed: true,
                rate_limited: Some(0.0),
                retry_attempt: Some(1),
                ..Completion::default()
            }),
    ]);
    fake.lock().leases = Some(HashMap::new());
    fake
}

/// The payload of a port error, which the fake sets to the name of the port it was injected into.
fn payload_of(outcome: Result<RunSummary, RunError>) -> &'static str {
    match outcome {
        Err(RunError::Port(error)) => *error
            .into_payload()
            .downcast::<&'static str>()
            .expect("the payload is the original one"),
        other => panic!("expected a port error, got {other:?}"),
    }
}

/// A loop-thread port that fails stops the loop at once, and the original error, payload included, comes back from `run`.
#[test]
fn a_loop_port_error_stops_the_loop_and_returns_the_original_payload() {
    let ports = [
        "cancel_state",
        "begin_pass",
        "agent_for",
        "block",
        "claims_for",
        "lease",
        "submit",
        "observe_pass",
        "complete",
        "log",
        "random_unit",
        "set_retry_after",
    ];
    for port in ports {
        let fake = every_port_scenario();
        fake.lock().inject = Some((port, Inject::Err));
        let outcome = start(&fake, config(2)).outcome();
        assert_eq!(payload_of(outcome), port);
    }
}

/// The two ports of the cancel flow fail the same way.
#[test]
fn a_cancel_port_error_stops_the_drain_and_returns_the_original_payload() {
    for port in ["cancel_begin", "kill"] {
        let fake = Fake::new(vec![Task::new("T1").act(Act::UntilKill("FAILED"))]);
        fake.lock().inject = Some((port, Inject::Err));
        let run = start(&fake, cancel_config(1, 0.01));
        fake.wait_until("the session running", |s| s.started.len() == 1);
        fake.request_cancel("swarm-timeout", false);
        assert_eq!(payload_of(run.outcome()), port);
        fake.end();
    }
}

/// A `worker` port failure is not a run error: it fails that task only. Only `internal_failure` itself failing stops the run.
#[test]
fn a_failing_internal_failure_port_stops_the_run() {
    let fake = Fake::new(vec![Task::new("T1").act(Act::Fail("RuntimeError: boom"))]);
    fake.lock().inject = Some(("internal_failure", Inject::Err));
    assert_eq!(
        payload_of(start(&fake, config(1)).outcome()),
        "internal_failure"
    );
}

/// A panic of a loop-thread port is contained: `run` returns an `E-INTERNAL` fault with a fixed message and never unwinds.
#[test]
fn a_panic_in_a_loop_port_is_contained_without_the_payload() {
    for port in ["begin_pass", "observe_pass", "complete", "cancel_state"] {
        let fake = every_port_scenario();
        fake.lock().inject = Some((port, Inject::Panic));
        match start(&fake, config(2)).outcome() {
            Err(RunError::Sched(error)) => {
                assert_eq!(error.code(), SchedCode::Internal, "{port}");
                assert_eq!(error.message(), LOOP_PANICKED, "{port}");
                assert!(!error.to_string().contains(PANIC_TEXT), "{port}");
            }
            other => panic!("{port}: expected a contained panic, got {other:?}"),
        }
    }
}

/// An invalid configuration is refused before any port is called.
#[test]
fn an_invalid_configuration_is_refused_before_any_port_call() {
    for bad in [
        RunConfig {
            max_concurrent: 0,
            ..config(1)
        },
        RunConfig {
            wait_tick_s: f64::NAN,
            ..config(1)
        },
    ] {
        let fake = Fake::new(tasks(1));
        let ports: Arc<dyn SchedulerPorts> = Arc::clone(&fake) as Arc<dyn SchedulerPorts>;
        match on_loop_thread(move || run(ports, &bad)) {
            Err(RunError::Sched(error)) => assert_eq!(error.code(), SchedCode::Input),
            other => panic!("expected an input error, got {other:?}"),
        }
        assert!(fake.lock().calls.is_empty());
    }
}

/// `run` itself (system clock, its own pool) completes a plain run.
#[test]
fn run_completes_a_plain_run_on_the_system_clock() {
    let fake = Fake::new(tasks(3));
    let ports: Arc<dyn SchedulerPorts> = Arc::clone(&fake) as Arc<dyn SchedulerPorts>;
    let summary = on_loop_thread(move || run(ports, &config(2))).expect("the run succeeded");
    assert_eq!(summary.dispatched, 3);
    assert_eq!(summary.stopped, StopReason::NothingReady);
}

/// No Rust lock is held across a port call: while the loop thread is parked inside the `complete` port, the other workers finish
/// and the pool, whose lock the loop thread would otherwise hold, still answers and shows nothing in flight.
#[test]
fn no_lock_is_held_while_the_loop_thread_is_inside_a_port() {
    let fake = Fake::new(tasks(2));
    fake.lock().hold_complete = true;
    let run = start(&fake, config(2));
    fake.wait_until("the loop thread parked in complete", |s| s.in_complete);
    let pool = Arc::clone(&run.pool);
    let (sender, answered) = mpsc::channel();
    thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(5);
        while pool.in_flight() != 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(1));
        }
        let _ = sender.send(pool.in_flight());
    });
    let left = answered
        .recv_timeout(WAIT)
        .expect("the pool answered while the loop thread was inside a port");
    assert_eq!(
        left, 0,
        "both workers finished while the loop thread was parked"
    );
    fake.lock().hold_complete = false;
    fake.wake();
    assert_eq!(run.summary().dispatched, 2);
}

/// Thirty-two workers on the fake host, one pass. Every worker waits for a permit, so all thirty-two run at once (`peak`), and the
/// loop is parked inside `complete` for the first completion while the pool answers from another thread: every session has finished
/// without the loop, so no Rust lock is held across the port. Then the run drives to its end and the summary counts are checked.
#[test]
fn thirty_two_workers_run_to_completion_with_no_lock_held_across_a_port() {
    const WORKERS: usize = 32;
    let fake = Fake::new(
        (1..=WORKERS)
            .map(|n| Task::new(&format!("T{n}")).act(Act::Permit("DONE")))
            .collect(),
    );
    fake.lock().hold_complete = true;
    let run = start(&fake, config(WORKERS as u32));
    fake.wait_until("all 32 sessions running at once", |s| {
        s.started.len() == WORKERS
    });
    assert_eq!(
        fake.lock().peak,
        WORKERS,
        "every worker ran at the same time"
    );
    fake.permit(WORKERS);
    fake.wait_until("the loop thread parked in complete", |s| s.in_complete);
    let pool = Arc::clone(&run.pool);
    let (sender, answered) = mpsc::channel();
    thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(5);
        while pool.in_flight() != 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(1));
        }
        let _ = sender.send((pool.in_flight(), pool.queued()));
    });
    let counts = answered
        .recv_timeout(WAIT)
        .expect("the pool answered while the loop thread was inside a port");
    assert_eq!(
        counts,
        (0, 0),
        "all sessions finished while the loop thread was parked"
    );
    fake.lock().hold_complete = false;
    fake.wake();
    let summary = run.summary();
    assert_eq!(summary.dispatched, WORKERS as u64);
    assert_eq!(summary.effective_parallel, WORKERS as u32);
    assert_eq!(summary.stopped, StopReason::NothingReady);
    assert!(!summary.closed && !summary.extension_down);
    assert!(summary.abandoned.is_empty() && summary.waiting.is_empty());
    assert!(
        summary.rounds >= 2,
        "a pass after the last completion found nothing"
    );
    let state = fake.lock();
    assert_eq!(
        state.passes[0].submitted, WORKERS,
        "one pass dispatched every task"
    );
    assert_eq!(state.calls_of("submit").len(), WORKERS);
    assert_eq!(
        state.calls_of("complete").len(),
        WORKERS,
        "each dispatch completed once"
    );
    assert_eq!((state.running, state.peak), (0, WORKERS));
    let mut started = state.started.clone();
    started.sort();
    started.dedup();
    assert_eq!(started.len(), WORKERS, "each task started exactly once");
    assert!(
        (1..=WORKERS).all(|n| { state.phase(&format!("T{n}")) == Phase::Final("DONE".to_owned()) })
    );
    assert_eq!(state.kills, 0);
}
