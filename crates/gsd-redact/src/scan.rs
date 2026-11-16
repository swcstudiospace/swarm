//! The value scanner's reader-independent pieces: the tail-name patterns `_continuation` uses to see whether a value swallowed
//! the name of a further assignment, `_KEY_VALUE_RE` with `_key_value_end`, and `_tail_name_start` (redact.py:689-736, 991-1002).
//!
//! Every scanner has the semantics of the Python call it stands for (`fullmatch(s, lo, end)` or `match(s, pos)`): lookbehinds
//! CAN see the characters before `lo`; lookaheads and `\Z` cannot see past `end` of a `fullmatch`, while a plain `match` sees
//! the whole string. Each pattern is deterministic once its caps are applied (the class that follows a greedy run excludes the
//! run's characters, so backtracking to a shorter run cannot succeed), so each scanner does bounded work.
//!
//! The detectors that need the quote reader are appended after the leaf functions: [`continuation`] (`_continuation`,
//! redact.py:955) and [`scan`] (`_scan`, redact.py:1009).

use crate::cls::{is_alnum, is_alpha, is_blank, is_space, is_word};
use crate::keys::{key_name, long_secret, name_hit};
use crate::patterns::{flag_bare, flag_sep, name_bare, name_sep};
use crate::reader::{Bare, Reader};
use crate::REDACTED;

/// `_TAIL_WINDOW` (redact.py:696): how far back from a value's end `_continuation` looks for the swallowed name.
pub const TAIL_WINDOW: usize = 200;

/// `_TAIL_DELIMS` (redact.py:697): characters after which a swallowed name can start.
pub const TAIL_DELIMS: [char; 5] = [',', ';', '&', '/', '?'];

fn at(s: &[char], i: usize) -> Option<char> {
    s.get(i).copied()
}

/// `[A-Za-z0-9_.-]`, `_NAME_CHARS` (redact.py:991).
pub fn is_name_char(c: char) -> bool {
    is_word(c) || c == '.' || c == '-'
}

/// `[=:]`.
fn is_sep(c: char) -> bool {
    c == '=' || c == ':'
}

/// A match of [`tail_name_full`]: group 1 is `lo..name_end`, group 2 is the optional `=`/`:` that follows it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TailName {
    /// End of group 1 (the name).
    pub name_end: usize,
    /// Group 2: `Some('=')`, `Some(':')`, or `None` when it is empty.
    pub sep: Option<char>,
}

/// Python `_TAIL_NAME_RE.fullmatch(s, lo, end)` (redact.py:693):
/// `(?<![A-Za-z0-9_])(?<![A-Za-z0-9_][.-])([A-Za-z_][A-Za-z0-9_.-]{0,127})([=:]?)`.
///
/// The match must span `lo..end`. A name character is never `=`/`:`, so the name is `lo..end`, or `lo..end-1` followed by
/// the separator; a shorter name would leave a name character that neither the separator nor the end accepts.
pub fn tail_name_full(s: &[char], lo: usize, end: usize) -> Option<TailName> {
    let end = end.min(s.len());
    let first = at(s, lo)?;
    if lo >= end || !(is_alpha(first) || first == '_') {
        return None;
    }
    // (?<![A-Za-z0-9_]) and (?<![A-Za-z0-9_][.-]): they look before `lo` even though the match is bounded.
    if lo > 0 && at(s, lo - 1).is_some_and(is_word) {
        return None;
    }
    if lo > 1
        && at(s, lo - 1).is_some_and(|c| c == '.' || c == '-')
        && at(s, lo - 2).is_some_and(is_word)
    {
        return None;
    }
    let mut i = lo + 1;
    while i < end && at(s, i).is_some_and(is_name_char) {
        i += 1;
    }
    let (name_end, sep) = if i == end {
        (end, None)
    } else if i + 1 == end && at(s, i).is_some_and(is_sep) {
        (i, at(s, i))
    } else {
        return None;
    };
    // {0,127} after the first character: the name is at most 128 characters.
    (name_end - lo <= 128).then_some(TailName { name_end, sep })
}

/// Python `_TAIL_FLAG_RE.fullmatch(s, lo, end) is not None` (redact.py:694): `(?<![A-Za-z0-9_-])--?[A-Za-z][A-Za-z0-9_-]{0,63}`.
///
/// `--?` takes the second dash when there is one (a lone dash would be followed by a dash, not a letter).
pub fn tail_flag_full(s: &[char], lo: usize, end: usize) -> bool {
    let end = end.min(s.len());
    if lo >= end || at(s, lo) != Some('-') {
        return false;
    }
    if lo > 0 && at(s, lo - 1).is_some_and(|c| is_alnum(c) || c == '_' || c == '-') {
        return false;
    }
    let mut i = lo + 1;
    if at(s, i) == Some('-') {
        i += 1;
    }
    if i >= end || !at(s, i).is_some_and(is_alpha) {
        return false;
    }
    let rest = &s[i + 1..end];
    rest.len() <= 63 && rest.iter().all(|&c| is_alnum(c) || c == '_' || c == '-')
}

