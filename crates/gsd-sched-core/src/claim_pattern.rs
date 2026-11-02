//! Path claims: repo-relative globs a task reserves, and the conflict test between two of them.
//!
//! [`PathClaim`] normalizes a pattern into segments. [`claims_conflict`] decides whether two
//! claims can match a common path while at least one is exclusive. Matching is glob against glob,
//! not glob against a concrete path: `**` matches any number of segments, `*` and `?` match within
//! one segment, and a character class counts as `?`.
//!
//! Threads and blocking: everything here is a pure function over plain data. Nothing locks, does
//! I/O or blocks; claims are `Send + Sync` values the caller may share freely.
//!
//! Cost: a claim tokenizes its segments once, when it is built, so a conflict test allocates nothing for any pair of
//! patterns whose comparison tables fit `STACK_CELLS` (256) cells (every realistic pattern), and the first segment that
//! cannot match, before any `**`, ends the test without a table.

use std::error::Error;
use std::fmt;

/// Whether a claim reserves its paths for one task or only reads them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClaimMode {
    /// The claimed paths are reserved; any overlapping claim conflicts.
    Exclusive,
    /// The claimed paths may be shared with other shared claims.
    Shared,
}

/// A claim pattern was empty, absolute, `~`-prefixed, drive-qualified or contained `..`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClaimError {
    /// The pattern as it was passed to [`PathClaim::new`], before normalization.
    pub pattern: String,
}

impl fmt::Display for ClaimError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid claim pattern: {:?}", self.pattern)
    }
}

impl Error for ClaimError {}

/// One piece of a segment glob: a run of `*`, one character, or a literal character.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Token {
    Star,
    Any,
    Literal(char),
}

/// One path segment of a claim: its text and the glob tokens the overlap test compares.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Segment {
    text: String,
    tokens: Vec<Token>,
    /// The segment is `**`: it matches any number of path segments.
    double_star: bool,
    /// Every token is a literal character, so the segment can match only its own text.
    plain: bool,
}

impl Segment {
    fn new(text: &str) -> Self {
        let tokens = tokenize(text);
        Self {
            text: text.to_string(),
            plain: tokens
                .iter()
                .all(|token| matches!(token, Token::Literal(_))),
            double_star: text == "**",
            tokens,
        }
    }
}

/// A repo-relative path glob claimed by a task, normalized into segments.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PathClaim {
    segments: Vec<Segment>,
    mode: ClaimMode,
}

impl PathClaim {
    /// Parses `pattern` into a claim. A trailing `/` means the whole directory.
    ///
    /// Returns [`ClaimError`] for an empty, absolute, `~`-prefixed, drive-qualified, or `..`-containing pattern.
    pub fn new(pattern: &str, mode: ClaimMode) -> Result<Self, ClaimError> {
        // Python `str.strip()` also strips the four ASCII separators U+001C to U+001F, which `char::is_whitespace` does not.
        let normalized = pattern
            .trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
            .replace('\\', "/");
        let invalid = || ClaimError {
            pattern: pattern.to_string(),
        };
        if normalized.is_empty()
            || normalized.starts_with('/')
            || normalized.starts_with('~')
            || is_drive_qualified(&normalized)
        {
            return Err(invalid());
        }
        let directory = normalized.ends_with('/');
        let mut segments: Vec<Segment> = Vec::new();
        for text in normalized.split('/') {
            match text {
                "" | "." => {}
                ".." => return Err(invalid()),
                "**" if segments.last().is_some_and(|last| last.double_star) => {}
                other => segments.push(Segment::new(other)),
            }
        }
        if segments.is_empty() {
            return Err(invalid());
        }
        if directory && segments.last().is_some_and(|last| !last.double_star) {
            segments.push(Segment::new("**"));
        }
        Ok(Self { segments, mode })
    }

    /// Returns the claim mode.
    pub fn mode(&self) -> ClaimMode {
        self.mode
    }

    /// The normalized pattern: the segments joined by `/` (`src/**` for `src/`).
    pub fn pattern(&self) -> String {
        let mut joined =
            String::with_capacity(self.segments.iter().map(|s| s.text.len() + 1).sum());
        for (index, segment) in self.segments.iter().enumerate() {
            if index > 0 {
                joined.push('/');
            }
            joined.push_str(&segment.text);
        }
        joined
    }

    /// Whether the claim reserves everything below its path too: an exclusive claim whose last segment has no glob character
    /// (a bare name such as `src/tools`).
    fn reserves_subtree(&self) -> bool {
        self.mode == ClaimMode::Exclusive
            && self
                .segments
                .last()
                .is_some_and(|last| !last.text.contains(['*', '?', '[']))
    }
}

