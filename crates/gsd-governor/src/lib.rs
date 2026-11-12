//! Resource Governor for GSD-Core
//!
//! Token budgets, rate limits, concurrency caps, cost controls per thread and globally.

use dashmap::DashMap;
use gsd_proto::gsd::v1::{Budget, BudgetSnapshot};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

/// Result type for governor operations
pub type GovernorResult<T> = std::result::Result<T, GovernorError>;

/// Largest accepted session, thread or pair id, in bytes. `gsd-pool` caps its ids at the same size: keep them equal.
pub const MAX_ID_BYTES: usize = 256;

/// Refuses a session id, or a thread id, over [`MAX_ID_BYTES`] with `InvalidBudget` (E-INPUT).
fn validate_ids(session_id: &str, thread_id: Option<&str>) -> GovernorResult<()> {
    let over = |what: &str, len: usize| {
        GovernorError::InvalidBudget(format!(
            "{what} is {len} bytes; the limit is {MAX_ID_BYTES}"
        ))
    };
    if session_id.len() > MAX_ID_BYTES {
        return Err(over("session id", session_id.len()));
    }
    if let Some(thread_id) = thread_id {
        if thread_id.len() > MAX_ID_BYTES {
            return Err(over("thread id", thread_id.len()));
        }
    }
    Ok(())
}

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
    #[error("Unknown reservation: {0}")]
    UnknownReservation(String),
    #[error("Other: {0}")]
    Other(String),
}

fn unknown_reservation(session_id: &str, thread_id: &str) -> GovernorError {
    GovernorError::UnknownReservation(format!("session {session_id}, thread {thread_id}"))
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

/// Amounts a release freed: the clamped amounts actually returned to the session/thread pair.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ReleasedAmounts {
    /// Tokens returned.
    pub tokens: i64,
    /// Wall-clock milliseconds returned.
    pub wall_time_ms: i64,
    /// Tool calls returned.
    pub tool_calls: i32,
    /// Cost in US dollars returned.
    pub cost_usd: f64,
}

/// Resource Governor
pub struct Governor {
    config: GovernorConfig,
    session_budgets: Arc<DashMap<String, Arc<parking_lot::RwLock<BudgetSnapshot>>>>,
    thread_budgets: Arc<DashMap<String, Arc<parking_lot::RwLock<BudgetSnapshot>>>>,
    global_usage: Arc<GlobalUsage>,
    rate_limiters: Arc<RateLimiterRegistry>,
    metrics: Arc<GovernorMetrics>,
    /// Serializes reservations, so a missing budget entry is created by exactly one granted reservation.
    reserve_gate: parking_lot::Mutex<()>,
}

impl Governor {
    pub fn new(config: GovernorConfig) -> Self {
        let global_usage =
            GlobalUsage::new(config.global_token_limit, config.global_cost_limit_usd);
        Self {
            config,
            session_budgets: Arc::new(DashMap::new()),
            thread_budgets: Arc::new(DashMap::new()),
            global_usage: Arc::new(global_usage),
            rate_limiters: Arc::new(RateLimiterRegistry::new()),
            metrics: Arc::new(GovernorMetrics::default()),
            reserve_gate: parking_lot::Mutex::new(()),
        }
    }

    /// Reserves budget for `request` and reports the reason for any refusal.
    ///
    /// Refuses with `InvalidBudget` when the session or thread id is missing, or when an amount
    /// is negative or non-finite. Refuses with `RateLimitExceeded` when the rate limiter refuses,
    /// and with `BudgetExceeded` when an amount exceeds what the session, thread or global limit leaves. A refusal changes no budget.
    pub async fn reserve_budget_checked(
        &self,
        request: gsd_proto::gsd::v1::BudgetRequest,
    ) -> GovernorResult<gsd_proto::gsd::v1::BudgetGrant> {
        let (Some(session_id), Some(thread_id)) =
            (request.session_id.as_ref(), request.thread_id.as_ref())
        else {
            return Err(GovernorError::InvalidBudget(
                "session_id and thread_id are required".to_string(),
            ));
        };
        validate_ids(&session_id.value, Some(&thread_id.value))?;
        // Check rate limits
        if !self.rate_limiters.check_rate_limit() {
            return Err(GovernorError::RateLimitExceeded(
                "request rate limit reached".to_string(),
            ));
        }

        self.try_reserve_budget(&request, &session_id.value, &thread_id.value)
    }

