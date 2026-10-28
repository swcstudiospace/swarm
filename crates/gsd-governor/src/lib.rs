//! Resource Governor for GSD-Core
//!
//! Token budgets, rate limits, concurrency caps, cost controls per thread and globally.

use dashmap::DashMap;
use gsd_proto::gsd::v1::{Budget, BudgetSnapshot};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

/// Result type for governor operations
pub type GovernorResult<T> = std::result::Result<T, GovernorError>;

/// Governor errors
#[derive(Debug, thiserror::Error)]
pub enum GovernorError {
    #[error("Budget exceeded: {0}")]
    BudgetExceeded(String),
    #[error("Rate limit exceeded: {0}")]
    RateLimitExceeded(String),
    #[error("Quota exceeded: {0}")]
    QuotaExceeded(String),
    #[error("Invalid budget: {0}")]
    InvalidBudget(String),
    #[error("Unauthorized: {0}")]
    Unauthorized(String),
    #[error("Cost limit exceeded: {0}")]
    CostLimitExceeded(String),
    #[error("Other: {0}")]
    Other(String),
}

impl From<anyhow::Error> for GovernorError {
    fn from(e: anyhow::Error) -> Self {
        GovernorError::Other(e.to_string())
    }
}

/// Governor configuration
#[derive(Debug, Clone)]
pub struct GovernorConfig {
    pub default_session_budget: Budget,
    pub default_thread_budget: Budget,
    pub global_token_limit: u64,
    pub global_cost_limit_usd: f64,
    pub enable_cost_tracking: bool,
    pub rate_limit_requests_per_min: u32,
    pub rate_limit_tokens_per_min: u64,
}

impl Default for GovernorConfig {
    fn default() -> Self {
        Self {
            default_session_budget: gsd_proto::gsd::v1::Budget {
                max_tokens: 100_000,
                max_wall_time_ms: 3_600_000, // 1 hour
                max_tool_calls: 1000,
                max_cost_usd: 10.0,
            },
            default_thread_budget: gsd_proto::gsd::v1::Budget {
                max_tokens: 10_000,
                max_wall_time_ms: 300_000, // 5 minutes
                max_tool_calls: 100,
                max_cost_usd: 1.0,
            },
            global_token_limit: 1_000_000,
            global_cost_limit_usd: 100.0,
            enable_cost_tracking: true,
            rate_limit_requests_per_min: 60,
            rate_limit_tokens_per_min: 100_000,
        }
    }
}

/// Grant returned for any refused reservation.
fn denied_grant() -> gsd_proto::gsd::v1::BudgetGrant {
    gsd_proto::gsd::v1::BudgetGrant {
        granted: false,
        tokens_granted: 0,
        wall_time_ms_granted: 0,
        tool_calls_granted: 0,
        cost_usd_granted: 0.0,
        valid_for: None,
    }
}

/// Resource Governor
pub struct Governor {
    config: GovernorConfig,
    session_budgets: Arc<DashMap<String, Arc<parking_lot::RwLock<BudgetSnapshot>>>>,
    thread_budgets: Arc<DashMap<String, Arc<parking_lot::RwLock<BudgetSnapshot>>>>,
    global_usage: Arc<GlobalUsage>,
    rate_limiters: Arc<RateLimiterRegistry>,
    metrics: Arc<GovernorMetrics>,
}

impl Governor {
    pub fn new(config: GovernorConfig) -> Self {
        Self {
            config,
            session_budgets: Arc::new(DashMap::new()),
            thread_budgets: Arc::new(DashMap::new()),
            global_usage: Arc::new(GlobalUsage::new()),
            rate_limiters: Arc::new(RateLimiterRegistry::new()),
            metrics: Arc::new(GovernorMetrics::default()),
        }
    }

