//! The stateless regex detectors of `swarm/redact.py`, as hand-written scanners over code points.
//!
//! Each detector reproduces what the Python pattern does under `re.finditer` (or, for the header
//! lead, `re.search(s, pos)`): the leftmost match, greedy quantifiers with their backtracking,
//! alternation in source order, lookbehind that sees the real preceding character (also right
//! after a previous match), and lookahead that sees the real following one. The Python patterns
//! that use `\s` or `\b` carry `re.ASCII`, and the case-insensitive ones pair `re.IGNORECASE`
//! with it, so every class and every case fold here is ASCII-only (the Kelvin sign U+212A and
//! the long s U+017F never match `k` and `s`).
//!
//! Every scanner is linear. A failed attempt reads a bounded number of characters (the minimum
//! length of its run, or a capped prefix), or a run that no later attempt can start inside; a
//! successful attempt consumes its whole match, and the next search resumes at the match end.
//!
//! The functions append `(start, end)` code-point offsets exactly as `redact.py:_spans` (lines
//! 1080-1103) reports them, in match order. Overlapping spans are merged by the caller.

use crate::cls::{is_alnum, is_alpha, is_blank, is_space, is_word};

/// One detector hit: the reported span `(start, end)` and the end of the whole match, where the
/// next `finditer` search resumes.
type Hit = (usize, usize, usize);

/// `[A-Za-z0-9_-]`, the JWT and vendor-token body class.
fn is_b64url(c: char) -> bool {
    is_word(c) || c == '-'
}

/// `[A-Za-z0-9-]`.
fn is_alnum_dash(c: char) -> bool {
    is_alnum(c) || c == '-'
}

/// `[A-Za-z0-9._~+/=-]`, the `Bearer` token class.
fn is_bearer_char(c: char) -> bool {
    is_alnum(c) || matches!(c, '.' | '_' | '~' | '+' | '/' | '=' | '-')
}

/// `[A-Za-z0-9+/_-]`, the `Basic` credential class.
fn is_basic_char(c: char) -> bool {
    is_alnum(c) || matches!(c, '+' | '/' | '_' | '-')
}

/// `[A-Za-z0-9+.-]`, the URL scheme class.
fn is_scheme_char(c: char) -> bool {
    is_alnum(c) || matches!(c, '+' | '.' | '-')
}

/// `s[i-1]` satisfies `pred`; false at the start of the string (so `(?<!pred)` holds there).
fn prev_is(s: &[char], i: usize, pred: fn(char) -> bool) -> bool {
    i.checked_sub(1)
        .and_then(|p| s.get(p))
        .is_some_and(|&c| pred(c))
}

/// Does the exact ASCII literal `lit` start at `s[i]`?
fn lit_at(s: &[char], i: usize, lit: &str) -> bool {
    lit.chars()
        .enumerate()
        .all(|(k, c)| s.get(i + k) == Some(&c))
}

/// Does the lowercase ASCII literal `lit` start at `s[i]`, folding ASCII case only (`re.IGNORECASE | re.ASCII`)?
fn lit_at_ci(s: &[char], i: usize, lit: &str) -> bool {
    lit.chars()
        .enumerate()
        .all(|(k, c)| s.get(i + k).is_some_and(|d| d.to_ascii_lowercase() == c))
}

/// The end of the maximal run of `pred` characters that starts at `i`.
fn run_end(s: &[char], i: usize, pred: fn(char) -> bool) -> usize {
    s.get(i..)
        .map_or(i, |t| i + t.iter().take_while(|&&c| pred(c)).count())
}

/// The end of a greedy `pred{min,}` run from `i`, or `None` when the run is shorter than `min`.
fn run_min(s: &[char], i: usize, pred: fn(char) -> bool, min: usize) -> Option<usize> {
    let end = run_end(s, i, pred);
    (end - i >= min).then_some(end)
}

/// The end of a greedy `[ \t]{0,max}` from `i`.
fn blanks_upto(s: &[char], i: usize, max: usize) -> usize {
    let mut p = i;
    while p - i < max && s.get(p).is_some_and(|&c| is_blank(c)) {
        p += 1;
    }
    p
}

/// Run `attempt` at every start offset in turn, like `finditer`: after a hit the search resumes at the match end.
fn finditer(
    s: &[char],
    spans: &mut Vec<(usize, usize)>,
    attempt: impl Fn(&[char], usize, char) -> Option<Hit>,
) {
    let mut i = 0;
    while let Some(&c) = s.get(i) {
        match attempt(s, i, c) {
            Some((start, end, resume)) => {
                spans.push((start, end));
                i = resume.max(i + 1);
            }
            None => i += 1,
        }
    }
}

/// `-----(BEGIN|END) [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----` at `s[i]` (the kind carries its trailing space); the end offset.
fn pem_marker(s: &[char], i: usize, kind: &str) -> Option<usize> {
    if !lit_at(s, i, "-----") || !lit_at(s, i + 5, kind) {
        return None;
    }
    let p = i + 5 + kind.len();
    let is_label = |c: char| c == ' ' || c.is_ascii_uppercase() || c.is_ascii_digit();
    let reach = s.get(p..).map_or(0, |t| {
        t.iter().take(40).take_while(|&&c| is_label(c)).count()
    });
    // `{0,40}` is greedy: the longest label first, then shorter ones until `PRIVATE KEY` fits
    for k in (0..=reach).rev() {
        let q = p + k;
        if lit_at(s, q, "PRIVATE KEY") {
            let r = q + 11;
            if lit_at(s, r, " BLOCK-----") {
                return Some(r + 11);
            }
            if lit_at(s, r, "-----") {
                return Some(r + 5);
            }
        }
    }
    None
}

/// `_PEM_RE` (`redact.py:134`): a private-key block, from `-----BEGIN ... PRIVATE KEY-----` to the first matching `-----END ... PRIVATE KEY-----`, or to the end of the text when
/// the block is never closed. Appends `(m.start(), m.end())` of every non-overlapping match.
pub fn pem_spans(s: &[char], spans: &mut Vec<(usize, usize)>) {
    finditer(s, spans, |s, i, c| {
        if c != '-' {
            return None;
        }
        let body = pem_marker(s, i, "BEGIN ")?;
        // lazy `.*?`: the first offset where the END marker fits, else `\Z`
        let end = (body..s.len())
            .find_map(|j| {
                if s.get(j) == Some(&'-') {
                    pem_marker(s, j, "END ")
                } else {
                    None
                }
            })
            .unwrap_or(s.len());
        Some((i, end, end))
    });
}

/// `_JWT_RE` (`redact.py:137`): `eyJ` header, payload and signature segments of base64url characters. Appends `(m.start(), m.end())`.
pub fn jwt_spans(s: &[char], spans: &mut Vec<(usize, usize)>) {
    finditer(s, spans, |s, i, c| {
        if c != 'e' || prev_is(s, i, is_b64url) || !lit_at(s, i, "eyJ") {
            return None;
        }
        // none of the three runs can give back a character: the next element is `.`, outside the class
        let a = run_min(s, i + 3, is_b64url, 5)?;
        if s.get(a) != Some(&'.') {
            return None;
        }
        let b = run_min(s, a + 1, is_b64url, 5)?;
        if s.get(b) != Some(&'.') {
            return None;
        }
        let end = run_end(s, b + 1, is_b64url);
        Some((i, end, end))
    });
}

/// `[spr]k_live_`, `whsec_` ... : a literal prefix, then a run of `class` at least `min` long.
fn prefixed(
    s: &[char],
    i: usize,
    prefix: &str,
    class: fn(char) -> bool,
    min: usize,
) -> Option<usize> {
    if lit_at(s, i, prefix) {
        run_min(s, i + prefix.len(), class, min)
    } else {
        None
    }
}

/// `_TOKEN_RES[0]` (`redact.py:139`).
fn gh_token(s: &[char], i: usize) -> Option<usize> {
    if prev_is(s, i, is_word) {
        return None;
    }
    if lit_at(s, i, "gh")
        && matches!(s.get(i + 2), Some('o' | 'p' | 'r' | 's' | 'u'))
        && s.get(i + 3) == Some(&'_')
    {
        return run_min(s, i + 4, is_alnum, 20);
    }
    prefixed(s, i, "github_pat_", is_word, 20)
}

