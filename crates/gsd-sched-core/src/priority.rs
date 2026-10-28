//! Dispatch order: the sort of `order_ready` in `scripts/swarm_run.py`.
//!
//! Ready tasks are dispatched by priority (`P0` first), then by DAG depth (the shallowest first), then by creation time (the oldest
//! first); tasks that tie on all three keep their input order. The store's `priority` column is unvalidated text, so a missing,
//! unknown or garbled value is possible: Python ranks every such value as `P2` (`PRIORITY_RANK.get(priority, PRIORITY_RANK["P2"])`)
//! and so does [`rank_or_p2`]. The strict wire parse of the proto dispatcher (which refuses an unknown priority) is a different
//! path and is not used here. A missing `dag_depth` or `created_at` ranks as 0, as Python's `value or 0` does.
//!
//! Threads and blocking: pure functions over plain data; they never block and hold no state.

/// The rank of a priority text: `P0` is 0, `P1` is 1, `P2` is 2 and `P3` is 3. Anything else, including `None`, the empty text,
/// a lower-case `p0`, `P4`, `P02` or a padded `" P0"`, is `P2` (rank 2): the match is exact and case-sensitive, as Python's
/// dictionary lookup is.
///
/// Lower ranks dispatch first.
pub fn rank_or_p2(priority: Option<&str>) -> u8 {
    match priority {
        Some("P0") => 0,
        Some("P1") => 1,
        Some("P2") => 2,
        Some("P3") => 3,
        _ => 2,
    }
}