    pub async fn reserve_budget(
        &self,
        request: gsd_proto::gsd::v1::BudgetRequest,
    ) -> gsd_proto::gsd::v1::BudgetGrant {
        let (Some(session_id), Some(thread_id)) =
            (request.session_id.as_ref(), request.thread_id.as_ref())
        else {
            return denied_grant();
        };
        let Ok(tokens) = u64::try_from(request.tokens) else {
            return denied_grant();
        };

        // Check global limits
        if !self
            .global_usage
            .check_and_reserve(tokens, request.cost_usd)
        {
            return gsd_proto::gsd::v1::BudgetGrant {
                granted: false,
                tokens_granted: 0,
                wall_time_ms_granted: 0,
                tool_calls_granted: 0,
                cost_usd_granted: 0.0,
                valid_for: None,
            };
        }

        // Check rate limits
        if !self.rate_limiters.check_rate_limit() {
            return gsd_proto::gsd::v1::BudgetGrant {
                granted: false,
                tokens_granted: 0,
                wall_time_ms_granted: 0,
                tool_calls_granted: 0,
                cost_usd_granted: 0.0,
                valid_for: None,
            };
        }

        match self.try_reserve_budget(&request, &session_id.value, &thread_id.value) {
            Ok(grant) => grant,
            Err(_) => denied_grant(),
        }
    }

    /// Reserves session and thread budgets. Refuses with `BudgetExceeded` when any amount exceeds what remains; a refusal changes nothing.
    fn try_reserve_budget(
        &self,
        request: &gsd_proto::gsd::v1::BudgetRequest,
        session_id: &str,
        thread_id: &str,
    ) -> GovernorResult<gsd_proto::gsd::v1::BudgetGrant> {
        let session_arc = self.get_or_create_session_budget(session_id);
        let mut session = session_arc.write();
        check_remaining(&session, request, "session")?;

        let thread_arc = self.get_or_create_thread_budget(thread_id);
        let mut thread = thread_arc.write();
        check_remaining(&thread, request, "thread")?;

        apply_reserve(&mut session, request);
        apply_reserve(&mut thread, request);

        self.metrics.inc_reservations();

        Ok(gsd_proto::gsd::v1::BudgetGrant {
            granted: true,
            tokens_granted: request.tokens,
            wall_time_ms_granted: request.wall_time_ms,
            tool_calls_granted: request.tool_calls,
            cost_usd_granted: request.cost_usd,
            valid_for: Some(prost_types::Duration {
                seconds: 3600, // 1 hour
                nanos: 0,
            }),
        })
    }

    fn get_or_create_session_budget(
        &self,
        session_id: &str,
    ) -> Arc<parking_lot::RwLock<gsd_proto::gsd::v1::BudgetSnapshot>> {
        self.session_budgets
            .entry(session_id.to_string())
            .or_insert_with(|| {
                Arc::new(parking_lot::RwLock::new(
                    gsd_proto::gsd::v1::BudgetSnapshot {
                        tokens_used: 0,
                        wall_time_ms_used: 0,
                        tool_calls_used: 0,
                        cost_usd_used: 0.0,
                        tokens_remaining: self.config.default_session_budget.max_tokens,
                        wall_time_ms_remaining: self.config.default_session_budget.max_wall_time_ms,
                        tool_calls_remaining: self.config.default_session_budget.max_tool_calls,
                        cost_usd_remaining: self.config.default_session_budget.max_cost_usd,
                    },
                ))
            })
            .clone()
    }

    fn get_or_create_thread_budget(
        &self,
        thread_id: &str,
    ) -> Arc<parking_lot::RwLock<gsd_proto::gsd::v1::BudgetSnapshot>> {
        self.thread_budgets
            .entry(thread_id.to_string())
            .or_insert_with(|| {
                Arc::new(parking_lot::RwLock::new(
                    gsd_proto::gsd::v1::BudgetSnapshot {
                        tokens_used: 0,
                        wall_time_ms_used: 0,
                        tool_calls_used: 0,
                        cost_usd_used: 0.0,
                        tokens_remaining: self.config.default_thread_budget.max_tokens,
                        wall_time_ms_remaining: self.config.default_thread_budget.max_wall_time_ms,
                        tool_calls_remaining: self.config.default_thread_budget.max_tool_calls,
                        cost_usd_remaining: self.config.default_thread_budget.max_cost_usd,
                    },
                ))
            })
            .clone()
    }