/// `_TOKEN_RES[1]` (`redact.py:140`).
fn sk_token(s: &[char], i: usize) -> Option<usize> {
    if prev_is(s, i, is_alnum) {
        return None;
    }
    prefixed(s, i, "sk-", is_b64url, 16)
}

/// `_TOKEN_RES[2]` (`redact.py:141`): an AWS key id, exactly 16 characters after the prefix and no alphanumeric after them.
fn aws_key(s: &[char], i: usize) -> Option<usize> {
    if prev_is(s, i, is_alnum) || !(lit_at(s, i, "AKIA") || lit_at(s, i, "ASIA")) {
        return None;
    }
    let end = i + 20;
    let body = s.get(i + 4..end)?;
    if !body
        .iter()
        .all(|&c| c.is_ascii_uppercase() || c.is_ascii_digit())
        || s.get(end).is_some_and(|&c| is_alnum(c))
    {
        return None;
    }
    Some(end)
}

/// `_TOKEN_RES[3]` (`redact.py:142`).
fn slack_token(s: &[char], i: usize) -> Option<usize> {
    if prev_is(s, i, is_alnum) {
        return None;
    }
    // `xox[baprs]` and `xapp` are both four characters long
    let xox = lit_at(s, i, "xox") && matches!(s.get(i + 3), Some('b' | 'a' | 'p' | 'r' | 's'));
    if !(xox || lit_at(s, i, "xapp")) || s.get(i + 4) != Some(&'-') {
        return None;
    }
    run_min(s, i + 5, is_alnum_dash, 10)
}

/// A literal prefix, the class of the run after it and that run's minimum length.
type Prefixed = (&'static str, fn(char) -> bool, usize);

/// The prefixed branches of `_TOKEN_RES[4]` (`redact.py:144-147`) except `SG.`, in source order, with `[spr]k_live` split in three: prefix, body class, minimum body length.
const VENDOR_PREFIXES: [Prefixed; 13] = [
    ("sk_live_", is_alnum, 16),
    ("pk_live_", is_alnum, 16),
    ("rk_live_", is_alnum, 16),
    ("whsec_", is_alnum, 16),
    ("AIza", is_b64url, 30),
    ("npm_", is_alnum, 30),
    ("glpat-", is_b64url, 16),
    ("dop_v1_", is_alnum, 32),
    ("hf_", is_alnum, 30),
    ("dckr_pat_", is_b64url, 20),
    ("shpat_", is_alnum, 32),
    ("lin_api_", is_alnum, 30),
    ("pypi-AgEIcHlwaS5vcmc", is_b64url, 20),
];

/// `_TOKEN_RES[4]` (`redact.py:144`): the 12-branch vendor alternation. The branch prefixes are mutually exclusive, so at most one can match at an offset.
fn vendor_token(s: &[char], i: usize) -> Option<usize> {
    if prev_is(s, i, is_alnum) {
        return None;
    }
    if lit_at(s, i, "SG.") {
        // the first run ends at `.`, a character outside its class, so it cannot give one back
        let a = run_min(s, i + 3, is_b64url, 16)?;
        if s.get(a) != Some(&'.') {
            return None;
        }
        return run_min(s, a + 1, is_b64url, 16);
    }
    VENDOR_PREFIXES
        .iter()
        .find_map(|&(prefix, class, min)| prefixed(s, i, prefix, class, min))
}

/// Append the `finditer` spans of one whole-match token pattern.
fn token_spans(
    s: &[char],
    spans: &mut Vec<(usize, usize)>,
    token: fn(&[char], usize) -> Option<usize>,
) {
    finditer(s, spans, |s, i, _| token(s, i).map(|end| (i, end, end)));
}

/// The five `_TOKEN_RES` patterns (`redact.py:139-147`), one `finditer` pass each in source order. Appends `(m.start(), m.end())`.
pub fn vendor_token_spans(s: &[char], spans: &mut Vec<(usize, usize)>) {
    for token in [gh_token, sk_token, aws_key, slack_token, vendor_token] {
        token_spans(s, spans, token);
    }
}

/// `_BEARER_RE` (`redact.py:151`): `Bearer <16+ token characters>`. Appends `(m.end(1), m.end())`, the token without its scheme.
pub fn bearer_spans(s: &[char], spans: &mut Vec<(usize, usize)>) {
    finditer(s, spans, |s, i, c| {
        if !matches!(c, 'B' | 'b') || prev_is(s, i, is_alnum) || !lit_at(s, i + 1, "earer") {
            return None;
        }
        // `\s{1,16}` then a class without whitespace: 17 or more blanks leave a blank where a token character is needed
        let p = i + 6;
        let mut q = p;
        while q - p < 16 && s.get(q).is_some_and(|&c| is_space(c)) {
            q += 1;
        }
        if q == p {
            return None;
        }
        let end = run_min(s, q, is_bearer_char, 16)?;
        Some((q, end, end))
    });
}

/// `_BASIC_RE` (`redact.py:153`): `[Proxy-]Authorization: Basic <4+ base64 characters>=?=?`, case-insensitive. Appends `(m.end(1), m.end())`, the credential without its lead.
pub fn basic_spans(s: &[char], spans: &mut Vec<(usize, usize)>) {
    finditer(s, spans, |s, i, c| {
        if !matches!(c, 'P' | 'p' | 'A' | 'a') || prev_is(s, i, is_alnum) {
            return None;
        }
        let mut p = i;
        if lit_at_ci(s, p, "proxy-") {
            p += 6;
        }
        if !lit_at_ci(s, p, "authorization") {
            return None;
        }
        // each `[ \t]{0,3}` is followed by a character that is no blank, so no backtracking is needed
        p = blanks_upto(s, p + 13, 3);
        if !matches!(s.get(p), Some(':' | '=')) {
            return None;
        }
        p = blanks_upto(s, p + 1, 3);
        if matches!(s.get(p), Some('"' | '\'')) {
            p += 1;
        }
        if !lit_at_ci(s, p, "basic") {
            return None;
        }
        let lead_end = blanks_upto(s, p + 5, 16);
        if lead_end == p + 5 {
            return None;
        }
        let body = run_min(s, lead_end, is_basic_char, 4)?;
        let end = body
            + s.get(body..)
                .map_or(0, |t| t.iter().take(2).take_while(|&&c| c == '=').count());
        Some((lead_end, end, end))
    });
}

/// `_USERINFO_RE` (`redact.py:164`): `scheme://user:password@`, the password running to the LAST `@` before the first `/` or whitespace. Appends
/// `(m.end(1), m.end() - 1)`, the `user:password` part without the scheme and without the final `@`.
pub fn userinfo_spans(s: &[char], spans: &mut Vec<(usize, usize)>) {
    finditer(s, spans, |s, i, c| {
        if !is_alpha(c) || prev_is(s, i, is_scheme_char) {
            return None;
        }
        // `[A-Za-z][A-Za-z0-9+.-]{0,31}://`: the scheme run is at most 32 characters and `://` is outside its class
        let scheme_end = run_end(s, i + 1, is_scheme_char);
        if scheme_end - i > 32 || !lit_at(s, scheme_end, "://") {
            return None;
        }
        let user = scheme_end + 3;
        // `[^/\s@:]*:`: the user run cannot give a character back, the next element is `:`
        let colon = user
            + s.get(user..).map_or(0, |t| {
                t.iter()
                    .take_while(|&&c| !matches!(c, '/' | '@' | ':') && !is_space(c))
                    .count()
            });
        if s.get(colon) != Some(&':') {
            return None;
        }
        // `[^/\s]*@`: greedy, then back to the last `@` of the run
        let run = run_end_not(s, colon + 1);
        let at = (colon + 1..run).rev().find(|&k| s.get(k) == Some(&'@'))?;
        Some((user, at, at + 1))
    });
}

/// The end of the run of characters that are neither `/` nor ASCII whitespace.
fn run_end_not(s: &[char], i: usize) -> usize {
    s.get(i..).map_or(i, |t| {
        i + t.iter().take_while(|&&c| c != '/' && !is_space(c)).count()
    })
}

/// `\b` after a word character: the next character is not a word character, or the text ends.
fn word_end(s: &[char], i: usize) -> bool {
    !s.get(i).is_some_and(|&c| is_word(c))
}

/// The lead of `_HEADER_RE`, a match found by [`header_prefix_search`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HeaderPrefix {
    /// `m.start()`: where the header name begins.
    pub start: usize,
    /// `m.end()`, which is also `m.end(1)` (the pattern ends in a lookahead): the first character of the header value, after the colon and its blanks.
    pub value_start: usize,
}

