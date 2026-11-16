//! The cancel-drain schedule: how long to wait for sessions to leave on their own, and how long to give the kill.
//!
//! This is the arithmetic of `Scheduler._cancel_drain` in `scripts/swarm_run.py`, which stays the oracle. A cancel request starts a
//! shutdown that must fit in `shutdown_budget` (the supervisor's SIGTERM to SIGKILL window, counted from the request). The
//! cooperative wait ends early enough that the kill, its grace and the final settling still complete inside the budget:
//!
//! ```text
//! kill_grace = max(0.5, min(kill_grace_max, stop_grace, shutdown_budget - kill_settle))
//! grace      = max(0.0, min(cancel_grace, shutdown_budget - kill_grace - kill_settle))
//! ```
//!
//! With the defaults (grace 10, budget 15, kill grace 5, stop grace 10, settle 3) the cooperative wait is 7 seconds.
//!
//! The comparisons are Python's, so a NaN input behaves as it does there (`min` and `max` keep their first argument when the second
//! compares false); the run configuration refuses NaN before this module sees it.
//!
//! Threads and blocking: a pure function; it never blocks and holds no state.

/// `STOP_GRACE_S`: the grace a stopped or timed-out child process group gets between SIGTERM and SIGKILL. The kill grace never
/// exceeds it.
pub const STOP_GRACE_S: f64 = 10.0;

/// The smallest kill grace, in seconds, whatever the budget leaves.
pub const KILL_GRACE_FLOOR_S: f64 = 0.5;

/// The schedule of one cancel drain, in seconds.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DrainPlan {
    /// The cooperative wait: the time sessions get to leave on their own after the cancel flag is written. Zero when the budget
    /// leaves no room. The loop counts it from the moment of the cancel request.
    pub grace_s: f64,
    /// The grace the kill gives each session process group between SIGTERM and SIGKILL (the `kill` port's `grace_s`), at least
    /// [`KILL_GRACE_FLOOR_S`].
    pub kill_grace_s: f64,
    /// The time after the kill grace that the reaping of the sessions and the final records need: the input
    /// `kill_settle_s`, unchanged. The loop waits `kill_grace_s + settle_s` after the kill before it reports the stragglers.
    pub settle_s: f64,
}

