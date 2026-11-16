//! Loom models of the pool: every interleaving of submit, completion and shutdown up to a preemption bound.
//!
//! Run with `RUSTFLAGS="--cfg loom" cargo test --release -p gsd-sched-core pool::loom_tests`. Loom's condition variable never times
//! wait below is an untimed wait: a lost wakeup shows up as a loom deadlock panic. The models are kept to at most three jobs and
//! one worker slot or two, because the interleavings grow very fast with each thread and lock operation.

use std::panic::resume_unwind;
use std::time::Duration;

use loom::model::Builder;
use loom::sync::atomic::{AtomicUsize, Ordering};
use loom::sync::Arc;

use super::{Outcome, Pool};

/// Preemption bound of every model. Six preemptions explore the three models in about 20 seconds in release mode (three take about
/// one second and already catch a lost wakeup); more would grow the run past what a CI job should spend on one module.
const PREEMPTIONS: usize = 6;

/// A wait no model reaches (loom ignores the value; the wait is unbounded).
const FOREVER: Duration = Duration::from_secs(3600);

fn model(body: impl Fn() + Sync + Send + 'static) {
    let mut builder = Builder::new();
    builder.preemption_bound = Some(PREEMPTIONS);
    builder.check(body);
}

/// One slot, two jobs: the second job is queued or starts at once depending on the interleaving. The loop thread blocks in
/// `next_completion`; both completions must arrive, in finish order (which with one slot is submission order), and each job runs once.
#[test]
fn submit_and_blocking_completion_never_lose_a_wakeup_or_a_job() {
    model(|| {
        let pool: Pool<usize> = Pool::new(1);
        let runs = Arc::new(AtomicUsize::new(0));
        for i in 0..2 {
            let runs = runs.clone();
            pool.submit(format!("t{i}"), move || {
                runs.fetch_add(1, Ordering::SeqCst);
                i
            })
            .unwrap();
        }
        let first = pool.next_completion(FOREVER).unwrap();
        let second = pool.next_completion(FOREVER).unwrap();
        assert_eq!(
            (first.task_id.as_str(), first.outcome),
            ("t0", Outcome::Done(0))
        );
        assert_eq!(
            (second.task_id.as_str(), second.outcome),
            ("t1", Outcome::Done(1))
        );
        assert_eq!(runs.load(Ordering::SeqCst), 2);
        assert_eq!(pool.in_flight(), 0);
    });
}

/// Two slots: both jobs run concurrently with each other and with the waiting loop thread; the two completions are both delivered.
#[test]
fn two_workers_deliver_both_completions() {
    model(|| {
        let pool: Pool<usize> = Pool::new(2);
        for i in 0..2 {
            pool.submit(format!("t{i}"), move || i).unwrap();
        }
        let mut ids = vec![
            pool.next_completion(FOREVER).unwrap().task_id,
            pool.next_completion(FOREVER).unwrap().task_id,
        ];
        ids.sort();
        assert_eq!(ids, ["t0", "t1"]);
        assert_eq!(pool.in_flight(), 0);
    });
}

/// Shutdown races the worker: it waits for the running job (a lost `drained` wakeup is a deadlock), drops a queued job only if that
/// job had not been taken yet, and after it returns every job is accounted for exactly once: completed, or dropped from the queue.
#[test]
fn shutdown_accounts_for_every_job_exactly_once() {
    model(|| {
        let pool: Pool<usize> = Pool::new(1);
        let runs = Arc::new(AtomicUsize::new(0));
        for i in 0..2 {
            let runs = runs.clone();
            pool.submit(format!("t{i}"), move || {
                runs.fetch_add(1, Ordering::SeqCst);
                i
            })
            .unwrap();
        }
        let shutdown = pool.shutdown(FOREVER);
        assert!(
            shutdown.abandoned.is_empty(),
            "an unbounded wait leaves nothing running"
        );
        let mut completed = 0;
        while pool.try_next_completion().is_some() {
            completed += 1;
        }
        assert_eq!(completed + shutdown.dropped.len(), 2);
        assert_eq!(
            runs.load(Ordering::SeqCst),
            completed,
            "a dropped job never ran, a completed one ran once"
        );
        assert!(
            shutdown.dropped.iter().all(|id| id == "t1"),
            "only the later job can still be queued"
        );
        assert_eq!(pool.in_flight(), 0);
        assert!(pool.submit("late", || 9).is_err());
    });
}

/// Two workers and three jobs, the first of which panics: the panicking worker must publish its `Crashed` completion, hand its slot
/// to the queued job and wake the loop thread blocked in `next_completion`. All three completions arrive, the payload never leaves the
/// worker and the pool ends empty. `resume_unwind` skips the panic hook, so no interleaving prints a message.
#[test]
fn a_worker_panic_under_contention_frees_its_slot_and_loses_no_completion() {
    model(|| {
        let pool: Pool<usize> = Pool::new(2);
        pool.submit("t0", || 0).unwrap();
        pool.submit("boom", || -> usize { resume_unwind(Box::new("boom")) })
            .unwrap();
        pool.submit("t2", || 2).unwrap();
        let mut got: Vec<(String, Outcome<usize>)> = (0..3)
            .map(|_| {
                let done = pool.next_completion(FOREVER).unwrap();
                (done.task_id, done.outcome)
            })
            .collect();
        got.sort_by(|a, b| a.0.cmp(&b.0));
        assert_eq!(
            got,
            [
                ("boom".to_owned(), Outcome::Crashed),
                ("t0".to_owned(), Outcome::Done(0)),
                ("t2".to_owned(), Outcome::Done(2)),
            ]
        );
        assert_eq!((pool.in_flight(), pool.queued()), (0, 0));
    });
}
