//! Behaviour tests of the pool with real threads. Waits are on condition variables with a generous deadline (a failure is a timeout,
//! never a flake); there are no sleeps standing in for synchronization.

use std::panic::panic_any;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use super::{Completion, Outcome, Pool, SubmitError};

/// A deadline no passing test comes near.
const LONG: Duration = Duration::from_secs(20);

/// A one-shot gate: jobs block in `wait` until the test calls `open`.
#[derive(Default)]
struct Gate {
    open: Mutex<bool>,
    changed: Condvar,
}

impl Gate {
    fn wait(&self) {
        let mut open = self.open.lock().unwrap();
        while !*open {
            open = self.changed.wait(open).unwrap();
        }
    }

    fn open(&self) {
        *self.open.lock().unwrap() = true;
        self.changed.notify_all();
    }
}

/// A counter the test can wait on without polling.
#[derive(Default)]
struct Latch {
    count: Mutex<usize>,
    changed: Condvar,
}

impl Latch {
    fn bump(&self) -> usize {
        let mut count = self.count.lock().unwrap();
        *count += 1;
        self.changed.notify_all();
        *count
    }

    /// Waits until the count is at least `n`; `false` on timeout.
    fn wait_for(&self, n: usize) -> bool {
        let deadline = Instant::now() + LONG;
        let mut count = self.count.lock().unwrap();
        while *count < n {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return false;
            }
            count = self.changed.wait_timeout(count, left).unwrap().0;
        }
        true
    }
}

fn next(pool: &Pool<usize>) -> Completion<usize> {
    pool.next_completion(LONG)
        .expect("a completion within the deadline")
}

#[test]
fn never_more_than_max_workers_run_and_the_rest_queue() {
    for max in [1_usize, 3, 8, 16, 32] {
        // Twice the cap and at least 20 jobs: the cap fills and at least one job queues behind it.
        let jobs = (2 * max).max(20);
        let pool: Pool<usize> = Pool::new(max);
        let gate = Arc::new(Gate::default());
        let running = Arc::new(AtomicUsize::new(0));
        let high_water = Arc::new(AtomicUsize::new(0));
        let started = Arc::new(Latch::default());
        for i in 0..jobs {
            let (gate, running, high_water, started) = (
                gate.clone(),
                running.clone(),
                high_water.clone(),
                started.clone(),
            );
            pool.submit(format!("t{i}"), move || {
                let now = running.fetch_add(1, Ordering::SeqCst) + 1;
                high_water.fetch_max(now, Ordering::SeqCst);
                started.bump();
                gate.wait();
                running.fetch_sub(1, Ordering::SeqCst);
                i
            })
            .unwrap();
        }
        // The cap is full and every other job waits: nothing beyond `max` has started while the gate is shut.
        assert!(started.wait_for(max), "max={max}: the first jobs start");
        assert_eq!(pool.in_flight(), jobs, "max={max}");
        assert_eq!(pool.queued(), jobs - max, "max={max}");
        gate.open();
        let mut seen: Vec<usize> = (0..jobs)
            .map(|_| next(&pool).outcome)
            .map(|o| {
                if let Outcome::Done(i) = o {
                    i
                } else {
                    usize::MAX
                }
            })
            .collect();
        seen.sort_unstable();
        assert_eq!(
            seen,
            (0..jobs).collect::<Vec<_>>(),
            "max={max}: every job completes once"
        );
        assert_eq!(
            high_water.load(Ordering::SeqCst),
            max,
            "max={max}: the cap is reached and never exceeded"
        );
        assert_eq!((pool.in_flight(), pool.queued()), (0, 0), "max={max}");
    }
}

#[test]
fn a_zero_cap_is_raised_to_one() {
    let pool: Pool<usize> = Pool::new(0);
    let gate = Arc::new(Gate::default());
    for i in 0..2 {
        let gate = gate.clone();
        pool.submit(format!("t{i}"), move || {
            gate.wait();
            i
        })
        .unwrap();
    }
    assert_eq!((pool.in_flight(), pool.queued()), (2, 1));
    gate.open();
    next(&pool);
    next(&pool);
}

#[test]
fn queued_jobs_start_in_submission_order() {
    let pool: Pool<usize> = Pool::new(1);
    let order = Arc::new(Mutex::new(Vec::new()));
    for i in 0..50 {
        let order = order.clone();
        pool.submit(format!("t{i}"), move || {
            order.lock().unwrap().push(i);
            i
        })
        .unwrap();
    }
    let finished: Vec<String> = (0..50).map(|_| next(&pool).task_id).collect();
    assert_eq!(*order.lock().unwrap(), (0..50).collect::<Vec<_>>());
    assert_eq!(
        finished,
        (0..50).map(|i| format!("t{i}")).collect::<Vec<_>>()
    );
}