/// Computes the drain schedule.
///
/// `cancel_grace_s` is `SWARM_CANCEL_GRACE_S`, `shutdown_budget_s` is `SWARM_SHUTDOWN_BUDGET_S`, `kill_grace_max_s` is
/// `KILL_GRACE_S` (5), `stop_grace_s` is [`STOP_GRACE_S`] and `kill_settle_s` is `KILL_SETTLE_S` (3). The result is never negative
/// for finite inputs, and `grace_s + kill_grace_s + settle_s` is at most `shutdown_budget_s` whenever the budget can hold the
/// floor kill grace and the settle time.
///
/// Pure and non-blocking; safe to call from any thread.
pub fn plan(
    cancel_grace_s: f64,
    shutdown_budget_s: f64,
    kill_grace_max_s: f64,
    stop_grace_s: f64,
    kill_settle_s: f64,
) -> DrainPlan {
    // Python `min(a, b, c)` keeps the first argument unless a later one is strictly smaller.
    let mut kill_grace = kill_grace_max_s;
    for bound in [stop_grace_s, shutdown_budget_s - kill_settle_s] {
        if bound < kill_grace {
            kill_grace = bound;
        }
    }
    // Python `max(0.5, kill_grace)`: the floor wins unless the value is strictly greater.
    let kill_grace_s = if kill_grace > KILL_GRACE_FLOOR_S {
        kill_grace
    } else {
        KILL_GRACE_FLOOR_S
    };
    let room = shutdown_budget_s - kill_grace_s - kill_settle_s;
    let grace = if room < cancel_grace_s {
        room
    } else {
        cancel_grace_s
    };
    DrainPlan {
        grace_s: if grace > 0.0 { grace } else { 0.0 },
        kill_grace_s,
        settle_s: kill_settle_s,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claims::vectors;

    #[test]
    fn the_defaults_leave_seven_seconds_of_cooperative_wait() {
        let plan = plan(10.0, 15.0, 5.0, STOP_GRACE_S, 3.0);
        assert_eq!(
            plan,
            DrainPlan {
                grace_s: 7.0,
                kill_grace_s: 5.0,
                settle_s: 3.0
            }
        );
    }

    #[test]
    fn the_grace_is_the_smaller_of_the_request_and_what_the_budget_leaves() {
        // (cancel_grace, budget, expected grace); kill grace 5 and settle 3 reserve 8 seconds.
        let cases = [
            (10.0, 30.0, 10.0),
            (10.0, 18.0, 10.0),
            (10.0, 17.5, 9.5),
            (10.0, 15.0, 7.0),
            (10.0, 8.0, 0.0),
            (10.0, 0.0, 0.0),
            (0.0, 30.0, 0.0),
            (2.5, 15.0, 2.5),
            (10.0, 1000.0, 10.0),
        ];
        for (cancel, budget, grace) in cases {
            let got = plan(cancel, budget, 5.0, STOP_GRACE_S, 3.0);
            assert_eq!(got.grace_s, grace, "cancel {cancel} budget {budget}");
        }
    }

    #[test]
    fn the_kill_grace_is_bounded_by_its_maximum_the_stop_grace_the_budget_and_the_floor() {
        // (budget, kill_max, stop, settle, expected kill grace)
        let cases = [
            (60.0, 30.0, 10.0, 3.0, 10.0), // the stop grace caps it
            (6.0, 5.0, 10.0, 3.0, 3.0),    // the budget minus the settle time caps it
            (60.0, 7.5, 10.0, 3.0, 7.5),   // its own maximum caps it
            (7.5, 5.0, 10.0, 3.0, 4.5),
            (60.0, 0.25, 10.0, 3.0, 0.5), // the floor lifts it
            (60.0, 5.0, 0.25, 3.0, 0.5),
            (3.0, 5.0, 10.0, 3.0, 0.5),
            (2.0, 5.0, 10.0, 3.0, 0.5),
            (0.0, 0.0, 0.0, 0.0, 0.5),
        ];
        for (budget, kill_max, stop, settle, expected) in cases {
            let got = plan(10.0, budget, kill_max, stop, settle);
            assert_eq!(
                got.kill_grace_s, expected,
                "budget {budget} kill_max {kill_max} stop {stop} settle {settle}"
            );
        }
    }

    #[test]
    fn the_settle_time_is_echoed_and_reserved_before_the_cooperative_wait() {
        let got = plan(10.0, 15.0, 5.0, STOP_GRACE_S, 10.0);
        assert_eq!(got.settle_s, 10.0);
        assert_eq!(got.grace_s, 0.0, "5 + 10 seconds of kill leave nothing");
        let got = plan(10.0, 15.0, 5.0, STOP_GRACE_S, 20.0);
        assert_eq!((got.grace_s, got.kill_grace_s), (0.0, 0.5));
    }

    #[test]
    fn a_whole_shutdown_fits_the_budget_when_the_budget_can_hold_the_kill() {
        for budget in [4.0, 8.0, 9.5, 15.0, 33.0, 120.0] {
            let got = plan(25.0, budget, 5.0, STOP_GRACE_S, 3.0);
            assert!(
                got.grace_s + got.kill_grace_s + got.settle_s <= budget,
                "budget {budget}: {got:?}"
            );
        }
    }

    #[test]
    fn nan_inputs_follow_python_min_and_max() {
        // NaN cancel grace: `min(nan, room)` keeps the NaN, `max(0.0, nan)` gives 0.0.
        assert_eq!(plan(f64::NAN, 15.0, 5.0, 10.0, 3.0).grace_s, 0.0);
        // NaN budget: no bound is smaller than the NaN-free maximum, and the room is NaN, so the grace is the request.
        let got = plan(10.0, f64::NAN, 5.0, 10.0, 3.0);
        assert_eq!((got.grace_s, got.kill_grace_s), (10.0, 5.0));
    }

    #[test]
    fn replays_the_python_drain_vectors_exactly() {
        let cases = vectors::load("drain_plan");
        for case in &cases {
            let input = &case.input;
            let got = plan(
                vectors::float(&input["cancel_grace_s"]),
                vectors::float(&input["shutdown_budget_s"]),
                vectors::float(&input["kill_grace_max_s"]),
                vectors::float(&input["stop_grace_s"]),
                vectors::float(&input["kill_settle_s"]),
            );
            assert_eq!(
                got.grace_s,
                vectors::float(&case.expected["grace"]),
                "{}: grace",
                case.id
            );
            assert_eq!(
                got.kill_grace_s,
                vectors::float(&case.expected["kill_grace"]),
                "{}: kill grace",
                case.id
            );
            assert_eq!(got.settle_s, vectors::float(&input["kill_settle_s"]));
        }
        assert!(cases.len() > 150, "the drain corpus shrank");
    }
}
