//! Plain-Rust wrappers of the scheduler primitives in `gsd-sched-core` (Phase 14.4, unit S1.4).
//!
//! Like [`crate::pure`], this module has no PyO3 types, so `cargo test` exercises it without an interpreter. The `#[pyfunction]` and
//! `#[pyclass]` layer in `lib.rs` only converts Python values and raises exceptions. Every entry point returns `Err(message)` for a
//! fault, with the wire code first (the `ValueError("<wire code>: <operation> failed: <detail>")` convention of [`crate::pure`]):
//!
//! * `E-INPUT` for a value the Python oracle rejects with `ValueError` (an invalid claim pattern or mode, bad limiter bounds, a
//!   jitter outside `[0, 1]`, a time that is not a finite number of seconds);
//! * `E-INTERNAL` for a panic ([`crate::pure::guard_panic`]; the payload is never quoted) and for a state mutex poisoned by one.
//!
//! The behaviour lives in `gsd-sched-core`, where the Python scheduler stays the oracle; the vectors in
//! `tests/parity/fixtures/scheduler` compare both. Nothing here blocks or does I/O. The two stateful types ([`SharedClaims`] and
//! [`Limiter`]) hold their state behind a `Mutex` so a shared Python object can be called from several threads: calls serialise,
//! and no lock is held while Python code runs because no Python code runs in here.

use std::fmt::Display;
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use gsd_codes::ErrorCode;
use gsd_sched_core::aimd::{AimdLimiter, RateLimitGate};
use gsd_sched_core::backoff::{self, BackoffError};
use gsd_sched_core::claim_pattern::{claims_overlap as core_claims_overlap, ClaimMode, PathClaim};
use gsd_sched_core::claims::{self, AcquireError, ClaimTable};
use gsd_sched_core::{drain, priority};

use crate::pure::guard_panic;

/// The largest number of seconds the limiter accepts for a time, a cooldown or a retry cap (about 31 years). Python floats reach far
/// beyond what an `Instant` can add, and a value this large is a caller bug, not a schedule: it is refused instead of overflowing.
pub const MAX_SECONDS: f64 = 1.0e9;

/// A row of `order_ready`: `(priority, dag_depth, created_at)`.
pub type OrderRow = (Option<String>, Option<i64>, Option<f64>);

/// A refused claim: `(holder, held pattern, requested pattern)`, patterns normalized.
pub type Conflict = (String, String, String);

/// The outcome of [`SharedClaims::acquire`]: whether the claims were taken, and the conflicts when they were not.
pub type Acquired = (bool, Vec<Conflict>);

/// An owner's claims as `(pattern, mode)` pairs with the mode spelled `"exclusive"` or `"shared"`.
pub type HeldList = Vec<(String, String)>;

/// The `E-INPUT` message for a refused argument of `operation`.
fn input_error(operation: &str, detail: impl Display) -> String {
    format!(
        "{}: {operation} failed: {detail}",
        ErrorCode::Input.as_str()
    )
}

/// Runs `work` under [`guard_panic`] and flattens its own `Err` with a panic's.
fn guarded<T>(operation: &str, work: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
    guard_panic(operation, work)?
}

/// Locks `mutex`; a mutex poisoned by an earlier panic is an `E-INTERNAL` fault, never a silent reuse of half-updated state.
fn lock<'a, T>(operation: &str, mutex: &'a Mutex<T>) -> Result<MutexGuard<'a, T>, String> {
    mutex.lock().map_err(|_| {
        format!(
            "{}: {operation} failed: state poisoned by an earlier panic",
            ErrorCode::Internal.as_str()
        )
    })
}

/// The dispatch order of `rows` as a permutation of their indices: priority rank (`P0` first, anything unknown or missing is `P2`),
/// then `dag_depth`, then `created_at`, ascending and stable. See [`gsd_sched_core::priority::order_ready`].
///
/// Sorting a ready list takes microseconds, so the binding keeps the GIL.
pub fn order_ready(rows: &[OrderRow]) -> Result<Vec<usize>, String> {
    guarded("sched_order_ready", || {
        Ok(priority::order_ready(rows.iter().map(
            |(priority, depth, created)| (priority.as_deref(), *depth, *created),
        )))
    })
}

