//! U3.6 teardown: returns a finished thread to the B2 warm pool or destroys it, and settles its reserved
//! budget through the governor.
//!
//! The caller chooses the [`Disposition`]. This module does not decide which threads are reusable. A
//! thread that is `RUNNING` or `BLOCKED` is refused, because it is still live and must be cancelled or
//! joined first. A thread that never ran (`IDLE` or `QUEUED`) and a terminal thread may be torn down.
//!
//! Settlement returns the unused part of the bind-time reservation, which is the reservation minus the
//! consumed amounts the caller supplies. The repo has no usage meter, so the caller measures consumption.
//! The refund goes back to the governor through `release_freed` and to the session's budget snapshot.
//! An overrun (consumed above reserved) refunds nothing and is reported.
//!
//! Checkpoint writing and branch or merge lineage (`SESSION_BRANCHED` / `SESSION_MERGED`) are not
//! implemented. Their storage was removed in the state repair, and the repo gives no answer for them.

use std::time::Instant;

use gsd_governor::{Governor, GovernorError};
use gsd_pool::{ChatPool, Placement as PoolPlacement, ThreadPhase};
use gsd_proto::gsd::v1::{BudgetRequest, BudgetSnapshot, SessionState, SubagentType, ThreadState};

use crate::session_dispatch::DispatchError;
use crate::session_join::phase_of;

/// Result type for teardown.
pub type TeardownResult<T> = Result<T, TeardownError>;

/// Typed refusal or failure from teardown.
#[derive(Debug, thiserror::Error)]
pub enum TeardownError {
    /// The thread is not in the session, including a thread already torn down.
    #[error("thread {0} is not in the session")]
    UnknownThread(String),
    /// The thread is still running or blocked. Cancel or join it before teardown.
    #[error("thread {thread} is {phase} and still live; cancel or join it before teardown")]
    Live {
        /// Thread that is still live.
        thread: String,
        /// Its current phase.
        phase: &'static str,
    },
    /// The thread lacks the id or subagent type a pool handle needs.
    #[error("thread {0} has no id or subagent type, so it has no pool handle")]
    NoHandle(String),
    /// The session has no session id, so its governor reservations cannot be found.
    #[error("session has no session id")]
    MissingSessionId,
    /// The session has no budget snapshot to settle against.
    #[error("session has no budget snapshot")]
    MissingBudget,
    /// A reserved or consumed amount is negative or not finite.
    #[error("amounts must be finite and non-negative")]
    InvalidAmounts,
    /// A thread lookup or thread-phase read refused. See [`DispatchError`].
    #[error("dispatch: {0}")]
    Dispatch(#[from] DispatchError),
    /// The governor refused the settlement.
    #[error("governor: {0}")]
    Governor(#[from] GovernorError),
    /// The chat pool refused an open, close or park. Carries the pool error text.
    #[error("pool: {0}")]
    Pool(String),
}

/// Where a torn-down thread goes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Disposition {
    /// Park the thread's handle in the warm pool. A full pool destroys it instead.
    ReleaseToPool,
    /// Drop the thread's handle.
    Destroy,
}

/// Where a torn-down thread's handle ended up.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Placement {
    /// The handle was parked in the warm pool.
    ReleasedToPool,
    /// The handle was dropped, either by request or because the pool was full.
    Destroyed,
}

/// A budget amount: the four dimensions the governor reserves.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Amounts {
    /// Tokens.
    pub tokens: i64,
    /// Wall-clock milliseconds.
    pub wall_time_ms: i64,
    /// Tool calls.
    pub tool_calls: i32,
    /// Cost in US dollars.
    pub cost_usd: f64,
}

impl From<&BudgetRequest> for Amounts {
    fn from(request: &BudgetRequest) -> Self {
        Self {
            tokens: request.tokens,
            wall_time_ms: request.wall_time_ms,
            tool_calls: request.tool_calls,
            cost_usd: request.cost_usd,
        }
    }
}

/// One teardown call.
#[derive(Debug, Clone, Copy)]
pub struct Teardown<'a> {
    /// Thread to tear down.
    pub thread_id: &'a str,
    /// Warm-pool release or destruction.
    pub disposition: Disposition,
    /// Amounts reserved when the thread was bound, from its `ThreadBinding.reservation`.
    pub reserved: Amounts,
    /// Amounts the thread actually consumed. Supplied by the caller.
    pub consumed: Amounts,
    /// Instant the pool uses to age the parked handle. Supplied by the caller.
    pub now: Instant,
}