#[test]
fn a_freed_slot_goes_to_the_oldest_queued_job() {
    let pool: Pool<usize> = Pool::new(2);
    let gates: Vec<Arc<Gate>> = (0..6).map(|_| Arc::new(Gate::default())).collect();
    let started = Arc::new(Mutex::new(Vec::new()));
    let signal = Arc::new(Latch::default());
    for (i, gate) in gates.iter().enumerate() {
        let (gate, started, signal) = (gate.clone(), started.clone(), signal.clone());
        pool.submit(format!("t{i}"), move || {
            started.lock().unwrap().push(i);
            signal.bump();
            gate.wait();
            i
        })
        .unwrap();
    }
    assert!(signal.wait_for(2));
    // Finish job 1 only: job 2 (the oldest queued), not job 5, takes the slot.
    gates[1].open();
    assert_eq!(next(&pool).task_id, "t1");
    assert!(signal.wait_for(3));
    assert_eq!(*started.lock().unwrap().last().unwrap(), 2);
    for gate in &gates {
        gate.open();
    }
    for _ in 0..5 {
        next(&pool);
    }
}

#[test]
fn completions_come_out_in_finish_order_with_their_results() {
    let pool: Pool<Result<u32, String>> = Pool::new(4);
    let gates: Vec<Arc<Gate>> = (0..4).map(|_| Arc::new(Gate::default())).collect();
    for (i, gate) in gates.iter().enumerate() {
        let gate = gate.clone();
        pool.submit(format!("t{i}"), move || {
            gate.wait();
            if i == 3 {
                Err(format!("failed {i}"))
            } else {
                Ok(i as u32 * 10)
            }
        })
        .unwrap();
    }
    let mut got = Vec::new();
    for i in [2_usize, 0, 3, 1] {
        gates[i].open();
        let done = pool.next_completion(LONG).unwrap();
        got.push((done.task_id, done.outcome));
    }
    assert_eq!(
        got,
        vec![
            ("t2".to_string(), Outcome::Done(Ok(20))),
            ("t0".to_string(), Outcome::Done(Ok(0))),
            ("t3".to_string(), Outcome::Done(Err("failed 3".to_string()))),
            ("t1".to_string(), Outcome::Done(Ok(10))),
        ]
    );
}

/// A panic payload whose drop panics too.
struct Bomb;

impl Drop for Bomb {
    fn drop(&mut self) {
        panic!("second panic while dropping the first payload");
    }
}

#[test]
fn a_panicking_job_is_a_payload_free_crash_and_the_pool_goes_on() {
    // One worker: the crash must free the slot and the queued jobs behind it must still run on the same pool.
    let pool: Pool<usize> = Pool::new(1);
    pool.submit("boom-str", || -> usize { panic!("secret-token-8841") })
        .unwrap();
    pool.submit("boom-bomb", || -> usize { panic_any(Bomb) })
        .unwrap();
    pool.submit("ok-1", || 1).unwrap();
    pool.submit("boom-after", || -> usize {
        panic!("another secret-token-8841")
    })
    .unwrap();
    pool.submit("ok-2", || 2).unwrap();
    let got: Vec<Completion<usize>> = (0..5).map(|_| next(&pool)).collect();
    let summary: Vec<(&str, Outcome<usize>)> = got
        .iter()
        .map(|c| (c.task_id.as_str(), c.outcome.clone()))
        .collect();
    assert_eq!(
        summary,
        vec![
            ("boom-str", Outcome::Crashed),
            ("boom-bomb", Outcome::Crashed),
            ("ok-1", Outcome::Done(1)),
            ("boom-after", Outcome::Crashed),
            ("ok-2", Outcome::Done(2)),
        ]
    );
    for completion in &got {
        assert!(
            !format!("{completion:?}").contains("secret-token"),
            "a crash carries no payload text"
        );
    }
    assert_eq!(pool.in_flight(), 0);
}

#[test]
fn shutdown_returns_a_stuck_job_as_abandoned_while_its_thread_keeps_running() {
    let pool: Pool<usize> = Pool::new(2);
    let (release, released) = mpsc::channel::<()>();
    let started = Arc::new(Latch::default());
    let signal = started.clone();
    pool.submit("stuck", move || {
        signal.bump();
        released.recv().ok();
        7
    })
    .unwrap();
    pool.submit("quick", || 1).unwrap();
    assert!(started.wait_for(1));
    assert_eq!(next(&pool).task_id, "quick");

    let wait = Duration::from_millis(100);
    let begun = Instant::now();
    let shutdown = pool.shutdown(wait);
    let took = begun.elapsed();
    assert_eq!(shutdown.abandoned, vec!["stuck".to_string()]);
    assert!(shutdown.dropped.is_empty());
    assert!(
        took >= wait && took < LONG,
        "the bound is honoured, took {took:?}"
    );
    assert_eq!(pool.in_flight(), 1, "the abandoned job is still running");
    assert!(pool.try_next_completion().is_none());

    // The thread was not killed or joined: releasing it lets the job finish and post its completion after the shutdown.
    release.send(()).unwrap();
    let done = next(&pool);
    assert_eq!(
        (done.task_id.as_str(), done.outcome),
        ("stuck", Outcome::Done(7))
    );
}

