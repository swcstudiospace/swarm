//! Test-only support shared by the scaling tests of every module: one way to time a piece of work that a busy host does not distort.
//!
//! A scaling test compares the time of an input as a whole with the time of the same input in pieces. On a shared CI pool or a host with a load average of 45 a wall clock grows by whatever the neighbours take, so every module measures the CPU time of its own thread instead ([`measure`]) and keeps the least disturbed of several runs ([`best_of`]). The scheduler accounts that time in clock ticks (about a millisecond), so [`assert_scales`] also repeats work that is shorter than that until one timing spans [`FLOOR`].

use std::time::{Duration, Instant};

/// CPU time this thread has used, from the Linux scheduler's accounting (`/proc/thread-self/schedstat`, nanoseconds on the CPU), or `None` where that file is missing or unreadable. Unlike a wall clock it does not grow when a busy host preempts the thread, which is what made timings of a 1 MB scan on a shared CI pool unreliable.
pub(crate) fn cpu_time() -> Option<Duration> {
    let text = std::fs::read_to_string("/proc/thread-self/schedstat").ok()?;
    let ns: u64 = text.split_whitespace().next()?.parse().ok()?;
    Some(Duration::from_nanos(ns))
}

/// How long `f` takes: its thread's CPU time when the scheduler reports it, else wall time.
pub(crate) fn measure(f: &mut impl FnMut()) -> Duration {
    let wall = Instant::now();
    let cpu = cpu_time();
    f();
    match (cpu, cpu_time()) {
        (Some(before), Some(after)) => after.saturating_sub(before),
        _ => wall.elapsed(),
    }
}

/// The minimum of `runs` runs of `f`: load spikes only ever add time, so the minimum is the run least disturbed by a noisy neighbour (a mean or median is not). The loop stops early once a run is at most `enough`: the minimum can only get smaller, so a verdict "at most `enough`" is already settled.
pub(crate) fn best_of(runs: usize, enough: Duration, mut f: impl FnMut()) -> Duration {
    let mut best = Duration::MAX;
    for _ in 0..runs {
        best = best.min(measure(&mut f));
        if best <= enough {
            break;
        }
    }
    best
}

/// The shortest time one timing of [`assert_scales`] may span. The scheduler's CPU accounting ticks about once a millisecond (a shorter run reads as 0 or 1 ms), so work shorter than this is repeated until a timing spans it, which keeps that granularity under a tenth of the measurement.
pub(crate) const FLOOR: Duration = Duration::from_millis(10);

/// Runs per timing of the parts.
const PARTS_RUNS: usize = 3;
/// Runs the whole input gets: it can only stop early on success, so linear work on a loaded host keeps looking for one undisturbed run while quadratic work fails every one.
const WHOLE_RUNS: usize = 15;
/// CPU time after which the whole is not run again: a linear whole takes tens of milliseconds, so the budget only ends the retries of one that is hopelessly slow.
const RETRY_BUDGET: Duration = Duration::from_secs(3);

