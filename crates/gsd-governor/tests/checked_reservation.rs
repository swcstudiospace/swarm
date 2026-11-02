use gsd_governor::{Governor, GovernorConfig, GovernorError};
use gsd_proto::gsd::v1::{BudgetGrant, BudgetRequest, ChatThreadId, SessionId};
use std::future::Future;
use std::pin::pin;
use std::sync::Arc;
use std::task::{Context, Poll, Wake, Waker};

/// Wakes a parked test thread when the future becomes ready to make progress.
struct ThreadWaker(std::thread::Thread);

impl Wake for ThreadWaker {
    fn wake(self: Arc<Self>) {
        self.0.unpark();
    }
}

/// Drives a future to completion on the current thread without an external runtime.
fn block_on<F: Future>(future: F) -> F::Output {
    let waker = Waker::from(Arc::new(ThreadWaker(std::thread::current())));
    let mut context = Context::from_waker(&waker);
    let mut future = pin!(future);
    loop {
        match future.as_mut().poll(&mut context) {
            Poll::Ready(output) => return output,
            Poll::Pending => std::thread::park(),
        }
    }
}

fn request(session: Option<&str>, thread: Option<&str>, tokens: i64) -> BudgetRequest {
    BudgetRequest {
        session_id: session.map(|value| SessionId {
            value: value.to_owned(),
        }),
        thread_id: thread.map(|value| ChatThreadId {
            value: value.to_owned(),
        }),
        tokens,
        wall_time_ms: 0,
        tool_calls: 0,
        cost_usd: 0.0,
    }
}

fn governor_with_session_tokens(max_tokens: i64) -> Governor {
    let mut config = GovernorConfig::default();
    config.default_session_budget.max_tokens = max_tokens;
    config.default_thread_budget.max_tokens = 1_000_000;
    Governor::new(config)
}

fn assert_refused_grant(grant: &BudgetGrant) {
    assert!(!grant.granted);
    assert_eq!(grant.tokens_granted, 0);
    assert_eq!(grant.wall_time_ms_granted, 0);
    assert_eq!(grant.tool_calls_granted, 0);
    assert_eq!(grant.cost_usd_granted, 0.0);
    assert!(grant.valid_for.is_none());
}

#[test]
fn checked_reservation_grants_within_budget() {
    let governor = Governor::new(GovernorConfig::default());
    let grant = block_on(governor.reserve_budget_checked(request(Some("s"), Some("t"), 1_000)))
        .expect("reservation within budget is accepted");
    assert!(grant.granted);
    assert_eq!(grant.tokens_granted, 1_000);
}

#[test]
fn missing_thread_id_is_invalid_budget_on_checked_path_and_refused_on_wire() {
    let governor = Governor::new(GovernorConfig::default());

    let err = block_on(governor.reserve_budget_checked(request(Some("s"), None, 1_000)))
        .expect_err("missing thread_id is refused");
    assert!(matches!(err, GovernorError::InvalidBudget(_)));

    let grant = block_on(governor.reserve_budget(request(Some("s"), None, 1_000)));
    assert_refused_grant(&grant);
}

#[test]
fn missing_session_id_is_invalid_budget_on_checked_path_and_refused_on_wire() {
    let governor = Governor::new(GovernorConfig::default());

    let err = block_on(governor.reserve_budget_checked(request(None, Some("t"), 1_000)))
        .expect_err("missing session_id is refused");
    assert!(matches!(err, GovernorError::InvalidBudget(_)));

    let grant = block_on(governor.reserve_budget(request(None, Some("t"), 1_000)));
    assert_refused_grant(&grant);
}

#[test]
fn negative_tokens_are_invalid_budget_on_checked_path_and_refused_on_wire() {
    let governor = Governor::new(GovernorConfig::default());

    let err = block_on(governor.reserve_budget_checked(request(Some("s"), Some("t"), -5)))
        .expect_err("negative tokens are refused");
    assert!(matches!(err, GovernorError::InvalidBudget(_)));

    let grant = block_on(governor.reserve_budget(request(Some("s"), Some("t"), -5)));
    assert_refused_grant(&grant);
}

#[test]
fn over_budget_is_budget_exceeded_and_leaves_snapshot_unchanged() {
    let governor = governor_with_session_tokens(1_000);
    block_on(governor.reserve_budget_checked(request(Some("s"), Some("t"), 100)))
        .expect("first reservation is accepted");
    let before = governor
        .get_budget_snapshot("s")
        .expect("session budget exists");

    let err = block_on(governor.reserve_budget_checked(request(Some("s"), Some("t"), 5_000)))
        .expect_err("request above the session budget is refused");
    assert!(matches!(err, GovernorError::BudgetExceeded(_)));

    let after = governor
        .get_budget_snapshot("s")
        .expect("session budget exists");
    assert_eq!(after, before);
}

#[test]
fn over_budget_is_refused_on_wire_with_zero_amounts() {
    let governor = governor_with_session_tokens(1_000);

    let grant = block_on(governor.reserve_budget(request(Some("s"), Some("t"), 5_000)));
    assert_refused_grant(&grant);
}

#[test]
fn release_of_an_unknown_reservation_is_refused() {
    let governor = Governor::new(GovernorConfig::default());
    let result = block_on(governor.release_budget("no-session", "no-thread", 1, 1, 1, 0.0));
    assert!(
        matches!(result, Err(GovernorError::UnknownReservation(_))),
        "a release that matches no reservation is refused, got {result:?}"
    );
}

#[test]
fn global_token_limit_comes_from_the_config() {
    let mut config = GovernorConfig {
        global_token_limit: 100,
        ..GovernorConfig::default()
    };
    config.default_session_budget.max_tokens = 1_000;
    config.default_thread_budget.max_tokens = 1_000;
    let governor = Governor::new(config);
    // A refused reservation changes nothing, so the 100-token reservation that follows still fits.
    let refused = request(Some("s"), Some("t"), 101);
    assert_refused_grant(&block_on(governor.reserve_budget(refused)));
    assert!(block_on(governor.reserve_budget(request(Some("s"), Some("t"), 100))).granted);
}

#[test]
fn check_release_refuses_a_pair_without_a_reservation_and_changes_nothing() {
    let mut config = GovernorConfig {
        global_token_limit: 100,
        ..GovernorConfig::default()
    };
    config.default_session_budget.max_tokens = 1_000;
    config.default_thread_budget.max_tokens = 1_000;
    let governor = Governor::new(config);
    // Another pair holds 40 of the 100 global tokens, so the session snapshot is not empty.
    block_on(governor.reserve_budget_checked(request(Some("s"), Some("other"), 40)))
        .expect("reservation for another pair is accepted");
    let before = governor
        .get_budget_snapshot("s")
        .expect("session budget exists");

    let err = governor
        .check_release("s", "t", 1, 0, 0, 0.0)
        .expect_err("a pair with no reservation is refused");
    assert!(matches!(err, GovernorError::UnknownReservation(_)));
    let after = governor
        .get_budget_snapshot("s")
        .expect("session budget exists");
    assert_eq!(after, before);

    // The refused check added nothing to the global totals, so 60 more tokens still reach the cap.
    block_on(governor.reserve_budget_checked(request(Some("s"), Some("t"), 60)))
        .expect("the 60-token reservation fits under the 100-token cap");
    governor
        .check_release("s", "t", 60, 0, 0, 0.0)
        .expect("a held pair passes the check");
    block_on(governor.release_budget("s", "t", 60, 0, 0, 0.0))
        .expect("the release the check admitted succeeds");
}