fn is_drive_qualified(pattern: &str) -> bool {
    let mut chars = pattern.chars();
    matches!((chars.next(), chars.next()), (Some(d), Some(':')) if d.is_ascii_alphabetic())
}

/// Segment lists a claim really reserves, as text. An exclusive bare name also reserves everything below it.
#[cfg(test)]
pub(crate) fn footprints(claim: &PathClaim) -> Vec<Vec<&str>> {
    let own: Vec<&str> = claim.segments.iter().map(|s| s.text.as_str()).collect();
    if claim.reserves_subtree() {
        let mut below = own.clone();
        below.push("**");
        vec![own, below]
    } else {
        vec![own]
    }
}

/// A path a claim reserves: its segments, and a trailing `**` when `tail` is set.
#[derive(Clone, Copy)]
struct Footprint<'a> {
    segments: &'a [Segment],
    tail: bool,
}

impl<'a> Footprint<'a> {
    /// The claim's own path.
    fn own(claim: &'a PathClaim) -> Self {
        Self {
            segments: &claim.segments,
            tail: false,
        }
    }

    /// The paths the claim reserves: its own, and for [`PathClaim::reserves_subtree`] everything below it.
    fn reserved(claim: &'a PathClaim) -> impl Iterator<Item = Footprint<'a>> {
        let tails: &[bool] = if claim.reserves_subtree() {
            &[false, true]
        } else {
            &[false]
        };
        tails.iter().map(move |&tail| Footprint {
            segments: &claim.segments,
            tail,
        })
    }

    fn len(&self) -> usize {
        self.segments.len() + usize::from(self.tail)
    }

    /// Whether position `index` (below `len`) is a `**`: a written one, or the tail.
    fn is_double_star(&self, index: usize) -> bool {
        self.segments
            .get(index)
            .is_none_or(|segment| segment.double_star)
    }

    fn segment(&self, index: usize) -> Option<&'a Segment> {
        self.segments.get(index)
    }
}

/// Two claims conflict when they can match a common path and at least one is exclusive.
pub fn claims_conflict(a: &PathClaim, b: &PathClaim) -> bool {
    if a.mode == ClaimMode::Shared && b.mode == ClaimMode::Shared {
        return false;
    }
    Footprint::reserved(a).any(|x| Footprint::reserved(b).any(|y| paths_overlap(x, y)))
}

/// Two claims can match a common path, whatever their modes: the glob-against-glob overlap of the normalized patterns themselves
/// (the Python `swarm.claims.overlaps`). Unlike [`claims_conflict`] it ignores the mode and does not extend an exclusive bare
/// name to its subtree, so `src` and `src/a.rs` do not overlap.
pub fn claims_overlap(a: &PathClaim, b: &PathClaim) -> bool {
    paths_overlap(Footprint::own(a), Footprint::own(b))
}

/// Decides whether two paths can match a common path. `**` matches any number of segments.
///
/// Before the first `**` on either side the segments pair up one to one, so the first pair that cannot match ends the test; the
/// table only covers what follows the paired prefix.
fn paths_overlap(a: Footprint<'_>, b: Footprint<'_>) -> bool {
    let (n, m) = (a.len(), b.len());
    let mut prefix = 0;
    while prefix < n && prefix < m && !a.is_double_star(prefix) && !b.is_double_star(prefix) {
        let (Some(x), Some(y)) = (a.segment(prefix), b.segment(prefix)) else {
            return false;
        };
        if !segment_overlap(x, y) {
            return false;
        }
        prefix += 1;
    }
    glob_overlap(
        n - prefix,
        m - prefix,
        |i| a.is_double_star(prefix + i),
        |j| b.is_double_star(prefix + j),
        |i, j| match (a.segment(prefix + i), b.segment(prefix + j)) {
            (Some(x), Some(y)) => segment_overlap(x, y),
            _ => false,
        },
    )
}

fn tokenize(segment: &str) -> Vec<Token> {
    let chars: Vec<char> = segment.chars().collect();
    let mut tokens = Vec::with_capacity(chars.len());
    let mut i = 0;
    while i < chars.len() {
        match chars[i] {
            '*' => {
                if tokens.last() != Some(&Token::Star) {
                    tokens.push(Token::Star);
                }
            }
            '?' => tokens.push(Token::Any),
            '[' => {
                if let Some(end) = class_end(&chars, i) {
                    tokens.push(Token::Any);
                    i = end;
                    continue;
                }
                tokens.push(Token::Literal('['));
            }
            other => tokens.push(Token::Literal(other)),
        }
        i += 1;
    }
    tokens
}