    /// Wire-level reservation: returns `granted == false` for every refusal, with zero amounts.
    ///
    /// Use `reserve_budget_checked` when the caller needs the reason for a refusal.
    #[must_use]
    pub async fn reserve_budget(
        &self,
        request: gsd_proto::gsd::v1::BudgetRequest,
    ) -> gsd_proto::gsd::v1::BudgetGrant {
        self.reserve_budget_checked(request)
            .await
            .unwrap_or_else(|_| denied_grant())
    }

    /// Reserves session, thread and global budgets. Refuses with `InvalidBudget` for negative or non-finite amounts and with `BudgetExceeded` when an amount exceeds what the session, thread or global limit leaves; a refusal changes nothing, and a session or thread entry is created only by a granted reservation.
    fn try_reserve_budget(
        &self,
        request: &gsd_proto::gsd::v1::BudgetRequest,
        session_id: &str,
        thread_id: &str,
    ) -> GovernorResult<gsd_proto::gsd::v1::BudgetGrant> {
        let tokens = validate_request(request)?;
        // The gate keeps another reservation from creating the same missing entry between this
        // lookup and the insert after the grant.
        let _gate = self.reserve_gate.lock();
        let session_arc = budget_entry(
            &self.session_budgets,
            session_id,
            &self.config.default_session_budget,
        );
        let thread_arc = budget_entry(
            &self.thread_budgets,
            thread_id,
            &self.config.default_thread_budget,
        );
        {
            let mut session = session_arc.write();
            let mut thread = thread_arc.write();
            check_remaining(&session, request, "session")?;
            check_remaining(&thread, request, "thread")?;

            // Global check and add run under the local locks, so a refusal at any level changes nothing.
            if !self
                .global_usage
                .try_reserve(request, session_id, thread_id, tokens)
            {
                return Err(GovernorError::BudgetExceeded(
                    "global token or cost cap reached".to_string(),
                ));
            }

            apply_reserve(&mut session, request);
            apply_reserve(&mut thread, request);
        }
        // The grant is final, so record the entries. `or_insert` keeps an entry that already exists.
        self.session_budgets
            .entry(session_id.to_string())
            .or_insert(session_arc);
        self.thread_budgets
            .entry(thread_id.to_string())
            .or_insert(thread_arc);

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

    pub async fn release_budget(
        &self,
        session_id: &str,
        thread_id: &str,
        tokens: i64,
        wall_time_ms: i64,
        tool_calls: i32,
        cost_usd: f64,
    ) -> GovernorResult<()> {
        self.release_freed(
            session_id,
            thread_id,
            tokens,
            wall_time_ms,
            tool_calls,
            cost_usd,
        )
        .await
        .map(|_| ())
    }

    /// Returns reserved capacity to the (session, thread) pair and reports the amounts it actually
    /// freed. A request above what the pair holds frees only what the pair holds.
    pub async fn release_freed(
        &self,
        session_id: &str,
        thread_id: &str,
        tokens: i64,
        wall_time_ms: i64,
        tool_calls: i32,
        cost_usd: f64,
    ) -> GovernorResult<ReleasedAmounts> {
        validate_ids(session_id, Some(thread_id))?;
        validate_amounts(tokens, wall_time_ms, tool_calls, cost_usd)?;

        // Match both ids before touching either aggregate. A session or thread alone does not
        // identify which reservation is being returned. A release that matches no reservation is
        // refused and changes nothing, so a refund that was never applied is never reported as one.
        let session_budget = self
            .session_budgets
            .get(session_id)
            .map(|budget| Arc::clone(&budget))
            .ok_or_else(|| unknown_reservation(session_id, thread_id))?;
        let thread_budget = self
            .thread_budgets
            .get(thread_id)
            .map(|budget| Arc::clone(&budget))
            .ok_or_else(|| unknown_reservation(session_id, thread_id))?;
        // Keep the reservation lock order: session, thread, then global accounting.
        let mut session = session_budget.write();
        let mut thread = thread_budget.write();
        let requested = ReservedAmounts {
            tokens,
            wall_time_ms,
            tool_calls,
            cost_nanos: usd_to_nanos(cost_usd),
            cost_micros: 0,
        };
        let freed = self
            .global_usage
            .release(session_id, thread_id, &requested)
            .ok_or_else(|| unknown_reservation(session_id, thread_id))?;
        let freed_cost_usd = nanos_to_usd(freed.cost_nanos);
        release_amounts(
            &mut session,
            freed.tokens,
            freed.wall_time_ms,
            freed.tool_calls,
            freed_cost_usd,
        );
        release_amounts(
            &mut thread,
            freed.tokens,
            freed.wall_time_ms,
            freed.tool_calls,
            freed_cost_usd,
        );
        self.metrics.inc_releases();
        Ok(ReleasedAmounts {
            tokens: freed.tokens,
            wall_time_ms: freed.wall_time_ms,
            tool_calls: freed.tool_calls,
            cost_usd: freed_cost_usd,
        })
    }

    /// Checks that `release_budget` would accept this release, and changes nothing.
    ///
    /// Refuses with `InvalidBudget` for a negative or non-finite amount, and with
    /// `UnknownReservation` when the (session, thread) pair holds no reservation: the refusal
    /// `release_budget` gives for that pair. A release that passes can still be refused if a
    /// concurrent release drains the pair first.
    pub fn check_release(
        &self,
        session_id: &str,
        thread_id: &str,
        tokens: i64,
        wall_time_ms: i64,
        tool_calls: i32,
        cost_usd: f64,
    ) -> GovernorResult<()> {
        validate_ids(session_id, Some(thread_id))?;
        validate_amounts(tokens, wall_time_ms, tool_calls, cost_usd)?;
        if self.global_usage.holds(session_id, thread_id) {
            Ok(())
        } else {
            Err(unknown_reservation(session_id, thread_id))
        }
    }

    /// Returns the snapshot for a session that holds a budget. Refuses with `InvalidBudget`, naming the session, when none exists.
    pub fn get_budget_snapshot(
        &self,
        session_id: &str,
    ) -> GovernorResult<gsd_proto::gsd::v1::BudgetSnapshot> {
        validate_ids(session_id, None)?;
        if let Some(budget) = self.session_budgets.get(session_id) {
            let snapshot = *budget.read();
            Ok(snapshot)
        } else {
            Err(GovernorError::InvalidBudget(format!(
                "Session not found: {session_id}"
            )))
        }
    }

    pub fn metrics(&self) -> &GovernorMetrics {
        &self.metrics
    }
}

/// Tokens and cost reserved across every session and thread.
struct GlobalUsage {
    totals: parking_lot::Mutex<GlobalTotals>,
    /// Limits from `GovernorConfig`, applied by `try_reserve`.
    token_limit: u64,
    cost_limit_usd: f64,
}

#[derive(Default)]
struct GlobalTotals {
    tokens_used: u64,
    cost_micros_used: u64,
    reservations: HashMap<String, HashMap<String, ReservedAmounts>>,
}

/// Outstanding amounts for one session/thread pair. Cost is held as whole nanodollars so that
/// sums and releases are exact, alongside the micros actually charged to the global total.
#[derive(Default)]
struct ReservedAmounts {
    tokens: i64,
    wall_time_ms: i64,
    tool_calls: i32,
    cost_nanos: u64,
    cost_micros: u64,
}

impl GlobalUsage {
    fn new(token_limit: u64, cost_limit_usd: f64) -> Self {
        Self {
            totals: parking_lot::Mutex::new(GlobalTotals::default()),
            token_limit,
            cost_limit_usd,
        }
    }