/// The canonical form of a claim pattern (`src/` is `src/**`, `./a//b` is `a/b`).
///
/// # Errors
///
/// `E-INPUT` for an empty, absolute, drive-qualified, `~`-prefixed or `..`-containing pattern.
pub fn normalize_claim(pattern: &str) -> Result<String, String> {
    guarded("sched_normalize_claim", || {
        claims::normalize_pattern(pattern)
            .map_err(|error| input_error("sched_normalize_claim", error))
    })
}

/// Whether two patterns can match a common path, ignoring modes. `E-INPUT` for an invalid pattern.
pub fn claims_overlap(a: &str, b: &str) -> Result<bool, String> {
    guarded("sched_claims_overlap", || {
        let parse = |pattern: &str| {
            PathClaim::new(pattern, ClaimMode::Exclusive)
                .map_err(|error| input_error("sched_claims_overlap", error))
        };
        Ok(core_claims_overlap(&parse(a)?, &parse(b)?))
    })
}

/// The mode a claim spells as `"exclusive"` or `"shared"` (case-sensitive, as `swarm.claims.MODES`).
fn parse_mode(operation: &str, text: &str) -> Result<ClaimMode, String> {
    match text {
        "exclusive" => Ok(ClaimMode::Exclusive),
        "shared" => Ok(ClaimMode::Shared),
        other => Err(input_error(
            operation,
            format!("claim mode must be one of ('exclusive', 'shared'), got {other:?}"),
        )),
    }
}

/// The spelling of a mode in [`HeldList`].
fn mode_text(mode: ClaimMode) -> &'static str {
    match mode {
        ClaimMode::Exclusive => "exclusive",
        ClaimMode::Shared => "shared",
    }
}

/// A [`ClaimTable`] that several threads may share: every call takes the table's mutex for its whole duration.
#[derive(Debug, Default)]
pub struct SharedClaims {
    table: Mutex<ClaimTable>,
}

impl SharedClaims {
    /// An empty table.
    pub fn new() -> Self {
        Self::default()
    }

    /// Takes every claim for `owner`, or none. `claims` are `(pattern, mode)` pairs.
    ///
    /// Returns `(true, [])` when the claims were taken and `(false, conflicts)` when another owner's claims collide, listed in the
    /// order of [`ClaimTable::acquire`] (requested claims in order, holders in acquisition order). The table is unchanged on a
    /// conflict.
    ///
    /// # Errors
    ///
    /// `E-INPUT` for an invalid pattern or mode, before anything changes.
    ///
    /// Cost is `O(requested x held)` glob comparisons while the mutex is held; the binding releases the GIL around the call.
    pub fn acquire(&self, owner: &str, claims: &[(String, String)]) -> Result<Acquired, String> {
        guarded("ClaimTable.acquire", || {
            let parsed = claims
                .iter()
                .map(|(pattern, mode)| {
                    parse_mode("ClaimTable.acquire", mode).map(|mode| (pattern.as_str(), mode))
                })
                .collect::<Result<Vec<_>, _>>()?;
            let mut table = lock("ClaimTable.acquire", &self.table)?;
            match table.acquire(owner, &parsed) {
                Ok(()) => Ok((true, Vec::new())),
                Err(AcquireError::Conflicts(conflicts)) => Ok((
                    false,
                    conflicts
                        .into_iter()
                        .map(|c| (c.holder, c.held, c.requested))
                        .collect(),
                )),
                Err(AcquireError::InvalidPattern(error)) => {
                    Err(input_error("ClaimTable.acquire", error))
                }
            }
        })
    }

    /// Drops everything `owner` holds and returns how many claims that was; idempotent.
    pub fn release(&self, owner: &str) -> Result<usize, String> {
        guarded("ClaimTable.release", || {
            Ok(lock("ClaimTable.release", &self.table)?.release(owner))
        })
    }