/// What a teardown did.
#[derive(Debug, Clone, PartialEq)]
pub struct TeardownReport {
    /// Thread that was torn down.
    pub thread_id: String,
    /// Where its handle went.
    pub placement: Placement,
    /// Unused reservation returned to the governor and the session snapshot.
    pub refunded: Amounts,
    /// `true` when consumed exceeded reserved in some dimension. Nothing is refunded in that dimension.
    pub overrun: bool,
}

/// Tears down thread `request.thread_id`: parks its handle in `pool` (or destroys it), returns its
/// unused reservation through `governor`, then removes the thread from the session.
///
/// Every refusal before the pool step changes nothing: `InvalidAmounts`, `MissingSessionId`,
/// `MissingBudget`, `UnknownThread`, `Dispatch`, `Live`, `NoHandle`, the governor check (`Governor`
/// holding `UnknownReservation` when the session and thread pair holds no reservation), and the pool
/// refusal (`Pool`). The governor check runs before the pool step, so a thread is never parked for a
/// release that is already known to be refused.
///
/// After the pool step the teardown completes. The release is refused as `UnknownReservation` only when
/// a concurrent release drained the pair after the check. Then nothing is left to refund: the report
/// shows zero, no snapshot refund is applied, and the teardown still succeeds, because the thread is
/// already parked. A second call for a removed thread is refused as [`TeardownError::UnknownThread`] and
/// settles nothing again.
pub async fn teardown_thread(
    state: &mut SessionState,
    governor: &Governor,
    pool: &ChatPool,
    request: Teardown<'_>,
) -> TeardownResult<TeardownReport> {
    if is_invalid(request.reserved) || is_invalid(request.consumed) {
        return Err(TeardownError::InvalidAmounts);
    }
    let thread_id = request.thread_id;
    let session_id = state
        .session_id
        .as_ref()
        .map(|id| id.value.clone())
        .ok_or(TeardownError::MissingSessionId)?;
    if state.budget_snapshot.is_none() {
        return Err(TeardownError::MissingBudget);
    }
    let thread = state
        .threads
        .get(thread_id)
        .ok_or_else(|| TeardownError::UnknownThread(thread_id.to_string()))?;
    let phase = phase_of(thread, thread_id)?;
    if matches!(phase, ThreadPhase::Running | ThreadPhase::Blocked) {
        return Err(TeardownError::Live {
            thread: thread_id.to_string(),
            phase: phase.as_str(),
        });
    }
    let identity = match request.disposition {
        Disposition::ReleaseToPool => Some(thread_identity(thread, thread_id)?),
        Disposition::Destroy => None,
    };
    let (unused, overrun) = settle_amounts(request.reserved, request.consumed);
    // The check is read-only, so a refusal here changes nothing. It runs before the pool step, so no
    // thread is parked for a release that is already known to be refused.
    if unused != Amounts::default() {
        governor.check_release(
            &session_id,
            thread_id,
            unused.tokens,
            unused.wall_time_ms,
            unused.tool_calls,
            unused.cost_usd,
        )?;
    }
    // The pool step refuses without changing anything; it runs before the release.
    let pool_placement = match &identity {
        Some((pool_thread_id, subagent_type)) => Some(
            pool.park_fresh(pool_thread_id, subagent_type.clone(), request.now)
                .map_err(|err| TeardownError::Pool(err.to_string()))?,
        ),
        None => None,
    };
    let refunded = if unused == Amounts::default() {
        Amounts::default()
    } else {
        release_unused(governor, &session_id, thread_id, unused).await?
    };
    if let Some(snapshot) = state.budget_snapshot.as_mut() {
        refund(snapshot, refunded);
    }
    state.threads.remove(thread_id);

    let placement = match pool_placement {
        Some(PoolPlacement::Parked) => Placement::ReleasedToPool,
        Some(PoolPlacement::Closed) | None => Placement::Destroyed,
    };
    Ok(TeardownReport {
        thread_id: thread_id.to_string(),
        placement,
        refunded,
        overrun,
    })
}

/// Returns `true` when any amount is negative or the cost is not finite.
fn is_invalid(amounts: Amounts) -> bool {
    amounts.tokens < 0
        || amounts.wall_time_ms < 0
        || amounts.tool_calls < 0
        || !amounts.cost_usd.is_finite()
        || amounts.cost_usd < 0.0
}

