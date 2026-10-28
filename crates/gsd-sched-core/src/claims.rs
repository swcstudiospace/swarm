//! The claim table: which in-flight task may touch which repo paths.
//!
//! [`ClaimTable`] is the Rust form of `swarm/claims.py` `ClaimTable`, which stays the oracle. Each owner (a task id) holds a list
//! of claims; [`ClaimTable::acquire`] takes all of an owner's requested claims or none of them. Two claims conflict when they can
//! match a common path and at least one is exclusive (see [`claims_conflict`]): an exclusive bare name such as `src/tools` also
//! reserves everything below it, a shared bare name is just that path, and two shared claims never conflict.
//!
//! Rules, each one covered by a test:
//!
//! * **All or nothing.** If any requested claim is invalid or conflicts, the table is unchanged.
//! * **No self-conflict.** An owner's requested claims are only checked against the claims of *other* owners; asking again for a
//!   claim the owner already holds changes nothing.
//! * **Conflict report order** (the order Python lists them in): requested claims in request order, for each of them the holders in
//!   the order they first acquired, for each holder its claims in acquisition order. The first entry is the one the scheduler
//!   names in its skip reason `claim-conflict:<holder>`.
//! * **Release** drops everything an owner holds, is idempotent, and returns how many claims it dropped. An owner that acquires
//!   again afterwards counts as acquiring for the first time (it moves to the end of the holder order).
//! * **Normalized patterns.** Patterns are normalized as `swarm.claims.normalize_pattern` does (`src/` is `src/**`, `./a//b` is
//!   `a/b`); the table stores and reports the normalized form.
//!
//! Threads and blocking: the table is a plain value with no interior mutability. Mutating calls take `&mut self`, so one thread
//! owns it (the scheduler loop does); share it across threads behind a `Mutex` if needed. Nothing here blocks or does I/O.
//! A call costs `O(requested x held claims)` conflict tests, each a small dynamic program over path segments.

use std::error::Error;
use std::fmt;

use crate::claim_pattern::{claims_conflict, footprints, ClaimError, ClaimMode, PathClaim};

/// A requested claim that collides with a claim another owner holds: the three-tuple Python reports as
/// `(owner, held.pattern, requested.pattern)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClaimConflict {
    /// The owner that holds the colliding claim.
    pub holder: String,
    /// The holder's claim pattern, normalized.
    pub held: String,
    /// The requested claim pattern, normalized.
    pub requested: String,
}

/// Why [`ClaimTable::acquire`] changed nothing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AcquireError {
    /// A requested pattern was empty, absolute, drive-qualified, `~`-prefixed or contained `..` (Python raises `ValueError`).
    /// Reported for the first invalid pattern, before any conflict is looked for.
    InvalidPattern(ClaimError),
    /// At least one requested claim collides with another owner's claim. Never empty; see the module docs for the order.
    Conflicts(Vec<ClaimConflict>),
}

impl fmt::Display for AcquireError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidPattern(error) => error.fmt(f),
            Self::Conflicts(conflicts) => match conflicts.first() {
                Some(first) => write!(
                    f,
                    "claim {:?} conflicts with {:?} held by {} ({} conflict(s))",
                    first.requested,
                    first.held,
                    first.holder,
                    conflicts.len()
                ),
                None => f.write_str("claim conflict"),
            },
        }
    }
}

impl Error for AcquireError {}

/// One claim an owner holds: the normalized pattern and its mode.
#[derive(Debug, Clone)]
pub struct HeldClaim {
    /// The normalized pattern (`src/**`, not `src/`).
    pub pattern: String,
    /// Whether the claim is exclusive or shared.
    pub mode: ClaimMode,
    claim: PathClaim,
}

#[derive(Debug, Clone)]
struct Owner {
    name: String,
    claims: Vec<HeldClaim>,
}

/// The claims of every in-flight owner. See the module docs for the rules.
#[derive(Debug, Clone, Default)]
pub struct ClaimTable {
    /// In order of first acquisition; an owner with no claims has no entry.
    owners: Vec<Owner>,
}