    /// Checks the global limits and adds the reservation under one lock, so concurrent callers cannot all pass the check.
    fn try_reserve(
        &self,
        request: &gsd_proto::gsd::v1::BudgetRequest,
        session_id: &str,
        thread_id: &str,
        tokens: u64,
    ) -> bool {
        let mut totals = self.totals.lock();
        let current_cost = micros_to_usd(totals.cost_micros_used);
        if totals.tokens_used + tokens > self.token_limit
            || current_cost + request.cost_usd > self.cost_limit_usd
        {
            return false;
        }
        let cost_micros = usd_to_micros(request.cost_usd);
        totals.tokens_used += tokens;
        totals.cost_micros_used += cost_micros;

        // Allocate keys only for a newly seen pair, not on every reservation.
        let threads = match totals.reservations.get_mut(session_id) {
            Some(threads) => threads,
            None => totals
                .reservations
                .entry(session_id.to_owned())
                .or_default(),
        };
        let reserved = match threads.get_mut(thread_id) {
            Some(reserved) => reserved,
            None => threads.entry(thread_id.to_owned()).or_default(),
        };
        reserved.tokens += request.tokens;
        reserved.wall_time_ms += request.wall_time_ms;
        reserved.tool_calls += request.tool_calls;
        reserved.cost_nanos += usd_to_nanos(request.cost_usd);
        reserved.cost_micros += cost_micros;
        true
    }