/// Returns the unused reservation and whether any dimension was overrun.
fn settle_amounts(reserved: Amounts, consumed: Amounts) -> (Amounts, bool) {
    let overrun = consumed.tokens > reserved.tokens
        || consumed.wall_time_ms > reserved.wall_time_ms
        || consumed.tool_calls > reserved.tool_calls
        || consumed.cost_usd > reserved.cost_usd;
    let refunded = Amounts {
        tokens: (reserved.tokens - consumed.tokens).max(0),
        wall_time_ms: (reserved.wall_time_ms - consumed.wall_time_ms).max(0),
        tool_calls: (reserved.tool_calls - consumed.tool_calls).max(0),
        cost_usd: (reserved.cost_usd - consumed.cost_usd).max(0.0),
    };
    (refunded, overrun)
}

/// Reverses the bind-time charge for `refunded` on the session snapshot.
fn refund(snapshot: &mut BudgetSnapshot, refunded: Amounts) {
    snapshot.tokens_used = snapshot.tokens_used.saturating_sub(refunded.tokens).max(0);
    snapshot.tokens_remaining = snapshot.tokens_remaining.saturating_add(refunded.tokens);
    snapshot.wall_time_ms_used = snapshot
        .wall_time_ms_used
        .saturating_sub(refunded.wall_time_ms)
        .max(0);
    snapshot.wall_time_ms_remaining = snapshot
        .wall_time_ms_remaining
        .saturating_add(refunded.wall_time_ms);
    snapshot.tool_calls_used = snapshot
        .tool_calls_used
        .saturating_sub(refunded.tool_calls)
        .max(0);
    snapshot.tool_calls_remaining = snapshot
        .tool_calls_remaining
        .saturating_add(refunded.tool_calls);
    snapshot.cost_usd_used = (snapshot.cost_usd_used - refunded.cost_usd).max(0.0);
    snapshot.cost_usd_remaining += refunded.cost_usd;
}

/// Releases `unused` through the governor and returns the amount it freed. The governor clamps the
/// release to what the pair holds, so the result can be less than `unused`. A release refused as
/// `UnknownReservation` means a concurrent release drained the pair after the check, so nothing is
/// left to refund and the result is zero. Every other refusal is returned.
async fn release_unused(
    governor: &Governor,
    session_id: &str,
    thread_id: &str,
    unused: Amounts,
) -> TeardownResult<Amounts> {
    let released = governor
        .release_freed(
            session_id,
            thread_id,
            unused.tokens,
            unused.wall_time_ms,
            unused.tool_calls,
            unused.cost_usd,
        )
        .await;
    match released {
        Ok(freed) => Ok(Amounts {
            tokens: freed.tokens,
            wall_time_ms: freed.wall_time_ms,
            tool_calls: freed.tool_calls,
            cost_usd: freed.cost_usd,
        }),
        Err(GovernorError::UnknownReservation(_)) => Ok(Amounts::default()),
        Err(err) => Err(TeardownError::Governor(err)),
    }
}