/// The canonical form of a claim pattern, as `swarm.claims.normalize_pattern` returns it: trimmed, `\` read as `/`, `.` and empty
/// segments dropped, repeated `**` collapsed, and a trailing `/` turned into `/**`.
///
/// # Errors
///
/// [`ClaimError`] for an empty, absolute (`/x`), drive-qualified (`C:x`), `~`-prefixed or `..`-containing pattern.
pub fn normalize_pattern(pattern: &str) -> Result<String, ClaimError> {
    let claim = PathClaim::new(pattern, ClaimMode::Exclusive)?;
    // `footprints` always lists the claim's own segments first, so index 0 exists and is the normalized pattern.
    Ok(footprints(&claim)[0].join("/"))
}

impl ClaimTable {
    /// An empty table.
    pub fn new() -> Self {
        Self::default()
    }

    /// Takes every claim in `claims` for `owner`, or none of them.
    ///
    /// Each item is a pattern and its mode. Returns `Ok(())` when no claim collides with a claim of a *different* owner; the
    /// claims are then recorded (a claim the owner already holds, same normalized pattern and same mode, is not recorded twice,
    /// and an empty request records nothing). An owner never conflicts with itself.
    ///
    /// # Errors
    ///
    /// [`AcquireError::InvalidPattern`] for the first invalid pattern, else [`AcquireError::Conflicts`] listing every collision in
    /// the order documented on the module. The table is unchanged in both cases.
    pub fn acquire<S: AsRef<str>>(
        &mut self,
        owner: &str,
        claims: &[(S, ClaimMode)],
    ) -> Result<(), AcquireError> {
        let mut wanted: Vec<HeldClaim> = Vec::with_capacity(claims.len());
        for (pattern, mode) in claims {
            let claim =
                PathClaim::new(pattern.as_ref(), *mode).map_err(AcquireError::InvalidPattern)?;
            wanted.push(HeldClaim {
                // see `normalize_pattern`: index 0 of `footprints` is the claim's own pattern
                pattern: footprints(&claim)[0].join("/"),
                mode: *mode,
                claim,
            });
        }
        let mut conflicts = Vec::new();
        for want in &wanted {
            for other in self.owners.iter().filter(|other| other.name != owner) {
                for held in &other.claims {
                    if claims_conflict(&want.claim, &held.claim) {
                        conflicts.push(ClaimConflict {
                            holder: other.name.clone(),
                            held: held.pattern.clone(),
                            requested: want.pattern.clone(),
                        });
                    }
                }
            }
        }
        if !conflicts.is_empty() {
            return Err(AcquireError::Conflicts(conflicts));
        }
        if wanted.is_empty() {
            return Ok(());
        }
        let index = match self.owners.iter().position(|held| held.name == owner) {
            Some(index) => index,
            None => {
                self.owners.push(Owner {
                    name: owner.to_string(),
                    claims: Vec::new(),
                });
                self.owners.len() - 1
            }
        };
        let mine = &mut self.owners[index].claims;
        for want in wanted {
            if !mine
                .iter()
                .any(|held| held.pattern == want.pattern && held.mode == want.mode)
            {
                mine.push(want);
            }
        }
        Ok(())
    }

    /// Drops everything `owner` holds and returns how many claims that was. Releasing an owner that holds nothing (or never held
    /// anything) returns 0 and changes nothing, so a second release is harmless.
    pub fn release(&mut self, owner: &str) -> usize {
        match self.owners.iter().position(|held| held.name == owner) {
            Some(index) => self.owners.remove(index).claims.len(),
            None => 0,
        }
    }

    /// Every owner with its claims, owners in the order they first acquired and claims in acquisition order. Owners that hold
    /// nothing are absent.
    pub fn held(&self) -> impl Iterator<Item = (&str, &[HeldClaim])> {
        self.owners
            .iter()
            .map(|owner| (owner.name.as_str(), owner.claims.as_slice()))
    }
}

/// The shared reader of the committed oracle vectors (`tests/parity/fixtures/scheduler/*.json`) for the replay tests of this crate.
#[cfg(test)]
pub(crate) mod vectors {
    use crate::jsonlite::{parse, Value};

