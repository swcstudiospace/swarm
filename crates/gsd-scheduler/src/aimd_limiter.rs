//! Additive-increase / multiplicative-decrease limiter for the effective parallelism.
//!
//! Port of `swarm/limiter.py` `AimdLimiter` and of the failure-streak circuit in
//! `scripts/swarm_run.py` (`_backpressure`). Time is passed in explicitly so the limiter is
//! deterministic and holds no clock of its own.
//!
//! Rules:
//! - Every rate-limit signal halves the limit (floor `min`) and holds growth for the cooldown.
//!   The once-per-burst dedupe is the dispatch runner's (`RateLimitGate`), not the limiter's.
//! - A success after the cooldown has elapsed adds one back (ceiling `max`). A success always
//!   resets the failure streak and re-arms the circuit.
//! - Non-rate-limit failures of distinct tasks form a streak. When the streak reaches
//!   `streak_threshold` the limit halves once; further failures do not halve again until a
//!   success resets the streak. A threshold of 0 disables the circuit.
//! - A rate-limited failure neither counts toward the streak nor resets it.

use std::collections::HashSet;
use std::time::{Duration, Instant};

use crate::{SchedulerError, SchedulerResult};

/// Upper bound on how long a server-requested `retry_after` may hold growth (`SWARM_RETRY_CAP_S`).
pub const DEFAULT_RETRY_CAP: Duration = Duration::from_secs(120);

/// Default failure-streak threshold (`SWARM_FAIL_STREAK`).
pub const DEFAULT_STREAK_THRESHOLD: usize = 3;

/// Effective-parallelism limiter driven by rate-limit, success and failure signals.
#[derive(Debug, Clone)]
pub struct AimdLimiter {
    max: usize,
    min: usize,
    current: usize,
    cooldown: Duration,
    retry_cap: Duration,
    cooldown_until: Option<Instant>,
    streak_threshold: usize,
    streak: HashSet<String>,
    streak_tripped: bool,
}

impl AimdLimiter {
    /// Creates a limiter that starts at `max`. Requires `1 <= min <= max`.
    pub fn new(max: usize, min: usize, cooldown: Duration) -> SchedulerResult<Self> {
        if min < 1 || max < min {
            return Err(SchedulerError::Other(format!(
                "need 1 <= min <= max, got min={min} max={max}"
            )));
        }
        Ok(Self {
            max,
            min,
            current: max,
            cooldown,
            retry_cap: DEFAULT_RETRY_CAP,
            cooldown_until: None,
            streak_threshold: DEFAULT_STREAK_THRESHOLD,
            streak: HashSet::new(),
            streak_tripped: false,
        })
    }

    /// Sets the failure-streak threshold; `0` turns the circuit off.
    pub fn with_streak_threshold(mut self, threshold: usize) -> Self {
        self.streak_threshold = threshold;
        self
    }

    /// Sets the cap applied to server-requested `retry_after` holds.
    pub fn with_retry_cap(mut self, cap: Duration) -> Self {
        self.retry_cap = cap;
        self
    }

    /// The cap on concurrent runs right now.
    pub fn current(&self) -> usize {
        self.current
    }

    /// Whether a rate-limit cooldown is running at `now`.
    pub fn in_burst(&self, now: Instant) -> bool {
        self.cooldown_until.is_some_and(|until| now < until)
    }

    /// A rate-limit signal at `now`. Halves the limit on every call (floor `min`) and holds growth
    /// for `max(cooldown, min(retry_after, retry_cap))`. Returns whether the limit dropped, which is
    /// false only when it was already at `min`. Python: `swarm/limiter.py` `AimdLimiter.on_rate_limit`.
    pub fn on_rate_limit(&mut self, now: Instant, retry_after: Option<Duration>) -> bool {
        let before = self.current;
        self.halve(now, retry_after);
        self.current < before
    }

    /// A success at `now`: resets the failure streak, and adds one back to the limit when no
    /// cooldown is running. Returns whether the limit grew.
    pub fn on_success(&mut self, now: Instant) -> bool {
        self.streak.clear();
        self.streak_tripped = false;
        if self.in_burst(now) || self.current >= self.max {
            return false;
        }
        self.current += 1;
        true
    }

    /// A non-rate-limit failure of task `task_id` at `now`. Returns whether the streak tripped
    /// and the limit was halved by this call.
    pub fn on_failure(&mut self, now: Instant, task_id: &str) -> bool {
        if self.streak_threshold == 0 {
            return false;
        }
        self.streak.insert(task_id.to_string());
        if self.streak.len() < self.streak_threshold || self.streak_tripped {
            return false;
        }
        self.streak_tripped = true;
        self.halve(now, None);
        true
    }

    /// Number of distinct tasks in the current failure streak.
    pub fn streak_len(&self) -> usize {
        self.streak.len()
    }