/// Asserts that `whole` costs at most `limit` times what `parts` costs, both as CPU time.
///
/// `whole` makes one complete pass over an input; `parts` makes one pass over the same memory cut into pieces (16 is a good number) and processes each piece on its own. Linear work takes about as long either way, work that is quadratic in the input takes as many times longer on the whole as there are pieces. Comparing the times of two freshly built inputs of different sizes instead mixes in cache effects and the speed the host happens to give each run (this host's CPU time per character varied by a factor of two between identical runs); pieces of the same memory cancel the cache part and the minimum of several runs most of the rest. Pick `limit` between 1 and the number of pieces (their geometric mean, 4 for 16 pieces, leaves the same distance to both).
///
/// Both closures are repeated as many times as it takes for the parts to span [`FLOOR`]. Each timing is the minimum of several runs ([`best_of`]); the whole stops at the first run within the limit, and the parts count as at least [`FLOOR`] so that a tick of rounding cannot shrink the limit. Once the runs of the whole have used up [`RETRY_BUDGET`] it is not run again: a linear scan finishes a run in milliseconds, so retries are only ever spent on a quadratic one, which would take minutes to fail.
pub(crate) fn assert_scales(
    label: &str,
    limit: u32,
    mut parts: impl FnMut(),
    mut whole: impl FnMut(),
) {
    // Without thread CPU accounting `measure` falls back to wall time, which counts a busy host's neighbours. The scaling
    // claim cannot be measured then, so the assertion is skipped.
    if cpu_time().is_none() {
        return;
    }
    // Double the repetitions until even the less disturbed of two timings spans the floor.
    let mut reps = 1u32;
    while reps < 1 << 24 {
        let mut batch = || (0..reps).for_each(|_| parts());
        if measure(&mut batch).min(measure(&mut batch)) >= FLOOR {
            break;
        }
        reps *= 2;
    }
    let t_parts = best_of(PARTS_RUNS, Duration::ZERO, || {
        (0..reps).for_each(|_| parts())
    })
    .max(FLOOR);
    let mut t_whole = Duration::MAX;
    let mut spent = Duration::ZERO;
    for _ in 0..WHOLE_RUNS {
        let run = measure(&mut || (0..reps).for_each(|_| whole()));
        t_whole = t_whole.min(run);
        spent += run;
        if t_whole <= limit * t_parts || spent > RETRY_BUDGET {
            break;
        }
    }
    assert!(
        t_whole <= limit * t_parts,
        "{label}: {t_parts:?} in pieces, {t_whole:?} as a whole (x{reps} passes each), over the limit of {limit}x"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Busy work of `n` steps that the optimizer cannot remove (about 0.3 ms per million).
    fn spin(n: u64) {
        for i in 0..n {
            std::hint::black_box(i);
        }
    }

    #[test]
    #[cfg_attr(
        not(target_os = "linux"),
        ignore = "needs Linux CPU accounting (/proc/thread-self/schedstat); no CPU-time seam off Linux"
    )]
    fn measure_counts_cpu_time_not_waiting() {
        let slept = best_of(5, Duration::from_millis(30), || {
            std::thread::sleep(Duration::from_millis(60))
        });
        assert!(
            slept <= Duration::from_millis(30),
            "sleep counted: {slept:?}"
        );
        let worked = measure(&mut || spin(30_000_000));
        assert!(worked > Duration::ZERO, "busy work measured as nothing");
    }

    #[test]
    fn best_of_returns_the_least_disturbed_run() {
        // heavy, light, heavy: the minimum is the light run, far below a heavy one
        let heavy = measure(&mut || spin(30_000_000));
        let mut work = [30_000_000, 100_000, 30_000_000].into_iter();
        let best = best_of(3, Duration::ZERO, || spin(work.next().unwrap_or(0)));
        assert!(
            best * 4 < heavy,
            "best {best:?} against a heavy run {heavy:?}"
        );
    }

    #[test]
    fn best_of_stops_at_the_first_run_within_the_bound() {
        let mut calls = 0;
        let best = best_of(10, Duration::MAX, || calls += 1);
        assert_eq!(calls, 1);
        assert!(best < Duration::MAX);
        // a bound no run can meet uses every run
        let mut calls = 0;
        best_of(4, Duration::ZERO, || {
            calls += 1;
            spin(30_000_000);
        });
        assert_eq!(calls, 4);
    }

    #[test]
    fn assert_scales_accepts_work_that_costs_the_same_whole_or_in_pieces() {
        // 1M steps is a fraction of a scheduler tick: only the repetition makes it measurable
        let parts = || (0..16).for_each(|_| spin(1_000_000));
        assert_scales("linear", 4, parts, || spin(16_000_000));
    }

    #[test]
    #[should_panic(expected = "over the limit of 4x")]
    fn assert_scales_rejects_a_whole_that_costs_too_much() {
        // 16 pieces against the 16 times larger whole of quadratic work, which costs 16 times as much
        let parts = || (0..16).for_each(|_| spin(1_000_000));
        assert_scales("quadratic", 4, parts, || spin(256_000_000));
    }
}