    /// One vector: `input` and the Python `expected` result.
    pub(crate) struct Case {
        pub(crate) id: String,
        pub(crate) input: Value,
        pub(crate) expected: Value,
    }

    /// Every case of `<behaviour>.json`. Panics (fails the test) when the file is missing or malformed: a replay never skips.
    pub(crate) fn load(behaviour: &str) -> Vec<Case> {
        let path = format!(
            "{}/../../tests/parity/fixtures/scheduler/{behaviour}.json",
            env!("CARGO_MANIFEST_DIR")
        );
        let text = std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{path}: {error}"));
        let file = parse(&text).unwrap_or_else(|error| panic!("{path}: {error}"));
        assert_eq!(file["behaviour"], behaviour, "{path}");
        assert_eq!(file["schema"], 1, "{path}");
        let cases: Vec<Case> = file["cases"]
            .as_array()
            .unwrap_or_else(|| panic!("{path}: no cases"))
            .iter()
            .map(|case| Case {
                id: case["id"].as_str().unwrap_or("?").to_string(),
                input: case["input"].clone(),
                expected: case["expected"].clone(),
            })
            .collect();
        assert!(!cases.is_empty(), "{path}: no cases");
        cases
    }

    /// A vector number: a JSON number, or the strings `"NaN"`, `"Infinity"` and `"-Infinity"` the corpus uses for non-finite ones.
    pub(crate) fn float(value: &Value) -> f64 {
        match value {
            Value::String(text) => match text.as_str() {
                "NaN" => f64::NAN,
                "Infinity" => f64::INFINITY,
                "-Infinity" => f64::NEG_INFINITY,
                other => panic!("not a vector number: {other:?}"),
            },
            number => number
                .as_f64()
                .unwrap_or_else(|| panic!("not a vector number: {number}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claim_pattern::claims_overlap;
    use crate::jsonlite::Value;

    use ClaimMode::{Exclusive as Excl, Shared};

    fn conflict(holder: &str, held: &str, requested: &str) -> ClaimConflict {
        ClaimConflict {
            holder: holder.to_string(),
            held: held.to_string(),
            requested: requested.to_string(),
        }
    }

    fn snapshot(table: &ClaimTable) -> Vec<(String, Vec<(String, ClaimMode)>)> {
        table
            .held()
            .map(|(owner, claims)| {
                (
                    owner.to_string(),
                    claims
                        .iter()
                        .map(|claim| (claim.pattern.clone(), claim.mode))
                        .collect(),
                )
            })
            .collect()
    }

    fn took(table: &mut ClaimTable, owner: &str, claims: &[(&str, ClaimMode)]) {
        assert_eq!(table.acquire(owner, claims), Ok(()), "{owner} {claims:?}");
    }

    #[test]
    fn acquire_is_all_or_nothing() {
        let mut table = ClaimTable::new();
        took(&mut table, "A", &[("src/a.rs", Excl)]);
        let before = snapshot(&table);
        // the first claim is free, the second collides: neither is taken
        let result = table.acquire("B", &[("docs/x.md", Excl), ("src/a.rs", Excl)]);
        assert_eq!(
            result,
            Err(AcquireError::Conflicts(vec![conflict(
                "A", "src/a.rs", "src/a.rs"
            )]))
        );
        assert_eq!(
            snapshot(&table),
            before,
            "a refused acquire must not leave B holding docs/x.md"
        );
        // so docs/x.md is still free for a third owner
        took(&mut table, "C", &[("docs/x.md", Excl)]);
    }

    #[test]
    fn an_invalid_pattern_leaves_the_table_unchanged() {
        for bad in ["../up", "/abs", "C:/x", "", "   ", "~/x", "~", "a/../b"] {
            let mut table = ClaimTable::new();
            took(&mut table, "A", &[("src/a.rs", Excl)]);
            let before = snapshot(&table);
            let result = table.acquire("B", &[("docs/x.md", Excl), (bad, Excl)]);
            assert_eq!(
                result,
                Err(AcquireError::InvalidPattern(ClaimError {
                    pattern: bad.to_string()
                })),
                "{bad:?}"
            );
            assert_eq!(snapshot(&table), before, "{bad:?}");
        }
    }

    #[test]
    fn an_invalid_pattern_is_reported_even_when_a_conflict_would_also_exist() {
        let mut table = ClaimTable::new();
        took(&mut table, "A", &[("src/a.rs", Excl)]);
        let result = table.acquire("B", &[("src/a.rs", Excl), ("../x", Excl)]);
        assert!(
            matches!(result, Err(AcquireError::InvalidPattern(_))),
            "{result:?}"
        );
    }

    #[test]
    fn an_owner_never_conflicts_with_itself() {
        let mut table = ClaimTable::new();
        took(&mut table, "A", &[("src/**", Excl)]);
        took(&mut table, "A", &[("src/a.rs", Excl), ("src/b.rs", Shared)]);
        assert_eq!(
            snapshot(&table),
            [(
                "A".to_string(),
                vec![
                    ("src/**".to_string(), Excl),
                    ("src/a.rs".to_string(), Excl),
                    ("src/b.rs".to_string(), Shared)
                ]
            )]
        );
    }

    #[test]
    fn asking_again_for_a_held_claim_changes_nothing() {
        let mut table = ClaimTable::new();
        took(&mut table, "A", &[("src/a.rs", Excl)]);
        took(
            &mut table,
            "A",
            &[("./src//a.rs", Excl), ("src/a.rs", Excl)],
        );
        assert_eq!(
            table
                .held()
                .map(|(_, claims)| claims.len())
                .collect::<Vec<_>>(),
            [1]
        );
        // the same pattern in the other mode is a different claim
        took(&mut table, "A", &[("src/a.rs", Shared)]);
        assert_eq!(table.release("A"), 2);
    }

    #[test]
    fn an_empty_request_records_no_owner() {
        let mut table = ClaimTable::new();
        took(&mut table, "A", &[]);
        assert_eq!(table.held().count(), 0);
        assert_eq!(table.release("A"), 0);
    }

    #[test]
    fn release_returns_the_count_and_is_idempotent() {
        let mut table = ClaimTable::new();
        took(
            &mut table,
            "A",
            &[("src/a.rs", Excl), ("src/b.rs", Shared), ("docs/", Excl)],
        );
        took(&mut table, "B", &[("lib/x.rs", Excl)]);
        assert_eq!(table.release("A"), 3);
        assert_eq!(table.release("A"), 0);
        assert_eq!(table.release("never-seen"), 0);
        assert_eq!(
            snapshot(&table),
            [("B".to_string(), vec![("lib/x.rs".to_string(), Excl)])]
        );
        // what A held is free again
        took(&mut table, "C", &[("src/a.rs", Excl), ("docs/**", Excl)]);
    }

    #[test]
    fn conflicts_come_in_request_order_then_holder_order_then_claim_order() {
        let mut table = ClaimTable::new();
        // B acquires before A, so B is listed first although "A" sorts first.
        took(&mut table, "B", &[("src/b1.rs", Excl), ("lib/**", Excl)]);
        took(&mut table, "A", &[("src/a1.rs", Excl), ("src/a2.rs", Excl)]);
        let result = table.acquire("C", &[("lib/y.rs", Excl), ("src/*.rs", Excl)]);
        assert_eq!(
            result,
            Err(AcquireError::Conflicts(vec![
                conflict("B", "lib/**", "lib/y.rs"),
                conflict("B", "src/b1.rs", "src/*.rs"),
                conflict("A", "src/a1.rs", "src/*.rs"),
                conflict("A", "src/a2.rs", "src/*.rs"),
            ]))
        );
    }

    #[test]
    fn the_first_conflict_names_the_first_acquirer_not_the_alphabetical_one() {
        let mut table = ClaimTable::new();
        took(&mut table, "T-9", &[("shared/x", Shared)]);
        took(&mut table, "T-1", &[("shared/x", Shared)]);
        let Err(AcquireError::Conflicts(conflicts)) = table.acquire("T-5", &[("shared/x", Excl)])
        else {
            panic!("expected conflicts");
        };
        assert_eq!(conflicts.first().map(|c| c.holder.as_str()), Some("T-9"));
        assert_eq!(conflicts.len(), 2);
    }

    #[test]
    fn an_owner_that_releases_and_acquires_again_moves_to_the_end_of_the_holder_order() {
        let mut table = ClaimTable::new();
        took(&mut table, "A", &[("x/a", Excl)]);
        took(&mut table, "B", &[("x/b", Excl)]);
        assert_eq!(table.release("A"), 1);
        took(&mut table, "A", &[("x/c", Excl)]);
        let owners: Vec<String> = snapshot(&table)
            .into_iter()
            .map(|(owner, _)| owner)
            .collect();
        assert_eq!(owners, ["B", "A"]);
    }

    #[test]
    fn conflicts_report_normalized_patterns() {
        let mut table = ClaimTable::new();
        took(&mut table, "A", &[(".\\lib//", Excl)]);
        assert_eq!(
            table.acquire("B", &[("lib/x/", Excl)]),
            Err(AcquireError::Conflicts(vec![conflict(
                "A", "lib/**", "lib/x/**"
            )]))
        );
    }

    #[test]
    fn an_exclusive_bare_name_reserves_its_subtree() {
        // (holder claim, requester claim, conflict?)
        let cases = [
            (("src", Excl), ("src/a.rs", Excl), true),
            (("src", Excl), ("src/a.rs", Shared), true),
            (("src", Excl), ("src/deep/er/b.rs", Excl), true),
            (("src/a.rs", Excl), ("src", Excl), true),
            (("src/a.rs", Shared), ("src", Excl), true),
            // a shared bare name is just that path
            (("src", Shared), ("src/a.rs", Excl), false),
            (("src", Shared), ("src", Excl), true),
            // a glob-ended pattern stands for itself
            (("src/*.rs", Excl), ("src/deep/b.rs", Excl), false),
            (("src/**", Excl), ("src/deep/b.rs", Excl), true),
            // two shared claims never conflict, whatever they overlap
            (("src/**", Shared), ("src/a.rs", Shared), false),
            // siblings of a bare name are not under it
            (("src", Excl), ("srcs/a.rs", Excl), false),
            (("src/a.rs", Excl), ("src/b.rs", Excl), false),
        ];
        for (held, requested, expect) in cases {
            let mut table = ClaimTable::new();
            took(&mut table, "A", &[held]);
            assert_eq!(
                table.acquire("B", &[requested]).is_err(),
                expect,
                "held {held:?} requested {requested:?}"
            );
        }
    }

    #[test]
    fn patterns_are_stored_and_reported_normalized() {
        let mut table = ClaimTable::new();
        took(
            &mut table,
            "A",
            &[("./src//", Excl), ("a\\b/./c", Shared), ("x/**/**/y", Excl)],
        );
        assert_eq!(
            snapshot(&table),
            [(
                "A".to_string(),
                vec![
                    ("src/**".to_string(), Excl),
                    ("a/b/c".to_string(), Shared),
                    ("x/**/y".to_string(), Excl)
                ]
            )]
        );
    }

    #[test]
    fn normalize_pattern_matches_the_documented_canonical_form() {
        for (raw, canonical) in [
            ("src/a.py", "src/a.py"),
            ("src/", "src/**"),
            ("**/", "**"),
            ("./src//a.py", "src/a.py"),
            ("  src\\a\\b.py  ", "src/a/b.py"),
            ("a/**/**/b", "a/**/b"),
            ("\u{1c}a/b\u{1d}", "a/b"),
        ] {
            assert_eq!(normalize_pattern(raw).as_deref(), Ok(canonical), "{raw:?}");
        }
        for bad in [
            "", " ", ".", "./", "/", "/src", "//src", "~", "~root/x", "..", "src/..", "C:", "c:foo",
        ] {
            assert!(normalize_pattern(bad).is_err(), "{bad:?}");
        }
        // `~` is only special at the start
        assert_eq!(normalize_pattern("a/~b").as_deref(), Ok("a/~b"));
    }

    #[test]
    fn a_character_class_ends_where_the_python_regex_ends_it() {
        // (a, b, can they match a common name): a class counts as one `?`; a `]` right after the `[` belongs to the class; an
        // unclosed `[` and the empty `[]` are plain characters.
        let cases = [
            ("[]ab]", "x", true),
            ("[]a]", "x", true),
            ("[]a]", "xy", false),
            ("[a]b]", "xb]", true),
            ("[a]b]", "xb", false),
            ("[[]", "x", true),
            ("[]", "?", false),
            ("[]", "??", true),
            ("[a", "?a", true),
            ("[a", "xa", false),
            ("[ab]", "c", true),
            ("a[]b", "axb", false),
        ];
        for (a, b, expected) in cases {
            let parse = |text: &str| PathClaim::new(text, Excl).expect("valid pattern");
            assert_eq!(
                claims_overlap(&parse(a), &parse(b)),
                expected,
                "{a:?} against {b:?}"
            );
            assert_eq!(
                claims_overlap(&parse(b), &parse(a)),
                expected,
                "{b:?} against {a:?}"
            );
        }
    }

    #[test]
    fn errors_print_something_a_log_line_can_use() {
        let invalid = AcquireError::InvalidPattern(ClaimError {
            pattern: "../x".to_string(),
        });
        assert_eq!(invalid.to_string(), r#"invalid claim pattern: "../x""#);
        let conflicts = AcquireError::Conflicts(vec![conflict("A", "src/**", "src/a.rs")]);
        assert_eq!(
            conflicts.to_string(),
            r#"claim "src/a.rs" conflicts with "src/**" held by A (1 conflict(s))"#
        );
    }

    fn mode_of(text: &str) -> Option<ClaimMode> {
        match text {
            "exclusive" => Some(Excl),
            "shared" => Some(Shared),
            _ => None,
        }
    }

    fn mode_name(mode: ClaimMode) -> &'static str {
        match mode {
            Excl => "exclusive",
            Shared => "shared",
        }
    }

    /// The claim `[pattern, mode]` of a vector, or `None` when the pattern or the mode is invalid (Python's `ValueError`).
    fn vector_claim(pair: &Value) -> Option<PathClaim> {
        PathClaim::new(pair[0].as_str()?, mode_of(pair[1].as_str()?)?).ok()
    }

    #[test]
    fn replays_the_python_normalize_vectors_exactly() {
        let cases = vectors::load("claim_normalize");
        for case in &cases {
            let pattern = case.input["pattern"].as_str().expect("pattern");
            let got = match normalize_pattern(pattern) {
                Ok(text) => Value::object([("ok", text.into())]),
                Err(_) => Value::object([("error", "ValueError".into())]),
            };
            assert_eq!(got, case.expected, "{}: {pattern:?}", case.id);
        }
        assert!(cases.len() > 150, "the claim_normalize corpus shrank");
    }

    #[test]
    fn replays_the_python_overlap_vectors_exactly() {
        let cases = vectors::load("claim_overlap");
        for case in &cases {
            let parse =
                |side: &str| PathClaim::new(case.input[side].as_str().expect("pattern"), Excl);
            let got = match (parse("a"), parse("b")) {
                (Ok(a), Ok(b)) => Value::object([("ok", claims_overlap(&a, &b).into())]),
                _ => Value::object([("error", "ValueError".into())]),
            };
            assert_eq!(got, case.expected, "{}: {}", case.id, case.input);
        }
        assert!(cases.len() > 200, "the claim_overlap corpus shrank");
    }

    #[test]
    fn replays_the_python_conflict_vectors_exactly() {
        let cases = vectors::load("claim_conflict");
        for case in &cases {
            let (Some(a), Some(b)) = (
                vector_claim(&case.input["a"]),
                vector_claim(&case.input["b"]),
            ) else {
                assert_eq!(
                    case.expected,
                    Value::object([("error", "ValueError".into())]),
                    "{}",
                    case.id
                );
                continue;
            };
            let footprint = |claim: &PathClaim| -> Vec<String> {
                footprints(claim)
                    .iter()
                    .map(|segments| segments.join("/"))
                    .collect()
            };
            assert_eq!(
                Value::from(footprint(&a)),
                case.expected["footprint_a"],
                "{}: footprint a",
                case.id
            );
            assert_eq!(
                Value::from(footprint(&b)),
                case.expected["footprint_b"],
                "{}: footprint b",
                case.id
            );
            let expected = case.expected["conflict"].as_bool().expect("conflict flag");
            assert_eq!(
                claims_conflict(&a, &b),
                expected,
                "{}: claims_conflict",
                case.id
            );
            // the way the oracle asks: A holds its claim, B requests its own
            let mut table = ClaimTable::new();
            let pair = |claim: &Value| {
                (
                    claim[0].as_str().expect("pattern").to_string(),
                    mode_of(claim[1].as_str().expect("mode")).expect("mode"),
                )
            };
            table
                .acquire("A", &[pair(&case.input["a"])])
                .expect("first owner");
            assert_eq!(
                table.acquire("B", &[pair(&case.input["b"])]).is_err(),
                expected,
                "{}: table",
                case.id
            );
        }
        assert!(cases.len() > 150, "the claim_conflict corpus shrank");
    }

    fn held_json(table: &ClaimTable) -> Value {
        let mut view = Vec::new();
        for (owner, claims) in table.held() {
            let list: Vec<Value> = claims
                .iter()
                .map(|claim| Value::from(vec![claim.pattern.as_str(), mode_name(claim.mode)]))
                .collect();
            view.push((owner.to_string(), Value::Array(list)));
        }
        Value::Object(view)
    }

    /// Runs one `claim_table` vector the way the oracle does: an invalid pattern or mode is an error before anything changes.
    fn run_table(ops: &[Value]) -> (Vec<Value>, Value) {
        let mut table = ClaimTable::new();
        let mut results = Vec::new();
        for op in ops {
            let owner = op["owner"].as_str().unwrap_or("");
            let result = match op["op"].as_str().expect("op") {
                "acquire" => {
                    let parsed: Option<Vec<(String, ClaimMode)>> = op["claims"]
                        .as_array()
                        .expect("claims")
                        .iter()
                        .map(|pair| {
                            Some((pair[0].as_str()?.to_string(), mode_of(pair[1].as_str()?)?))
                        })
                        .collect();
                    match parsed {
                        None => Value::object([("error", "ValueError".into())]),
                        Some(claims) => match table.acquire(owner, &claims) {
                            Ok(()) => Value::object([
                                ("ok", true.into()),
                                ("conflicts", Value::Array(Vec::new())),
                            ]),
                            Err(AcquireError::InvalidPattern(_)) => {
                                Value::object([("error", "ValueError".into())])
                            }
                            Err(AcquireError::Conflicts(conflicts)) => Value::object([
                                ("ok", false.into()),
                                (
                                    "conflicts",
                                    conflicts
                                        .iter()
                                        .map(|c| {
                                            Value::from(vec![
                                                c.holder.as_str(),
                                                c.held.as_str(),
                                                c.requested.as_str(),
                                            ])
                                        })
                                        .collect::<Vec<_>>()
                                        .into(),
                                ),
                            ]),
                        },
                    }
                }
                "release" => Value::object([("released", table.release(owner).into())]),
                "held" => Value::object([("held", held_json(&table))]),
                other => panic!("unknown op {other}"),
            };
            results.push(result);
        }
        (results, held_json(&table))
    }

    #[test]
    fn replays_the_python_claim_table_vectors_exactly() {
        let cases = vectors::load("claim_table");
        for case in &cases {
            let ops = case.input["ops"].as_array().expect("ops");
            let (results, final_held) = run_table(ops);
            let want = case.expected["results"].as_array().expect("results");
            assert_eq!(results.len(), want.len(), "{}", case.id);
            for (step, (got, want)) in results.iter().zip(want).enumerate() {
                assert_eq!(got, want, "{} step {step}", case.id);
            }
            assert_eq!(
                final_held, case.expected["final_held"],
                "{}: final held {final_held}",
                case.id
            );
        }
        assert!(cases.len() > 40, "the claim_table corpus shrank");
    }
}