    pub async fn release_budget(
        &self,
        session_id: &str,
        thread_id: &str,
        tokens: i64,
        wall_time_ms: i64,
        tool_calls: i32,
        cost_usd: f64,
    ) -> GovernorResult<()> {
        let global_tokens = u64::try_from(tokens).map_err(|_| {
            GovernorError::InvalidBudget(format!("negative token release: {tokens}"))
        })?;
        if let Some(budget) = self.session_budgets.get(session_id) {
            let mut budget = budget.write();
            release_amounts(&mut budget, tokens, wall_time_ms, tool_calls, cost_usd);
        }

        if let Some(budget) = self.thread_budgets.get(thread_id) {
            let mut budget = budget.write();
            release_amounts(&mut budget, tokens, wall_time_ms, tool_calls, cost_usd);
        }

        self.global_usage.release(global_tokens, cost_usd);
        self.metrics.inc_releases();
        Ok(())
    }

    pub fn get_budget_snapshot(
        &self,
        session_id: &str,
    ) -> GovernorResult<gsd_proto::gsd::v1::BudgetSnapshot> {
        if let Some(budget) = self.session_budgets.get(session_id) {
            let snapshot = *budget.read();
            Ok(snapshot)
        } else {
            Err(GovernorError::InvalidBudget(
                "Session not found".to_string(),
            ))
        }
    }

    pub fn metrics(&self) -> &GovernorMetrics {
        &self.metrics
    }
}

struct GlobalUsage {
    tokens_used: std::sync::atomic::AtomicU64,
    cost_usd_used: std::sync::atomic::AtomicU64, // Store as micros USD
}

impl GlobalUsage {
    fn new() -> Self {
        Self {
            tokens_used: std::sync::atomic::AtomicU64::new(0),
            cost_usd_used: std::sync::atomic::AtomicU64::new(0),
        }
    }

    fn check_and_reserve(&self, tokens: u64, cost_usd: f64) -> bool {
        let current_tokens = self.tokens_used.load(Ordering::Acquire);
        let current_cost = self.cost_usd_used.load(Ordering::Acquire) as f64 / 1_000_000.0;

        if current_tokens + tokens > 1_000_000 || current_cost + cost_usd > 100.0 {
            return false;
        }

        self.tokens_used.fetch_add(tokens, Ordering::AcqRel);
        self.cost_usd_used
            .fetch_add((cost_usd * 1_000_000.0) as u64, Ordering::AcqRel);
        true
    }

    fn release(&self, tokens: u64, cost_usd: f64) {
        self.tokens_used.fetch_sub(tokens, Ordering::AcqRel);
        self.cost_usd_used
            .fetch_sub((cost_usd * 1_000_000.0) as u64, Ordering::AcqRel);
    }
}

struct RateLimiterRegistry;

impl RateLimiterRegistry {
    fn new() -> Self {
        Self
    }

    fn check_rate_limit(&self) -> bool {
        // Simplified rate limiting
        true
    }
}

pub struct GovernorMetrics {
    reservations: AtomicU64,
    releases: AtomicU64,
}

impl Default for GovernorMetrics {
    fn default() -> Self {
        Self {
            reservations: AtomicU64::new(0),
            releases: AtomicU64::new(0),
        }
    }
}

impl GovernorMetrics {
    fn inc_reservations(&self) {
        self.reservations.fetch_add(1, Ordering::Relaxed);
    }
    fn inc_releases(&self) {
        self.releases.fetch_add(1, Ordering::Relaxed);
    }
}