/// The dispatch order of `rows`, as a permutation: the result lists the indices of `rows` in the order the tasks are dispatched.
///
/// Each row is `(priority, dag_depth, created_at)`, the three sort columns of a ready task. The order is by [`rank_or_p2`]
/// ascending, then `dag_depth` ascending (`None` is 0), then `created_at` ascending (`None`, `0.0` and `-0.0` are all 0). The sort
/// is stable: rows equal on all three keys keep their input order. `created_at` compares by the IEEE total order, so an infinity
/// sorts at the ends as it does in Python; a NaN, whose Python position depends on the input order, sorts after every number
/// (this case is outside the oracle's corpus).
///
/// Allocates one key vector and sorts in `O(n log n)`; safe to call from any thread.
pub fn order_ready<'a>(
    rows: impl IntoIterator<Item = (Option<&'a str>, Option<i64>, Option<f64>)>,
) -> Vec<usize> {
    let mut keys: Vec<(u8, i64, f64, usize)> = rows
        .into_iter()
        .enumerate()
        .map(|(index, (priority, depth, created))| {
            // Python `created_at or 0`: a zero of either sign is the integer 0, so the signs must not order.
            let created = match created {
                Some(seconds) if seconds != 0.0 => seconds,
                _ => 0.0,
            };
            (rank_or_p2(priority), depth.unwrap_or(0), created, index)
        })
        .collect();
    keys.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| a.1.cmp(&b.1))
            .then_with(|| a.2.total_cmp(&b.2))
    });
    keys.into_iter().map(|key| key.3).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claims::vectors;
    use crate::jsonlite::Value;

    type Row<'a> = (Option<&'a str>, Option<i64>, Option<f64>);

    fn order(rows: &[Row<'_>]) -> Vec<usize> {
        order_ready(rows.iter().copied())
    }

    #[test]
    fn the_four_priorities_rank_zero_to_three() {
        for (text, rank) in [("P0", 0), ("P1", 1), ("P2", 2), ("P3", 3)] {
            assert_eq!(rank_or_p2(Some(text)), rank, "{text}");
        }
    }

    #[test]
    fn a_missing_unknown_or_garbled_priority_is_p2() {
        for text in [
            "",
            "P4",
            "P9",
            "P10",
            "P-1",
            "p0",
            "p2",
            "p3",
            " P0",
            "P0 ",
            "P2\n",
            "P02",
            "0",
            "2",
            "high",
            "PO",
            "\u{ff30}\u{ff12}",
            "P\u{b2}",
            "\u{e9}",
        ] {
            assert_eq!(rank_or_p2(Some(text)), 2, "{text:?}");
        }
        assert_eq!(rank_or_p2(None), 2);
    }

    #[test]
    fn priority_beats_depth_and_depth_beats_creation_time() {
        assert_eq!(
            order(&[
                (Some("P1"), Some(0), Some(0.0)),
                (Some("P0"), Some(9), Some(99.0))
            ]),
            [1, 0]
        );
        assert_eq!(
            order(&[
                (Some("P1"), Some(2), Some(1.0)),
                (Some("P1"), Some(1), Some(50.0)),
                (Some("P1"), Some(0), Some(99.0)),
            ]),
            [2, 1, 0]
        );
        assert_eq!(
            order(&[
                (Some("P1"), Some(1), Some(30.5)),
                (Some("P1"), Some(1), Some(10.25)),
                (Some("P1"), Some(1), Some(20.0)),
            ]),
            [1, 2, 0]
        );
    }

    #[test]
    fn an_unknown_priority_ties_with_p2_and_keeps_the_input_order() {
        let rows = [
            (Some("zz"), Some(0), Some(0.0)),
            (Some("P2"), Some(0), Some(0.0)),
            (None, Some(0), Some(0.0)),
            (Some("P3"), Some(0), Some(0.0)),
            (Some("P1"), Some(0), Some(0.0)),
            (Some("p0"), Some(0), Some(0.0)),
        ];
        assert_eq!(order(&rows), [4, 0, 1, 2, 5, 3]);
    }

    #[test]
    fn a_missing_depth_or_creation_time_ranks_as_zero() {
        let rows = [
            (Some("P1"), None, None),
            (Some("P1"), Some(0), Some(0.0)),
            (Some("P1"), Some(0), Some(-1.0)),
            (Some("P1"), Some(-1), Some(5.0)),
            (Some("P1"), Some(0), Some(-0.0)),
        ];
        assert_eq!(order(&rows), [3, 2, 0, 1, 4]);
    }

    #[test]
    fn infinite_creation_times_sort_to_the_ends() {
        let rows = [
            (Some("P1"), Some(0), Some(f64::INFINITY)),
            (Some("P1"), Some(0), Some(1e18)),
            (Some("P1"), Some(0), Some(f64::NEG_INFINITY)),
        ];
        assert_eq!(order(&rows), [2, 1, 0]);
    }

    #[test]
    fn equal_keys_keep_the_input_order_for_any_batch_size() {
        let rows = vec![(Some("P2"), Some(1), Some(5.0)); 200];
        assert_eq!(order(&rows), (0..200).collect::<Vec<_>>());
    }

    #[test]
    fn ties_keep_the_input_order_in_a_large_shuffled_batch() {
        // 2000 rows over a small key domain, so each of the 36 distinct keys repeats about 55 times and an unstable sort would reorder some of them.
        const PRIORITIES: [Option<&str>; 6] = [
            Some("P0"),
            Some("P1"),
            Some("P2"),
            Some("P3"),
            None,
            Some("junk"),
        ];
        let mut state: u64 = 0x2545_f491_4f6c_dd1d;
        let mut next = || {
            state = state
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            (state >> 33) as usize
        };
        let rows: Vec<Row<'_>> = (0..2000)
            .map(|_| {
                (
                    PRIORITIES[next() % 6],
                    Some((next() % 3) as i64),
                    Some((next() % 3) as f64),
                )
            })
            .collect();
        // the expected order: sort the indices by (rank, depth, created, index), the index being the explicit stability rule
        let mut want: Vec<usize> = (0..rows.len()).collect();
        want.sort_by(|&a, &b| {
            let key = |i: usize| {
                (
                    rank_or_p2(rows[i].0),
                    rows[i].1,
                    rows[i].2.map(|seconds| seconds as i64),
                    i,
                )
            };
            key(a).cmp(&key(b))
        });
        assert_eq!(order(&rows), want);
    }

    #[test]
    fn an_empty_batch_gives_an_empty_order() {
        assert_eq!(order(&[]), Vec::<usize>::new());
    }

    fn row_from_json(row: &Value) -> Row<'_> {
        let created = match &row[2] {
            Value::Null => None,
            other => Some(vectors::float(other)),
        };
        (row[0].as_str(), row[1].as_i64(), created)
    }

    #[test]
    fn replays_the_python_order_ready_vectors_exactly() {
        let cases = vectors::load("order_ready");
        for case in &cases {
            let rows: Vec<Row<'_>> = case.input["rows"]
                .as_array()
                .expect("rows is a list")
                .iter()
                .map(row_from_json)
                .collect();
            let want: Vec<usize> = case
                .expected
                .as_array()
                .expect("expected is a list")
                .iter()
                .map(|index| usize::try_from(index.as_u64().expect("index")).expect("index"))
                .collect();
            assert_eq!(order(&rows), want, "{}", case.id);
        }
        assert!(cases.len() > 100, "the order_ready corpus shrank");
    }

    #[test]
    fn replays_the_python_priority_fallback_vectors_exactly() {
        let probe = |priority: Option<&str>| {
            // The oracle's probe: one row of each rank, then the row under test, all at depth 0 and time 0.
            order_ready([
                (Some("P0"), Some(0), Some(0.0)),
                (Some("P1"), Some(0), Some(0.0)),
                (Some("P2"), Some(0), Some(0.0)),
                (Some("P3"), Some(0), Some(0.0)),
                (priority, Some(0), Some(0.0)),
            ])
        };
        let cases = vectors::load("priority_fallback");
        for case in &cases {
            let priority = case.input["priority"].as_str();
            let want: Vec<usize> = case.expected["probe"]
                .as_array()
                .expect("probe is a list")
                .iter()
                .map(|index| usize::try_from(index.as_u64().expect("index")).expect("index"))
                .collect();
            assert_eq!(probe(priority), want, "{}", case.id);
            assert_eq!(
                i64::from(rank_or_p2(priority)),
                case.expected["rank"].as_i64().expect("rank"),
                "{}",
                case.id
            );
        }
        assert!(cases.len() >= 25, "the priority_fallback corpus shrank");
    }
}
