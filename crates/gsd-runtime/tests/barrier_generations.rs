use gsd_runtime::AsyncBarrier;
use std::time::Duration;
use tokio_test::{assert_pending, assert_ready, task::spawn};

// These public-API tests establish generation invariants. They do not force the
// old implementation's non-yielding reset/notify interleaving between threads.
#[test]
fn successive_cohorts_have_exactly_one_leader() {
    let barrier = AsyncBarrier::new(3);
    let first_clone = barrier.clone();
    let second_clone = barrier.clone();

    for _ in 0..8 {
        let mut first = spawn(first_clone.wait());
        let mut second = spawn(second_clone.wait());
        let mut third = spawn(barrier.wait());
        assert_pending!(first.poll());
        assert_pending!(second.poll());
        assert_pending!(first.poll());

        let last_result = assert_ready!(third.poll());
        let results = [
            assert_ready!(first.poll()),
            assert_ready!(second.poll()),
            last_result,
        ];
        assert_eq!(results.iter().filter(|leader| **leader).count(), 1);
    }
}

#[test]
fn next_cohort_waits_while_previous_cohort_drains() {
    let barrier = AsyncBarrier::new(3);
    let mut previous_first = spawn(barrier.wait());
    let mut previous_second = spawn(barrier.wait());
    let mut previous_third = spawn(barrier.wait());
    assert_pending!(previous_first.poll());
    assert_pending!(previous_second.poll());
    let previous_last_result = assert_ready!(previous_third.poll());

    // Enter the next generation before the released waiters have been repolled.
    let mut next_first = spawn(barrier.wait());
    assert_pending!(next_first.poll());
    let previous_results = [
        assert_ready!(previous_first.poll()),
        assert_ready!(previous_second.poll()),
        previous_last_result,
    ];
    assert_eq!(previous_results.iter().filter(|leader| **leader).count(), 1);
    assert_pending!(next_first.poll());

    let mut next_second = spawn(barrier.wait());
    assert_pending!(next_second.poll());
    // Draining the old generation must not supply this missing third arrival.
    assert_pending!(next_first.poll());
    assert_pending!(next_second.poll());

    let mut next_third = spawn(barrier.wait());
    let next_last_result = assert_ready!(next_third.poll());
    let next_results = [
        assert_ready!(next_first.poll()),
        assert_ready!(next_second.poll()),
        next_last_result,
    ];
    assert_eq!(next_results.iter().filter(|leader| **leader).count(), 1);
}

#[test]
fn single_participant_leads_every_generation() {
    let barrier = AsyncBarrier::new(1);
    for _ in 0..8 {
        let mut arrival = spawn(barrier.wait());
        assert!(assert_ready!(arrival.poll()));
    }
}

#[test]
#[should_panic(expected = "AsyncBarrier needs at least one participant")]
fn zero_participants_are_rejected() {
    AsyncBarrier::new(0);
}

#[tokio::test]
async fn timeout_wrapper_preserves_generation_leader_results() {
    let barrier = AsyncBarrier::new(2);
    for _ in 0..3 {
        let mut first = spawn(barrier.wait_timeout(Duration::from_secs(60)));
        let mut second = spawn(barrier.wait_timeout(Duration::from_secs(60)));
        assert_pending!(first.poll());
        let last_result = assert_ready!(second.poll()).expect("complete cohort timed out");
        let results = [
            assert_ready!(first.poll()).expect("released waiter timed out"),
            last_result,
        ];
        assert_eq!(results.iter().filter(|leader| **leader).count(), 1);
    }
}

#[tokio::test]
async fn cancelling_timeout_wait_does_not_roll_back_counted_arrival() {
    let barrier = AsyncBarrier::new(2);
    let mut cancelled = spawn(barrier.wait_timeout(Duration::from_secs(60)));
    assert_pending!(cancelled.poll());
    drop(cancelled);

    // Cancellation is deliberately not repaired: the dropped wait still counts.
    let mut remaining = spawn(barrier.wait());
    assert!(assert_ready!(remaining.poll()));

    // The following generation still requires a full cohort of new arrivals.
    let mut next_first = spawn(barrier.wait());
    assert_pending!(next_first.poll());
    let mut next_second = spawn(barrier.wait());
    let next_last_result = assert_ready!(next_second.poll());
    let results = [assert_ready!(next_first.poll()), next_last_result];
    assert_eq!(results.iter().filter(|leader| **leader).count(), 1);
}