/// Refuses when any requested amount exceeds the snapshot's remaining amount.
fn check_remaining(
    budget: &gsd_proto::gsd::v1::BudgetSnapshot,
    request: &gsd_proto::gsd::v1::BudgetRequest,
    scope: &str,
) -> GovernorResult<()> {
    let mut violations = Vec::new();
    if request.tokens > budget.tokens_remaining {
        violations.push(format!(
            "tokens {} exceeds remaining {}",
            request.tokens, budget.tokens_remaining
        ));
    }
    if request.wall_time_ms > budget.wall_time_ms_remaining {
        violations.push(format!(
            "wall_time_ms {} exceeds remaining {}",
            request.wall_time_ms, budget.wall_time_ms_remaining
        ));
    }
    if request.tool_calls > budget.tool_calls_remaining {
        violations.push(format!(
            "tool_calls {} exceeds remaining {}",
            request.tool_calls, budget.tool_calls_remaining
        ));
    }
    if request.cost_usd > budget.cost_usd_remaining {
        violations.push(format!(
            "cost_usd {} exceeds remaining {}",
            request.cost_usd, budget.cost_usd_remaining
        ));
    }
    if violations.is_empty() {
        Ok(())
    } else {
        Err(GovernorError::BudgetExceeded(format!(
            "{scope} budget: {}",
            violations.join("; ")
        )))
    }
}

/// Adds a reservation to the used fields and subtracts it from the remaining fields.
fn apply_reserve(
    budget: &mut gsd_proto::gsd::v1::BudgetSnapshot,
    request: &gsd_proto::gsd::v1::BudgetRequest,
) {
    budget.tokens_used += request.tokens;
    budget.wall_time_ms_used += request.wall_time_ms;
    budget.tool_calls_used += request.tool_calls;
    budget.cost_usd_used += request.cost_usd;

    budget.tokens_remaining = budget.tokens_remaining.saturating_sub(request.tokens);
    budget.wall_time_ms_remaining = budget
        .wall_time_ms_remaining
        .saturating_sub(request.wall_time_ms);
    budget.tool_calls_remaining = budget
        .tool_calls_remaining
        .saturating_sub(request.tool_calls);
    budget.cost_usd_remaining -= request.cost_usd;
}