/// The `_HEADER_RE` match at `s[i]`: `(?<![A-Za-z0-9_-])((?:Set-|Proxy-)?(?:Cookie|Authorization)[ \t]*:(?![ \t]*(?:Bearer|Basic)\b)[ \t]*)(?=[^\s"'])`.
fn header_at(s: &[char], i: usize) -> Option<usize> {
    if !matches!(s.get(i)?.to_ascii_lowercase(), 's' | 'p' | 'c' | 'a') || prev_is(s, i, is_b64url)
    {
        return None;
    }
    let mut p = i;
    if lit_at_ci(s, p, "set-") {
        p += 4;
    } else if lit_at_ci(s, p, "proxy-") {
        p += 6;
    }
    if lit_at_ci(s, p, "cookie") {
        p += 6;
    } else if lit_at_ci(s, p, "authorization") {
        p += 13;
    } else {
        return None;
    }
    // `[ \t]*:` cannot give a blank back, the next element is `:`
    p = run_end(s, p, is_blank);
    if s.get(p) != Some(&':') {
        return None;
    }
    // the same blank run serves the negative lookahead and the `[ \t]*` that follows the colon
    let value = run_end(s, p + 1, is_blank);
    if (lit_at_ci(s, value, "bearer") && word_end(s, value + 6))
        || (lit_at_ci(s, value, "basic") && word_end(s, value + 5))
    {
        return None;
    }
    // `(?=[^\s"'])`: a blank cannot satisfy it either, so fewer blanks never help
    let next = *s.get(value)?;
    (!is_space(next) && next != '"' && next != '\'').then_some(value)
}