    /// A snapshot: every owner with its claims as `(pattern, mode)`, owners in the order they first acquired and claims in
    /// acquisition order.
    pub fn held(&self) -> Result<Vec<(String, HeldList)>, String> {
        guarded("ClaimTable.held", || {
            let table = lock("ClaimTable.held", &self.table)?;
            Ok(table
                .held()
                .map(|(owner, claims)| {
                    let list = claims
                        .iter()
                        .map(|claim| (claim.pattern.clone(), mode_text(claim.mode).to_string()))
                        .collect();
                    (owner.to_string(), list)
                })
                .collect())
        })
    }
}

#[derive(Debug)]
struct LimiterState {
    limiter: AimdLimiter,
    gate: RateLimitGate,
}

/// The AIMD limiter, its once-per-burst gate and the failure-streak circuit behind one mutex, driven by caller-supplied seconds.
///
/// `now` and `submitted_at` are monotonic seconds, finite and in `[0, MAX_SECONDS]` (Python's gate starts at 0.0). They are mapped
/// to `Instant`s as an offset from the moment of construction, with a resolution of one nanosecond; two times closer than that
/// compare equal.
#[derive(Debug)]
pub struct Limiter {
    base: Instant,
    state: Mutex<LimiterState>,
}

impl Limiter {
    /// A limiter that starts at `max_n`.
    ///
    /// `cooldown_s` is finite and `>= 0`, `retry_cap_s` finite and `> 0` (the caller resolves `SWARM_RATE_COOLDOWN_S` and
    /// `SWARM_RETRY_CAP_S` first), and a `streak_threshold` of `0` or less turns the failure-streak circuit off.
    ///
    /// # Errors
    ///
    /// `E-INPUT` unless `1 <= min_n <= max_n` and the two durations are within range.
    pub fn new(
        max_n: i64,
        min_n: i64,
        cooldown_s: f64,
        retry_cap_s: f64,
        streak_threshold: i64,
    ) -> Result<Self, String> {
        const OPERATION: &str = "AimdLimiter";
        guarded(OPERATION, || {
            let bounds = (usize::try_from(max_n), usize::try_from(min_n));
            let (Ok(max), Ok(min)) = bounds else {
                return Err(input_error(
                    OPERATION,
                    format!("need 1 <= min_n <= max_n, got min_n={min_n} max_n={max_n}"),
                ));
            };
            if !(0.0..=MAX_SECONDS).contains(&cooldown_s) {
                return Err(input_error(
                    OPERATION,
                    format!("cooldown_s must be a finite number in [0, {MAX_SECONDS}], got {cooldown_s}"),
                ));
            }
            if !(retry_cap_s > 0.0 && retry_cap_s <= MAX_SECONDS) {
                return Err(input_error(
                    OPERATION,
                    format!("retry_cap_s must be a finite number in (0, {MAX_SECONDS}], got {retry_cap_s}"),
                ));
            }
            let limiter = AimdLimiter::new(max, min, Duration::from_secs_f64(cooldown_s))
                .map_err(|error| input_error(OPERATION, error))?
                .with_retry_cap(Duration::from_secs_f64(retry_cap_s))
                .with_streak_threshold(usize::try_from(streak_threshold).unwrap_or(0));
            Ok(Self {
                base: Instant::now(),
                state: Mutex::new(LimiterState {
                    limiter,
                    gate: RateLimitGate::new(),
                }),
            })
        })
    }

    /// The cap on concurrent runs right now.
    pub fn current(&self) -> Result<usize, String> {
        guarded("AimdLimiter.current", || {
            Ok(lock("AimdLimiter.current", &self.state)?.limiter.current())
        })
    }

    /// A success at `now`; returns whether the limit grew.
    pub fn on_success(&self, now: f64) -> Result<bool, String> {
        const OPERATION: &str = "AimdLimiter.on_success";
        guarded(OPERATION, || {
            let now = self.instant(OPERATION, "now", now)?;
            Ok(lock(OPERATION, &self.state)?.limiter.on_success(now))
        })
    }

    /// A bare rate-limit signal at `now` (halves on every call, no burst dedupe); returns whether the limit dropped.
    /// `retry_after_s` of `None`, NaN or `<= 0` adds no hold beyond the cooldown; `+inf` holds for the retry cap.
    pub fn on_rate_limit(&self, now: f64, retry_after_s: Option<f64>) -> Result<bool, String> {
        const OPERATION: &str = "AimdLimiter.on_rate_limit";
        guarded(OPERATION, || {
            let now = self.instant(OPERATION, "now", now)?;
            let retry_after = retry_after(retry_after_s);
            Ok(lock(OPERATION, &self.state)?
                .limiter
                .on_rate_limit(now, retry_after))
        })
    }