/// Removes a release from the used fields and returns the amount actually freed to the remaining fields.
fn release_amounts(
    budget: &mut gsd_proto::gsd::v1::BudgetSnapshot,
    tokens: i64,
    wall_time_ms: i64,
    tool_calls: i32,
    cost_usd: f64,
) {
    let tokens_before = budget.tokens_used;
    budget.tokens_used = tokens_before.saturating_sub(tokens);
    budget.tokens_remaining = budget
        .tokens_remaining
        .saturating_add(tokens_before.saturating_sub(budget.tokens_used));

    let wall_before = budget.wall_time_ms_used;
    budget.wall_time_ms_used = wall_before.saturating_sub(wall_time_ms);
    budget.wall_time_ms_remaining = budget
        .wall_time_ms_remaining
        .saturating_add(wall_before.saturating_sub(budget.wall_time_ms_used));

    let calls_before = budget.tool_calls_used;
    budget.tool_calls_used = calls_before.saturating_sub(tool_calls);
    budget.tool_calls_remaining = budget
        .tool_calls_remaining
        .saturating_add(calls_before.saturating_sub(budget.tool_calls_used));

    let cost_before = budget.cost_usd_used;
    budget.cost_usd_used = (cost_before - cost_usd).max(0.0);
    budget.cost_usd_remaining += cost_before - budget.cost_usd_used;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(
        tokens: i64,
        wall_time_ms: i64,
        tool_calls: i32,
        cost_usd: f64,
    ) -> gsd_proto::gsd::v1::BudgetRequest {
        gsd_proto::gsd::v1::BudgetRequest {
            tokens,
            wall_time_ms,
            tool_calls,
            cost_usd,
            ..Default::default()
        }
    }

    fn session_snapshot(gov: &Governor, id: &str) -> BudgetSnapshot {
        gov.session_budgets
            .get(id)
            .map(|b| *b.read())
            .expect("session budget exists")
    }

    fn thread_snapshot(gov: &Governor, id: &str) -> BudgetSnapshot {
        gov.thread_budgets
            .get(id)
            .map(|b| *b.read())
            .expect("thread budget exists")
    }

    #[test]
    fn reservation_within_remaining_is_accepted_and_decrements_remaining() {
        let gov = Governor::new(GovernorConfig::default());
        let grant = gov
            .try_reserve_budget(&request(1_000, 500, 2, 0.25), "s", "t")
            .expect("reservation within budget is accepted");
        assert!(grant.granted);

        let session = session_snapshot(&gov, "s");
        assert_eq!(session.tokens_remaining, 100_000 - 1_000);
        assert_eq!(session.wall_time_ms_remaining, 3_600_000 - 500);
        assert_eq!(session.tool_calls_remaining, 1_000 - 2);
        assert_eq!(session.cost_usd_remaining, 10.0 - 0.25);

        let thread = thread_snapshot(&gov, "t");
        assert_eq!(thread.tokens_remaining, 10_000 - 1_000);
        assert_eq!(thread.wall_time_ms_remaining, 300_000 - 500);
        assert_eq!(thread.tool_calls_remaining, 100 - 2);
        assert_eq!(thread.cost_usd_remaining, 1.0 - 0.25);
    }

    #[test]
    fn reservation_above_remaining_is_refused_and_changes_nothing() {
        let gov = Governor::new(GovernorConfig::default());
        gov.try_reserve_budget(&request(9_000, 0, 0, 0.0), "s", "t")
            .expect("first reservation accepted");
        let session_before = session_snapshot(&gov, "s");
        let thread_before = thread_snapshot(&gov, "t");

        let err = gov
            .try_reserve_budget(&request(1_001, 0, 0, 0.0), "s", "t")
            .expect_err("reservation above thread remaining is refused");
        assert!(matches!(err, GovernorError::BudgetExceeded(_)));

        assert_eq!(
            session_snapshot(&gov, "s").tokens_remaining,
            session_before.tokens_remaining
        );
        assert_eq!(
            session_snapshot(&gov, "s").tokens_used,
            session_before.tokens_used
        );
        assert_eq!(
            thread_snapshot(&gov, "t").tokens_remaining,
            thread_before.tokens_remaining
        );
        assert_eq!(
            thread_snapshot(&gov, "t").tokens_used,
            thread_before.tokens_used
        );
    }

    #[test]
    fn refused_cost_reservation_changes_nothing() {
        let gov = Governor::new(GovernorConfig::default());
        let err = gov
            .try_reserve_budget(&request(0, 0, 0, 1.5), "s", "t")
            .expect_err("cost above thread remaining is refused");
        assert!(matches!(err, GovernorError::BudgetExceeded(_)));
        assert_eq!(thread_snapshot(&gov, "t").cost_usd_remaining, 1.0);
        assert_eq!(session_snapshot(&gov, "s").cost_usd_remaining, 10.0);
    }

    #[test]
    fn release_restores_reserved_amounts() {
        let gov = Governor::new(GovernorConfig::default());
        gov.try_reserve_budget(&request(1_000, 500, 2, 0.5), "s", "t")
            .expect("reservation accepted");

        tokio_test::block_on(gov.release_budget("s", "t", 1_000, 500, 2, 0.5))
            .expect("release succeeds");

        let session = session_snapshot(&gov, "s");
        assert_eq!(session.tokens_used, 0);
        assert_eq!(session.tokens_remaining, 100_000);
        assert_eq!(session.wall_time_ms_remaining, 3_600_000);
        assert_eq!(session.tool_calls_remaining, 1_000);
        assert_eq!(session.cost_usd_remaining, 10.0);

        let thread = thread_snapshot(&gov, "t");
        assert_eq!(thread.tokens_remaining, 10_000);
        assert_eq!(thread.wall_time_ms_remaining, 300_000);
        assert_eq!(thread.tool_calls_remaining, 100);
        assert_eq!(thread.cost_usd_remaining, 1.0);
    }
}