    fn release(
        &self,
        session_id: &str,
        thread_id: &str,
        requested: &ReservedAmounts,
    ) -> Option<ReservedAmounts> {
        let mut totals = self.totals.lock();
        let threads = totals.reservations.get_mut(session_id)?;
        let reserved = threads.get_mut(thread_id)?;
        let mut freed = ReservedAmounts {
            tokens: requested.tokens.min(reserved.tokens),
            wall_time_ms: requested.wall_time_ms.min(reserved.wall_time_ms),
            tool_calls: requested.tool_calls.min(reserved.tool_calls),
            cost_nanos: requested.cost_nanos.min(reserved.cost_nanos),
            cost_micros: 0,
        };
        reserved.tokens -= freed.tokens;
        reserved.wall_time_ms -= freed.wall_time_ms;
        reserved.tool_calls -= freed.tool_calls;
        reserved.cost_nanos -= freed.cost_nanos;
        // A full return refunds exactly the micros charged to this pair, even when separate
        // sub-micro reservations rounded differently from their aggregate USD amount.
        freed.cost_micros = if reserved.cost_nanos == 0 {
            reserved.cost_micros
        } else {
            nanos_to_micros(freed.cost_nanos).min(reserved.cost_micros)
        };
        reserved.cost_micros -= freed.cost_micros;
        if reserved.tokens == 0
            && reserved.wall_time_ms == 0
            && reserved.tool_calls == 0
            && reserved.cost_nanos == 0
            && reserved.cost_micros == 0
        {
            threads.remove(thread_id);
        }
        if threads.is_empty() {
            totals.reservations.remove(session_id);
        }
        totals.tokens_used -= freed.tokens as u64;
        totals.cost_micros_used -= freed.cost_micros;
        Some(freed)
    }

