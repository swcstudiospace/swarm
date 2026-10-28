//! Path claims: repo-relative globs a task reserves, and the conflict test between two of them.
//!
//! [`PathClaim`] normalizes a pattern into segments. [`claims_conflict`] decides whether two
//! claims can match a common path while at least one is exclusive. Matching is glob against glob,
//! not glob against a concrete path: `**` matches any number of segments, `*` and `?` match within
//! one segment, and a character class counts as `?`.
//!
//! Threads and blocking: everything here is a pure function over plain data. Nothing locks, does
//! I/O or blocks; claims are `Send + Sync` values the caller may share freely.

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

/// A claim pattern was empty, absolute, drive-qualified or contained `..`.
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

/// A repo-relative path glob claimed by a task, normalized into segments.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PathClaim {
    segments: Vec<String>,
    mode: ClaimMode,
}

impl PathClaim {
    /// Parses `pattern` into a claim. A trailing `/` means the whole directory.
    ///
    /// Returns [`ClaimError`] for an empty, absolute, drive-qualified, or `..`-containing pattern.
    pub fn new(pattern: &str, mode: ClaimMode) -> Result<Self, ClaimError> {
        let normalized = pattern.trim().replace('\\', "/");
        let invalid = || ClaimError {
            pattern: pattern.to_string(),
        };
        if normalized.is_empty() || normalized.starts_with('/') || is_drive_qualified(&normalized) {
            return Err(invalid());
        }
        let directory = normalized.ends_with('/');
        let mut segments: Vec<String> = Vec::new();
        for segment in normalized.split('/') {
            match segment {
                "" | "." => {}
                ".." => return Err(invalid()),
                "**" if segments.last().is_some_and(|last| last == "**") => {}
                other => segments.push(other.to_string()),
            }
        }
        if segments.is_empty() {
            return Err(invalid());
        }
        if directory && segments.last().is_some_and(|last| last != "**") {
            segments.push("**".to_string());
        }
        Ok(Self { segments, mode })
    }

    /// Returns the claim mode.
    pub fn mode(&self) -> ClaimMode {
        self.mode
    }
}

fn is_drive_qualified(pattern: &str) -> bool {
    let mut chars = pattern.chars();
    matches!((chars.next(), chars.next()), (Some(d), Some(':')) if d.is_ascii_alphabetic())
}

/// Segment lists a claim really reserves. An exclusive bare name also reserves everything below it.
pub(crate) fn footprints(claim: &PathClaim) -> Vec<Vec<&str>> {
    let own: Vec<&str> = claim.segments.iter().map(String::as_str).collect();
    let bare = claim
        .segments
        .last()
        .is_some_and(|last| !last.contains(['*', '?', '[']));
    if claim.mode == ClaimMode::Exclusive && bare {
        let mut below = own.clone();
        below.push("**");
        vec![own, below]
    } else {
        vec![own]
    }
}

/// Two claims conflict when they can match a common path and at least one is exclusive.
pub fn claims_conflict(a: &PathClaim, b: &PathClaim) -> bool {
    if a.mode == ClaimMode::Shared && b.mode == ClaimMode::Shared {
        return false;
    }
    let left = footprints(a);
    let right = footprints(b);
    left.iter()
        .any(|x| right.iter().any(|y| path_segments_overlap(x, y)))
}

/// Decides whether two segment lists can match a common path. `**` matches any number of segments.
fn path_segments_overlap(a: &[&str], b: &[&str]) -> bool {
    glob_overlap(
        a,
        b,
        |segment| *segment == "**",
        |x, y| segment_overlap(x, y),
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Token {
    Star,
    Any,
    Literal(char),
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
                if let Some(offset) = chars[i + 1..].iter().position(|&c| c == ']') {
                    tokens.push(Token::Any);
                    i += offset + 2;
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

/// Decides whether two single-segment globs can match a common name. Character classes count as `?`.
fn segment_overlap(a: &str, b: &str) -> bool {
    if a == b {
        return true;
    }
    glob_overlap(
        &tokenize(a),
        &tokenize(b),
        |token| *token == Token::Star,
        |x, y| match (x, y) {
            (Token::Any, _) | (_, Token::Any) => true,
            (Token::Literal(p), Token::Literal(q)) => p == q,
            _ => false,
        },
    )
}

/// Bottom-up table: `ok[i][j]` is whether `a[i..]` and `b[j..]` can match a common sequence.
fn glob_overlap<T: Copy>(
    a: &[T],
    b: &[T],
    is_star: impl Fn(&T) -> bool,
    can_match: impl Fn(&T, &T) -> bool,
) -> bool {
    let (n, m) = (a.len(), b.len());
    let width = m + 1;
    let mut ok = vec![false; (n + 1) * width];
    ok[n * width + m] = true;
    for i in (0..=n).rev() {
        for j in (0..=m).rev() {
            if i == n && j == m {
                continue;
            }
            let a_star = i < n && is_star(&a[i]);
            let b_star = j < m && is_star(&b[j]);
            let mut hit = false;
            if a_star {
                hit |= ok[(i + 1) * width + j] || (j < m && ok[i * width + j + 1]);
            }
            if b_star {
                hit |= ok[i * width + j + 1] || (i < n && ok[(i + 1) * width + j]);
            }
            if i < n && j < m && !a_star && !b_star && can_match(&a[i], &b[j]) {
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
}
