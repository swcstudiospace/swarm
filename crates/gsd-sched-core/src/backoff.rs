//! Retry backoff: the delay before retry number `attempt`, the arithmetic of `swarm/retry.py` `backoff_s`.
//!
//! The delay doubles with every attempt from `base_s`, is capped at `cap_s`, and is then scaled by `1 + jitter * unit` and clamped
//! to `[0, cap_s]` again. `unit` is one sample in `[-1, 1]` that the caller draws (the `random_unit` port, Python's
//! `random.uniform(-1, 1)`): this module owns no random generator, so a seeded sample makes every delay reproducible.
//!
//! The Python function is the oracle and the comparisons below are the ones Python makes, so a NaN behaves as it does there: the
//! checks `base_s <= 0` and `cap_s <= 0` let a NaN through, and `min`/`max` keep their first argument when the second compares
//! false. Environment variables (`SWARM_RETRY_BASE_S`, `SWARM_RETRY_CAP_S`) are resolved by the caller; this module sees numbers.
//!
//! Threads and blocking: a pure function of its arguments; it never blocks and holds no state.

use std::error::Error;
use std::fmt;

/// The largest exponent of the doubling: attempt 63 and above wait as long as attempt 63 (`2**62` times the base).
pub const MAX_EXPONENT: i64 = 62;

/// Why [`backoff_s`] refused its arguments (Python raises `ValueError` for both).
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum BackoffError {
    /// `jitter` is outside `[0, 1]` or NaN. Checked first, so it wins over [`BackoffError::BaseOrCapNotPositive`].
    JitterOutOfRange(f64),
    /// `base_s <= 0` or `cap_s <= 0`.
    BaseOrCapNotPositive,
}

impl fmt::Display for BackoffError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::JitterOutOfRange(jitter) => {
                write!(f, "jitter must be within [0, 1], got {jitter:?}")
            }
            Self::BaseOrCapNotPositive => f.write_str("base and cap must be > 0"),
        }
    }
}

impl Error for BackoffError {}

