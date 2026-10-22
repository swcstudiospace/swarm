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
                max_wall_time_ms: 3600_000, // 1 hour
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
    cost_tracker: Arc<CostTracker>,
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
            cost_tracker: Arc::new(CostTracker::new()),
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
        if !self.rate_limiters.check_rate_limit(&request) {
            return gsd_proto::gsd::v1::BudgetGrant {
                granted: false,
                tokens_granted: 0,
                wall_time_ms_granted: 0,
                tool_calls_granted: 0,
                cost_usd_granted: 0.0,
                valid_for: None,
            };
        }

        // Reserve session budget
        let session_budget = self.get_or_create_session_budget(&session_id.value);
        let mut budget = session_budget.write();

        if budget.tokens_used + request.tokens > self.config.default_session_budget.max_tokens {
            return gsd_proto::gsd::v1::BudgetGrant {
                granted: false,
                tokens_granted: 0,
                wall_time_ms_granted: 0,
                tool_calls_granted: 0,
                cost_usd_granted: 0.0,
                valid_for: None,
            };
        }

        // Reserve thread budget
        let thread_budget = self.get_or_create_thread_budget(&thread_id.value);
        let mut thread_budget = thread_budget.write();

        if thread_budget.tokens_used + request.tokens > self.config.default_thread_budget.max_tokens
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

        // Reserve
        budget.tokens_used += request.tokens;
        budget.wall_time_ms_used += request.wall_time_ms;
        budget.tool_calls_used += request.tool_calls;
        budget.cost_usd_used += request.cost_usd;

        thread_budget.tokens_used += request.tokens;
        thread_budget.wall_time_ms_used += request.wall_time_ms;
        thread_budget.tool_calls_used += request.tool_calls;
        thread_budget.cost_usd_used += request.cost_usd;

        self.metrics.inc_reservations();

        gsd_proto::gsd::v1::BudgetGrant {
            granted: true,
            tokens_granted: request.tokens,
            wall_time_ms_granted: request.wall_time_ms,
            tool_calls_granted: request.tool_calls,
            cost_usd_granted: request.cost_usd,
            valid_for: Some(prost_types::Duration {
                seconds: 3600, // 1 hour
                nanos: 0,
            }),
        }
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
            budget.tokens_used = budget.tokens_used.saturating_sub(tokens);
            budget.wall_time_ms_used = budget.wall_time_ms_used.saturating_sub(wall_time_ms);
            budget.tool_calls_used = budget.tool_calls_used.saturating_sub(tool_calls);
            budget.cost_usd_used = (budget.cost_usd_used - cost_usd).max(0.0);
        }

        if let Some(budget) = self.thread_budgets.get(thread_id) {
            let mut budget = budget.write();
            budget.tokens_used = budget.tokens_used.saturating_sub(tokens);
            budget.wall_time_ms_used = budget.wall_time_ms_used.saturating_sub(wall_time_ms);
            budget.tool_calls_used = budget.tool_calls_used.saturating_sub(tool_calls);
            budget.cost_usd_used = (budget.cost_usd_used - cost_usd).max(0.0);
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
            let snapshot = budget.read().clone();
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
    token_limit: u64,
    cost_limit_usd: f64,
}

impl GlobalUsage {
    fn new() -> Self {
        Self {
            tokens_used: std::sync::atomic::AtomicU64::new(0),
            cost_usd_used: std::sync::atomic::AtomicU64::new(0),
            token_limit: 1_000_000,
            cost_limit_usd: 100.0,
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

struct RateLimiterRegistry {
    limiters: dashmap::DashMap<String, Arc<RateLimiter>>,
}

impl RateLimiterRegistry {
    fn new() -> Self {
        Self {
            limiters: DashMap::new(),
        }
    }

    fn check_rate_limit(&self, request: &gsd_proto::gsd::v1::BudgetRequest) -> bool {
        // Simplified rate limiting
        true
    }
}

struct RateLimiter {
    max_requests_per_min: u32,
    max_tokens_per_min: u64,
    request_count: std::sync::atomic::AtomicU32,
    token_count: std::sync::atomic::AtomicU64,
    window_start: std::sync::atomic::AtomicU64,
}

impl RateLimiter {
    fn check(&self, tokens: u64) -> bool {
        // Simplified rate limiting
        true
    }
}

struct CostTracker {
    total_cost_usd: std::sync::atomic::AtomicU64, // micros USD
    cost_by_model: dashmap::DashMap<String, AtomicU64>,
    cost_by_thread: dashmap::DashMap<String, AtomicU64>,
}

impl CostTracker {
    fn new() -> Self {
        Self {
            total_cost_usd: std::sync::atomic::AtomicU64::new(0),
            cost_by_model: DashMap::new(),
            cost_by_thread: DashMap::new(),
        }
    }

    fn record_cost(&self, model: &str, thread_id: &str, cost_usd: f64) {
        let micros = (cost_usd * 1_000_000.0) as u64;
        self.total_cost_usd.fetch_add(micros, Ordering::AcqRel);
        self.cost_by_model
            .entry(model.to_string())
            .or_insert_with(|| AtomicU64::new(0))
            .fetch_add(micros, Ordering::AcqRel);
        self.cost_by_thread
            .entry(thread_id.to_string())
            .or_insert_with(|| AtomicU64::new(0))
            .fetch_add(micros, Ordering::AcqRel);
    }
}

struct GovernorMetrics {
    reservations: AtomicU64,
    releases: AtomicU64,
    denials: AtomicU64,
    rate_limit_hits: AtomicU64,
}

impl Default for GovernorMetrics {
    fn default() -> Self {
        Self {
            reservations: AtomicU64::new(0),
            releases: AtomicU64::new(0),
            denials: AtomicU64::new(0),
            rate_limit_hits: AtomicU64::new(0),
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
    fn inc_denials(&self) {
        self.denials.fetch_add(1, Ordering::Relaxed);
    }
    fn inc_rate_limit_hits(&self) {
        self.rate_limit_hits.fetch_add(1, Ordering::Relaxed);
    }
}