    fn halve(&mut self, now: Instant, retry_after: Option<Duration>) {
        let mut hold = self.cooldown;
        if let Some(requested) = retry_after {
            hold = hold.max(requested.min(self.retry_cap));
        }
        self.current = (self.current / 2).max(self.min);
        let until = now + hold;
        self.cooldown_until = Some(self.cooldown_until.map_or(until, |prev| prev.max(until)));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limiter(max: usize) -> AimdLimiter {
        AimdLimiter::new(max, 1, Duration::from_secs(10)).expect("valid bounds")
    }

    #[test]
    fn rejects_bad_bounds() {
        assert!(AimdLimiter::new(4, 0, Duration::ZERO).is_err());
        assert!(AimdLimiter::new(2, 3, Duration::ZERO).is_err());
    }

    #[test]
    fn rate_limit_halves_on_every_call() {
        let t0 = Instant::now();
        let mut l = limiter(16);
        assert!(l.on_rate_limit(t0, None));
        assert_eq!(l.current(), 8);
        assert!(l.on_rate_limit(t0 + Duration::from_secs(1), None));
        assert_eq!(l.current(), 4);
        assert!(l.on_rate_limit(t0 + Duration::from_secs(2), None));
        assert_eq!(l.current(), 2);
    }

    #[test]
    fn recovers_one_slot_after_cooldown() {
        let t0 = Instant::now();
        let mut l = limiter(8);
        l.on_rate_limit(t0, None);
        assert_eq!(l.current(), 4);
        assert!(!l.on_success(t0 + Duration::from_secs(5)));
        assert_eq!(l.current(), 4);
        assert!(l.on_success(t0 + Duration::from_secs(10)));
        assert_eq!(l.current(), 5);
    }

    #[test]
    fn retry_after_extends_cooldown_up_to_cap() {
        let t0 = Instant::now();
        let mut l = limiter(8).with_retry_cap(Duration::from_secs(30));
        l.on_rate_limit(t0, Some(Duration::from_secs(3600)));
        assert!(!l.on_success(t0 + Duration::from_secs(29)));
        assert!(l.on_success(t0 + Duration::from_secs(30)));
    }

    #[test]
    fn fail_streak_halves_once_and_success_resets() {
        let t0 = Instant::now();
        let mut l = limiter(8);
        assert!(!l.on_failure(t0, "a"));
        assert!(!l.on_failure(t0, "b"));
        assert!(l.on_failure(t0, "c"));
        assert_eq!(l.current(), 4);
        assert!(!l.on_failure(t0, "d"));
        assert_eq!(l.current(), 4);
        l.on_success(t0 + Duration::from_secs(60));
        assert_eq!(l.streak_len(), 0);
        assert!(!l.on_failure(t0, "e"));
        assert!(!l.on_failure(t0, "f"));
        assert!(l.on_failure(t0, "g"));
        assert_eq!(l.current(), 2);
    }

    #[test]
    fn repeated_failures_of_one_task_do_not_trip_the_streak() {
        let t0 = Instant::now();
        let mut l = limiter(8);
        for _ in 0..5 {
            assert!(!l.on_failure(t0, "same"));
        }
        assert_eq!(l.current(), 8);
    }

    #[test]
    fn zero_threshold_disables_the_circuit() {
        let t0 = Instant::now();
        let mut l = limiter(8).with_streak_threshold(0);
        for id in ["a", "b", "c", "d"] {
            assert!(!l.on_failure(t0, id));
        }
        assert_eq!(l.current(), 8);
    }

    #[test]
    fn limit_stays_within_bounds() {
        let t0 = Instant::now();
        let mut l = AimdLimiter::new(4, 2, Duration::ZERO).expect("valid bounds");
        l.on_rate_limit(t0, None);
        l.on_rate_limit(t0 + Duration::from_secs(1), None);
        l.on_rate_limit(t0 + Duration::from_secs(2), None);
        assert_eq!(l.current(), 2);
        for i in 0..10 {
            l.on_success(t0 + Duration::from_secs(10 + i));
        }
        assert_eq!(l.current(), 4);
    }

    #[test]
    fn rate_limit_at_floor_reports_no_drop() {
        let t0 = Instant::now();
        let mut l = AimdLimiter::new(2, 2, Duration::from_secs(10)).expect("valid bounds");
        assert!(!l.on_rate_limit(t0, None));
        assert_eq!(l.current(), 2);
    }

    #[test]
    fn later_signal_extends_growth_hold() {
        let t0 = Instant::now();
        let mut l = limiter(16);
        l.on_rate_limit(t0, None);
        l.on_rate_limit(t0 + Duration::from_secs(5), None);
        assert_eq!(l.current(), 4);
        assert!(!l.on_success(t0 + Duration::from_secs(9)));
        assert!(l.on_success(t0 + Duration::from_secs(15)));
        assert_eq!(l.current(), 5);
    }
}