#[test]
fn shutdown_drops_queued_jobs_without_running_them_and_lists_them_in_order() {
    let pool: Pool<usize> = Pool::new(1);
    let (release, released) = mpsc::channel::<()>();
    let started = Arc::new(Latch::default());
    let signal = started.clone();
    let ran = Arc::new(AtomicUsize::new(0));
    pool.submit("stuck", move || {
        signal.bump();
        released.recv().ok();
        0
    })
    .unwrap();
    for name in ["q2", "q1", "q3"] {
        let ran = ran.clone();
        pool.submit(name, move || {
            ran.fetch_add(1, Ordering::SeqCst);
            1
        })
        .unwrap();
    }
    assert!(started.wait_for(1));
    let shutdown = pool.shutdown(Duration::from_millis(20));
    assert_eq!(shutdown.abandoned, vec!["stuck".to_string()]);
    assert_eq!(
        shutdown.dropped,
        vec!["q2".to_string(), "q1".to_string(), "q3".to_string()]
    );
    assert_eq!(pool.queued(), 0);
    release.send(()).unwrap();
    assert_eq!(next(&pool).task_id, "stuck");
    assert_eq!(pool.in_flight(), 0);
    assert!(
        pool.try_next_completion().is_none(),
        "the dropped jobs left no completion"
    );
    assert_eq!(ran.load(Ordering::SeqCst), 0, "the dropped jobs never ran");
}

#[test]
fn shutdown_returns_as_soon_as_the_running_jobs_finish() {
    let pool: Pool<usize> = Pool::new(2);
    let gate = Arc::new(Gate::default());
    let started = Arc::new(Latch::default());
    for i in 0..2 {
        let (gate, started) = (gate.clone(), started.clone());
        pool.submit(format!("t{i}"), move || {
            started.bump();
            gate.wait();
            i
        })
        .unwrap();
    }
    assert!(started.wait_for(2));
    let opener = std::thread::spawn({
        let gate = gate.clone();
        move || gate.open()
    });
    let begun = Instant::now();
    let shutdown = pool.shutdown(Duration::from_secs(60));
    opener.join().unwrap();
    assert!(shutdown.abandoned.is_empty() && shutdown.dropped.is_empty());
    assert!(
        begun.elapsed() < LONG,
        "a quick job does not cost the whole wait"
    );
    assert_eq!(pool.in_flight(), 0);
}

#[test]
fn submit_after_shutdown_is_refused_and_the_job_never_runs() {
    let pool: Pool<usize> = Pool::new(2);
    pool.submit("before", || 1).unwrap();
    assert_eq!(next(&pool).task_id, "before");
    assert_eq!(pool.shutdown(LONG), super::Shutdown::default());
    let ran = Arc::new(AtomicUsize::new(0));
    let flag = ran.clone();
    let refused = pool.submit("after", move || {
        flag.fetch_add(1, Ordering::SeqCst);
        2
    });
    assert_eq!(refused, Err(SubmitError::Closed));
    assert_eq!(refused.unwrap_err().to_string(), "worker pool is shut down");
    assert_eq!(
        pool.shutdown(Duration::ZERO),
        super::Shutdown::default(),
        "a second shutdown is harmless"
    );
    assert!(pool.try_next_completion().is_none());
    assert_eq!(ran.load(Ordering::SeqCst), 0);
}

#[test]
fn dropping_the_pool_drops_the_queued_jobs_but_not_the_running_one() {
    let pool: Pool<usize> = Pool::new(1);
    let (release, released) = mpsc::channel::<()>();
    let started = Arc::new(Latch::default());
    let signal = started.clone();
    let finished = Arc::new(Latch::default());
    let done = finished.clone();
    let ran = Arc::new(AtomicUsize::new(0));
    pool.submit("running", move || {
        signal.bump();
        released.recv().ok();
        done.bump();
        0
    })
    .unwrap();
    let flag = ran.clone();
    pool.submit("queued", move || {
        flag.fetch_add(1, Ordering::SeqCst);
        1
    })
    .unwrap();
    assert!(started.wait_for(1));
    drop(pool);
    release.send(()).unwrap();
    assert!(
        finished.wait_for(1),
        "the running job finished on its detached thread"
    );
    assert_eq!(ran.load(Ordering::SeqCst), 0);
}