/// The delay in seconds before retry number `attempt` (1-based; zero and negative values count as 1).
///
/// `nominal = min(cap_s, base_s * 2^(min(max(attempt, 1) - 1, 62)))`. With `jitter == 0` that is the result and `unit` is ignored
/// (even a NaN). Otherwise the result is `max(0, min(cap_s, nominal * (1 + unit * jitter)))`: a `unit` of `-1` with `jitter` 1
/// gives 0, and a delay pushed over the cap by a positive `unit` is clamped to `cap_s`. `unit` is not clamped: a sample outside
/// `[-1, 1]` is the caller's fault and is used as given.
///
/// # Errors
///
/// [`BackoffError::JitterOutOfRange`] when `jitter` is not within `[0, 1]`, else [`BackoffError::BaseOrCapNotPositive`] when
/// `base_s` or `cap_s` is zero or negative.
///
/// Pure and non-blocking; safe to call from any thread.
pub fn backoff_s(
    attempt: i64,
    base_s: f64,
    cap_s: f64,
    jitter: f64,
    unit: f64,
) -> Result<f64, BackoffError> {
    if !(0.0..=1.0).contains(&jitter) {
        return Err(BackoffError::JitterOutOfRange(jitter));
    }
    if base_s <= 0.0 || cap_s <= 0.0 {
        return Err(BackoffError::BaseOrCapNotPositive);
    }
    let exponent = attempt.max(1).saturating_sub(1).min(MAX_EXPONENT);
    // `exponent` is in 0..=62, so the shift is exact and a power of two converts to f64 without rounding.
    let scaled = base_s * (1_u64 << exponent) as f64;
    // Python `min(cap_s, scaled)`: the cap wins unless `scaled` is strictly smaller, so a NaN `scaled` gives the cap.
    let nominal = if scaled < cap_s { scaled } else { cap_s };
    if jitter == 0.0 {
        return Ok(nominal);
    }
    let jittered = nominal * (1.0 + unit * jitter);
    let capped = if jittered < cap_s { jittered } else { cap_s };
    // Python `max(0.0, capped)`: zero wins unless `capped` is strictly greater (so -0.0 gives 0.0).
    Ok(if capped > 0.0 { capped } else { 0.0 })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claims::vectors;

    fn same(a: f64, b: f64) -> bool {
        a.to_bits() == b.to_bits() || (a.is_nan() && b.is_nan())
    }

    #[test]
    fn delays_double_per_attempt_and_stop_at_the_cap() {
        // (attempt, base, cap, expected)
        let cases = [
            (1, 0.1, 5.0, 0.1),
            (2, 0.1, 5.0, 0.2),
            (3, 0.1, 5.0, 0.4),
            (6, 0.1, 5.0, 3.2),
            (7, 0.1, 5.0, 5.0),
            (50, 0.1, 5.0, 5.0),
            (5, 2.0, 2.0, 2.0),
            (1, 10.0, 1.0, 1.0),
            (3, 1e-300, 5.0, 4e-300),
        ];
        for (attempt, base, cap, expected) in cases {
            assert_eq!(
                backoff_s(attempt, base, cap, 0.0, 0.7),
                Ok(expected),
                "attempt {attempt} base {base} cap {cap}"
            );
        }
    }

    #[test]
    fn an_attempt_below_one_is_the_first_attempt() {
        for attempt in [0, -1, -100, i64::MIN] {
            assert_eq!(backoff_s(attempt, 0.1, 5.0, 0.0, 0.0), Ok(0.1));
        }
    }

    #[test]
    fn the_exponent_stops_growing_at_62() {
        // With a huge cap the delay is base * 2^exponent: attempt 63 is the last one that grows.
        let cap = 1.0e30;
        assert_eq!(backoff_s(62, 1.0, cap, 0.0, 0.0), Ok((1_u64 << 61) as f64));
        assert_eq!(backoff_s(63, 1.0, cap, 0.0, 0.0), Ok((1_u64 << 62) as f64));
        for attempt in [64, 65, 1000, i64::from(i32::MAX), i64::MAX] {
            assert_eq!(
                backoff_s(attempt, 1.0, cap, 0.0, 0.0),
                Ok((1_u64 << 62) as f64),
                "attempt {attempt}"
            );
        }
    }

    #[test]
    fn a_product_that_overflows_to_infinity_is_capped() {
        assert_eq!(backoff_s(1, 1e300, 5.0, 0.0, 0.0), Ok(5.0));
        assert_eq!(backoff_s(62, 1e300, 1e308, 0.0, 0.0), Ok(1e308));
    }

    #[test]
    fn jitter_scales_the_delay_by_one_plus_unit_times_jitter() {
        // (attempt, base, cap, jitter, unit, expected); nominal at attempt 3 is 4 * base.
        let cases = [
            (3, 1.0, 100.0, 0.25, 0.0, 4.0),
            (3, 1.0, 100.0, 0.25, 1.0, 5.0),
            (3, 1.0, 100.0, 0.25, -1.0, 3.0),
            (3, 2.0, 100.0, 0.5, 0.25, 9.0),
            (1, 0.1, 5.0, 0.25, 0.5, 0.1125),
            // zero jitter ignores the sample
            (3, 1.0, 100.0, 0.0, 0.9, 4.0),
            // a positive sample pushes over the cap: clamped to it
            (2, 1.0, 3.0, 1.0, 1.0, 3.0),
            (6, 0.1, 5.0, 0.25, 1.0, 4.0),
            // full jitter at -1 gives 0, and a sample below -1 would go negative: clamped to 0
            (3, 1.0, 100.0, 1.0, -1.0, 0.0),
            (2, 1.0, 100.0, 1.0, -2.0, 0.0),
            // a sample above 1 is used as given
            (2, 1.0, 100.0, 1.0, 2.0, 6.0),
        ];
        for (attempt, base, cap, jitter, unit, expected) in cases {
            assert_eq!(
                backoff_s(attempt, base, cap, jitter, unit),
                Ok(expected),
                "attempt {attempt} base {base} jitter {jitter} unit {unit}"
            );
        }
    }

    #[test]
    fn a_nan_sample_follows_python_min_and_max() {
        // `min(cap, nan)` is the cap, so a jittered delay with a NaN sample waits the whole cap ...
        assert_eq!(backoff_s(2, 1.0, 100.0, 0.5, f64::NAN), Ok(100.0));
        // ... and without jitter the sample is never read.
        assert_eq!(backoff_s(2, 1.0, 100.0, 0.0, f64::NAN), Ok(2.0));
    }

    #[test]
    fn bad_jitter_is_refused_before_bad_base_or_cap() {
        for jitter in [
            -0.1,
            1.000_000_1,
            2.0,
            f64::NAN,
            f64::INFINITY,
            f64::NEG_INFINITY,
        ] {
            assert!(
                matches!(
                    backoff_s(1, 0.1, 5.0, jitter, 0.0),
                    Err(BackoffError::JitterOutOfRange(_))
                ),
                "jitter {jitter}"
            );
        }
        assert!(matches!(
            backoff_s(1, -1.0, 5.0, 3.0, 0.0),
            Err(BackoffError::JitterOutOfRange(j)) if j == 3.0
        ));
        // both ends of the valid range are valid
        assert_eq!(backoff_s(1, 0.1, 5.0, 0.0, 0.5), Ok(0.1));
        assert_eq!(
            backoff_s(1, 0.1, 5.0, 1.0, 0.5),
            Ok(0.150_000_000_000_000_02)
        );
    }

    #[test]
    fn a_zero_or_negative_base_or_cap_is_refused() {
        for (base, cap) in [
            (0.0, 5.0),
            (-1.0, 5.0),
            (0.1, 0.0),
            (0.1, -5.0),
            (0.0, 0.0),
            (f64::NEG_INFINITY, 5.0),
        ] {
            assert_eq!(
                backoff_s(1, base, cap, 0.0, 0.0),
                Err(BackoffError::BaseOrCapNotPositive),
                "base {base} cap {cap}"
            );
        }
    }

    #[test]
    fn a_nan_base_passes_the_check_and_waits_the_cap_as_python_does() {
        assert_eq!(backoff_s(3, f64::NAN, 5.0, 0.0, 0.0), Ok(5.0));
    }

    #[test]
    fn errors_name_the_offending_rule() {
        assert_eq!(
            BackoffError::JitterOutOfRange(2.0).to_string(),
            "jitter must be within [0, 1], got 2.0"
        );
        assert_eq!(
            BackoffError::BaseOrCapNotPositive.to_string(),
            "base and cap must be > 0"
        );
    }

    #[test]
    fn replays_the_python_backoff_vectors_exactly() {
        let cases = vectors::load("backoff");
        for case in &cases {
            let input = &case.input;
            let attempt = input["attempt"].as_i64().expect("attempt is an integer");
            let result = backoff_s(
                attempt,
                vectors::float(&input["base_s"]),
                vectors::float(&input["cap_s"]),
                vectors::float(&input["jitter"]),
                vectors::float(&input["unit"]),
            );
            match (&case.expected["ok"], &case.expected["error"], result) {
                (ok, _, Ok(got)) if !ok.is_null() => {
                    let want = vectors::float(ok);
                    assert!(same(got, want), "{}: got {got:?}, want {want:?}", case.id);
                }
                (_, error, Err(_)) if error == "ValueError" => {}
                (_, _, result) => panic!(
                    "{}: result {result:?} vs expected {}",
                    case.id, case.expected
                ),
            }
        }
        assert!(cases.len() > 200, "the backoff corpus shrank");
    }
}
