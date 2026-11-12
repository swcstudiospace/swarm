use gsd_governor::{Governor, GovernorConfig};
use gsd_proto::gsd::v1::{BudgetRequest, BudgetSnapshot, ChatThreadId, SessionId};

fn request(
    session: &str,
    thread: &str,
    tokens: i64,
    wall_time_ms: i64,
    tool_calls: i32,
    cost_usd: f64,
) -> BudgetRequest {
    BudgetRequest {
        session_id: Some(SessionId {
            value: session.to_owned(),
        }),
        thread_id: Some(ChatThreadId {
            value: thread.to_owned(),
        }),
        tokens,
        wall_time_ms,
        tool_calls,
        cost_usd,
    }
}

fn config() -> GovernorConfig {
    let mut config = GovernorConfig::default();
    config.default_session_budget.max_tokens = 1_000_000;
    config.default_session_budget.max_cost_usd = 100.0;
    config.default_thread_budget.max_tokens = 1_000_000;
    config.default_thread_budget.max_cost_usd = 100.0;
    config
}

fn assert_empty(snapshot: BudgetSnapshot, config: &GovernorConfig) {
    assert_eq!(snapshot.tokens_used, 0);
    assert_eq!(snapshot.wall_time_ms_used, 0);
    assert_eq!(snapshot.tool_calls_used, 0);
    assert_eq!(snapshot.cost_usd_used, 0.0);
    assert_eq!(
        snapshot.tokens_remaining,
        config.default_session_budget.max_tokens
    );
    assert_eq!(
        snapshot.wall_time_ms_remaining,
        config.default_session_budget.max_wall_time_ms
    );
    assert_eq!(
        snapshot.tool_calls_remaining,
        config.default_session_budget.max_tool_calls
    );
    assert_eq!(
        snapshot.cost_usd_remaining,
        config.default_session_budget.max_cost_usd
    );
}

async fn assert_global_remainder(governor: &Governor, tokens: i64, cost_usd: f64) {
    assert!(
        !governor
            .reserve_budget(request("probe", "probe", tokens + 1, 0, 0, 0.0))
            .await
            .granted,
        "unrelated reservations must still consume the global token cap"
    );
    assert!(
        !governor
            .reserve_budget(request("probe", "probe", 0, 0, 0, cost_usd + 0.25))
            .await
            .granted,
        "unrelated reservations must still consume the global cost cap"
    );
    assert!(
        governor
            .reserve_budget(request("probe", "probe", tokens, 0, 0, cost_usd))
            .await
            .granted,
        "only the actual released capacity should be available"
    );
}

#[tokio::test]
async fn repeated_release_preserves_unrelated_tokens_and_cost() {
    let config = config();
    let governor = Governor::new(config.clone());
    assert!(
        governor
            .reserve_budget(request("s", "t", 400_000, 500, 2, 40.0))
            .await
            .granted
    );
    assert!(
        governor
            .reserve_budget(request("other", "other", 600_000, 500, 2, 60.0))
            .await
            .granted
    );
    let other_before = governor.get_budget_snapshot("other").unwrap();

    // The first release returns the reservation. Each repeat matches nothing, so it is refused and changes
    // nothing: the unrelated session stays as it was.
    governor
        .release_budget("s", "t", 400_000, 500, 2, 40.0)
        .await
        .unwrap();
    assert_empty(governor.get_budget_snapshot("s").unwrap(), &config);
    for _ in 0..2 {
        let repeat = governor
            .release_budget("s", "t", 400_000, 500, 2, 40.0)
            .await;
        assert!(matches!(
            repeat,
            Err(gsd_governor::GovernorError::UnknownReservation(_))
        ));
        assert_empty(governor.get_budget_snapshot("s").unwrap(), &config);
        assert_eq!(governor.get_budget_snapshot("other").unwrap(), other_before);
    }
    assert_global_remainder(&governor, 400_000, 40.0).await;
}

#[tokio::test]
async fn oversized_release_restores_only_reserved_local_and_global_capacity() {
    let config = config();
    let governor = Governor::new(config.clone());
    assert!(
        governor
            .reserve_budget(request("s", "t", 400_000, 500, 2, 40.0))
            .await
            .granted
    );
    assert!(
        governor
            .reserve_budget(request("other", "other", 600_000, 500, 2, 60.0))
            .await
            .granted
    );
    let other_before = governor.get_budget_snapshot("other").unwrap();

    governor
        .release_budget("s", "t", i64::MAX, i64::MAX, i32::MAX, f64::MAX)
        .await
        .unwrap();
    assert_empty(governor.get_budget_snapshot("s").unwrap(), &config);
    assert_eq!(governor.get_budget_snapshot("other").unwrap(), other_before);
    assert_global_remainder(&governor, 400_000, 40.0).await;

    // A different session using the released thread can consume exactly its original limits.
    assert!(
        governor
            .reserve_budget(request("thread-probe", "t", 0, 300_000, 100, 0.0))
            .await
            .granted
    );
    assert!(
        !governor
            .reserve_budget(request("thread-probe", "t", 0, 1, 0, 0.0))
            .await
            .granted
    );
    assert!(
        !governor
            .reserve_budget(request("thread-probe", "t", 0, 0, 1, 0.0))
            .await
            .granted
    );
}