#[test]
fn a_completion_wait_times_out_on_time_and_wakes_on_arrival() {
    let pool: Pool<usize> = Pool::new(1);
    let wait = Duration::from_millis(60);
    let begun = Instant::now();
    assert!(pool.next_completion(wait).is_none());
    let took = begun.elapsed();
    assert!(took >= wait && took < LONG, "timed out after {took:?}");
    assert!(pool.next_completion(Duration::ZERO).is_none());

    let gate = Arc::new(Gate::default());
    let started = Arc::new(Latch::default());
    let (job_gate, job_started) = (gate.clone(), started.clone());
    pool.submit("late", move || {
        job_started.bump();
        job_gate.wait();
        5
    })
    .unwrap();
    assert!(started.wait_for(1));
    assert!(
        pool.next_completion(Duration::from_millis(30)).is_none(),
        "a running job is no completion"
    );
    let opener = std::thread::spawn(move || gate.open());
    let begun = Instant::now();
    let done = pool
        .next_completion(Duration::from_secs(60))
        .expect("woken by the completion, not by the timeout");
    opener.join().unwrap();
    assert!(begun.elapsed() < LONG);
    assert_eq!(done.outcome, Outcome::Done(5));
}

#[test]
fn a_timeout_too_large_for_the_clock_still_returns_the_completion() {
    let pool: Pool<usize> = Pool::new(1);
    pool.submit("t", || 9).unwrap();
    let done = pool.next_completion(Duration::MAX).expect("completion");
    assert_eq!(done.outcome, Outcome::Done(9));
}

#[test]
fn a_thousand_jobs_complete_exactly_once() {
    let pool: Pool<usize> = Pool::new(8);
    let runs: Arc<Vec<AtomicUsize>> = Arc::new((0..1000).map(|_| AtomicUsize::new(0)).collect());
    let running = Arc::new(AtomicUsize::new(0));
    let high_water = Arc::new(AtomicUsize::new(0));
    for i in 0..1000 {
        let (runs, running, high_water) = (runs.clone(), running.clone(), high_water.clone());
        pool.submit(format!("t{i}"), move || {
            let now = running.fetch_add(1, Ordering::SeqCst) + 1;
            high_water.fetch_max(now, Ordering::SeqCst);
            runs[i].fetch_add(1, Ordering::SeqCst);
            running.fetch_sub(1, Ordering::SeqCst);
            i
        })
        .unwrap();
    }
    let mut seen = vec![0_usize; 1000];
    for _ in 0..1000 {
        let done = next(&pool);
        let Outcome::Done(i) = done.outcome else {
            panic!("job {} crashed", done.task_id)
        };
        assert_eq!(done.task_id, format!("t{i}"));
        seen[i] += 1;
    }
    assert!(
        seen.iter().all(|&n| n == 1),
        "every completion exactly once"
    );
    assert!(
        runs.iter().all(|n| n.load(Ordering::SeqCst) == 1),
        "every job ran exactly once"
    );
    assert!(high_water.load(Ordering::SeqCst) <= 8);
    assert!(pool.try_next_completion().is_none());
    assert_eq!(pool.in_flight(), 0);
}

#[test]
fn no_lock_is_held_while_a_job_runs() {
    // Every job calls back into the pool through a shared handle: it reads the counters and submits a child, which takes the same
    // mutex while the job's own completion is not posted yet. With the lock held around a job that deadlocks, and the loop's own
    // pool calls block too, so the whole exchange runs on a driver thread the test watches with a deadline.
    let (report, reported) = mpsc::channel();
    std::thread::spawn(move || {
        let pool: Arc<Pool<usize>> = Arc::new(Pool::new(3));
        for i in 0..6 {
            let handle = pool.clone();
            pool.submit(format!("t{i}"), move || {
                let in_flight = handle.in_flight();
                assert!(in_flight >= 1 && handle.queued() < in_flight);
                handle.submit(format!("child{i}"), move || 100 + i).unwrap();
                i
            })
            .unwrap();
        }
        let mut got: Vec<(String, Outcome<usize>)> = (0..12)
            .map(|_| next(&pool))
            .map(|c| (c.task_id, c.outcome))
            .collect();
        got.sort_by(|a, b| a.0.cmp(&b.0));
        report.send((got, pool.in_flight())).ok();
    });
    let (got, in_flight) = reported
        .recv_timeout(LONG)
        .expect("the jobs and the pool calls finished: no lock is held while a job runs");
    let mut want: Vec<(String, Outcome<usize>)> = (0..6)
        .flat_map(|i| {
            [
                (format!("t{i}"), Outcome::Done(i)),
                (format!("child{i}"), Outcome::Done(100 + i)),
            ]
        })
        .collect();
    want.sort_by(|a, b| a.0.cmp(&b.0));
    assert_eq!(got, want);
    assert_eq!(in_flight, 0);
}