    /// A non-rate-limit failure of `task_id` at `now`; returns whether the failure streak tripped and halved the limit.
    pub fn on_failure(&self, now: f64, task_id: &str) -> Result<bool, String> {
        const OPERATION: &str = "AimdLimiter.on_failure";
        guarded(OPERATION, || {
            let now = self.instant(OPERATION, "now", now)?;
            Ok(lock(OPERATION, &self.state)?
                .limiter
                .on_failure(now, task_id))
        })
    }

    /// A rate-limit hit from a session submitted at `submitted_at`, observed at `now`, through the once-per-burst gate
    /// (`Scheduler._backpressure`). Returns whether the limit dropped: `false` for a hit of the same burst
    /// (`submitted_at` before the last decrease) and for a hit at the floor.
    pub fn on_burst_hit(
        &self,
        submitted_at: f64,
        now: f64,
        retry_after_s: Option<f64>,
    ) -> Result<bool, String> {
        const OPERATION: &str = "AimdLimiter.on_burst_hit";
        guarded(OPERATION, || {
            let submitted_at = self.instant(OPERATION, "submitted_at", submitted_at)?;
            let now = self.instant(OPERATION, "now", now)?;
            let retry_after = retry_after(retry_after_s);
            let mut state = lock(OPERATION, &self.state)?;
            let LimiterState { limiter, gate } = &mut *state;
            Ok(gate.on_rate_limit(limiter, submitted_at, now, retry_after))
        })
    }

    /// `secs` seconds after this limiter's base instant; `E-INPUT` unless `secs` is finite and within `[0, MAX_SECONDS]`.
    fn instant(&self, operation: &str, name: &str, secs: f64) -> Result<Instant, String> {
        let refused = || {
            input_error(
                operation,
                format!(
                    "{name} must be a finite number of seconds in [0, {MAX_SECONDS}], got {secs}"
                ),
            )
        };
        if !(0.0..=MAX_SECONDS).contains(&secs) {
            return Err(refused());
        }
        let offset = Duration::try_from_secs_f64(secs).map_err(|_| refused())?;
        self.base.checked_add(offset).ok_or_else(refused)
    }
}

/// The hold a server-supplied `Retry-After` asks for: nothing for `None`, NaN, zero or a negative number (Python's
/// `max(cooldown, min(value, cap))` ignores them), otherwise the value, saturating at the largest `Duration` for `+inf`; the
/// limiter's own cap then bounds it.
fn retry_after(seconds: Option<f64>) -> Option<Duration> {
    match seconds {
        Some(value) if value > 0.0 => {
            Some(Duration::try_from_secs_f64(value).unwrap_or(Duration::MAX))
        }
        _ => None,
    }
}

/// The delay in seconds before retry number `attempt`; see [`gsd_sched_core::backoff::backoff_s`].
///
/// # Errors
///
/// `E-INPUT` for a `jitter` outside `[0, 1]` (checked first) and for a `base_s` or `cap_s` that is not positive.
pub fn backoff_s(
    attempt: i64,
    base_s: f64,
    cap_s: f64,
    jitter: f64,
    unit: f64,
) -> Result<f64, String> {
    guarded("sched_backoff_s", || {
        backoff::backoff_s(attempt, base_s, cap_s, jitter, unit)
            .map_err(|error: BackoffError| input_error("sched_backoff_s", error))
    })
}