#[tokio::test]
async fn unknown_or_mismatched_release_cannot_free_existing_reservations() {
    let governor = Governor::new(config());
    assert!(
        governor
            .reserve_budget(request("s1", "t1", 400_000, 500, 2, 40.0))
            .await
            .granted
    );
    assert!(
        governor
            .reserve_budget(request("s2", "t2", 600_000, 700, 3, 60.0))
            .await
            .granted
    );
    let first_before = governor.get_budget_snapshot("s1").unwrap();
    let second_before = governor.get_budget_snapshot("s2").unwrap();

    for (session, thread) in [
        ("unknown", "unknown"),
        ("unknown", "t1"),
        ("s1", "unknown"),
        ("s1", "t2"),
        ("s2", "t1"),
    ] {
        let refused = governor
            .release_budget(session, thread, i64::MAX, i64::MAX, i32::MAX, f64::MAX)
            .await;
        assert!(
            matches!(
                refused,
                Err(gsd_governor::GovernorError::UnknownReservation(_))
            ),
            "{session}/{thread} matches no reservation, got {refused:?}"
        );
        assert_eq!(governor.get_budget_snapshot("s1").unwrap(), first_before);
        assert_eq!(governor.get_budget_snapshot("s2").unwrap(), second_before);
        assert!(
            !governor
                .reserve_budget(request("probe", "probe", 1, 0, 0, 0.0))
                .await
                .granted
        );
        assert!(
            !governor
                .reserve_budget(request("probe", "probe", 0, 0, 0, 0.25))
                .await
                .granted
        );
    }

    // Both original pairs must still be fully releasable, including their cost.
    governor
        .release_budget("s1", "t1", 400_000, 500, 2, 40.0)
        .await
        .unwrap();
    governor
        .release_budget("s2", "t2", 600_000, 700, 3, 60.0)
        .await
        .unwrap();
    assert_global_remainder(&governor, 1_000_000, 100.0).await;
}

#[tokio::test]
async fn oversized_release_cannot_drain_another_thread_in_the_same_session() {
    let governor = Governor::new(config());
    assert!(
        governor
            .reserve_budget(request("s", "t1", 400_000, 500, 2, 40.0))
            .await
            .granted
    );
    assert!(
        governor
            .reserve_budget(request("s", "t2", 600_000, 700, 3, 60.0))
            .await
            .granted
    );

    governor
        .release_budget("s", "t1", i64::MAX, i64::MAX, i32::MAX, f64::MAX)
        .await
        .unwrap();
    // The reservation is gone, so a repeat is refused and changes nothing.
    let refused = governor
        .release_budget("s", "t1", i64::MAX, i64::MAX, i32::MAX, f64::MAX)
        .await;
    assert!(matches!(
        refused,
        Err(gsd_governor::GovernorError::UnknownReservation(_))
    ));
    let snapshot = governor.get_budget_snapshot("s").unwrap();
    assert_eq!(snapshot.tokens_used, 600_000);
    assert_eq!(snapshot.wall_time_ms_used, 700);
    assert_eq!(snapshot.tool_calls_used, 3);
    assert_eq!(snapshot.cost_usd_used, 60.0);
    assert_global_remainder(&governor, 400_000, 40.0).await;
}

#[tokio::test]
async fn oversized_release_cannot_drain_another_session_on_the_same_thread() {
    let governor = Governor::new(config());
    assert!(
        governor
            .reserve_budget(request("s1", "t", 400_000, 500, 2, 40.0))
            .await
            .granted
    );
    assert!(
        governor
            .reserve_budget(request("s2", "t", 600_000, 700, 3, 60.0))
            .await
            .granted
    );
    let other_before = governor.get_budget_snapshot("s2").unwrap();

    governor
        .release_budget("s1", "t", i64::MAX, i64::MAX, i32::MAX, f64::MAX)
        .await
        .unwrap();
    let refused = governor
        .release_budget("s1", "t", i64::MAX, i64::MAX, i32::MAX, f64::MAX)
        .await;
    assert!(matches!(
        refused,
        Err(gsd_governor::GovernorError::UnknownReservation(_))
    ));
    assert_eq!(governor.get_budget_snapshot("s2").unwrap(), other_before);
    assert_global_remainder(&governor, 400_000, 40.0).await;

    // Global room exists after the probe is released, but the shared thread still holds s2.
    governor
        .release_budget("probe", "probe", 400_000, 0, 0, 40.0)
        .await
        .unwrap();
    assert!(
        !governor
            .reserve_budget(request("s1", "t", 400_001, 0, 0, 0.0))
            .await
            .granted
    );
    assert!(
        !governor
            .reserve_budget(request("s1", "t", 0, 0, 0, 40.25))
            .await
            .granted
    );
    assert!(
        governor
            .reserve_budget(request("s1", "t", 400_000, 0, 0, 40.0))
            .await
            .granted
    );
}