/// End of the blank run at `pos`, but only if the run is at most `max` long (a longer run is followed by a blank, which no
/// pattern here accepts, whatever the backtracking). `None` when it is longer.
fn blanks_upto(s: &[char], pos: usize, max: usize) -> Option<usize> {
    let mut i = pos;
    while at(s, i).is_some_and(is_blank) {
        i += 1;
        if i - pos > max {
            return None;
        }
    }
    Some(i)
}

/// End of up to `max` blanks at `pos` (greedy, no failure).
fn blanks_take(s: &[char], pos: usize, max: usize) -> usize {
    let mut i = pos;
    while i - pos < max && at(s, i).is_some_and(is_blank) {
        i += 1;
    }
    i
}

/// A match of [`after_name`]: the separator and the end of the match.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AfterName {
    /// Group 1: `=` or `:`.
    pub sep: char,
    /// `m.end()`: after the blanks that follow the separator (at most 32).
    pub end: usize,
}

/// Python `_AFTER_NAME_RE.match(s, pos)` (redact.py:695): `[ \t]{0,32}([=:])[ \t]{0,32}`.
pub fn after_name(s: &[char], pos: usize) -> Option<AfterName> {
    let i = blanks_upto(s, pos, 32)?;
    let sep = at(s, i).filter(|&c| is_sep(c))?;
    Some(AfterName {
        sep,
        end: blanks_take(s, i + 1, 32),
    })
}

/// Python `_AFTER_SEP_RE.match(s, pos).end()` (redact.py:696): `[ \t]{0,32}` always matches.
pub fn after_sep(s: &[char], pos: usize) -> usize {
    blanks_take(s, pos, 32)
}

/// Python `_FLAG_GAP_RE.match(s, pos)` end (redact.py:697): `[ \t]{1,8}(?![-\s])` with `re.ASCII`.
///
/// Fewer blanks than the whole run would be followed by a blank, which is `\s`, so only the full run (at most 8) can match.
pub fn flag_gap(s: &[char], pos: usize) -> Option<usize> {
    let i = blanks_upto(s, pos, 8)?;
    if i == pos || at(s, i).is_some_and(|c| c == '-' || is_space(c)) {
        return None;
    }
    Some(i)
}

/// `_KEY_VALUE_RE.match(s, pos)` (redact.py:708): `(\\?["']?)([A-Za-z0-9_+/=~.-]{20,})`.
///
/// Returns `(group 1 end, match end)`; group 2 is `group1_end..match_end`. The prefix takes a backslash and a quote greedily;
/// neither is in the token class, so a shorter prefix could not start a token.
fn key_value_match(s: &[char], pos: usize) -> Option<(usize, usize)> {
    let mut g1 = pos;
    if at(s, g1) == Some('\\') {
        g1 += 1;
    }
    if at(s, g1).is_some_and(|c| c == '"' || c == '\'') {
        g1 += 1;
    }
    let mut end = g1;
    while at(s, end).is_some_and(is_secret_char) {
        end += 1;
    }
    (end - g1 >= 20).then_some((g1, end))
}

/// `[A-Za-z0-9_+/=~.-]`, the token class of `_KEY_VALUE_RE`.
fn is_secret_char(c: char) -> bool {
    is_alnum(c) || matches!(c, '_' | '+' | '/' | '=' | '~' | '.' | '-')
}

/// Python `_key_value_end(s, pos)` (redact.py:731): `(end, checked_run_end)`.
///
/// `end` is `None` (Python `-1`) when there is no credential-shaped token at `pos`; otherwise the end of the token plus its
/// closing quote when the opening quote group is non-empty and repeated right after it. `checked_run_end` is the end of the
/// token run that was checked, or 0 when there is no run.
pub fn key_value_end(s: &[char], pos: usize) -> (Option<usize>, usize) {
    let Some((g1, run_end)) = key_value_match(s, pos) else {
        return (None, 0);
    };
    if !long_secret(&s[g1..run_end]) {
        return (None, run_end);
    }
    let q = &s[pos..g1];
    let closes = !q.is_empty() && s.get(run_end..run_end + q.len()) == Some(q);
    (
        Some(if closes { run_end + q.len() } else { run_end }),
        run_end,
    )
}