/// Where the character class that opens at `chars[start]` (a `[`) ends, as an exclusive index, or `None` when it is no class.
///
/// This is the match of the Python regex `\[(?:[^\]]|(?<=\[)\])+\]`: the body is a non-empty run of characters that are not `]`,
/// except that a `]` directly after a `[` belongs to the body. The match is greedy, so the class ends at the first `]` that does
/// not directly follow a `[`; when the text ends first, it ends at the last `]` that has at least one body character before it.
/// `[]` is therefore no class, and `[]a]` is one.
fn class_end(chars: &[char], start: usize) -> Option<usize> {
    for (offset, &c) in chars[start + 1..].iter().enumerate() {
        let at = start + 1 + offset;
        if c == ']' && chars[at - 1] != '[' {
            return Some(at + 1);
        }
    }
    (start + 2..chars.len())
        .rev()
        .find(|&at| chars[at] == ']')
        .map(|at| at + 1)
}

/// Decides whether two single-segment globs can match a common name. Character classes count as `?`.
fn segment_overlap(a: &Segment, b: &Segment) -> bool {
    if a.text == b.text {
        return true;
    }
    if a.plain && b.plain {
        return false;
    }
    glob_overlap(
        a.tokens.len(),
        b.tokens.len(),
        |i| a.tokens[i] == Token::Star,
        |j| b.tokens[j] == Token::Star,
        |i, j| match (a.tokens[i], b.tokens[j]) {
            (Token::Any, _) | (_, Token::Any) => true,
            (Token::Literal(p), Token::Literal(q)) => p == q,
            _ => false,
        },
    )
}

/// The largest comparison table, in cells, that lives on the stack; a bigger one is allocated.
const STACK_CELLS: usize = 256;