/// `_HEADER_RE.search(s, pos)` (`redact.py:159`): the leftmost `Cookie:` / `Set-Cookie:` / `Authorization:` / `Proxy-Authorization:` lead at or after `pos` whose value is not a
/// `Bearer` / `Basic` scheme and starts with a character that is neither whitespace nor a quote. The lookbehind still sees the characters before `pos`.
pub fn header_prefix_search(s: &[char], pos: usize) -> Option<HeaderPrefix> {
    (pos..s.len()).find_map(|start| {
        header_at(s, start).map(|value_start| HeaderPrefix { start, value_start })
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::best_of;
    use std::time::Duration;

    type Scanner = fn(&[char], &mut Vec<(usize, usize)>);
    /// An input, the offset to search from and the `(start, value_start)` of the expected match.
    type HeaderRow = (&'static str, usize, Option<(usize, usize)>);

    fn chars(s: &str) -> Vec<char> {
        s.chars().collect()
    }

    fn spans_of(scan: Scanner, s: &str) -> Vec<(usize, usize)> {
        let mut spans = Vec::new();
        scan(&chars(s), &mut spans);
        spans
    }

    /// Check every `(input, spans)` row; the spans of each row are what Python's `_spans` computes for that detector (see the module header).
    fn check(scan: Scanner, table: &[(&str, &[(usize, usize)])]) {
        for (input, want) in table {
            assert_eq!(spans_of(scan, input), *want, "input {input:?}");
        }
    }

    // The expected values of the tables below were produced by running the real `swarm.redact` patterns (`finditer`, `_HEADER_RE.search`).

    const PEM: &[(&str, &[(usize, usize)])] = &[
        ("-----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY-----", &[(0, 55)]),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\nline\r\n-----END RSA PRIVATE KEY-----", &[(0, 72)]),
        ("-----BEGIN PRIVATE KEY BLOCK-----x-----END PRIVATE KEY BLOCK-----", &[(0, 65)]),
        ("-----BEGIN PRIVATE KEY-----x-----END PRIVATE KEY BLOCK-----", &[(0, 59)]),
        ("-----BEGIN PRIVATE KEY BLOCK-----x-----END PRIVATE KEY-----", &[(0, 59)]),
        ("-----BEGIN PRIVATE KEY-----body with no end", &[(0, 43)]),
        ("-----BEGIN PRIVATE KEY-----", &[(0, 27)]),
        ("-----BEGIN PRIVATE KEY----", &[]),
        ("-----BEGIN PRIVATE KEY-----x-----END RSA PRIVATE KEY-----tail", &[(0, 57)]),
        ("-----BEGIN PRIVATE KEY-----x-----END PRIVATE KEY-----y-----BEGIN EC PRIVATE KEY-----z-----END EC PRIVATE KEY-----w", &[(0, 53), (54, 113)]),
        ("-----BEGIN PRIVATE KEY-----x-----BEGIN PRIVATE KEY-----y-----END PRIVATE KEY-----z-----END PRIVATE KEY-----", &[(0, 81)]),
        ("-----BEGIN PRIVATE KEY-----x-----BEGIN PRIVATE KEY-----y", &[(0, 56)]),
        ("-----BEGIN PRIVATE KEY-----END PRIVATE KEY-----", &[(0, 47)]),
        ("-----BEGIN PRIVATE KEY--------END PRIVATE KEY-----", &[(0, 50)]),
        ("-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----", &[(0, 53)]),
        ("-----BEGIN AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPRIVATE KEY-----x-----END PRIVATE KEY-----", &[(0, 93)]),
        ("-----BEGIN AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPRIVATE KEY-----x-----END PRIVATE KEY-----", &[]),
        ("-----BEGIN AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA PRIVATE KEY-----x", &[(0, 68)]),
        ("-----BEGIN AAAAAAAAAAAAAAAAAAAAAAAAAAAAA PRIVATE KEY-----x", &[(0, 58)]),
        ("-----BEGIN PRIVATE KEY-----x-----END BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBPRIVATE KEY-----y", &[(0, 93)]),
        ("-----BEGIN PRIVATE KEY-----x-----END BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBPRIVATE KEY-----y", &[(0, 95)]),
        ("-----BEGIN RSA 2048 PRIVATE KEY-----x-----END EC2 PRIVATE KEY-----", &[(0, 66)]),
        ("-----BEGIN rsa PRIVATE KEY-----x", &[]),
        ("-----BEGIN private key-----x", &[]),
        ("-----BEGIN PRIVATEKEY-----x", &[]),
        ("-----BEGINPRIVATE KEY-----x", &[]),
        ("----BEGIN PRIVATE KEY-----x", &[]),
        ("------BEGIN PRIVATE KEY-----x-----END PRIVATE KEY-----", &[(1, 54)]),
        ("-----BEGIN PRIVATE KEY PRIVATE KEY-----x-----END PRIVATE KEY-----", &[(0, 65)]),
        ("-----BEGIN KEY PRIVATE KEY BLOCK PRIVATE KEY-----x", &[(0, 50)]),
        ("-----BEGIN PRIVATE KEY BLOCK BLOCK-----x", &[]),
        ("-----BEGIN PRIVATE KEY BLOCK----x-----END PRIVATE KEY-----", &[]),
        ("-----BEGIN PRIVATE KEY-----x-----END PRIVATE KEY----y", &[(0, 53)]),
        ("-----BEGIN PRIVATE KEY-----x-----END PRIVATE KEY----y-----END PRIVATE KEY-----z", &[(0, 78)]),
        ("\u{e9}-----BEGIN PRIVATE KEY-----\u{1f600}\u{212a}-----END PRIVATE KEY-----\u{e9}", &[(1, 55)]),
        ("\u{1f600}\u{1f600}-----BEGIN PRIVATE KEY-----\u{2028}tail", &[(2, 34)]),
        ("a-----BEGIN PRIVATE KEY-----b-----END PRIVATE KEY-----c-----BEGIN PRIVATE KEY-----d", &[(1, 54), (55, 83)]),
        ("-----BEGIN PRIVATE KEY-----x-----END PRIVATE KEY----------BEGIN PRIVATE KEY-----x-----END PRIVATE KEY----------BEGIN PRIVATE KEY-----x-----END PRIVATE KEY-----", &[(0, 53), (53, 106), (106, 159)]),
        ("-----BEGIN -----BEGIN -----BEGIN -----BEGIN -----BEGIN ", &[]),
        ("-----BEGIN PRIVATE KEY----------BEGIN PRIVATE KEY----------BEGIN PRIVATE KEY-----", &[(0, 81)]),
        ("-----", &[]),
        ("", &[]),
        ("x", &[]),
    ];
    const JWT: &[(&str, &[(usize, usize)])] = &[
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", &[(0, 92)]),
        ("x eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c y", &[(2, 94)]),
        ("eyJabcde.fghij.", &[(0, 15)]),
        ("eyJabcde.fghij.k", &[(0, 16)]),
        ("eyJabcde.fghij", &[]),
        ("eyJabcd.fghij.k", &[]),
        ("eyJabcde.fghi.k", &[]),
        ("eyJabcde.fghij.-_-", &[(0, 18)]),
        ("eyJabcde.fghij.k.l", &[(0, 16)]),
        ("eyJabcde.fghij.k-", &[(0, 17)]),
        ("eyJ-----.-----.-----", &[(0, 20)]),
        ("eyJ_____.aaaaa.", &[(0, 15)]),
        ("eyJaaaaa..bbbbb.", &[]),
        ("eyJaaaaa.bbbbb..", &[(0, 15)]),
        ("EYJabcde.fghij.k", &[]),
        ("eyjabcde.fghij.k", &[]),
        ("eyJ.abcde.fghij.", &[]),
        ("eyJabcde.fghij,k", &[]),
        ("aeyJabcde.fghij.k", &[]),
        ("-eyJabcde.fghij.k", &[]),
        ("_eyJabcde.fghij.k", &[]),
        (".eyJabcde.fghij.k", &[(1, 17)]),
        (" eyJabcde.fghij.k", &[(1, 17)]),
        ("5eyJabcde.fghij.k", &[]),
        ("\u{e9}eyJabcde.fghij.k", &[(1, 17)]),
        ("\u{212a}eyJabcde.fghij.k", &[(1, 17)]),
        ("eyJabc\u{e9}de.fghij.k", &[]),
        ("eyJabcde.fgh\u{e9}ij.k", &[]),
        ("eyJabcde.fghij.k\u{e9}", &[(0, 16)]),
        ("eyJabcde.fghij.eyJabcde.fghij.k", &[(0, 23)]),
        ("eyJabcde.eyJabcde.eyJabcde.fghij.k", &[(0, 26)]),
        ("eyJabcde.fghij.k eyJabcde.fghij.l", &[(0, 16), (17, 33)]),
        ("eyJabcde.fghij.k.eyJabcde.fghij.l", &[(0, 16), (17, 33)]),
        ("eyJabcde.fghij.-eyJabcde.fghij.l", &[(0, 24)]),
        ("eyJaaaaaaaaaaaaaaaaaaaa", &[]),
        ("eyJ-eyJ-eyJ-eyJ-eyJ-", &[]),
        ("eyJaaaaa.eyJaaaaa.eyJaaaaa.", &[(0, 26)]),
        ("eyJaaaaa.bbbbb.eyJaaaaa.bbbbb.eyJaaaaa.bbbbb.", &[(0, 23), (30, 45)]),
        ("a.a.a.a.a.a.", &[]),
        ("a-a-a-a-a-a-", &[]),
        ("eyJ-eyJ-eyJ-.eyJ-eyJ-eyJ-.", &[(0, 26)]),
        ("eyJabcde.fghij.k\neyJabcde.fghij.l", &[(0, 16), (17, 33)]),
        ("", &[]),
        ("eyJ", &[]),
    ];
    const VENDOR: &[(&str, &[(usize, usize)])] = &[
        ("ghp_aaaaaaaaaaaaaaaaaaaa", &[(0, 24)]),
        ("ghp_aaaaaaaaaaaaaaaaaaa", &[]),
        ("gho_aaaaaaaaaaaaaaaaaaaa", &[(0, 24)]),
        ("ghr_aaaaaaaaaaaaaaaaaaaaa", &[(0, 25)]),
        ("ghs_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[(0, 44)]),
        ("ghu_aaaaaaaaaaaaaaaaaaaa", &[(0, 24)]),
        ("ghx_aaaaaaaaaaaaaaaaaaaa", &[]),
        ("gh_aaaaaaaaaaaaaaaaaaaa", &[]),
        ("ghp_aaaaaaaaaa_aaaaaaaaaa", &[]),
        ("ghp_aaaaaaaaaaaaaaaaaaaa_bcccccccccccccccccccc", &[(0, 24)]),
        ("github_pat_aaaaaaaaaaaaaaaaaaaa", &[(0, 31)]),
        ("github_pat_aaaaaaaaaaaaaaaaaaa", &[]),
        ("github_pat_a_a_a_a_a_a_a_a_a_a_", &[(0, 31)]),
        ("github_pat_11ABC_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", &[(0, 47)]),
        ("github_pat-aaaaaaaaaaaaaaaaaaaa", &[]),
        ("xghp_aaaaaaaaaaaaaaaaaaaa", &[]),
        ("_ghp_aaaaaaaaaaaaaaaaaaaa", &[]),
        ("-ghp_aaaaaaaaaaaaaaaaaaaa", &[(1, 25)]),
        (" ghp_aaaaaaaaaaaaaaaaaaaa", &[(1, 25)]),
        ("\u{e9}ghp_aaaaaaaaaaaaaaaaaaaa", &[(1, 25)]),
        ("ghp_aaaaaaaaaaaaaaaaaaaa\u{e9}", &[(0, 24)]),
        ("ghp_aaaaaaaaaaaaaaaaaaa\u{e9}a", &[]),
        ("sk-aaaaaaaaaaaaaaaa", &[(0, 19)]),
        ("sk-aaaaaaaaaaaaaaa", &[]),
        ("sk-ab_-ab_-ab_-ab_-ab_-ab_-ab_-ab_-", &[(0, 35)]),
        ("sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa x", &[(0, 33)]),
        ("Ask-aaaaaaaaaaaaaaaaaaaa", &[]),
        ("_sk-aaaaaaaaaaaaaaaaaaaa", &[(1, 24)]),
        ("-sk-aaaaaaaaaaaaaaaaaaaa", &[(1, 24)]),
        (".sk-aaaaaaaaaaaaaaaaaaaa", &[(1, 24)]),
        ("sk-sk-aaaaaaaaaaaaaaaaaaaa", &[(0, 26)]),
        ("sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[(0, 38)]),
        ("sk_aaaaaaaaaaaaaaaaaaaa", &[]),
        ("sk-\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}\u{e9}", &[]),
        ("AKIAAAAAAAAAAAAAAAAA", &[(0, 20)]),
        ("AKIAAAAAAAAAAAAAAAA", &[]),
        ("AKIAAAAAAAAAAAAAAAAAA", &[]),
        ("AKIA0000000000000000a", &[]),
        ("AKIA0000000000000000-", &[(0, 20)]),
        ("AKIA0000000000000000_", &[(0, 20)]),
        ("AKIA0000000000000000\u{e9}", &[(0, 20)]),
        ("AKIA000000000000000a", &[]),
        ("ASIAZ9Z9Z9Z9Z9Z9Z9Z9", &[(0, 20)]),
        ("AKIBAAAAAAAAAAAAAAAA", &[]),
        ("xAKIAAAAAAAAAAAAAAAAA", &[]),
        ("_AKIAAAAAAAAAAAAAAAAA", &[(1, 21)]),
        ("AKIAAAAAAAAAAAAAAAAAAKIABBBBBBBBBBBBBBBB", &[]),
        ("xoxb-aaaaaaaaaa", &[(0, 15)]),
        ("xoxb-aaaaaaaaa", &[]),
        ("xoxa-1-2-3-4-5", &[]),
        ("xoxp-1-1-1-1-1-1-", &[(0, 17)]),
        ("xoxr-aaaaaaaaaaaa", &[(0, 17)]),
        ("xoxs-aaaaaaaaaaaa", &[(0, 17)]),
        ("xoxz-aaaaaaaaaaaa", &[]),
        ("xoxb_aaaaaaaaaaaa", &[]),
        ("xapp-aaaaaaaaaa", &[(0, 15)]),
        ("xapp-1-A0-bbbbbbbbbbbb", &[(0, 22)]),
        ("xappaaaaaaaaaaaa", &[]),
        ("_xoxb-aaaaaaaaaaaa", &[(1, 18)]),
        ("xoxb-aaaaa_aaaaaaaaaa", &[]),
        ("sk_live_aaaaaaaaaaaaaaaa", &[(0, 24)]),
        ("sk_live_aaaaaaaaaaaaaaa", &[]),
        ("pk_live_AAAAAAAAAAAAAAAAAAAA", &[(0, 28)]),
        ("rk_live_9999999999999999", &[(0, 24)]),
        ("xk_live_aaaaaaaaaaaaaaaa", &[]),
        ("sk_test_aaaaaaaaaaaaaaaa", &[]),
        ("whsec_aaaaaaaaaaaaaaaa", &[(0, 22)]),
        ("whsec_aaaaaaaaaaaaaaa", &[]),
        ("sk_live_a_a_a_a_a_a_a_a_a_a_", &[]),
        ("_sk_live_aaaaaaaaaaaaaaaa", &[(1, 25)]),
        ("xsk_live_aaaaaaaaaaaaaaaa", &[]),
        ("AIzaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[(0, 34)]),
        ("AIzaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[]),
        ("AIzaa-_a-_a-_a-_a-_a-_a-_a-_a-_a-_", &[(0, 34)]),
        ("SG.aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb", &[(0, 36)]),
        ("SG.aaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb", &[]),
        ("SG.aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbb", &[]),
        ("SG.aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.c", &[(0, 60)]),
        ("SG.aaaaaaaaaaaaaaaab", &[]),
        ("SG.a-_a-_a-_a-_a-_a-_.b-_b-_b-_b-_b-_b-_", &[(0, 40)]),
        ("xSG.aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb", &[]),
        ("SGaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb", &[]),
        ("npm_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[(0, 34)]),
        ("npm_aaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[]),
        ("glpat-aaaaaaaaaaaaaaaa", &[(0, 22)]),
        ("glpat-aaaaaaaaaaaaaaa", &[]),
        ("glpat-a-_a-_a-_a-_a-_a-_", &[(0, 24)]),
        ("dop_v1_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[(0, 39)]),
        ("dop_v1_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[]),
        ("hf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[(0, 33)]),
        ("hf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[]),
        ("dckr_pat_aaaaaaaaaaaaaaaaaaaa", &[(0, 29)]),
        ("dckr_pat_aaaaaaaaaaaaaaaaaaa", &[]),
        ("dckr_pat_a_-a_-a_-a_-a_-a_-a_-", &[(0, 30)]),
        ("shpat_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[(0, 38)]),
        ("shpat_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[]),
        ("lin_api_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[(0, 38)]),
        ("lin_api_aaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[]),
        ("pypi-AgEIcHlwaS5vcmcaaaaaaaaaaaaaaaaaaaa", &[(0, 40)]),
        ("pypi-AgEIcHlwaS5vcmcaaaaaaaaaaaaaaaaaaa", &[]),
        ("pypi-AgEIcHlwaS5vcmaaaaaaaaaaaaaaaaaaaaaaaaa", &[]),
        ("pypi-AgEIcHlwaS5vcmc-_-_-_-_-_-_-_-_-_-_", &[(0, 40)]),
        ("ghp_aaaaaaaaaaaaaaaaaaaa sk-bbbbbbbbbbbbbbbb AKIACCCCCCCCCCCCCCCC", &[(0, 24), (25, 44), (45, 65)]),
        ("sk-aaaaaaaaaaaaaaaa ghp_bbbbbbbbbbbbbbbbbbbb", &[(20, 44), (0, 19)]),
        ("npm_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa sk_live_bbbbbbbbbbbbbbbb hf_cccccccccccccccccccccccccccccc", &[(0, 34), (35, 59), (60, 93)]),
        ("sk-sk_live_aaaaaaaaaaaaaaaaaaaa", &[(0, 31), (3, 31)]),
        ("hf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaahf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", &[(0, 35)]),
        ("sk-sk-sk-sk-sk-sk-sk-sk-sk-sk-", &[(0, 30)]),
        ("sk-ask-ask-ask-ask-ask-ask-ask-ask-ask-a", &[(0, 40)]),
        ("SG.aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb SG.cccccccccccccccc.dddddddddddddddd", &[(0, 36), (37, 73)]),
        ("a.a.a.a.a.a.", &[]),
        ("sk-aaaaaaaaaaaaaaaa\u{212a}", &[(0, 19)]),
        ("", &[]),
    ];
    const BEARER: &[(&str, &[(usize, usize)])] = &[
        ("Bearer aaaaaaaaaaaaaaaa", &[(7, 23)]),
        ("Bearer aaaaaaaaaaaaaaa", &[]),
        ("bearer aaaaaaaaaaaaaaaa", &[(7, 23)]),
        ("BEARER aaaaaaaaaaaaaaaa", &[]),
        ("bEarer aaaaaaaaaaaaaaaa", &[]),
        ("Beareraaaaaaaaaaaaaaaa", &[]),
        ("Bearer\taaaaaaaaaaaaaaaa", &[(7, 23)]),
        ("Bearer\naaaaaaaaaaaaaaaa", &[(7, 23)]),
        ("Bearer\r\naaaaaaaaaaaaaaaa", &[(8, 24)]),
        ("Bearer\u{c}\u{b} aaaaaaaaaaaaaaaa", &[(9, 25)]),
        ("Bearer \t \naaaaaaaaaaaaaaaa", &[(10, 26)]),
        ("Bearer                aaaaaaaaaaaaaaaa", &[(22, 38)]),
        ("Bearer                 aaaaaaaaaaaaaaaa", &[]),
        ("Bearer               aaaaaaaaaaaaaaaa", &[(21, 37)]),
        (
            "Bearer\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\naaaaaaaaaaaaaaaa",
            &[(22, 38)],
        ),
        (
            "Bearer\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\naaaaaaaaaaaaaaaa",
            &[],
        ),
        ("Bearer\u{1c}aaaaaaaaaaaaaaaa", &[]),
        ("Bearer\u{1f}aaaaaaaaaaaaaaaa", &[]),
        ("Bearer\u{a0}aaaaaaaaaaaaaaaa", &[]),
        ("Bearer\u{85}aaaaaaaaaaaaaaaa", &[]),
        ("Bearer\u{2028}aaaaaaaaaaaaaaaa", &[]),
        ("Bearer \u{a0}aaaaaaaaaaaaaaaa", &[]),
        ("Bearer\u{2003} aaaaaaaaaaaaaaaa", &[]),
        ("Bearer a._~+/=-a._~+/=-a._~+/=-", &[(7, 31)]),
        ("Bearer ================", &[(7, 23)]),
        ("Bearer ----------------", &[(7, 23)]),
        ("Bearer abcdefghij!klmnopqrst", &[]),
        ("Bearer aaaaaaaaaaaaaaaaaaaa tail", &[(7, 27)]),
        ("Bearer aaaaaaaaaaaaaaa\u{e9}a", &[]),
        ("Bearer aaaaaaaaaaaaaaaa\u{e9}", &[(7, 23)]),
        ("Bearer aaaaaaaaaaaaaaaa\nbbbbbbbbbbbbbbbb", &[(7, 23)]),
        ("xBearer aaaaaaaaaaaaaaaa", &[]),
        ("-Bearer aaaaaaaaaaaaaaaa", &[(8, 24)]),
        ("_Bearer aaaaaaaaaaaaaaaa", &[(8, 24)]),
        (".Bearer aaaaaaaaaaaaaaaa", &[(8, 24)]),
        ("9Bearer aaaaaaaaaaaaaaaa", &[]),
        (" Bearer aaaaaaaaaaaaaaaa", &[(8, 24)]),
        ("\u{e9}Bearer aaaaaaaaaaaaaaaa", &[(8, 24)]),
        ("Authorization: Bearer abcdefghij0123456789xyz", &[(22, 45)]),
        (
            "authorization: bearer aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            &[(22, 62)],
        ),
        ("Bearer Bearer aaaaaaaaaaaaaaaa", &[(14, 30)]),
        ("Bearer BearerBearerBearer", &[(7, 25)]),
        ("Bearer aaaaaaaaaaaaaaaaBearer bbbbbbbbbbbbbbbb", &[(7, 29)]),
        (
            "Bearer aaaaaaaaaaaaaaaa Bearer bbbbbbbbbbbbbbbb",
            &[(7, 23), (31, 47)],
        ),
        ("Bearer", &[]),
        ("Bearer ", &[]),
        ("Bearer Bearer Bearer Bearer Bearer Bearer ", &[]),
        ("Bearer aaaaaaaaaaaaaaaaBearer", &[(7, 29)]),
        ("Bear\u{435}r aaaaaaaaaaaaaaaa", &[]),
        ("", &[]),
    ];
    const BASIC: &[(&str, &[(usize, usize)])] = &[
        ("Authorization: Basic dXNlcjpwYXNz", &[(21, 33)]),
        ("Authorization: Basic dXNlcjpwYXNz==", &[(21, 35)]),
        ("Authorization: Basic dXNlcjpwYXNz=", &[(21, 34)]),
        ("Authorization: Basic dXNlcjpwYXNz===", &[(21, 35)]),
        ("Authorization: Basic dXNlcjpwYXNz=x", &[(21, 34)]),
        ("Authorization: Basic dXNlcjpwYXNz= =", &[(21, 34)]),
        ("Proxy-Authorization: Basic dXNlcjpwYXNz", &[(27, 39)]),
        ("proxy-authorization: basic dXNlcjpwYXNz", &[(27, 39)]),
        ("PROXY-AUTHORIZATION: BASIC dXNlcjpwYXNz", &[(27, 39)]),
        ("AUTHORIZATION=basic dXNlcjpwYXNz", &[(20, 32)]),
        ("authorization = basic dXNlcjpwYXNz", &[(22, 34)]),
        ("aUtHoRiZaTiOn:BaSiC dXNlcjpwYXNz", &[(20, 32)]),
        ("Authorization:Basic dXNlcjpwYXNz", &[(20, 32)]),
        ("Authorization:   Basic dXNlcjpwYXNz", &[(23, 35)]),
        ("Authorization:    Basic dXNlcjpwYXNz", &[]),
        ("Authorization   : Basic dXNlcjpwYXNz", &[(24, 36)]),
        ("Authorization    : Basic dXNlcjpwYXNz", &[]),
        ("Authorization\t:\tBasic dXNlcjpwYXNz", &[(22, 34)]),
        ("Authorization Basic dXNlcjpwYXNz", &[]),
        ("Authorization; Basic dXNlcjpwYXNz", &[]),
        ("Authorization:: Basic dXNlcjpwYXNz", &[]),
        ("Authorization: \"Basic dXNlcjpwYXNz\"", &[(22, 34)]),
        ("Authorization: 'Basic dXNlcjpwYXNz'", &[(22, 34)]),
        ("Authorization: \"\"Basic dXNlcjpwYXNz", &[]),
        ("Authorization: `Basic dXNlcjpwYXNz", &[]),
        ("Authorization: \u{201c}Basic dXNlcjpwYXNz", &[]),
        ("Authorization: BasicdXNlcjpwYXNz", &[]),
        ("Authorization: Basic", &[]),
        ("Authorization: Basic ", &[]),
        (
            "Authorization: Basic                dXNlcjpwYXNz",
            &[(36, 48)],
        ),
        ("Authorization: Basic                 dXNlcjpwYXNz", &[]),
        ("Authorization: Basic                  dXNlcjpwYXNz", &[]),
        ("Authorization: Basic\t\tdXNlcjpwYXNz", &[(22, 34)]),
        ("Authorization: Basic\ndXNlcjpwYXNz", &[]),
        ("Authorization: Basic abc", &[]),
        ("Authorization: Basic abcd", &[(21, 25)]),
        ("Authorization: Basic ab=cd", &[]),
        ("Authorization: Basic a+/_-b", &[(21, 27)]),
        ("Authorization: Basic ab.cd", &[]),
        ("Authorization: Basic aaaaa\u{e9}bcd", &[(21, 26)]),
        ("Authorization: Basic ab\u{e9}cd", &[]),
        ("xAuthorization: Basic dXNlcjpwYXNz", &[]),
        ("-Authorization: Basic dXNlcjpwYXNz", &[(22, 34)]),
        ("_Authorization: Basic dXNlcjpwYXNz", &[(22, 34)]),
        (".Authorization: Basic dXNlcjpwYXNz", &[(22, 34)]),
        ("9Authorization: Basic dXNlcjpwYXNz", &[]),
        (
            "Proxy-Authorization: Basic dXNlcjpwYXNz Authorization: Basic dXNlcjpwYXNz",
            &[(27, 39), (61, 73)],
        ),
        ("Proxy-Basic dXNlcjpwYXNz", &[]),
        ("Proxy Authorization: Basic dXNlcjpwYXNz", &[(27, 39)]),
        ("Authorizatio: Basic dXNlcjpwYXNz", &[]),
        ("Authorizationx: Basic dXNlcjpwYXNz", &[]),
        ("Proxy-Proxy-Authorization: Basic dXNlcjpwYXNz", &[(33, 45)]),
        ("Authorization: Ba\u{17f}ic dXNlcjpwYXNz", &[]),
        ("Authorization: BA\u{17f}IC dXNlcjpwYXNz", &[]),
        ("Authorizat\u{131}on: Basic dXNlcjpwYXNz", &[]),
        ("Authorizat\u{130}on: Basic dXNlcjpwYXNz", &[]),
        ("Authorization: Ba\u{17f}\u{131}c dXNlcjpwYXNz", &[]),
        ("\u{212a}Authorization: Basic dXNlcjpwYXNz", &[(22, 34)]),
        (
            "Authorization: Basic dXNlcjpwYXNz\nProxy-Authorization: Basic dXNlcjpwYXNz",
            &[(21, 33), (61, 73)],
        ),
        (
            "Authorization: Basic Authorization: Basic Authorization: Basic Authorization: Basic ",
            &[(21, 34), (63, 76)],
        ),
        (
            "Authorization:Basic Authorization:Basic Authorization:Basic Authorization:Basic ",
            &[(20, 33), (60, 73)],
        ),
        ("", &[]),
    ];
    const USERINFO: &[(&str, &[(usize, usize)])] = &[
        ("postgres://user:p@ss@host/db", &[(11, 20)]),
        ("https://bob:a@b@c@example.com/r.git", &[(8, 17)]),
        ("redis://:onlypw@cache:6379/0", &[(8, 15)]),
        ("a://u:p@h", &[(4, 7)]),
        ("a://u:@h", &[(4, 6)]),
        ("a://:@h", &[(4, 5)]),
        ("a://u:p", &[]),
        ("a://u:p/x@h", &[]),
        ("a://u:p @h", &[]),
        ("a://u:p\n@h", &[]),
        ("a://u@v:p@h", &[]),
        ("a://u:p:q@h", &[(4, 9)]),
        ("a://u:p@", &[(4, 7)]),
        ("a://u:@", &[(4, 6)]),
        ("a://:@", &[(4, 5)]),
        ("a://u@:p@h", &[]),
        ("a://@:p@h", &[]),
        ("a://u:p@h b://v:q@h", &[(4, 7), (14, 17)]),
        ("a://u:p@b://v:q@h", &[(4, 7), (12, 15)]),
        ("a://u:p@h/b://v:q@h", &[(4, 7), (14, 17)]),
        ("a://u:p@@h", &[(4, 8)]),
        ("a://u:@@@", &[(4, 8)]),
        ("a://u:p@h@", &[(4, 9)]),
        (
            "x://uuuuuuuuuuuuuuuuuuuu:pppppppppppppppppppp@h",
            &[(4, 45)],
        ),
        ("ab c://u:p@h", &[(7, 10)]),
        ("ab\tc://u:p@h", &[(7, 10)]),
        ("a://u:\u{a0}p@h", &[(4, 8)]),
        ("a://\u{a0}u:p@h", &[(4, 8)]),
        ("a://u:p\u{a0}@h", &[(4, 8)]),
        ("a://u:p\u{2028}@h", &[(4, 8)]),
        ("a://u:p\u{1c}@h", &[(4, 8)]),
        ("a://u:p\u{b}@h", &[]),
        ("a://u:p\u{c}@h", &[]),
        ("a://\u{e9}:\u{1f600}@h", &[(4, 7)]),
        ("\u{63a5}\u{7d9a}postgres://u:p@h", &[(13, 16)]),
        ("\u{e9}a://u:p@h", &[(5, 8)]),
        ("xhttp://u:p@h", &[(8, 11)]),
        ("1a://u:p@h", &[]),
        ("+a://u:p@h", &[]),
        ("-a://u:p@h", &[]),
        (".a://u:p@h", &[]),
        ("_a://u:p@h", &[(5, 8)]),
        ("a.b-c+d://u:p@h", &[(10, 13)]),
        ("a1://u:p@h", &[(5, 8)]),
        ("a_b://u:p@h", &[(6, 9)]),
        ("a b://u:p@h", &[(6, 9)]),
        ("://u:p@h", &[]),
        ("a:/u:p@h", &[]),
        ("a:///u:p@h", &[]),
        ("a:://u:p@h", &[]),
        ("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx://u:p@h", &[(35, 38)]),
        ("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx://u:p@h", &[(34, 37)]),
        ("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx://u:p@h", &[]),
        ("ab-b-b-b-b-b-b-b-b-b-b-b-b-b-b-c://u:p@h", &[(35, 38)]),
        ("9xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx://u:p@h", &[]),
        ("-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx://u:p@h", &[]),
        ("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-://u:p@h", &[(35, 38)]),
        ("https://user:pass@host.com:8080/path?q=1@2", &[(8, 17)]),
        (
            "git clone https://bob:secret@github.com/a/b.git",
            &[(18, 28)],
        ),
        ("ssh://git@host/r", &[]),
        ("mongodb+srv://u:p@c.example/db?x=y@z", &[(14, 17)]),
        ("a://b:b:b:a://b:b:b:a://b:b:b:a://b:b:b:a://b:b:b:", &[]),
        ("a://uuuuuuuuuuuuuuuuuuuu::a://uuuuuuuuuuuuuuuuuuuu:", &[]),
        ("a://:@:@:@:@:@", &[(4, 13)]),
        ("a://u:@@@@@", &[(4, 10)]),
        ("a://:::::::::::::::::::::", &[]),
        (
            "a://u:p@a://u:p@a://u:p@a://u:p@",
            &[(4, 7), (12, 15), (20, 23), (28, 31)],
        ),
        ("http://u:p@h/x http://v:q@h/y", &[(7, 10), (22, 25)]),
        ("", &[]),
    ];
    const HEADER: &[HeaderRow] = &[
        ("Cookie: a=b", 0, Some((0, 8))),
        ("Cookie:a", 0, Some((0, 7))),
        ("cookie:a", 0, Some((0, 7))),
        ("COOKIE:a", 0, Some((0, 7))),
        ("Set-Cookie: a=b", 0, Some((0, 12))),
        ("set-cookie:a", 0, Some((0, 11))),
        ("Proxy-Authorization: x", 0, Some((0, 21))),
        ("Authorization: Token x", 0, Some((0, 15))),
        ("Proxy-Cookie: x", 0, Some((0, 14))),
        ("Set-Authorization: x", 0, Some((0, 19))),
        ("Set-Proxy-Cookie: x", 0, None),
        ("Proxy-Set-Cookie: x", 0, None),
        ("Authorization: Bearer x", 0, None),
        ("Authorization: Basic x", 0, None),
        ("Authorization:Bearer x", 0, None),
        ("Authorization:   bearer x", 0, None),
        ("AUTHORIZATION: BASIC x", 0, None),
        ("Authorization: Bearer", 0, None),
        ("Authorization: Basic", 0, None),
        ("Authorization: Bearer,", 0, None),
        ("Authorization: Bearer\n", 0, None),
        ("Authorization: Bearerx", 0, Some((0, 15))),
        ("Authorization: Basicx", 0, Some((0, 15))),
        ("Authorization: Bearer_", 0, Some((0, 15))),
        ("Authorization: Basic9", 0, Some((0, 15))),
        ("Authorization: Basic-", 0, None),
        ("Authorization: Bearer\u{e9}", 0, None),
        ("Authorization: Basic\u{212a}", 0, None),
        ("Authorization: Bear", 0, Some((0, 15))),
        ("Authorization: Bas", 0, Some((0, 15))),
        ("Cookie: Bearer a; x=y", 0, None),
        ("Cookie: Basic", 0, None),
        ("Cookie: Basically fine", 0, Some((0, 8))),
        ("Cookie: Bearers x", 0, Some((0, 8))),
        ("Cookie:\n", 0, None),
        ("Cookie: \nfoo", 0, None),
        ("Cookie:", 0, None),
        ("Cookie: ", 0, None),
        ("Cookie:   ", 0, None),
        ("Cookie: \"x\"", 0, None),
        ("Cookie: 'x'", 0, None),
        ("Cookie:   \"x\"", 0, None),
        ("Cookie: \tx", 0, Some((0, 9))),
        ("Cookie:\t\tx", 0, Some((0, 9))),
        ("Cookie:   x", 0, Some((0, 10))),
        ("Cookie: \u{a0}x", 0, Some((0, 8))),
        ("Cookie: \u{1c}x", 0, Some((0, 8))),
        ("Cookie: \u{b}x", 0, None),
        ("Cookie: \u{c}x", 0, None),
        ("Cookie: \u{2028}x", 0, Some((0, 8))),
        ("Cookie : x", 0, Some((0, 9))),
        ("Cookie\t:x", 0, Some((0, 8))),
        ("Cookie  \t :x", 0, Some((0, 11))),
        ("Cookie\n: x", 0, None),
        ("Cookie x", 0, None),
        ("Cookie=x", 0, None),
        ("Cookie; x", 0, None),
        ("x-Cookie:y", 0, None),
        ("_Cookie: y", 0, None),
        ("-Cookie: y", 0, None),
        ("9Cookie: y", 0, None),
        ("xCookie: y", 0, None),
        (".Cookie: y", 0, Some((1, 9))),
        (" Cookie: y", 0, Some((1, 9))),
        ("\u{e9}Cookie: y", 0, Some((1, 9))),
        ("\u{212a}Cookie: y", 0, Some((1, 9))),
        ("X-Set-Cookie: y", 0, None),
        ("my.Authorization: y", 0, Some((3, 18))),
        ("Cookies: y", 0, None),
        ("Cookiee: y", 0, None),
        ("Authorizations: y", 0, None),
        ("Coo\u{212a}ie: x", 0, None),
        ("Set-Coo\u{212a}ie: x", 0, None),
        ("\u{17f}et-Cookie: x", 0, None),
        ("Cookie\u{131}: x", 0, None),
        ("Author\u{131}zation: x", 0, None),
        ("Author\u{130}zation: x", 0, None),
        ("Authorizat\u{ef}on: x", 0, None),
        ("\"Cookie: x\"", 0, Some((1, 9))),
        ("curl -H 'Cookie: a=b' http://x", 0, Some((9, 17))),
        ("Cookie: \"a\" Cookie: b", 0, Some((12, 20))),
        ("Cookie: \nCookie: b", 0, Some((9, 17))),
        ("Cookie: a; Set-Cookie: b", 0, Some((0, 8))),
        ("Cookie: a; Set-Cookie: b", 1, Some((11, 23))),
        ("Cookie: a; Set-Cookie: b", 11, Some((11, 23))),
        ("Cookie: a; Set-Cookie: b", 15, None),
        ("Cookie: a; Set-Cookie: b", 16, None),
        ("Cookie: a", 1, None),
        ("Cookie: a", 8, None),
        ("Cookie: a", 9, None),
        ("Cookie: a", 10, None),
        ("Cookie: a", 99, None),
        ("Cookie: a", 0, Some((0, 8))),
        ("x Cookie: a", 2, Some((2, 10))),
        ("x Cookie: a", 3, None),
        ("xCookie: a", 1, None),
        ("-Cookie: a", 1, None),
        ("Set-Cookie: a", 4, None),
        ("Set-Cookie: a", 1, None),
        ("Proxy-Authorization: a", 6, None),
        ("", 0, None),
        ("", 3, None),
        ("C", 0, None),
        ("Cookie: Cookie: a", 0, Some((0, 8))),
        ("Cookie:Cookie:a", 0, Some((0, 7))),
        ("Cookie:Cookie:a", 7, Some((7, 14))),
        ("Cookie: Cookie: Cookie: Cookie: ", 0, Some((0, 8))),
        ("Cookie:                    x", 0, Some((0, 27))),
        ("Cookie:                    ", 0, None),
    ];

    #[test]
    fn pem_vectors() {
        check(pem_spans, PEM);
    }

    #[test]
    fn jwt_vectors() {
        check(jwt_spans, JWT);
    }

    #[test]
    fn vendor_token_vectors() {
        check(vendor_token_spans, VENDOR);
    }

    #[test]
    fn bearer_vectors() {
        check(bearer_spans, BEARER);
    }

    #[test]
    fn basic_vectors() {
        check(basic_spans, BASIC);
    }

    #[test]
    fn userinfo_vectors() {
        check(userinfo_spans, USERINFO);
    }

    #[test]
    fn header_prefix_vectors() {
        for &(input, pos, want) in HEADER {
            let got = header_prefix_search(&chars(input), pos).map(|h| (h.start, h.value_start));
            assert_eq!(got, want, "input {input:?} from {pos}");
        }
    }

    /// Small deterministic xorshift, enough to walk the scanners over many odd strings.
    struct Rng(u32);

    impl Rng {
        fn below(&mut self, n: usize) -> usize {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 17;
            self.0 ^= self.0 << 5;
            usize::try_from(self.0).map_or(0, |v| v % n)
        }
    }

    #[test]
    fn every_scanner_reports_in_range_nonempty_spans_on_odd_input() {
        const PIECES: [&str; 40] = [
            "-----BEGIN ",
            "-----END ",
            "PRIVATE KEY",
            " BLOCK",
            "-----",
            "eyJ",
            ".",
            "-",
            "_",
            "ghp_",
            "github_pat_",
            "sk-",
            "AKIA",
            "xoxb-",
            "SG.",
            "pypi-AgEIcHlwaS5vcmc",
            "Bearer",
            "Authorization",
            "Proxy-",
            "Set-Cookie",
            "Basic",
            "://",
            ":",
            "@",
            "/",
            " ",
            "\t",
            "\n",
            "=",
            "\"",
            "aaaaaaaaaaaaaaaaaaaa",
            "0123456789abcdef",
            "\u{212a}",
            "\u{17f}",
            "\u{e9}",
            "\u{1f600}",
            "\u{a0}",
            "\x1c",
            "\x0b",
            "x",
        ];
        let scanners: [Scanner; 6] = [
            pem_spans,
            jwt_spans,
            vendor_token_spans,
            bearer_spans,
            basic_spans,
            userinfo_spans,
        ];
        let mut rng = Rng(0x2545_f491);
        for _ in 0..20_000 {
            let mut text = String::new();
            for _ in 0..=rng.below(10) {
                text.push_str(PIECES[rng.below(PIECES.len())]);
            }
            let s = chars(&text);
            for scan in scanners {
                let mut spans = Vec::new();
                scan(&s, &mut spans);
                assert!(
                    spans.iter().all(|&(a, b)| a < b && b <= s.len()),
                    "{text:?} {spans:?}"
                );
            }
            let pos = rng.below(s.len() + 3);
            if let Some(h) = header_prefix_search(&s, pos) {
                assert!(
                    pos <= h.start && h.start < h.value_start && h.value_start < s.len(),
                    "{text:?} {h:?}"
                );
            }
        }
    }

    /// Runs per timing: the minimum of this many.
    const RUNS: usize = 3;
    /// Extra runs the whole text gets: it can only stop early on success, so a linear scan on a loaded host keeps looking for one undisturbed run while a quadratic one fails every time.
    const WHOLE_RUNS: usize = 5 * RUNS;

    fn flood(unit: &str, len: usize) -> Vec<char> {
        let unit = chars(unit);
        unit.iter().copied().cycle().take(len).collect()
    }

    /// Run one scanner over `text` as its caller would run it.
    fn scan(name: &str, text: &[char]) {
        let mut spans = Vec::new();
        match name {
            "pem" => pem_spans(text, &mut spans),
            "jwt" => jwt_spans(text, &mut spans),
            "vendor" => vendor_token_spans(text, &mut spans),
            "bearer" => bearer_spans(text, &mut spans),
            "basic" => basic_spans(text, &mut spans),
            "userinfo" => userinfo_spans(text, &mut spans),
            _ => {
                // the `_header_spans` loop without the value reader: the next search starts at the value
                let mut pos = 0;
                while let Some(h) = header_prefix_search(text, pos) {
                    spans.push((h.start, h.value_start));
                    pos = h.value_start.max(pos + 1);
                }
            }
        }
        std::hint::black_box(&spans);
    }

    /// The floods of `tests/test_redact.py` (its 1 MB and 200 KB adversarial inputs) plus shapes that aim at each scanner's backtracking.
    ///
    /// Each flood of 512 K characters is scanned twice: whole, and as 16 separate pieces of 32 K. Both runs read the same memory, so cache and memory-bandwidth effects (a 512 K flood is 2 MB of `char`s, past the per-core cache, which made a plain small-against-large comparison drift to 4 times the linear ratio on a loaded host) cancel out. A linear scan takes about as long on the whole text as on its pieces; a quadratic one takes about 16 times as long (one text of 512 K against 16 of 32 K). The limit is 4, the same distance from both. Each side takes the minimum of its runs (the whole text up to 15, stopping at the first run within the limit, since the minimum can only shrink) and the pieces' time is floored at 1 ms.
    #[test]
    fn scanners_stay_linear_on_floods() {
        const BIG: usize = 512_000;
        const PIECE: usize = 32_000;
        const PIECES: u32 = (BIG / PIECE) as u32;
        const LIMIT: u32 = 4;
        let userinfo_unit = format!("a://{}:", "u".repeat(200));
        let units = [
            "eyJ-",
            "a.",
            "a-",
            "a+",
            "a.b-",
            "x://:",
            "-",
            "Bearer ",
            "-----BEGIN PRIVATE KEY-----",
            "-----BEGIN ",
            "a://b:b:b:",
            "Authorization: Basic ",
            "Authorization:x",
            "Cookie: ",
            "Cookie:",
            "a://u:",
            "SG.aaaaaaaaaaaaaaaa.",
            "sk-",
            "ghp_aaaa_",
            "AKIA",
            "xoxb-a",
            "eyJaaaaa.",
            "Bearer                  ",
            "Authorization:   ",
            "-----BEGIN AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "Proxy-Authorization: \"Basic ",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_",
            userinfo_unit.as_str(),
            // the single-run shapes: one huge run of word characters, one huge blank gap
            "a",
            " ",
        ];
        const NAMES: [&str; 7] = [
            "pem", "jwt", "vendor", "bearer", "basic", "userinfo", "header",
        ];
        for unit in units {
            let big = flood(unit, BIG);
            for name in NAMES {
                let t_pieces = best_of(RUNS, Duration::ZERO, || {
                    for piece in big.chunks(PIECE) {
                        scan(name, piece);
                    }
                });
                let floor = t_pieces.max(Duration::from_millis(1));
                let t_whole = best_of(WHOLE_RUNS, LIMIT * floor, || scan(name, &big));
                let ratio = t_whole.as_secs_f64() / floor.as_secs_f64();
                assert!(
                    t_whole <= LIMIT * floor,
                    "{name} on {unit:?}: {BIG} chars in {PIECES} pieces {t_pieces:?}, whole {t_whole:?}, ratio {ratio:.1} over limit {LIMIT} (linear is about 1, quadratic about {PIECES})"
                );
            }
        }
    }
}