    /// Reports whether the (session, thread) pair holds a reservation. Reads under the lock and
    /// inserts nothing.
    fn holds(&self, session_id: &str, thread_id: &str) -> bool {
        let totals = self.totals.lock();
        totals
            .reservations
            .get(session_id)
            .and_then(|threads| threads.get(thread_id))
            .is_some()
    }
}

fn usd_to_micros(cost_usd: f64) -> u64 {
    (cost_usd * 1_000_000.0).round() as u64
}

fn micros_to_usd(micros: u64) -> f64 {
    micros as f64 / 1_000_000.0
}

/// Converts a USD amount to whole nanodollars. Callers validate the amount first, so it is finite and not negative.
fn usd_to_nanos(cost_usd: f64) -> u64 {
    (cost_usd * 1_000_000_000.0).round() as u64
}

fn nanos_to_usd(nanos: u64) -> f64 {
    nanos as f64 / 1_000_000_000.0
}

/// Rounds nanodollars to micros, with halves rounding up.
fn nanos_to_micros(nanos: u64) -> u64 {
    nanos / 1_000 + u64::from(nanos % 1_000 >= 500)
}

/// A budget with nothing used and every configured limit still remaining.
fn fresh_snapshot(limits: &Budget) -> BudgetSnapshot {
    BudgetSnapshot {
        tokens_used: 0,
        wall_time_ms_used: 0,
        tool_calls_used: 0,
        cost_usd_used: 0.0,
        tokens_remaining: limits.max_tokens,
        wall_time_ms_remaining: limits.max_wall_time_ms,
        tool_calls_remaining: limits.max_tool_calls,
        cost_usd_remaining: limits.max_cost_usd,
    }
}

/// Returns the budget registered under `id`, or a fresh one from `limits` for the caller to insert once its grant succeeds.
fn budget_entry(
    budgets: &DashMap<String, Arc<parking_lot::RwLock<BudgetSnapshot>>>,
    id: &str,
    limits: &Budget,
) -> Arc<parking_lot::RwLock<BudgetSnapshot>> {
    match budgets.get(id) {
        Some(existing) => Arc::clone(&existing),
        None => Arc::new(parking_lot::RwLock::new(fresh_snapshot(limits))),
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

/// Checks every reservation or release amount. Negative or non-finite values would mint or corrupt budget.
fn validate_amounts(
    tokens: i64,
    wall_time_ms: i64,
    tool_calls: i32,
    cost_usd: f64,
) -> GovernorResult<u64> {
    let tokens_checked = u64::try_from(tokens)
        .map_err(|_| GovernorError::InvalidBudget(format!("negative token amount: {tokens}")))?;
    if wall_time_ms < 0 {
        return Err(GovernorError::InvalidBudget(format!(
            "negative wall_time_ms amount: {wall_time_ms}"
        )));
    }
    if tool_calls < 0 {
        return Err(GovernorError::InvalidBudget(format!(
            "negative tool_calls amount: {tool_calls}"
        )));
    }
    if !cost_usd.is_finite() || cost_usd < 0.0 {
        return Err(GovernorError::InvalidBudget(format!(
            "invalid cost_usd amount: {cost_usd}"
        )));
    }
    Ok(tokens_checked)
}

fn validate_request(request: &gsd_proto::gsd::v1::BudgetRequest) -> GovernorResult<u64> {
    validate_amounts(
        request.tokens,
        request.wall_time_ms,
        request.tool_calls,
        request.cost_usd,
    )
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

/// Removes at most the amounts held locally and restores only that capacity.
fn release_amounts(
    budget: &mut gsd_proto::gsd::v1::BudgetSnapshot,
    tokens: i64,
    wall_time_ms: i64,
    tool_calls: i32,
    cost_usd: f64,
) {
    let tokens_before = budget.tokens_used;
    budget.tokens_used = tokens_before.saturating_sub(tokens).max(0);
    let tokens_freed = tokens_before.saturating_sub(budget.tokens_used).max(0);
    budget.tokens_remaining = budget.tokens_remaining.saturating_add(tokens_freed);

    let wall_before = budget.wall_time_ms_used;
    budget.wall_time_ms_used = wall_before.saturating_sub(wall_time_ms).max(0);
    budget.wall_time_ms_remaining = budget
        .wall_time_ms_remaining
        .saturating_add(wall_before.saturating_sub(budget.wall_time_ms_used));

    let calls_before = budget.tool_calls_used;
    budget.tool_calls_used = calls_before.saturating_sub(tool_calls).max(0);
    budget.tool_calls_remaining = budget
        .tool_calls_remaining
        .saturating_add(calls_before.saturating_sub(budget.tool_calls_used));

    let cost_before = budget.cost_usd_used;
    budget.cost_usd_used = (cost_before - cost_usd).max(0.0);
    let cost_freed = cost_before - budget.cost_usd_used;
    budget.cost_usd_remaining += cost_freed;
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
        // A refused reservation leaves no entry, so neither pair exists to hold any spend.
        assert!(gov.thread_budgets.get("t").is_none());
        assert!(gov.session_budgets.get("s").is_none());
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

    #[test]
    fn an_id_over_the_byte_cap_is_refused_and_moves_no_budget() {
        let gov = Governor::new(GovernorConfig::default());
        // 257 bytes: one over the 256-byte id cap.
        let long = "s".repeat(257);
        let reserved = tokio_test::block_on(
            gov.reserve_budget_checked(budget_request(&long, "t", 5_000, 0.0)),
        );
        assert!(
            matches!(reserved, Err(GovernorError::InvalidBudget(_))),
            "{reserved:?}"
        );
        let released = tokio_test::block_on(gov.release_budget(&long, "t", 1_000, 500, 2, 0.5));
        assert!(
            matches!(released, Err(GovernorError::InvalidBudget(_))),
            "{released:?}"
        );
    }

    fn budget_request(
        session: &str,
        thread: &str,
        tokens: i64,
        cost_usd: f64,
    ) -> gsd_proto::gsd::v1::BudgetRequest {
        gsd_proto::gsd::v1::BudgetRequest {
            session_id: Some(gsd_proto::gsd::v1::SessionId {
                value: session.to_string(),
            }),
            thread_id: Some(gsd_proto::gsd::v1::ChatThreadId {
                value: thread.to_string(),
            }),
            tokens,
            cost_usd,
            ..Default::default()
        }
    }

    #[test]
    fn refused_reservation_leaves_global_usage_untouched() {
        let gov = Governor::new(GovernorConfig::default());
        for _ in 0..100 {
            let grant =
                tokio_test::block_on(gov.reserve_budget(budget_request("s", "t", 25_000, 0.0)));
            assert!(!grant.granted);
        }
        assert_eq!(gov.global_usage.totals.lock().tokens_used, 0);

        let grant = tokio_test::block_on(gov.reserve_budget(budget_request("s", "t", 1_000, 0.0)));
        assert!(grant.granted);
    }

    #[test]
    fn release_without_matching_reservation_frees_no_global_usage() {
        let gov = Governor::new(GovernorConfig::default());
        gov.try_reserve_budget(&request(1_000, 0, 0, 0.0), "s", "t")
            .expect("reservation accepted");
        gov.try_reserve_budget(&request(2_000, 0, 0, 0.0), "s2", "t2")
            .expect("reservation accepted");

        tokio_test::block_on(gov.release_budget("s", "t", 1_000, 0, 0, 0.0))
            .expect("release accepted");
        assert_eq!(gov.global_usage.totals.lock().tokens_used, 2_000);

        // A second release of the same pair and a release of an unknown pair match no reservation:
        // both are refused, and neither frees anything.
        for (session, thread) in [("s", "t"), ("unknown", "unknown")] {
            let refused =
                tokio_test::block_on(gov.release_budget(session, thread, 1_000, 0, 0, 0.0));
            assert!(
                matches!(refused, Err(GovernorError::UnknownReservation(_))),
                "{refused:?}"
            );
        }
        assert_eq!(gov.global_usage.totals.lock().tokens_used, 2_000);
    }

    #[test]
    fn invalid_amounts_are_refused_before_any_mutation() {
        let gov = Governor::new(GovernorConfig::default());
        gov.try_reserve_budget(&request(0, 0, 0, 0.0), "s", "t")
            .expect("zero reservation accepted");
        let session_before = session_snapshot(&gov, "s");
        let thread_before = thread_snapshot(&gov, "t");

        for bad in [
            request(0, 0, 0, -5.0),
            request(0, 0, 0, f64::NAN),
            request(0, -500, 0, 0.0),
            request(0, 0, -2, 0.0),
        ] {
            let err = gov
                .try_reserve_budget(&bad, "s", "t")
                .expect_err("invalid amount is refused");
            assert!(matches!(err, GovernorError::InvalidBudget(_)));
        }

        let grant = tokio_test::block_on(gov.reserve_budget(budget_request("s", "t", 0, -5.0)));
        assert!(!grant.granted);

        assert_eq!(session_snapshot(&gov, "s"), session_before);
        assert_eq!(thread_snapshot(&gov, "t"), thread_before);
        assert_eq!(gov.global_usage.totals.lock().cost_micros_used, 0);
    }

    #[test]
    fn concurrent_reservations_never_exceed_global_token_cap() {
        let mut config = GovernorConfig::default();
        config.default_session_budget.max_tokens = 10_000_000;
        config.default_thread_budget.max_tokens = 10_000_000;
        let gov = Arc::new(Governor::new(config));

        let granted: u64 = std::thread::scope(|scope| {
            let handles: Vec<_> = (0..64)
                .map(|i| {
                    let gov = Arc::clone(&gov);
                    scope.spawn(move || {
                        let thread = format!("t{i}");
                        let grant = tokio_test::block_on(
                            gov.reserve_budget(budget_request("s", &thread, 20_000, 0.0)),
                        );
                        if grant.granted {
                            20_000u64
                        } else {
                            0u64
                        }
                    })
                })
                .collect();
            handles
                .into_iter()
                .map(|h| h.join().expect("reserving thread joins"))
                .sum()
        });

        assert!(granted <= 1_000_000);
        assert_eq!(gov.global_usage.totals.lock().tokens_used, granted);
    }

    #[test]
    fn refused_reservation_leaves_no_budget_entries() {
        let mut config = GovernorConfig::default();
        config.default_thread_budget.max_tokens = 1_000;
        let gov = Governor::new(config);

        // 5_000 tokens fit the session budget but exceed the thread budget, so the thread check refuses.
        let refused =
            tokio_test::block_on(gov.reserve_budget_checked(budget_request("s", "t", 5_000, 0.0)));
        assert!(
            matches!(refused, Err(GovernorError::BudgetExceeded(_))),
            "{refused:?}"
        );

        assert!(gov.get_budget_snapshot("s").is_err());
        assert!(gov.thread_budgets.get("t").is_none());
    }

    #[test]
    fn full_release_of_split_cost_pair_removes_entry() {
        let gov = Governor::new(GovernorConfig::default());
        gov.try_reserve_budget(&request(0, 0, 0, 0.1), "s", "t")
            .expect("first cost reservation accepted");
        gov.try_reserve_budget(&request(0, 0, 0, 0.2), "s", "t")
            .expect("second cost reservation accepted");

        tokio_test::block_on(gov.release_budget("s", "t", 0, 0, 0, 0.1))
            .expect("first release accepted");
        tokio_test::block_on(gov.release_budget("s", "t", 0, 0, 0, 0.2))
            .expect("second release accepted");

        // Both pairs are fully returned, so no reservation remains for the pair.
        let held = gov.check_release("s", "t", 0, 0, 0, 0.0);
        assert!(
            matches!(held, Err(GovernorError::UnknownReservation(_))),
            "{held:?}"
        );
        let released = tokio_test::block_on(gov.release_budget("s", "t", 0, 0, 0, 0.0));
        assert!(
            matches!(released, Err(GovernorError::UnknownReservation(_))),
            "{released:?}"
        );
    }

    #[test]
    fn snapshot_for_an_unknown_session_names_the_session() {
        let gov = Governor::new(GovernorConfig::default());
        let id = "never-reserved-session";
        match gov.get_budget_snapshot(id) {
            Err(GovernorError::InvalidBudget(message)) => {
                assert!(message.contains(id), "{message}");
            }
            other => panic!("expected InvalidBudget, got {other:?}"),
        }
    }
}