/// The cancel-drain schedule `(grace_s, kill_grace_s)`; see [`gsd_sched_core::drain::plan`].
///
/// # Errors
///
/// `E-INPUT` for a NaN argument (the run configuration refuses NaN before the plan is computed).
pub fn drain_plan(
    cancel_grace_s: f64,
    shutdown_budget_s: f64,
    kill_grace_max_s: f64,
    stop_grace_s: f64,
    kill_settle_s: f64,
) -> Result<(f64, f64), String> {
    guarded("sched_drain_plan", || {
        let inputs = [
            cancel_grace_s,
            shutdown_budget_s,
            kill_grace_max_s,
            stop_grace_s,
            kill_settle_s,
        ];
        if inputs.iter().any(|value| value.is_nan()) {
            return Err(input_error("sched_drain_plan", "arguments must not be NaN"));
        }
        let plan = drain::plan(
            cancel_grace_s,
            shutdown_budget_s,
            kill_grace_max_s,
            stop_grace_s,
            kill_settle_s,
        );
        Ok((plan.grace_s, plan.kill_grace_s))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(pattern: &str, mode: &str) -> (String, String) {
        (pattern.to_string(), mode.to_string())
    }

    fn limiter(max: i64, min: i64) -> Limiter {
        Limiter::new(max, min, 10.0, 120.0, 3).expect("valid limiter")
    }

    fn assert_input_error(result: Result<impl std::fmt::Debug, String>, operation: &str) {
        let Err(message) = result else {
            panic!("expected an E-INPUT error from {operation}");
        };
        assert!(
            message.starts_with(&format!("E-INPUT: {operation} failed: ")),
            "{message}"
        );
    }

    #[test]
    fn order_ready_lists_indices_by_rank_depth_and_age_and_ranks_the_unknown_as_p2() {
        let rows: [OrderRow; 6] = [
            (Some("P3".into()), Some(0), Some(0.0)),
            (Some("zz".into()), Some(0), Some(0.0)),
            (None, Some(0), Some(0.0)),
            (Some("P0".into()), Some(5), Some(9.0)),
            (Some("P2".into()), Some(0), Some(0.0)),
            (Some("P1".into()), None, None),
        ];
        // P0, P1, then the three P2 rows in input order, then P3
        assert_eq!(order_ready(&rows), Ok(vec![3, 5, 1, 2, 4, 0]));
        assert_eq!(order_ready(&[]), Ok(vec![]));
    }

    #[test]
    fn claim_patterns_are_normalized_and_invalid_ones_are_input_errors() {
        for (pattern, expected) in [("src/", "src/**"), ("./a//b", "a/b"), (" a\\b ", "a/b")] {
            assert_eq!(
                normalize_claim(pattern),
                Ok(expected.to_string()),
                "{pattern}"
            );
        }
        for bad in ["", "  ", "/abs", "~/x", "C:/x", "a/../b"] {
            assert_input_error(normalize_claim(bad), "sched_normalize_claim");
            assert_input_error(claims_overlap(bad, "a"), "sched_claims_overlap");
            assert_input_error(claims_overlap("a", bad), "sched_claims_overlap");
        }
    }

    #[test]
    fn overlap_ignores_the_mode_and_does_not_extend_a_bare_name_to_its_subtree() {
        for (a, b, expected) in [
            ("src/*.py", "src/a.py", true),
            ("src/**", "src/a/b.py", true),
            ("src", "src/a.py", false),
            ("a/*", "b/*", false),
        ] {
            assert_eq!(claims_overlap(a, b), Ok(expected), "{a} {b}");
        }
    }

    #[test]
    fn a_refused_acquire_reports_holder_held_and_requested_in_python_order_and_changes_nothing() {
        let table = SharedClaims::new();
        assert_eq!(
            table.acquire(
                "A",
                &[pair("src/a.py", "exclusive"), pair("lib/", "shared")]
            ),
            Ok((true, vec![]))
        );
        assert_eq!(
            table.acquire(
                "B",
                &[pair("src", "exclusive"), pair("lib/x.py", "exclusive")]
            ),
            Ok((
                false,
                vec![
                    ("A".to_string(), "src/a.py".to_string(), "src".to_string()),
                    (
                        "A".to_string(),
                        "lib/**".to_string(),
                        "lib/x.py".to_string()
                    ),
                ]
            ))
        );
        assert_eq!(
            table.held(),
            Ok(vec![(
                "A".to_string(),
                vec![pair("src/a.py", "exclusive"), pair("lib/**", "shared")]
            )])
        );
    }

    #[test]
    fn an_invalid_pattern_or_mode_is_an_input_error_and_the_table_keeps_its_claims() {
        let table = SharedClaims::new();
        table
            .acquire("A", &[pair("a", "exclusive")])
            .expect("first acquire");
        for claims in [
            vec![pair("b", "exclusive"), pair("../x", "exclusive")],
            vec![pair("b", "exclusive"), pair("c", "Exclusive")],
            vec![pair("b", "exclusive"), pair("c", "")],
        ] {
            assert_input_error(table.acquire("B", &claims), "ClaimTable.acquire");
        }
        assert_eq!(table.release("B"), Ok(0), "B must hold nothing");
        assert_eq!(table.held().map(|held| held.len()), Ok(1));
    }

    #[test]
    fn release_counts_the_dropped_claims_and_is_idempotent() {
        let table = SharedClaims::new();
        table
            .acquire("A", &[pair("a", "exclusive"), pair("b", "shared")])
            .expect("acquire");
        assert_eq!(table.release("A"), Ok(2));
        assert_eq!(table.release("A"), Ok(0));
        assert_eq!(table.held(), Ok(vec![]));
    }

    #[test]
    fn a_table_poisoned_by_a_panic_reports_e_internal_instead_of_reusing_its_state() {
        let table = SharedClaims::new();
        std::thread::scope(|scope| {
            let _ = scope
                .spawn(|| {
                    let _guard = table.table.lock();
                    panic!("poison the table");
                })
                .join();
        });
        for message in [
            table.release("A").expect_err("poisoned"),
            table.held().expect_err("poisoned"),
        ] {
            assert!(message.starts_with("E-INTERNAL: "), "{message}");
        }
    }

    #[test]
    fn the_limiter_halves_holds_for_the_cooldown_and_grows_back_on_the_caller_clock() {
        let l = limiter(8, 1);
        assert_eq!(l.on_rate_limit(100.0, None), Ok(true));
        assert_eq!(l.current(), Ok(4));
        assert_eq!(l.on_success(105.0), Ok(false));
        assert_eq!(l.on_success(110.0), Ok(true));
        assert_eq!(l.current(), Ok(5));
    }

    #[test]
    fn a_retry_after_extends_the_hold_up_to_the_cap_and_a_non_positive_or_nan_one_adds_nothing() {
        // (retry_after_s, first success time that grows)
        let cases = [
            (Some(50.0), 150.0),
            (Some(1.0e9), 220.0),
            (Some(f64::INFINITY), 220.0),
            (Some(0.0), 110.0),
            (Some(-3.0), 110.0),
            (Some(f64::NAN), 110.0),
            (Some(f64::NEG_INFINITY), 110.0),
            (None, 110.0),
        ];
        for (retry_after_s, grows_at) in cases {
            let l = limiter(8, 1);
            l.on_rate_limit(100.0, retry_after_s).expect("signal");
            assert_eq!(
                l.on_success(grows_at - 0.25),
                Ok(false),
                "{retry_after_s:?}"
            );
            assert_eq!(l.on_success(grows_at), Ok(true), "{retry_after_s:?}");
        }
    }

    #[test]
    fn a_burst_hit_counts_once_per_burst_and_a_hit_at_the_floor_reports_no_drop() {
        let l = limiter(8, 2);
        assert_eq!(l.on_burst_hit(0.0, 100.0, None), Ok(true));
        assert_eq!(l.on_burst_hit(99.0, 101.0, None), Ok(false), "same burst");
        assert_eq!(l.current(), Ok(4));
        assert_eq!(l.on_burst_hit(100.0, 102.0, None), Ok(true), "a new burst");
        assert_eq!(
            l.on_burst_hit(101.0, 103.0, None),
            Ok(false),
            "same burst again"
        );
        assert_eq!(
            l.on_burst_hit(102.0, 104.0, None),
            Ok(false),
            "at the floor"
        );
        assert_eq!(l.current(), Ok(2));
    }

    #[test]
    fn three_distinct_failures_halve_once_and_a_threshold_of_zero_or_less_turns_the_circuit_off() {
        let l = limiter(8, 1);
        assert_eq!(l.on_failure(1.0, "a"), Ok(false));
        assert_eq!(l.on_failure(1.0, "a"), Ok(false), "the same task twice");
        assert_eq!(l.on_failure(1.0, "b"), Ok(false));
        assert_eq!(l.on_failure(1.0, "c"), Ok(true));
        assert_eq!(l.on_failure(1.0, "d"), Ok(false), "already tripped");
        assert_eq!(l.current(), Ok(4));
        for threshold in [0, -1] {
            let off = Limiter::new(8, 1, 10.0, 120.0, threshold).expect("limiter");
            for task in ["a", "b", "c", "d"] {
                assert_eq!(off.on_failure(1.0, task), Ok(false));
            }
            assert_eq!(off.current(), Ok(8));
        }
    }

    #[test]
    fn the_constructor_refuses_what_python_refuses() {
        for (max, min, cooldown, cap) in [
            (0, 1, 10.0, 120.0),
            (2, 3, 10.0, 120.0),
            (4, 0, 10.0, 120.0),
            (-1, -2, 10.0, 120.0),
            (4, 1, -1.0, 120.0),
            (4, 1, f64::NAN, 120.0),
            (4, 1, f64::INFINITY, 120.0),
            (4, 1, 10.0, 0.0),
            (4, 1, 10.0, f64::INFINITY),
            (4, 1, 10.0, f64::NAN),
            (4, 1, 2.0 * MAX_SECONDS, 120.0),
        ] {
            assert_input_error(Limiter::new(max, min, cooldown, cap, 3), "AimdLimiter");
        }
    }

    #[test]
    fn a_time_that_is_not_a_finite_number_of_seconds_in_range_is_an_input_error() {
        let l = limiter(8, 1);
        for bad in [
            -1.0,
            f64::NAN,
            f64::INFINITY,
            f64::NEG_INFINITY,
            2.0 * MAX_SECONDS,
        ] {
            assert_input_error(l.on_success(bad), "AimdLimiter.on_success");
            assert_input_error(l.on_rate_limit(bad, None), "AimdLimiter.on_rate_limit");
            assert_input_error(l.on_failure(bad, "t"), "AimdLimiter.on_failure");
            assert_input_error(l.on_burst_hit(bad, 1.0, None), "AimdLimiter.on_burst_hit");
            assert_input_error(l.on_burst_hit(1.0, bad, None), "AimdLimiter.on_burst_hit");
        }
        assert_eq!(l.current(), Ok(8), "a refused call changes nothing");
        assert_eq!(l.on_success(0.0), Ok(false), "zero is a valid time");
        assert_eq!(l.on_success(-0.0), Ok(false), "so is minus zero");
        assert_eq!(
            l.on_success(MAX_SECONDS),
            Ok(false),
            "and so is the largest one"
        );
    }

    #[test]
    fn backoff_forwards_the_delay_and_maps_the_refusals_to_input_errors() {
        assert_eq!(backoff_s(3, 0.1, 5.0, 0.0, 0.7), Ok(0.4));
        assert_eq!(backoff_s(3, 1.0, 100.0, 1.0, -1.0), Ok(0.0));
        assert_input_error(backoff_s(1, 0.1, 5.0, 1.5, 0.0), "sched_backoff_s");
        assert_input_error(backoff_s(1, 0.1, 5.0, f64::NAN, 0.0), "sched_backoff_s");
        assert_input_error(backoff_s(1, 0.0, 5.0, 0.0, 0.0), "sched_backoff_s");
        assert_input_error(backoff_s(1, 0.1, -5.0, 0.0, 0.0), "sched_backoff_s");
    }

    #[test]
    fn the_drain_plan_is_grace_then_kill_grace_and_a_nan_is_an_input_error() {
        // the defaults: cancel grace 10, budget 15, kill 5, stop 10, settle 3
        assert_eq!(drain_plan(10.0, 15.0, 5.0, 10.0, 3.0), Ok((7.0, 5.0)));
        assert_eq!(drain_plan(10.0, 2.0, 5.0, 10.0, 3.0), Ok((0.0, 0.5)));
        for slot in 0..5 {
            let mut args = [10.0, 15.0, 5.0, 10.0, 3.0];
            args[slot] = f64::NAN;
            assert_input_error(
                drain_plan(args[0], args[1], args[2], args[3], args[4]),
                "sched_drain_plan",
            );
        }
    }
}