/// Python `_tail_name_start(s, floor, end)` (redact.py:994): where the name (and its `=`/`:`) that a value may have swallowed
/// at its end starts: at most 130 characters back, never before `floor`.
pub fn tail_name_start(s: &[char], floor: usize, end: usize) -> usize {
    let mut i = end.min(s.len());
    if i > floor && at(s, i - 1).is_some_and(is_sep) {
        i -= 1;
    }
    let low = floor.max(i.saturating_sub(130));
    while i > low && at(s, i - 1).is_some_and(is_name_char) {
        i -= 1;
    }
    i
}

/// Which of the two uses of Python `_scan` to run (its `sep_re`, `bare_re` and `eq_sep` arguments travel together).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScanKind {
    /// `NAME=value` and `NAME: value`: `_NAME_SEP_RE`, `_NAME_BARE_RE`, `eq_sep=True`.
    Name,
    /// `--flag value`: `_FLAG_SEP_RE`, `_FLAG_BARE_RE`, `eq_sep=False`.
    Flag,
}

impl ScanKind {
    /// The `bare_re` of the kind, as the reader takes it.
    fn bare(self) -> Bare {
        match self {
            ScanKind::Name => name_bare,
            ScanKind::Flag => flag_bare,
        }
    }

    /// `sep_re.search(s, pos)` as `(start, name_end, end, "=" in m.group(2))`. A flag separator is blanks only, so `eq` is false.
    fn search(self, s: &[char], pos: usize) -> Option<(usize, usize, usize, bool)> {
        match self {
            ScanKind::Name => name_sep(s, pos).map(|m| (m.start, m.name_end, m.end, m.eq)),
            ScanKind::Flag => flag_sep(s, pos).map(|m| (m.start, m.name_end, m.end, false)),
        }
    }
}

/// Python `_continuation(s, pos, end, bare_re, rd)` (redact.py:955): the end of the next value segment that still belongs to
/// the secret whose value spans `pos..end` (the text is `rd.s`), or `None` (Python `-1`).
///
/// A value that stops at a blank or a quote may be followed by a further assignment whose name the value swallowed at its end
/// (`a=b c=d`): the window of at most [`TAIL_WINDOW`] characters before `end`, cut after the last [`TAIL_DELIMS`] character,
/// must be exactly such a name, or a flag.
pub fn continuation(pos: usize, end: usize, bare: Bare, rd: &Reader) -> Option<usize> {
    let s = rd.s;
    let c = at(s, end);
    let escaped_quote = at(s, end) == Some('\\') && at(s, end + 1) == Some('"');
    if !matches!(c, Some(' ' | '\t' | '"' | '\'')) && !escaped_quote {
        return None;
    }
    let mut lo = pos.max(end.saturating_sub(TAIL_WINDOW));
    // `max(s.rfind(d, lo, end) for d in _TAIL_DELIMS)`: the last delimiter of the window.
    let cut = s
        .get(lo..end)
        .and_then(|w| w.iter().rposition(|c| TAIL_DELIMS.contains(c)));
    if let Some(k) = cut {
        lo += k + 1;
    } else if end.saturating_sub(pos) > TAIL_WINDOW {
        return None;
    }
    let colon; // `NAME://...` is a URL, not `NAME: value` (see `scan`)
    let gap_end;
    if let Some(a) = tail_name_full(s, lo, end) {
        let name = s.get(lo..a.name_end)?;
        if let Some(sep) = a.sep {
            if !name_hit(name, sep == '=') {
                return None;
            }
            gap_end = after_sep(s, end);
            colon = sep == ':';
        } else {
            let gap = after_name(s, end)?;
            if !name_hit(name, gap.sep == '=') {
                return None;
            }
            gap_end = gap.end;
            colon = gap.sep == ':';
        }
    } else if tail_flag_full(s, lo, end) && name_hit(s.get(lo..end)?, false) {
        gap_end = flag_gap(s, end)?;
        colon = false;
    } else {
        return None;
    }
    if colon && at(s, gap_end) == Some('/') && at(s, gap_end + 1) == Some('/') {
        return None;
    }
    let value_end = rd.read(gap_end, bare).0;
    (value_end != gap_end).then_some(value_end)
}