#[tokio::test]
async fn partial_release_keeps_other_dimensions_reserved() {
    let governor = Governor::new(config());
    assert!(
        governor
            .reserve_budget(request("s", "t", 400_000, 500, 2, 40.0))
            .await
            .granted
    );
    assert!(
        governor
            .reserve_budget(request("other", "other", 600_000, 700, 3, 60.0))
            .await
            .granted
    );

    governor
        .release_budget("s", "t", i64::MAX, 100, 0, 10.0)
        .await
        .unwrap();
    let snapshot = governor.get_budget_snapshot("s").unwrap();
    assert_eq!(snapshot.tokens_used, 0);
    assert_eq!(snapshot.wall_time_ms_used, 400);
    assert_eq!(snapshot.tool_calls_used, 2);
    assert_eq!(snapshot.cost_usd_used, 30.0);
    assert_global_remainder(&governor, 400_000, 10.0).await;

    // Zero remaining tokens must not make the remaining wall time, calls or cost unreleasable.
    governor
        .release_budget("s", "t", i64::MAX, i64::MAX, i32::MAX, f64::MAX)
        .await
        .unwrap();
    assert_empty(governor.get_budget_snapshot("s").unwrap(), &config());
    assert!(
        !governor
            .reserve_budget(request("probe", "probe", 1, 0, 0, 0.0))
            .await
            .granted
    );
    assert!(
        !governor
            .reserve_budget(request("probe", "probe", 0, 0, 0, 30.25))
            .await
            .granted
    );
    assert!(
        governor
            .reserve_budget(request("probe", "probe", 0, 0, 0, 30.0))
            .await
            .granted
    );
}

#[tokio::test]
async fn full_cost_release_refunds_only_the_micros_charged_to_its_pair() {
    let governor = Governor::new(config());
    assert!(
        governor
            .reserve_budget(request("other", "other", 0, 0, 0, 99.999_998))
            .await
            .granted
    );
    let other_before = governor.get_budget_snapshot("other").unwrap();
    // Each reservation rounds to one micro; rounding the pair's total would return only one.
    for _ in 0..2 {
        assert!(
            governor
                .reserve_budget(request("s", "t", 0, 0, 0, 0.000_000_6))
                .await
                .granted
        );
    }
    governor
        .release_budget("s", "t", 0, 0, 0, f64::MAX)
        .await
        .unwrap();
    assert_eq!(
        governor.get_budget_snapshot("s").unwrap().cost_usd_used,
        0.0
    );
    assert_eq!(governor.get_budget_snapshot("other").unwrap(), other_before);
    let refused = governor.release_budget("s", "t", 0, 0, 0, f64::MAX).await;
    assert!(matches!(
        refused,
        Err(gsd_governor::GovernorError::UnknownReservation(_))
    ));
    assert_eq!(
        governor.get_budget_snapshot("s").unwrap().cost_usd_used,
        0.0
    );
    assert_eq!(governor.get_budget_snapshot("other").unwrap(), other_before);
    assert!(
        !governor
            .reserve_budget(request("probe", "probe", 0, 0, 0, 0.000_003))
            .await
            .granted
    );
    assert!(
        governor
            .reserve_budget(request("probe", "probe", 0, 0, 0, 0.000_002))
            .await
            .granted
    );
}

#[tokio::test]
async fn a_clamped_release_reports_the_freed_amount() {
    let config = config();
    let governor = Governor::new(config.clone());
    assert!(
        governor
            .reserve_budget(request("s", "t", 300, 0, 0, 0.0))
            .await
            .granted
    );

    let freed = governor
        .release_freed("s", "t", 500, 0, 0, 0.0)
        .await
        .unwrap();
    assert_eq!(freed.tokens, 300);
    assert_eq!(governor.get_budget_snapshot("s").unwrap().tokens_used, 0);
    assert_empty(governor.get_budget_snapshot("s").unwrap(), &config);
}