/// Whether sequences `a` (length `n`) and `b` (length `m`) can match a common sequence, where a star item matches any run
/// (including none) and `can_match(i, j)` says whether the non-star items `a[i]` and `b[j]` can match one item.
///
/// Bottom-up table: `ok[i][j]` is whether `a[i..]` and `b[j..]` can match a common sequence. The closures are only called with
/// `i < n` and `j < m`.
fn glob_overlap(
    n: usize,
    m: usize,
    a_star: impl Fn(usize) -> bool,
    b_star: impl Fn(usize) -> bool,
    can_match: impl Fn(usize, usize) -> bool,
) -> bool {
    let width = m + 1;
    let cells = (n + 1) * width;
    let mut stack = [false; STACK_CELLS];
    let mut heap = Vec::new();
    let ok: &mut [bool] = if cells <= STACK_CELLS {
        &mut stack[..cells]
    } else {
        heap.resize(cells, false);
        &mut heap
    };
    ok[n * width + m] = true;
    for i in (0..=n).rev() {
        for j in (0..=m).rev() {
            if i == n && j == m {
                continue;
            }
            let a_is_star = i < n && a_star(i);
            let b_is_star = j < m && b_star(j);
            let mut hit = false;
            if a_is_star {
                hit |= ok[(i + 1) * width + j] || (j < m && ok[i * width + j + 1]);
            }
            if b_is_star {
                hit |= ok[i * width + j + 1] || (i < n && ok[(i + 1) * width + j]);
            }
            if i < n && j < m && !a_is_star && !b_is_star && can_match(i, j) {
                hit |= ok[(i + 1) * width + j + 1];
            }
            ok[i * width + j] = hit;
        }
    }
    ok[0]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn excl(pattern: &str) -> PathClaim {
        let Ok(claim) = PathClaim::new(pattern, ClaimMode::Exclusive) else {
            panic!("fixture claim must parse");
        };
        claim
    }

    fn shared(pattern: &str) -> PathClaim {
        let Ok(claim) = PathClaim::new(pattern, ClaimMode::Shared) else {
            panic!("fixture claim must parse");
        };
        claim
    }

    #[test]
    fn claim_patterns_are_validated() {
        assert!(PathClaim::new("../escape", ClaimMode::Exclusive).is_err());
        assert!(PathClaim::new("/abs/path", ClaimMode::Exclusive).is_err());
        assert!(PathClaim::new("C:/win", ClaimMode::Exclusive).is_err());
        assert!(PathClaim::new("  ", ClaimMode::Exclusive).is_err());
        assert!(PathClaim::new("src/", ClaimMode::Exclusive).is_ok());
    }

    #[test]
    fn overlap_rules_follow_claim_modes() {
        // Two shared claims never conflict.
        assert!(!claims_conflict(
            &shared("src/lib.rs"),
            &shared("src/lib.rs")
        ));
        // A shared claim against an exclusive one conflicts when the paths overlap.
        assert!(claims_conflict(&shared("src/lib.rs"), &excl("src/*.rs")));
        // An exclusive bare name reserves everything beneath it.
        assert!(claims_conflict(&excl("src"), &excl("src/a.rs")));
        // Directory claims cover their contents.
        assert!(claims_conflict(&excl("src/"), &excl("src/deep/b.rs")));
        // Disjoint paths do not conflict.
        assert!(!claims_conflict(&excl("docs/"), &excl("src/")));
        assert!(!claims_conflict(&excl("src/a.rs"), &excl("src/b.rs")));
    }

    #[test]
    fn rejected_pattern_error_keeps_the_raw_pattern_and_the_original_text() {
        for (pattern, text) in [
            ("../escape", r#"invalid claim pattern: "../escape""#),
            ("  ", r#"invalid claim pattern: "  ""#),
            ("C:\\win", r#"invalid claim pattern: "C:\\win""#),
        ] {
            let Err(error) = PathClaim::new(pattern, ClaimMode::Shared) else {
                panic!("pattern {pattern:?} must be rejected");
            };
            assert_eq!(error.pattern, pattern);
            assert_eq!(error.to_string(), text);
        }
    }

    #[test]
    fn the_pattern_is_the_normalized_form() {
        for (raw, normalized) in [
            ("src/", "src/**"),
            ("./a//b", "a/b"),
            (" a\\b ", "a/b"),
            ("a/**/**/b", "a/**/b"),
            ("x", "x"),
        ] {
            assert_eq!(excl(raw).pattern(), normalized, "{raw}");
        }
    }

    #[test]
    fn a_segment_that_cannot_match_before_any_double_star_ends_the_test() {
        // (a, b, overlap): the paired prefix decides, the table never runs past a mismatch
        let cases = [
            ("src/x/**/deep.rs", "lib/**/deep.rs", false),
            ("src/*.rs/**", "src/a.rs/**", true),
            ("src/*.rs/z", "src/a.py/z", false),
            ("a/b/c", "a/b", false),
            ("a/b", "a/b/**", true),
            ("a/**", "a", true),
            ("**", "x/y/z", true),
            ("a/**/z", "a/b/c/z", true),
            ("a/**/z", "a/b/c/y", false),
            ("a/?/c", "a/[xy]/c", true),
            ("a/b?d/c", "a/bc/c", false),
        ];
        for (a, b, expected) in cases {
            assert_eq!(claims_overlap(&excl(a), &excl(b)), expected, "{a} vs {b}");
            assert_eq!(claims_overlap(&excl(b), &excl(a)), expected, "{b} vs {a}");
        }
    }

    #[test]
    fn segment_tables_on_both_sides_of_the_stack_buffer_give_the_same_answer() {
        // `?`*len against `*` + `?`*len overlap (the star absorbs nothing); against `?`*(len+1) they do not (one char more).
        // The token tables are (len + 1) * (len + 2) cells: 240 at len 14, 272 at len 15, so the loop crosses STACK_CELLS.
        let mut crossed = (false, false);
        for len in 1..=40_usize {
            let exact = "?".repeat(len);
            let starred = format!("*{exact}");
            let longer = "?".repeat(len + 1);
            let cells = (len + 1) * (len + 2);
            crossed = (
                crossed.0 || cells <= STACK_CELLS,
                crossed.1 || cells > STACK_CELLS,
            );
            assert!(claims_overlap(&excl(&exact), &excl(&starred)), "{len}");
            assert!(claims_overlap(&excl(&starred), &excl(&exact)), "{len}");
            assert!(!claims_overlap(&excl(&exact), &excl(&longer)), "{len}");
            assert!(!claims_overlap(&excl(&longer), &excl(&exact)), "{len}");
        }
        assert_eq!(crossed, (true, true), "the loop must use both buffers");
    }

    #[test]
    fn path_tables_on_both_sides_of_the_stack_buffer_give_the_same_answer() {
        // `**/p/**/p/...` (k literal `p` segments between `**`) against k+1 plain `p` segments: the stars absorb the extra ones.
        // Against plain `r` segments there is no `p` to match. The path table is (2k + 2) * (k + 2) cells: 220 at k=9, 264 at k=10.
        let mut crossed = (false, false);
        for k in 1..=12_usize {
            let stars = format!("**/{}", vec!["p/**"; k].join("/"));
            let ps = vec!["p"; k + 1].join("/");
            let rs = vec!["r"; k + 1].join("/");
            let cells = (2 * k + 2) * (k + 2);
            crossed = (
                crossed.0 || cells <= STACK_CELLS,
                crossed.1 || cells > STACK_CELLS,
            );
            assert!(claims_overlap(&excl(&stars), &excl(&ps)), "{k}");
            assert!(claims_overlap(&excl(&ps), &excl(&stars)), "{k}");
            assert!(!claims_overlap(&excl(&stars), &excl(&rs)), "{k}");
            assert!(!claims_overlap(&excl(&rs), &excl(&stars)), "{k}");
        }
        assert_eq!(crossed, (true, true), "the loop must use both buffers");
    }
}