/// Returns the pool thread id and subagent type of `thread`, which a pool handle needs.
fn thread_identity(
    thread: &ThreadState,
    thread_id: &str,
) -> TeardownResult<(String, SubagentType)> {
    let id = thread
        .id
        .as_ref()
        .ok_or_else(|| TeardownError::NoHandle(thread_id.to_string()))?;
    let subagent_type = thread
        .subagent_type
        .clone()
        .ok_or_else(|| TeardownError::NoHandle(thread_id.to_string()))?;
    Ok((id.value.clone(), subagent_type))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::session_dispatch::{admit_thread, handoff_thread};
    use crate::session_join::test_fixtures::bound_state_with;
    use crate::session_join::{apply_terminal, Terminal};
    use gsd_governor::GovernorConfig;
    use gsd_pool::{BudgetPort, BudgetRefusal, BudgetReservation, ChatRuntimeConfig};
    use gsd_proto::gsd::v1::SubagentType;
    use std::sync::Arc;

    type TestResult = Result<(), Box<dyn std::error::Error>>;

    /// Budget port for teardown tests. Teardown settles through the governor, never the port.
    struct NoBudget;

    impl BudgetPort for NoBudget {
        fn reserve(&self, _reservation: &BudgetReservation) -> Result<(), BudgetRefusal> {
            Ok(())
        }

        fn release(&self, _reservation: &BudgetReservation) -> Result<(), BudgetRefusal> {
            Ok(())
        }
    }

    /// A chat pool with `warm_pool_size` warm slots and the default live-thread cap.
    fn test_pool(warm_pool_size: usize) -> Result<ChatPool, gsd_pool::ChatError> {
        ChatPool::new(
            ChatRuntimeConfig {
                warm_pool_size,
                ..ChatRuntimeConfig::default()
            },
            Arc::new(NoBudget),
        )
    }

    /// Binds `th1` reserving `tokens` through `governor`, and runs it to `SUCCEEDED`.
    async fn finished(
        governor: &Governor,
        tokens: i64,
    ) -> Result<SessionState, Box<dyn std::error::Error>> {
        let mut state = bound_state_with(governor, &[("th1", "t1")], tokens).await?;
        admit_thread(&mut state, "th1")?;
        handoff_thread(&mut state, "th1")?;
        apply_terminal(&mut state, "th1", Terminal::Succeeded)?;
        Ok(state)
    }

    fn reserved(tokens: i64) -> Amounts {
        Amounts {
            tokens,
            ..Amounts::default()
        }
    }

    fn consumed(tokens: i64) -> Amounts {
        Amounts {
            tokens,
            ..Amounts::default()
        }
    }

    fn request(
        disposition: Disposition,
        reserved: Amounts,
        consumed: Amounts,
        now: Instant,
    ) -> Teardown<'static> {
        Teardown {
            thread_id: "th1",
            disposition,
            reserved,
            consumed,
            now,
        }
    }

    #[tokio::test]
    async fn teardown_returns_the_thread_to_the_warm_pool() -> TestResult {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = finished(&governor, 1000).await?;
        let pool = test_pool(4)?;
        let now = Instant::now();

        let report = teardown_thread(
            &mut state,
            &governor,
            &pool,
            request(
                Disposition::ReleaseToPool,
                reserved(1000),
                consumed(1000),
                now,
            ),
        )
        .await?;

        assert_eq!(report.placement, Placement::ReleasedToPool);
        assert!(!state.threads.contains_key("th1"));
        // A warm hit returns the parked id; a new thread would have a generated id.
        let reused = pool
            .acquire(SubagentType {
                value: "w".to_string(),
            })
            .await?;
        assert_eq!(reused, "th1");
        Ok(())
    }

    #[tokio::test]
    async fn teardown_destroys_the_thread_when_asked() -> TestResult {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = finished(&governor, 1000).await?;
        let pool = test_pool(4)?;

        let report = teardown_thread(
            &mut state,
            &governor,
            &pool,
            request(
                Disposition::Destroy,
                reserved(1000),
                consumed(1000),
                Instant::now(),
            ),
        )
        .await?;

        assert_eq!(report.placement, Placement::Destroyed);
        assert!(!state.threads.contains_key("th1"));
        assert_ne!(
            pool.acquire(SubagentType {
                value: "w".to_string(),
            })
            .await?,
            "th1"
        );
        Ok(())
    }

    #[tokio::test]
    async fn release_into_a_full_pool_destroys_the_handle() -> TestResult {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = finished(&governor, 1000).await?;
        let pool = test_pool(1)?;
        pool.open_thread(
            "th0",
            SubagentType {
                value: "w".to_string(),
            },
        )?;
        assert_eq!(pool.park("th0", Instant::now())?, PoolPlacement::Parked);

        let report = teardown_thread(
            &mut state,
            &governor,
            &pool,
            request(
                Disposition::ReleaseToPool,
                reserved(1000),
                consumed(1000),
                Instant::now(),
            ),
        )
        .await?;

        assert_eq!(report.placement, Placement::Destroyed);
        assert_ne!(
            pool.acquire(SubagentType {
                value: "w".to_string(),
            })
            .await?,
            "th1"
        );
        Ok(())
    }

    #[tokio::test]
    async fn a_refused_pool_step_changes_nothing() -> TestResult {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = finished(&governor, 1000).await?;
        let pool = test_pool(1)?;
        // A live thread already holds the id this teardown would park, so the pool step refuses first.
        pool.open_thread(
            "th1",
            SubagentType {
                value: "w".to_string(),
            },
        )?;
        let before = state.budget_snapshot;

        let refused = teardown_thread(
            &mut state,
            &governor,
            &pool,
            request(
                Disposition::ReleaseToPool,
                reserved(1000),
                consumed(1000),
                Instant::now(),
            ),
        )
        .await;

        assert!(
            matches!(refused, Err(TeardownError::Pool(_))),
            "{refused:?}"
        );
        assert!(
            state.threads.contains_key("th1"),
            "the session still holds its thread"
        );
        assert_eq!(state.budget_snapshot, before, "no refund was applied");
        Ok(())
    }

    #[tokio::test]
    async fn teardown_settles_the_reserved_budget_once() -> TestResult {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = finished(&governor, 1000).await?;
        // The bind charged 1000 of the 10 000-token workflow budget.
        assert_eq!(
            state.budget_snapshot.as_ref().map(|s| s.tokens_remaining),
            Some(9_000)
        );
        let pool = test_pool(4)?;
        let now = Instant::now();

        let report = teardown_thread(
            &mut state,
            &governor,
            &pool,
            request(Disposition::Destroy, reserved(1000), consumed(400), now),
        )
        .await?;

        assert_eq!(report.refunded.tokens, 600);
        assert!(!report.overrun);
        let snapshot = state.budget_snapshot.ok_or("snapshot missing")?;
        assert_eq!(snapshot.tokens_used, 400);
        assert_eq!(snapshot.tokens_remaining, 9_600);
        assert_eq!(governor.get_budget_snapshot("s1")?.tokens_used, 400);

        // The thread is gone, so a second teardown refuses and settles nothing.
        let again = teardown_thread(
            &mut state,
            &governor,
            &pool,
            request(Disposition::Destroy, reserved(1000), consumed(400), now),
        )
        .await;
        assert!(matches!(again, Err(TeardownError::UnknownThread(_))));
        assert_eq!(governor.get_budget_snapshot("s1")?.tokens_used, 400);
        Ok(())
    }

    #[tokio::test]
    async fn overrun_refunds_nothing_and_is_reported() -> TestResult {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = finished(&governor, 1000).await?;
        let pool = test_pool(4)?;

        let report = teardown_thread(
            &mut state,
            &governor,
            &pool,
            request(
                Disposition::Destroy,
                reserved(1000),
                consumed(1500),
                Instant::now(),
            ),
        )
        .await?;

        assert!(report.overrun);
        assert_eq!(report.refunded.tokens, 0);
        assert_eq!(governor.get_budget_snapshot("s1")?.tokens_used, 1000);
        Ok(())
    }

    #[tokio::test]
    async fn a_refund_above_the_held_reservation_reports_only_the_freed_amount() -> TestResult {
        let governor = Governor::new(GovernorConfig::default());
        // The governor holds 400 for (s1, th1); the caller claims 1000 was reserved at bind time.
        let mut state = finished(&governor, 400).await?;
        let pool = test_pool(4)?;

        let report = teardown_thread(
            &mut state,
            &governor,
            &pool,
            request(
                Disposition::Destroy,
                reserved(1000),
                consumed(0),
                Instant::now(),
            ),
        )
        .await?;

        assert_eq!(report.refunded.tokens, 400);
        assert!(!report.overrun);
        let snapshot = state.budget_snapshot.ok_or("snapshot missing")?;
        assert_eq!(snapshot.tokens_used, 0);
        assert_eq!(snapshot.tokens_remaining, 10_000);
        assert_eq!(governor.get_budget_snapshot("s1")?.tokens_used, 0);
        Ok(())
    }

    #[tokio::test]
    async fn live_thread_is_refused_and_nothing_settles() -> TestResult {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = bound_state_with(&governor, &[("th1", "t1")], 1000).await?;
        admit_thread(&mut state, "th1")?;
        handoff_thread(&mut state, "th1")?;
        let pool = test_pool(4)?;

        let refused = teardown_thread(
            &mut state,
            &governor,
            &pool,
            request(
                Disposition::ReleaseToPool,
                reserved(1000),
                consumed(0),
                Instant::now(),
            ),
        )
        .await;

        assert!(matches!(refused, Err(TeardownError::Live { .. })));
        assert!(state.threads.contains_key("th1"));
        assert_ne!(
            pool.acquire(SubagentType {
                value: "w".to_string(),
            })
            .await?,
            "th1"
        );
        assert_eq!(governor.get_budget_snapshot("s1")?.tokens_used, 1000);
        Ok(())
    }

    #[tokio::test]
    async fn a_governor_without_the_reservation_refuses_before_the_pool_step() -> TestResult {
        let governor_a = Governor::new(GovernorConfig::default());
        let mut state = finished(&governor_a, 1000).await?;
        // A governor that never reserved this pair cannot return its budget.
        let governor_b = Governor::new(GovernorConfig::default());
        let pool = test_pool(4)?;
        let before = state.budget_snapshot;

        let refused = teardown_thread(
            &mut state,
            &governor_b,
            &pool,
            request(
                Disposition::ReleaseToPool,
                reserved(1000),
                consumed(0),
                Instant::now(),
            ),
        )
        .await;
        assert!(
            matches!(
                refused,
                Err(TeardownError::Governor(GovernorError::UnknownReservation(
                    _
                )))
            ),
            "{refused:?}"
        );
        assert!(
            state.threads.contains_key("th1"),
            "the session still holds its thread"
        );
        assert_eq!(state.budget_snapshot, before, "no refund was applied");

        // The pool never parked the id, so a live thread can open it.
        pool.open_thread(
            "th1",
            SubagentType {
                value: "w".to_string(),
            },
        )?;

        // The check still refuses before the pool step, so a retry settles nothing either.
        let again = teardown_thread(
            &mut state,
            &governor_b,
            &pool,
            request(
                Disposition::ReleaseToPool,
                reserved(1000),
                consumed(0),
                Instant::now(),
            ),
        )
        .await;
        assert!(
            matches!(
                again,
                Err(TeardownError::Governor(GovernorError::UnknownReservation(
                    _
                )))
            ),
            "{again:?}"
        );
        Ok(())
    }

    #[tokio::test]
    async fn a_full_live_cap_does_not_block_teardown() -> TestResult {
        let governor = Governor::new(GovernorConfig::default());
        let mut state = finished(&governor, 1000).await?;
        let pool = ChatPool::new(
            ChatRuntimeConfig {
                max_threads: 1,
                ..ChatRuntimeConfig::default()
            },
            Arc::new(NoBudget),
        )?;
        // th1 is never opened in the pool, so it holds no live slot.
        // Opening th2 fills the one-thread live cap.
        pool.open_thread(
            "th2",
            SubagentType {
                value: "w".to_string(),
            },
        )?;
        let full = pool.open_thread(
            "th3",
            SubagentType {
                value: "w".to_string(),
            },
        );
        assert!(
            matches!(full, Err(gsd_pool::ChatError::Capacity { .. })),
            "{full:?}"
        );

        let report = teardown_thread(
            &mut state,
            &governor,
            &pool,
            request(
                Disposition::ReleaseToPool,
                reserved(1000),
                consumed(1000),
                Instant::now(),
            ),
        )
        .await?;

        assert_eq!(report.placement, Placement::ReleasedToPool);
        assert!(!state.threads.contains_key("th1"));
        // th2 is still live, so a fresh open of its id is refused.
        let still_live = pool.open_thread(
            "th2",
            SubagentType {
                value: "w".to_string(),
            },
        );
        assert!(
            matches!(still_live, Err(gsd_pool::ChatError::ThreadExists(_))),
            "{still_live:?}"
        );
        // Closing th2 frees the only slot. The next acquire takes th1 back from the
        // warm pool, which shows th1 was parked and not live.
        pool.close_thread("th2")?;
        let reused = pool
            .acquire(SubagentType {
                value: "w".to_string(),
            })
            .await?;
        assert_eq!(reused, "th1");
        Ok(())
    }

    #[tokio::test]
    async fn release_unused_of_a_pair_with_no_reservation_refunds_nothing() -> TestResult {
        let governor = Governor::new(GovernorConfig::default());
        let unused = Amounts {
            tokens: 500,
            wall_time_ms: 1000,
            tool_calls: 3,
            cost_usd: 0.25,
        };

        // The pair (s-none, th-none) holds no reservation, so the governor refuses the release as
        // UnknownReservation. release_unused maps that refusal to a zero refund, not an error.
        let result = release_unused(&governor, "s-none", "th-none", unused).await;
        assert!(result.is_ok(), "{result:?}");
        let freed = result?;
        assert_eq!(freed.tokens, 0);
        assert_eq!(freed.wall_time_ms, 0);
        assert_eq!(freed.tool_calls, 0);
        assert_eq!(freed.cost_usd, 0.0);
        Ok(())
    }
}