/// Python `_scan(s, sep_re, bare_re, eq_sep, add, rd)` (redact.py:1009) over the text of `rd`: reports the value that follows
/// every sensitive `NAME<sep>` match, appending its `(start, end)` code-point range to `add` in the order Python's `add` is
/// called. One forward pass with a cursor (no recursion, no rescans); a benign name only moves the cursor past its separator,
/// so what looks like its value is scanned too.
pub fn scan(kind: ScanKind, add: &mut Vec<(usize, usize)>, rd: &Reader) {
    let s = rd.s;
    let bare = kind.bare();
    let eq_sep = kind == ScanKind::Name;
    let mask: Vec<char> = REDACTED.chars().collect();
    let (mut pos, mut key_fail) = (0, 0);
    while let Some((nstart, nend, sep_end, eq)) = kind.search(s, pos) {
        pos = sep_end;
        if eq_sep && !eq && at(s, pos) == Some('/') && at(s, pos + 1) == Some('/') {
            continue; // `auth://host`, `token://x`: a URL scheme, not `NAME: value` (`PASSWORD=//x` is still a secret)
        }
        let Some(name) = s.get(nstart..nend) else {
            break;
        };
        let hit = name_hit(name, eq_sep && eq);
        if !hit && !key_name(name) {
            continue;
        }
        let start = pos;
        let mut end;
        if hit {
            end = rd.read(pos, bare).0;
            if end == pos {
                continue;
            }
            // an already-masked value (`[REDACTED],x`) ends where the mask does, like a closing quote
            if s.get(start..start + mask.len()) == Some(&mask[..])
                && end > start + mask.len()
                && !at(s, start + mask.len()).is_some_and(is_alnum)
            {
                end = start + mask.len();
            }
        } else {
            if pos < key_fail {
                continue; // inside a token run whose tail was already found not to look like a credential
            }
            let (found, run_end) = key_value_end(s, pos);
            let Some(e) = found else {
                key_fail = key_fail.max(run_end);
                continue;
            };
            end = e;
        }
        while let Some(more) = continuation(pos, end, bare, rd) {
            end = more;
        }
        add.push((start, end));
        // a value can end with the NAME of the next assignment (`--api-key = {--password=\"x\"`): scanning resumes where that name starts
        pos = tail_name_start(s, pos, end);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::assert_scales;

    /// Expands the `<c*n>` runs of a test string (n copies of c), so the cap vectors stay readable.
    fn x(spec: &str) -> Vec<char> {
        let mut out = Vec::new();
        let mut it = spec.chars();
        while let Some(c) = it.next() {
            if c != '<' {
                out.push(c);
                continue;
            }
            let ch = it.next().unwrap_or('?');
            let n: String = it.by_ref().skip(1).take_while(|&d| d != '>').collect();
            out.extend(std::iter::repeat_n(ch, n.parse().unwrap_or(0)));
        }
        out
    }

    // Every expected value below was produced by the Python oracle (`swarm.redact`, SWARM_PURE_PYTHON=1).

    #[test]
    fn tail_name_full_matches_python() {
        type Want = Option<(usize, Option<char>)>;
        let cases: &[(&str, usize, usize, Want)] = &[
            ("password=", 0, 9, Some((8, Some('=')))),
            ("password", 0, 8, Some((8, None))),
            ("password:", 0, 9, Some((8, Some(':')))),
            ("password==", 0, 10, None),
            ("x password=", 2, 11, Some((10, Some('=')))),
            ("x.password", 2, 10, None),
            ("a.password", 2, 10, None),
            ("a-password", 2, 10, None),
            ("a password", 2, 10, Some((10, None))),
            ("_a", 0, 2, Some((2, None))),
            ("1a", 0, 2, None),
            ("é", 0, 1, None),
            ("aé", 0, 2, None),
            ("a=b", 0, 3, None),
            ("a=b", 0, 2, Some((1, Some('=')))),
            ("a", 0, 0, None),
            ("a", 0, 1, Some((1, None))),
            ("a<b*127>", 0, 128, Some((128, None))),
            ("a<b*128>", 0, 129, None),
            ("a<b*127>=", 0, 129, Some((128, Some('=')))),
            ("a<b*128>=", 0, 130, None),
            ("a.-_", 0, 4, Some((4, None))),
            ("ab", 1, 2, None),
            ("..a", 2, 3, Some((3, None))),
            ("-.a", 2, 3, Some((3, None))),
            ("xx.a", 3, 4, None),
            ("x.a", 2, 3, None),
        ];
        for &(s, lo, end, want) in cases {
            let got = tail_name_full(&x(s), lo, end).map(|m| (m.name_end, m.sep));
            assert_eq!(got, want, "{s:?} {lo}..{end}");
        }
    }

    #[test]
    fn tail_flag_full_matches_python() {
        let cases: &[(&str, usize, usize, bool)] = &[
            ("-a", 0, 2, true),
            ("--a", 0, 3, true),
            ("---a", 0, 4, false),
            ("-", 0, 1, false),
            ("--", 0, 2, false),
            ("-1", 0, 2, false),
            ("a-b", 1, 3, false),
            ("x-ab", 1, 4, false),
            ("x-ab", 0, 4, false),
            ("_-ab", 1, 4, false),
            ("--ab-c_d", 0, 8, true),
            ("-ab c", 0, 5, false),
            ("-ab c", 0, 3, true),
            ("-a<b*63>", 0, 65, true),
            ("-a<b*64>", 0, 66, false),
            ("--a<b*63>", 0, 66, true),
            ("--a<b*64>", 0, 67, false),
            ("-éa", 0, 3, false),
            ("--é", 0, 3, false),
        ];
        for &(s, lo, end, want) in cases {
            assert_eq!(tail_flag_full(&x(s), lo, end), want, "{s:?} {lo}..{end}");
        }
    }

    #[test]
    fn after_name_matches_python() {
        type Want = Option<(char, usize)>;
        let cases: &[(&str, usize, Want)] = &[
            ("=", 0, Some(('=', 1))),
            (" = x", 0, Some(('=', 3))),
            (":", 0, Some((':', 1))),
            ("\t:\t", 0, Some((':', 3))),
            ("< *32>=", 0, Some(('=', 33))),
            ("< *33>=", 0, None),
            ("=< *32>x", 0, Some(('=', 33))),
            ("=< *33>x", 0, Some(('=', 33))),
            ("< *32>=< *32>x", 0, Some(('=', 65))),
            ("x  =", 1, Some(('=', 4))),
            ("x  ", 1, None),
            ("", 0, None),
            ("a", 0, None),
            ("=", 1, None),
            (" \n=", 0, None),
            ("==", 0, Some(('=', 1))),
            ("é=", 0, None),
        ];
        for &(s, pos, want) in cases {
            let got = after_name(&x(s), pos).map(|m| (m.sep, m.end));
            assert_eq!(got, want, "{s:?} @{pos}");
        }
    }

    #[test]
    fn after_sep_matches_python() {
        let cases: &[(&str, usize, usize)] = &[
            ("", 0, 0),
            ("x", 0, 0),
            ("< *32>x", 0, 32),
            ("< *33>x", 0, 32),
            (" \t \tx", 0, 4),
            ("ab  c", 2, 4),
            (" \n", 0, 1),
        ];
        for &(s, pos, want) in cases {
            assert_eq!(after_sep(&x(s), pos), want, "{s:?} @{pos}");
        }
    }

    #[test]
    fn flag_gap_matches_python() {
        let cases: &[(&str, usize, Option<usize>)] = &[
            ("", 0, None),
            (" ", 0, Some(1)),
            (" x", 0, Some(1)),
            (" -x", 0, None),
            ("  - x", 0, None),
            ("        x", 0, Some(8)),
            ("         x", 0, None),
            ("        ", 0, Some(8)),
            ("        -", 0, None),
            (" \nx", 0, None),
            (" \tx", 0, Some(2)),
            (" \x0bx", 0, None),
            (" \x1cx", 0, Some(1)),
            (" \u{a0}", 0, Some(1)),
            (" é", 0, Some(1)),
            ("x x", 1, Some(2)),
            ("x", 0, None),
            ("\t-", 0, None),
        ];
        for &(s, pos, want) in cases {
            assert_eq!(flag_gap(&x(s), pos), want, "{s:?} @{pos}");
        }
    }

    #[test]
    fn key_value_end_matches_python() {
        let cases: &[(&str, usize, Option<usize>, usize)] = &[
            ("aB3<x*17>", 0, Some(20), 20),
            ("aB3<x*16>", 0, None, 0),
            ("aB3<x*16>y", 0, Some(20), 20),
            ("\"aB3<x*17>\"", 0, Some(22), 21),
            ("'aB3<x*17>'", 0, Some(22), 21),
            ("\"aB3<x*17>'", 0, Some(21), 21),
            ("\\\"aB3<x*17>\\\"", 0, Some(24), 22),
            ("\\\"aB3<x*17>\"", 0, Some(22), 22),
            ("\\'aB3<x*17>'", 0, Some(22), 22),
            ("\\aB3<x*17>\\", 0, Some(22), 21),
            ("\\aB3<x*17>", 0, Some(21), 21),
            ("\"\"", 0, None, 0),
            ("\\\\aB3<x*17>", 0, None, 0),
            ("zzaB3<x*17>", 2, Some(22), 22),
            ("aB3<x*17>=", 0, Some(21), 21),
            ("<a*20>", 0, None, 20),
            ("A1<-*18>", 0, None, 20),
            ("\"A1<-*17>", 0, None, 0),
            ("\"a1A<-*17> x", 0, Some(21), 21),
            ("éaB3<x*17>", 0, None, 0),
            ("aB3<x*17>é", 0, Some(20), 20),
            ("", 0, None, 0),
            ("\"", 0, None, 0),
            ("aB3<x*17>", 1, None, 0),
            ("\"aB3<x*17>\"", 1, Some(21), 21),
        ];
        for &(s, pos, end, run) in cases {
            assert_eq!(key_value_end(&x(s), pos), (end, run), "{s:?} @{pos}");
        }
    }

    #[test]
    fn tail_name_start_matches_python() {
        let cases: &[(&str, usize, usize, usize)] = &[
            ("", 0, 0, 0),
            ("abc", 0, 3, 0),
            ("abc=", 0, 4, 0),
            ("abc:", 0, 4, 0),
            ("abc=", 3, 4, 3),
            ("x,abc=", 0, 6, 2),
            ("abc==", 0, 5, 4),
            ("abc=", 1, 4, 1),
            ("a=", 0, 2, 0),
            ("=", 0, 1, 0),
            ("é=", 0, 2, 1),
            ("a é", 0, 3, 3),
            ("a.b-c_d", 0, 7, 0),
            ("<b*300>", 0, 300, 170),
            ("<b*300>=", 0, 301, 170),
            ("<b*300>", 150, 300, 170),
            ("<b*130>", 0, 130, 0),
            ("<b*131>", 0, 131, 1),
            ("<b*129>=", 0, 130, 0),
            ("<b*130>=", 0, 131, 0),
            ("<b*131>=", 0, 132, 1),
            ("<b*131>=", 40, 132, 40),
            ("ab:=", 0, 4, 3),
            ("x ab", 0, 4, 2),
        ];
        for &(s, floor, end, want) in cases {
            assert_eq!(
                tail_name_start(&x(s), floor, end),
                want,
                "{s:?} {floor}..{end}"
            );
        }
    }

    type Spans = Vec<(usize, usize)>;

    /// `scan` over `text` for both kinds: `(NAME spans, FLAG spans)`.
    fn run_scan(text: &str) -> (Spans, Spans) {
        let s = x(text);
        let rd = Reader::new(&s).expect("short text");
        let (mut names, mut flags) = (Vec::new(), Vec::new());
        scan(ScanKind::Name, &mut names, &rd);
        scan(ScanKind::Flag, &mut flags, &rd);
        (names, flags)
    }

    #[test]
    fn scan_matches_python() {
        type Row = (
            &'static str,
            &'static [(usize, usize)],
            &'static [(usize, usize)],
        );
        let cases: &[Row] = &[
            ("password=hunter2", &[(9, 16)], &[]),
            ("password: hunter2", &[(10, 17)], &[]),
            ("PASSWORD=//x", &[(9, 12)], &[]),
            ("auth://host", &[], &[]),
            ("token://x", &[], &[]),
            ("password://x", &[], &[]),
            ("name=bob", &[], &[]),
            ("x=abc password=def", &[(15, 18)], &[]),
            ("db_password = \"a b\" rest", &[(14, 19)], &[]),
            ("token='a b'c d", &[(6, 12)], &[]),
            ("secret=\\\"esc val\\\" tail", &[(7, 18)], &[]),
            ("token=$'a b'", &[(6, 12)], &[]),
            ("password=", &[], &[]),
            ("password=   ", &[], &[]),
            ("password=\"unclosed value", &[(9, 24)], &[]),
            ("token=a\nb=c", &[(6, 7)], &[]),
            ("STRIPE_KEY=AbCdEfGhIjKlMnOpQrSt12", &[(11, 33)], &[]),
            ("STRIPE_KEY=short", &[], &[]),
            ("sort_key=AbCdEfGhIjKlMnOpQrSt12", &[], &[]),
            ("API_KEY=\"AbCdEfGhIjKlMnOpQrStUv12\" x", &[(8, 34)], &[]),
            (
                "API_KEY=\\\"AbCdEfGhIjKlMnOpQrStUv12\\\" x",
                &[(8, 36)],
                &[],
            ),
            ("my_key=Ab1<c*17>", &[(7, 27)], &[]),
            ("my_key=Ab1<c*16>", &[], &[]),
            ("my_key='Ab1<c*17>'", &[(7, 29)], &[]),
            ("my_key='Ab1<c*17>x y", &[(7, 29)], &[]),
            ("my_key='Ab1<c*17>\" y", &[(7, 28)], &[]),
            ("my_key=Ab1<c*17>,token=x \"y\"", &[(7, 27), (34, 35)], &[]),
            ("a_key=<x*10>b_key=<x*20>", &[], &[]),
            ("a_key=<x*10>b_key=<x*20> c_key=Ab1<c*17>", &[(49, 69)], &[]),
            ("key=Ab1<c*17>", &[], &[]),
            ("v_key=Ab1<c*17> password=x", &[(6, 26), (36, 37)], &[]),
            ("my_key: Ab1<c*17>", &[(8, 28)], &[]),
            ("my_key://Ab1<c*17>", &[], &[]),
            ("password=[REDACTED],x", &[(9, 19)], &[]),
            ("password=[REDACTED]x,y", &[(9, 22)], &[]),
            ("password=[REDACTED] y", &[(9, 19)], &[]),
            ("password=[REDACTED]", &[(9, 19)], &[]),
            ("<p*127>=x", &[], &[]),
            ("<p*128>=x", &[], &[]),
            ("<p*129>=x", &[], &[]),
            ("<a*120>password=x", &[(129, 130)], &[]),
            ("<a*128>=x token=y", &[(137, 138)], &[]),
            ("password<\t*1>=x", &[(10, 11)], &[]),
            ("password< *32>=x", &[(41, 42)], &[]),
            ("password< *33>=x", &[], &[]),
            ("password=< *32>x", &[(41, 42)], &[]),
            ("password=< *33>x", &[], &[]),
            ("--password hunter2", &[], &[(11, 18)]),
            ("-p hunter2", &[], &[]),
            ("--name bob", &[], &[]),
            ("--password  \"a b\"", &[], &[(12, 17)]),
            ("--password\t--x", &[], &[]),
            ("--password --x", &[], &[]),
            ("--api-key=x", &[(10, 11)], &[]),
            ("-p<a*8> x", &[], &[]),
            ("password=abc,token=\"x\"", &[(9, 22)], &[]),
            ("password=abc;token= \"x y\"", &[(9, 12), (20, 25)], &[]),
            ("password=abc&token:'x'", &[(9, 12), (19, 22)], &[]),
            ("password=abc/token=\"x\"", &[(9, 22)], &[]),
            ("password=abc?token=\\\"x\\\"", &[(9, 24)], &[]),
            ("a=b password=c", &[(13, 14)], &[]),
            ("password=a,b=c d", &[(9, 14)], &[]),
            ("password=x\"y\"", &[(9, 13)], &[]),
            (
                "--api-key = {--password=\\\"x\\\"",
                &[(12, 29)],
                &[(10, 11)],
            ),
            ("--password a,--token \"y\"", &[], &[(11, 24)]),
            ("--password a,--name \"y\"", &[], &[(11, 19)]),
            ("password=abc,token://x\" y", &[(9, 23)], &[]),
            ("password=abc,token:\"//x\"", &[(9, 24)], &[]),
            ("password=abc,token: //x\" z", &[(9, 19)], &[]),
            ("token=<a*198>,x=y \"q\"", &[(6, 208)], &[]),
            ("token=<a*200>x\"q\"", &[(6, 210)], &[]),
            ("token=<a*5>,token=\"x\"", &[(6, 21)], &[]),
            ("token=<a*205>,token=\"x\"", &[(6, 221)], &[]),
            ("token=a<,*1><a*199>token=\"x\"", &[(6, 216)], &[]),
            ("password=a,<n*127>=\"x\"", &[(9, 142)], &[]),
            ("password=a,<n*128>=\"x\"", &[(9, 143)], &[]),
            ("password=a,token< *32>=\"x\"", &[(9, 52)], &[]),
            ("password=a,token< *33>=\"x\"", &[(9, 16)], &[]),
            ("password=a,token=< *32>\"x\"", &[(9, 52)], &[]),
            ("password=a,token=< *33>\"x\"", &[(9, 17)], &[]),
            ("password=a,--token< *8>\"x\"", &[(9, 29)], &[(26, 29)]),
            ("password=a,--token< *9>\"x\"", &[(9, 18)], &[]),
            ("password=a,--token < *1>-x", &[(9, 18)], &[]),
            ("password=a,token=b,secret=\"c\"", &[(9, 29)], &[]),
            ("p=a,password=b,token=\"c\"", &[(13, 24)], &[]),
            ("password=\u{e9}", &[(9, 10)], &[]),
            ("password=a\u{a0}b", &[(9, 12)], &[]),
            ("\u{e9}=x password=\u{2028}", &[(13, 14)], &[]),
            ("\u{1f600}password=x", &[(10, 11)], &[]),
        ];
        for &(text, names, flags) in cases {
            let (n, f) = run_scan(text);
            assert_eq!(n, names, "NAME spans of {text:?}");
            assert_eq!(f, flags, "FLAG spans of {text:?}");
        }
    }

    #[test]
    fn continuation_matches_python() {
        type Row = (&'static str, usize, ScanKind, Option<usize>);
        let cases: &[Row] = &[
            ("password=abc,token=\"x\"", 9, ScanKind::Name, None),
            ("password=abc,token= \"x y\"", 9, ScanKind::Name, Some(25)),
            ("password=abc token", 9, ScanKind::Name, None),
            ("password=abc token=1", 9, ScanKind::Name, None),
            ("--password a,--token \"y\"", 11, ScanKind::Flag, Some(24)),
            ("--password a,--name \"y\"", 11, ScanKind::Flag, None),
            ("--password a,--token -y", 11, ScanKind::Flag, None),
            ("password=abc,token: //x\" z", 9, ScanKind::Name, None),
            ("password=abc,token://x\" y", 9, ScanKind::Name, None),
            ("password=abc,token:\"//x\"", 9, ScanKind::Name, None),
            ("token=<a*199>x\"q\"", 6, ScanKind::Name, None),
            ("token=<a*200>x\"q\"", 6, ScanKind::Name, None),
            ("token=<a*201>,token=\"x\"", 6, ScanKind::Name, None),
            ("token=a<,*1><a*199>token=\"x\"", 6, ScanKind::Name, None),
            ("password=abc/token= \"x\"", 9, ScanKind::Name, Some(23)),
            ("password=abc?token: \"x\"", 9, ScanKind::Name, Some(23)),
            ("password=abc,token ='x'", 9, ScanKind::Name, Some(23)),
            ("password=abc,token = x", 9, ScanKind::Name, Some(22)),
            ("password=abc,token: //x", 9, ScanKind::Name, None),
            ("password=abc,token= //x", 9, ScanKind::Name, Some(23)),
            ("password=abc,token:\t\"x\"", 9, ScanKind::Name, Some(23)),
            ("password=abc,tok ='x'", 9, ScanKind::Name, None),
            ("--password a;--token \"y\"", 11, ScanKind::Flag, Some(24)),
            ("--password a&-p \"y\"", 11, ScanKind::Flag, None),
            ("--password a,--token  \"y\"", 11, ScanKind::Flag, Some(25)),
            ("--password a,--token< *9>\"y\"", 11, ScanKind::Flag, None),
            ("a=b,c=d x", 2, ScanKind::Name, None),
            ("password=a\\\"b", 9, ScanKind::Name, None),
            ("password=a,token=b", 9, ScanKind::Name, None),
            ("password=a,token='x'", 9, ScanKind::Name, None),
        ];
        for &(text, pos, kind, want) in cases {
            let s = x(text);
            let rd = Reader::new(&s).expect("short text");
            let end = rd.read(pos, kind.bare()).0;
            assert_eq!(
                continuation(pos, end, kind.bare(), &rd),
                want,
                "{text:?} from {pos}"
            );
        }
    }

    /// One pass of both detectors over `rd`.
    fn scan_both(rd: &Reader<'_>) {
        let (mut a, mut b) = (Vec::new(), Vec::new());
        scan(ScanKind::Name, &mut a, rd);
        scan(ScanKind::Flag, &mut b, rd);
        std::hint::black_box((&a, &b));
    }

    /// The key_fail cursor, the failed-run end and the tail window keep long adversarial runs linear: a text of 24000 repeats takes
    /// about as long as 16 texts of 1500 repeats (linear: 1x, a rescanning detector: 16x; the limit is 4x). The timing is the
    /// shared CPU-time one of `crate::testing`.
    #[test]
    fn scan_is_linear_on_adversarial_runs() {
        type Shape = fn(usize) -> String;
        let shapes: [(&str, Shape); 4] = [
            ("failing key runs", |n| "STRIPE_KEY=".repeat(n)),
            ("swallowed names", |n| "token=a,".repeat(n)),
            ("flags", |n| "--password a;".repeat(n)),
            ("long values", |n| {
                format!("password={}", "a,".repeat(n * 4))
            }),
        ];
        for (label, shape) in shapes {
            let whole: Vec<char> = shape(24_000).chars().collect();
            let rd_whole = Reader::new(&whole).expect("short text");
            let texts: Vec<Vec<char>> = (0..16).map(|_| shape(1_500).chars().collect()).collect();
            let readers: Vec<Reader<'_>> = texts
                .iter()
                .map(|t| Reader::new(t).expect("short text"))
                .collect();
            assert_scales(
                label,
                4,
                || readers.iter().for_each(scan_both),
                || scan_both(&rd_whole),
            );
        }
    }
}
