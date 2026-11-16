//! Hand-written scanners for the regexes of `swarm/redact.py` that more than one detector uses.
//!
//! Every function has the semantics of Python's `pattern.search(s, pos)` (or `pattern.match(s, pos)` for the two bare-word
//! patterns): the leftmost match that starts at or after `pos`, with lookbehinds that CAN see the characters before `pos`.
//! A caller that slices (`line = s[p:skip_to]`, redact.py:345) passes the sub-slice and `pos = 0`, so a lookbehind cannot see
//! the text before the slice, exactly as in Python. Python's `finditer` is a loop of `search` from the previous `end`
//! (every pattern here matches at least one character, so the loop always advances).
//!
//! All offsets are indices into the `&[char]` (Python code points). Every pattern is deterministic once its quantifier caps are
//! applied: the classes that follow a greedy run exclude the run's own characters, so backtracking can never find a shorter
//! match (the doc of each scanner states the argument). That keeps every scan linear: the work per candidate start is bounded by
//! the pattern's own caps (128, 64, 81 characters ...), and for [`name_sep`] a memo of the current name-character run makes it
//! O(n) overall.
//!
//! Classes: the `re.ASCII` patterns use ASCII `\s` (`[ \t\n\r\f\v]`); the others spell their classes out, and a negated class
//! such as `[^"'\\\n]` accepts every non-ASCII code point.

use crate::cls::{is_alpha, is_blank, is_eol, is_space, is_word};

/// A `NAME<sep>` match of [`name_sep`] (`_NAME_SEP_RE`, redact.py:173).
///
/// Python group 1 is `start..name_end` (the name) and group 2 is `name_end..end` (blanks, one `=` or `:`, blanks).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NameSep {
    /// Start of the match and of group 1.
    pub start: usize,
    /// End of group 1 and start of group 2.
    pub name_end: usize,
    /// End of the match and of group 2: where the value starts.
    pub end: usize,
    /// `"=" in m.group(2)`: the separator is `=` (otherwise it is `:`).
    pub eq: bool,
}

/// A `--flag<blanks>` match of [`flag_sep`] (`_FLAG_SEP_RE`, redact.py:178).
///
/// Python group 1 is `start..name_end` (the flag with its dashes) and group 2 is `name_end..end` (the blanks).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FlagSep {
    /// Start of the match and of group 1.
    pub start: usize,
    /// End of group 1 and start of group 2.
    pub name_end: usize,
    /// End of the match and of group 2: where the value starts.
    pub end: usize,
}

/// A `"name": ` match of [`pair`] (`_PAIR_RE`, redact.py:675).
///
/// The quote that opens the pair is `s[start]`. The match ends before the quote that opens the value, which the pattern only
/// looks at (`(?=["'])`), so `s[end]` is that quote.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Pair {
    /// Start of the match: the opening quote of the name.
    pub start: usize,
    /// Start of group `name` (`start + 1`).
    pub name_start: usize,
    /// End of group `name`; `s[name_end]` is the closing quote.
    pub name_end: usize,
    /// End of the match (and of group 1): the value's opening quote is `s[end]`.
    pub end: usize,
}

/// A `\"name\": ` match of [`esc_pair`] (`_ESC_PAIR_RE`, redact.py:677).
///
/// The match ends before the backslash run and quote that open the value, which the pattern only looks at.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EscPair {
    /// Start of the match and of group `bs`: the first backslash of the run.
    pub start: usize,
    /// `len(m.group("bs"))`, 1 to 31. The runs before the name, after the name and before the value all have this length.
    pub bs_len: usize,
    /// Start of group `name` (`start + bs_len + 1`).
    pub name_start: usize,
    /// End of group `name`.
    pub name_end: usize,
    /// End of the match: the value's backslash run starts here.
    pub end: usize,
}

fn at(s: &[char], i: usize) -> Option<char> {
    s.get(i).copied()
}

/// Number of consecutive characters from `i` that satisfy `f`, looking at no more than `cap` of them.
fn run_len(s: &[char], i: usize, cap: usize, f: fn(char) -> bool) -> usize {
    s.iter().skip(i).take(cap).take_while(|&&c| f(c)).count()
}

/// `s[i..i + k]` is `k` backslashes followed by `"` (the backreference `(?P=bs)"`).
fn esc_quote_at(s: &[char], i: usize, k: usize) -> bool {
    run_len(s, i, k, |c| c == '\\') == k && at(s, i + k) == Some('"')
}

/// `[A-Za-z0-9_.-]`, the characters of a NAME after its first one.
fn is_name_char(c: char) -> bool {
    is_word(c) || c == '.' || c == '-'
}

/// `[A-Za-z0-9_-]`, the characters of a flag.
fn is_flag_char(c: char) -> bool {
    is_word(c) || c == '-'
}

/// Python `_NAME_SEP_RE.search(s, pos)`:
/// `(?<![A-Za-z0-9_])(?<![A-Za-z0-9_][.-])([A-Za-z_][A-Za-z0-9_.-]{0,127})([ \t]{0,32}[=:][ \t]{0,32})`.
///
/// Serves `_scan` (redact.py:1009, with `pos` the end of the previous match) and `_value_starts` (redact.py:267, `finditer`
/// over a comment slice).
///
/// The name is the whole run of name characters that begins at the match start; if that run is longer than 128 characters,
/// or is not followed by blanks and `=`/`:`, there is no match from this start, because every shorter name would be followed
/// by a name character, which is neither a blank nor a separator. The same holds for a blank run of more than 32 characters
/// before the separator. The trailing blanks take up to 32.
pub fn name_sep(s: &[char], pos: usize) -> Option<NameSep> {
    // Memo of the name-character run that contains the candidate start: its end and what follows it as (end, eq).
    // Every candidate inside one run shares both, so a long run is read once.
    let mut run_end = 0;
    let mut tail: Option<(usize, bool)> = None;
    for i in pos..s.len() {
        let Some(c) = at(s, i) else { break };
        if !(is_alpha(c) || c == '_') {
            continue;
        }
        // (?<![A-Za-z0-9_]) and (?<![A-Za-z0-9_][.-])
        if i > 0 && at(s, i - 1).is_some_and(is_word) {
            continue;
        }
        if i > 1
            && at(s, i - 1).is_some_and(|p| p == '.' || p == '-')
            && at(s, i - 2).is_some_and(is_word)
        {
            continue;
        }
        if i >= run_end {
            run_end = i + run_len(s, i, usize::MAX, is_name_char);
            tail = name_sep_tail(s, run_end);
        }
        if run_end - i <= 128 {
            if let Some((end, eq)) = tail {
                return Some(NameSep {
                    start: i,
                    name_end: run_end,
                    end,
                    eq,
                });
            }
        }
    }
    None
}

/// What `[ \t]{0,32}[=:][ \t]{0,32}` matches at `e`: `(end, separator is '=')`.
fn name_sep_tail(s: &[char], e: usize) -> Option<(usize, bool)> {
    let before = run_len(s, e, 33, is_blank);
    if before > 32 {
        return None;
    }
    let sep = at(s, e + before).filter(|&c| c == '=' || c == ':')?;
    let after = run_len(s, e + before + 1, 32, is_blank);
    Some((e + before + 1 + after, sep == '='))
}

/// Python `_FLAG_SEP_RE.search(s, pos)` (`re.ASCII`):
/// `(?<![A-Za-z0-9_-])(--?[A-Za-z][A-Za-z0-9_-]{0,63})([ \t]{1,8})(?![-\s])`.
///
/// Serves `_scan` (redact.py:1009, the flag pass) and `_value_starts` (redact.py:273).
///
/// A start is a `-` whose predecessor is not a flag character, so it is the first character of its run and the runs of
/// different starts do not overlap. The flag is the whole run (at most 1 or 2 dashes, a letter and 63 more characters), then
/// 1 to 8 blanks, then neither `-` nor ASCII whitespace; each of those is the only way to match (a shorter flag or fewer blanks
/// would leave a flag character or a blank at the place where the pattern needs something else).
pub fn flag_sep(s: &[char], pos: usize) -> Option<FlagSep> {
    for i in pos..s.len() {
        if at(s, i) != Some('-') || (i > 0 && at(s, i - 1).is_some_and(is_flag_char)) {
            continue;
        }
        let mut j = i + 1;
        if at(s, j) == Some('-') {
            j += 1;
        }
        if !at(s, j).is_some_and(is_alpha) {
            continue;
        }
        j += 1;
        let tail = run_len(s, j, 64, is_flag_char);
        if tail > 63 {
            continue;
        }
        let name_end = j + tail;
        let blanks = run_len(s, name_end, 9, is_blank);
        if !(1..=8).contains(&blanks) {
            continue;
        }
        let end = name_end + blanks;
        // (?![-\s]): the end of the string passes.
        if at(s, end).is_some_and(|c| c == '-' || is_space(c)) {
            continue;
        }
        return Some(FlagSep {
            start: i,
            name_end,
            end,
        });
    }
    None
}

/// Python `_NAME_BARE_RE.match(s, pos)` (`re.ASCII`): `(?:[^\s&;"'\\]|\\[ \t]|\\(?!"))+`. Returns the end of the match.
///
/// Serves the bare word of `_Reader.read` (redact.py:579, 598, 621) for every `NAME=`, `NAME:` and pair value.
pub fn name_bare(s: &[char], pos: usize) -> Option<usize> {
    bare(s, pos, true)
}

/// Python `_FLAG_BARE_RE.match(s, pos)` (`re.ASCII`): `(?:[^\s"'\\]|\\[ \t]|\\(?!"))+`. Returns the end of the match.
///
/// Like [`name_bare`] but `&` and `;` are word characters. Serves the bare word of `_Reader.read` for a `--flag value`.
pub fn flag_bare(s: &[char], pos: usize) -> Option<usize> {
    bare(s, pos, false)
}

/// The greedy loop of both bare patterns. Nothing follows the loop in the pattern, so it never backtracks: a character that no
/// alternative takes ends the match. A backslash takes the blank after it (second alternative) before it takes itself (third),
/// and takes itself only when no `"` follows (a following end of string passes the negative lookahead).
fn bare(s: &[char], pos: usize, stop_joiners: bool) -> Option<usize> {
    let mut i = pos;
    while let Some(c) = at(s, i) {
        if c == '\\' {
            match at(s, i + 1) {
                Some(d) if is_blank(d) => i += 2,
                Some('"') => break,
                _ => i += 1,
            }
        } else if is_space(c) || c == '"' || c == '\'' || (stop_joiners && (c == '&' || c == ';')) {
            break;
        } else {
            i += 1;
        }
    }
    (i > pos).then_some(i)
}

/// Python `_EOL_RE.search(s, pos)` (`[\r\n]`): the index of the first CR or LF at or after `pos`.
///
/// Serves `_Lexer.__init__` (redact.py:343, end of a comment line) and `_Reader.line_end` (redact.py:531).
pub fn eol_search(s: &[char], pos: usize) -> Option<usize> {
    s.iter().skip(pos).position(|&c| is_eol(c)).map(|k| pos + k)
}

/// Python `[x.start() for x in _EOL_RE.finditer(s)]`: the index of every CR and LF, ascending.
///
/// Serves `_Reader.closes` (redact.py:649).
pub fn eol_positions(s: &[char]) -> Vec<usize> {
    s.iter()
        .enumerate()
        .filter(|&(_, &c)| is_eol(c))
        .map(|(i, _)| i)
        .collect()
}

/// `[^"'\\\n]`: a character of a pair's name. Any non-ASCII code point qualifies.
fn is_pair_name_char(c: char) -> bool {
    !matches!(c, '"' | '\'' | '\\' | '\n')
}

/// Python `_PAIR_RE.search(s, pos)`:
/// `((?P<q>["'])(?P<name>[^"'\\\n]{1,80})(?P=q)[ \t]{0,8}:[ \t]{0,8})(?=["'])`.
///
/// Serves `_pair_spans` (redact.py:1043, `pos` is the end of the previous accepted pair or `p + 1`) and `_value_starts`
/// (redact.py:276, `finditer` over a comment slice).
///
/// The name is the whole run of name characters (1 to 80; a name class never contains a quote, so a shorter name would be
/// followed by a name character, not by the closing quote). The blank runs are capped at 8, and after the colon the blanks
/// must be followed by a quote, so a ninth blank fails the match.
pub fn pair(s: &[char], pos: usize) -> Option<Pair> {
    for i in pos..s.len() {
        let Some(q) = at(s, i).filter(|&c| c == '"' || c == '\'') else {
            continue;
        };
        let name_len = run_len(s, i + 1, 81, is_pair_name_char);
        if !(1..=80).contains(&name_len) {
            continue;
        }
        let name_end = i + 1 + name_len;
        if at(s, name_end) != Some(q) {
            continue;
        }
        let mut k = name_end + 1;
        let blanks = run_len(s, k, 9, is_blank);
        k += blanks;
        if blanks > 8 || at(s, k) != Some(':') {
            continue;
        }
        k += 1;
        let blanks = run_len(s, k, 9, is_blank);
        k += blanks;
        if blanks > 8 || !at(s, k).is_some_and(|c| c == '"' || c == '\'') {
            continue;
        }
        return Some(Pair {
            start: i,
            name_start: i + 1,
            name_end,
            end: k,
        });
    }
    None
}

/// Python `_ESC_PAIR_RE.search(s, pos)`:
/// `(?<!\\)(?P<bs>\\{1,31})"(?P<name>[^"'\\\n]{1,80})(?P=bs)"[ \t]{0,8}:[ \t]{0,8}(?=(?P=bs)")`.
///
/// Serves `_pair_spans` (redact.py:1052, `pos` is `p + run + 1`) and `_value_starts` (redact.py:279, `finditer` over a comment
/// slice).
///
/// The `(?<!\\)` lookbehind makes `bs` the whole backslash run (the lookbehind sees the character before `pos`): a run of 32 or
/// more backslashes never matches, from any start, because every shorter `bs` would be followed by a backslash, not the quote.
/// The name is the whole run of name characters (1 to 80). The name is then followed by exactly `bs_len` backslashes and a
/// quote, and the value by exactly `bs_len` backslashes and a quote that the pattern only looks at.
pub fn esc_pair(s: &[char], pos: usize) -> Option<EscPair> {
    for i in pos..s.len() {
        if at(s, i) != Some('\\') || (i > 0 && at(s, i - 1) == Some('\\')) {
            continue;
        }
        let bs_len = run_len(s, i, 32, |c| c == '\\');
        if bs_len > 31 || at(s, i + bs_len) != Some('"') {
            continue;
        }
        let name_start = i + bs_len + 1;
        let name_len = run_len(s, name_start, 81, is_pair_name_char);
        if !(1..=80).contains(&name_len) {
            continue;
        }
        let name_end = name_start + name_len;
        if !esc_quote_at(s, name_end, bs_len) {
            continue;
        }
        let mut k = name_end + bs_len + 1;
        let blanks = run_len(s, k, 9, is_blank);
        k += blanks;
        if blanks > 8 || at(s, k) != Some(':') {
            continue;
        }
        k += 1;
        let blanks = run_len(s, k, 9, is_blank);
        k += blanks;
        if blanks > 8 || !esc_quote_at(s, k, bs_len) {
            continue;
        }
        return Some(EscPair {
            start: i,
            bs_len,
            name_start,
            name_end,
            end: k,
        });
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `(start, end of group 1, end, "=" in group 2)`.
    type NameSepSpan = Option<(usize, usize, usize, bool)>;
    /// `(start, end of the name group, end)`.
    type ThreeSpan = Option<(usize, usize, usize)>;
    /// `(start, len of group bs, start of group name, end of group name, end)`.
    type EscPairSpan = Option<(usize, usize, usize, usize, usize)>;

    fn cs(s: &str) -> Vec<char> {
        s.chars().collect()
    }

    // Every expected value in the tables below was produced by running the real Python pattern
    // (`swarm.redact._NAME_SEP_RE.search(s, pos)` and so on) on the input.

    #[test]
    fn name_sep_vectors() {
        // (input, pos, (start, end of group 1, end, "=" in group 2))
        let table: &[(&str, usize, NameSepSpan)] = &[
            ("password=hunter2", 0, Some((0, 8, 9, true))),
            ("PASSWORD = x", 0, Some((0, 8, 11, true))),
            ("token:x", 0, Some((0, 5, 6, false))),
            ("a:b", 0, Some((0, 1, 2, false))),
            ("  X_Y\t:\t y", 0, Some((2, 5, 9, false))),
            ("=x", 0, None),
            ("1a=b", 0, None),
            ("a.b=c", 0, Some((0, 3, 4, true))),
            ("a.-b=c", 0, Some((0, 4, 5, true))),
            ("b.a=1", 0, Some((0, 3, 4, true))),
            ("b.a=1", 2, None),
            ("ab=c", 1, None),
            ("ab=c", 0, Some((0, 2, 3, true))),
            (".-a=1", 0, Some((2, 3, 4, true))),
            ("x.y.z=1", 0, Some((0, 5, 6, true))),
            ("-a=b", 0, Some((1, 2, 3, true))),
            ("a-b-c: d", 0, Some((0, 5, 7, false))),
            ("a:=x", 0, Some((0, 1, 2, false))),
            ("a=:", 0, Some((0, 1, 2, true))),
            ("a  :  =  b", 0, Some((0, 1, 6, false))),
            ("a=\n", 0, Some((0, 1, 2, true))),
            ("a\n=b", 0, None),
            ("a =\tb  ", 0, Some((0, 1, 4, true))),
            ("\u{e9}=x", 0, None),
            ("a\u{e9}=x", 0, None),
            ("\u{e9}a=1", 0, Some((1, 2, 3, true))),
            ("a\u{e9} b=1", 0, Some((3, 4, 5, true))),
            ("x\u{e9}y=1", 0, Some((2, 3, 4, true))),
            ("user=alice,password=x", 0, Some((0, 4, 5, true))),
            ("user=alice,password=x", 6, Some((11, 19, 20, true))),
            ("user=alice,password=x", 11, Some((11, 19, 20, true))),
            ("user=alice,password=x", 12, None),
            ("a=b", 3, None),
            ("a=b", 99, None),
            ("", 0, None),
            ("_=1", 0, Some((0, 1, 2, true))),
            ("_a1.b-c=2", 0, Some((0, 7, 8, true))),
            ("9=1", 0, None),
            ("a;b=1", 0, Some((2, 3, 4, true))),
            ("a = ", 0, Some((0, 1, 4, true))),
            ("a:// x", 0, Some((0, 1, 2, false))),
            ("a://x", 0, Some((0, 1, 2, false))),
            ("k=v k2=v2", 2, Some((4, 6, 7, true))),
            ("k=v k2=v2", 3, Some((4, 6, 7, true))),
            ("\"a\": 1", 0, None),
            ("\\a=1", 0, Some((1, 2, 3, true))),
            ("a_.=1", 0, Some((0, 3, 4, true))),
            ("a.=1", 0, Some((0, 2, 3, true))),
            ("-.a=1", 0, Some((2, 3, 4, true))),
            ("._a=1", 0, Some((1, 3, 4, true))),
            ("a._=1", 0, Some((0, 3, 4, true))),
            ("a-.b=1", 0, Some((0, 4, 5, true))),
        ];
        for &(s, pos, want) in table {
            let got = name_sep(&cs(s), pos).map(|m| (m.start, m.name_end, m.end, m.eq));
            assert_eq!(got, want, "{s:?} @{pos}");
        }
    }

    #[test]
    fn name_sep_caps() {
        let a = |n: usize| "a".repeat(n);
        let blanks = |n: usize| " ".repeat(n);
        let got = |s: String, pos: usize| {
            name_sep(&cs(&s), pos).map(|m| (m.start, m.name_end, m.end, m.eq))
        };
        // a name is at most 128 characters; a longer run has no match from any start inside it
        assert_eq!(got(format!("-{}=1", a(128)), 0), Some((1, 129, 130, true)));
        assert_eq!(got(format!("-{}=1", a(129)), 0), None);
        // at most 32 blanks before the separator, and the trailing blanks stop at 32
        assert_eq!(got(format!("a{}=x", blanks(32)), 0), Some((0, 1, 34, true)));
        assert_eq!(got(format!("a{}=x", blanks(33)), 0), None);
        assert_eq!(got(format!("a={}", blanks(40)), 0), Some((0, 1, 34, true)));
        assert_eq!(
            got(format!("a:{}v", blanks(32)), 0),
            Some((0, 1, 34, false))
        );
        // a run of 9000 name characters with valid starts every three: the nearest start with a name of <= 128 wins
        assert_eq!(
            got(format!("{}=", "a..".repeat(3000)), 0),
            Some((8874, 9000, 9001, true))
        );
        assert_eq!(got("a..".repeat(300_000) + "!", 0), None);
    }

    #[test]
    fn flag_sep_vectors() {
        // (input, pos, (start, end of group 1, end))
        let table: &[(&str, usize, ThreeSpan)] = &[
            ("--password hunter2", 0, Some((0, 10, 11))),
            ("-p x", 0, Some((0, 2, 3))),
            ("--a-b c", 0, Some((0, 5, 6))),
            ("-a  b", 0, Some((0, 2, 4))),
            ("--x", 0, None),
            ("--x ", 0, Some((0, 3, 4))),
            ("--x -y", 0, None),
            ("--x\n", 0, None),
            ("--x \n", 0, None),
            ("--x \t y", 0, Some((0, 3, 6))),
            ("--x - y", 0, None),
            ("--", 0, None),
            ("- a", 0, None),
            ("--9a b", 0, None),
            ("-9 b", 0, None),
            ("---a b", 0, None),
            ("a-b c", 0, None),
            ("a --b c", 0, Some((2, 5, 6))),
            ("a--b c", 0, None),
            ("_-b c", 0, None),
            ("x -b c", 0, Some((2, 4, 5))),
            ("--a--b c", 0, Some((0, 6, 7))),
            ("--a\u{e9} b", 0, None),
            ("--a b\u{e9}", 0, Some((0, 3, 4))),
            ("--a \u{e9}", 0, Some((0, 3, 4))),
            ("--a \u{a0}b", 0, Some((0, 3, 4))),
            ("--a \u{c}b", 0, None),
            ("--a \u{b}b", 0, None),
            ("--a \rb", 0, None),
            ("--a b", 0, Some((0, 3, 4))),
            ("--a b", 1, None),
            ("--a b", 2, None),
            ("-a --b c", 1, Some((3, 6, 7))),
            ("-a --b c", 2, Some((3, 6, 7))),
            ("-a --b c", 3, Some((3, 6, 7))),
            ("--a         b", 0, None),
            ("--a        b", 0, Some((0, 3, 11))),
            ("--a      -b", 0, None),
            ("--a=b c", 0, None),
            ("--a-", 0, None),
            ("--a- b", 0, Some((0, 4, 5))),
            ("-", 0, None),
            ("", 0, None),
            ("--a b", 20, None),
            ("-ab-cd_ef 1", 0, Some((0, 9, 10))),
            ("-a\tb", 0, Some((0, 2, 3))),
            ("--a b c --d e", 0, Some((0, 3, 4))),
            ("--a b c --d e", 4, Some((8, 11, 12))),
            ("\n--a b", 0, Some((1, 4, 5))),
        ];
        for &(s, pos, want) in table {
            let got = flag_sep(&cs(s), pos).map(|m| (m.start, m.name_end, m.end));
            assert_eq!(got, want, "{s:?} @{pos}");
        }
    }

    #[test]
    fn flag_sep_caps() {
        let b = |n: usize| "b".repeat(n);
        let got = |s: String| flag_sep(&cs(&s), 0).map(|m| (m.start, m.name_end, m.end));
        assert_eq!(got(format!("--a{} x", b(63))), Some((0, 66, 67)));
        assert_eq!(got(format!("--a{} x", b(64))), None);
        assert_eq!(got(format!("-a{} x", b(63))), Some((0, 65, 66)));
        assert_eq!(got("-".repeat(1_000_000)), None);
        assert_eq!(got("-a".repeat(500_000)), None);
    }

    #[test]
    fn name_bare_vectors() {
        let table: &[(&str, usize, Option<usize>)] = &[
            ("abc", 0, Some(3)),
            ("abc def", 0, Some(3)),
            ("a&b", 0, Some(1)),
            ("a;b", 0, Some(1)),
            (";b", 0, None),
            ("", 0, None),
            ("abc", 3, None),
            ("abc", 9, None),
            ("a\"b", 0, Some(1)),
            ("\"b", 0, None),
            ("a'b", 0, Some(1)),
            ("pa\\ ss x", 0, Some(6)),
            ("pa\\\tss x", 0, Some(6)),
            ("a\\\"b", 0, Some(1)),
            ("a\\", 0, Some(2)),
            ("\\", 0, Some(1)),
            ("\\\\", 0, Some(2)),
            ("\\\\\"", 0, Some(1)),
            ("\\x", 0, Some(2)),
            ("\\ ", 0, Some(2)),
            ("\\  x", 0, Some(2)),
            ("\\\n", 0, Some(1)),
            ("\\\\\\ x", 0, Some(5)),
            ("a\\\\\"b", 0, Some(2)),
            ("a\\", 0, Some(2)),
            ("\u{e9}\u{e8}", 0, Some(2)),
            ("a\u{a0}b", 0, Some(3)),
            ("a\u{b}b", 0, Some(1)),
            ("a\u{c}b", 0, Some(1)),
            ("a\rb", 0, Some(1)),
            ("a\nb", 0, Some(1)),
            ("a,b=c;d", 0, Some(5)),
            ("x\\;y", 0, Some(2)),
            ("x\\&y", 0, Some(2)),
            ("\\&", 0, Some(1)),
            ("\\\\ ", 0, Some(3)),
            ("a\\\"", 0, Some(1)),
            ("\\\"", 0, None),
            ("\\ \\\"", 0, Some(2)),
            ("a\\ ", 0, Some(3)),
        ];
        for &(s, pos, want) in table {
            assert_eq!(name_bare(&cs(s), pos), want, "{s:?} @{pos}");
        }
    }

    #[test]
    fn flag_bare_vectors() {
        let table: &[(&str, usize, Option<usize>)] = &[
            ("abc", 0, Some(3)),
            ("abc def", 0, Some(3)),
            ("a&b", 0, Some(3)),
            ("a;b", 0, Some(3)),
            (";b", 0, Some(2)),
            ("&;", 0, Some(2)),
            ("", 0, None),
            ("a\"b", 0, Some(1)),
            ("\"b", 0, None),
            ("pa\\ ss x", 0, Some(6)),
            ("a\\\"b", 0, Some(1)),
            ("a\\", 0, Some(2)),
            ("\\x", 0, Some(2)),
            ("\\\\\"", 0, Some(1)),
            ("\\ ", 0, Some(2)),
            ("a;b c", 0, Some(3)),
            ("--x;--y z", 0, Some(7)),
            ("\u{e9};\u{e8}", 0, Some(3)),
            ("a\u{b}b", 0, Some(1)),
            ("a\u{1c}b", 0, Some(3)),
            ("a\u{85}b", 0, Some(3)),
            ("a\u{2028}b", 0, Some(3)),
        ];
        for &(s, pos, want) in table {
            assert_eq!(flag_bare(&cs(s), pos), want, "{s:?} @{pos}");
        }
    }

    #[test]
    fn eol_vectors() {
        let table: &[(&str, usize, Option<usize>)] = &[
            ("abc", 0, None),
            ("a\nb", 0, Some(1)),
            ("a\rb", 0, Some(1)),
            ("a\r\nb", 0, Some(1)),
            ("a\r\nb", 2, Some(2)),
            ("a\nb", 2, None),
            ("\n", 0, Some(0)),
            ("", 0, None),
            ("a\u{c}b\n", 0, Some(3)),
            ("a\u{2028}\u{85}b", 0, None),
            ("a\nb", 9, None),
        ];
        for &(s, pos, want) in table {
            assert_eq!(eol_search(&cs(s), pos), want, "{s:?} @{pos}");
        }
        assert_eq!(eol_positions(&cs("a\r\nb\n\nc\r")), vec![1, 2, 4, 5, 7]);
        assert_eq!(eol_positions(&cs("abc")), Vec::<usize>::new());
    }

    #[test]
    fn pair_vectors() {
        // (input, pos, (start, end of group name, end))
        let table: &[(&str, usize, ThreeSpan)] = &[
            ("\"password\": \"x\"", 0, Some((0, 9, 12))),
            ("'password': 'x'", 0, Some((0, 9, 12))),
            ("\"password\": 'x'", 0, Some((0, 9, 12))),
            ("\"p\":\"x\"", 0, Some((0, 2, 4))),
            ("\"p\" : \"x\"", 0, Some((0, 2, 6))),
            ("\"p\"\t:\t\"x\"", 0, Some((0, 2, 6))),
            ("\"p\"\n:\"x\"", 0, None),
            ("\"p\": x", 0, None),
            ("\"p\": ", 0, None),
            ("\"p\":", 0, None),
            ("\"p\"", 0, None),
            ("\"\"", 0, None),
            ("\"\":\"x\"", 0, None),
            ("\"a'b\": \"x\"", 0, None),
            ("\"a\"b\": \"x\"", 0, Some((2, 4, 7))),
            ("'a\": 'x'", 0, None),
            ("\"a\\b\": \"x\"", 0, None),
            ("\"a\nb\": \"x\"", 0, None),
            ("\"\u{e9}\u{e8}\": \"x\"", 0, Some((0, 3, 6))),
            ("\"a\": \"x\"", 1, None),
            ("\"a\": \"x\"", 5, None),
            ("\"a\": \"x\"", 9, None),
            ("{\"a\": \"b\", \"password\": \"x\"}", 0, Some((1, 3, 6))),
            ("{\"a\": \"b\", \"password\": \"x\"}", 5, Some((11, 20, 23))),
            ("x\"a\": \"b\"", 0, Some((1, 3, 6))),
            ("\"a\": \"b\": \"c\"", 0, Some((0, 2, 5))),
            ("\"a\": \"b\": \"c\"", 4, Some((5, 7, 10))),
            ("\"a\":         \"b\"", 0, None),
            ("\"a\":        \"b\"", 0, Some((0, 2, 12))),
            ("\"a\"         : \"b\"", 0, None),
            ("\"a\"        : \"b\"", 0, Some((0, 2, 13))),
            ("\"a\": \n\"b\"", 0, None),
            ("\"a\":\u{a0}\"b\"", 0, None),
            ("\"a\":  `b`", 0, None),
            ("\"a\": 'b'", 0, Some((0, 2, 5))),
            ("\"a\"'b", 0, None),
        ];
        for &(s, pos, want) in table {
            let got = pair(&cs(s), pos).map(|m| {
                assert_eq!(m.name_start, m.start + 1);
                (m.start, m.name_end, m.end)
            });
            assert_eq!(got, want, "{s:?} @{pos}");
        }
    }

    #[test]
    fn pair_caps() {
        let got = |s: String| pair(&cs(&s), 0).map(|m| (m.start, m.name_end, m.end));
        assert_eq!(
            got(format!("\"{}\": \"x\"", "n".repeat(80))),
            Some((0, 81, 84))
        );
        assert_eq!(got(format!("\"{}\": \"x\"", "n".repeat(81))), None);
        assert_eq!(got("\"".repeat(1_000_000)), None);
        assert_eq!(got("\"a ".repeat(300_000)), None);
    }

    #[test]
    fn esc_pair_vectors() {
        // (input, pos, (start, len of group bs, start of group name, end of group name, end))
        let table: &[(&str, usize, EscPairSpan)] = &[
            ("\\\"password\\\": \\\"x\\\"", 0, Some((0, 1, 2, 10, 14))),
            (
                "\\\\\\\"password\\\\\\\": \\\\\\\"x\\\\\\\"",
                0,
                Some((0, 3, 4, 12, 18)),
            ),
            (
                "\\\\\"password\\\\\": \\\\\"x\\\\\"",
                0,
                Some((0, 2, 3, 11, 16)),
            ),
            ("\\\"p\\\":\\\"x", 0, Some((0, 1, 2, 3, 6))),
            ("\\\"p\\\" : \\\"x", 0, Some((0, 1, 2, 3, 8))),
            ("\\\"p\\\": x", 0, None),
            ("\\\"p\\\": \\", 0, None),
            ("\\\"p\\\": \\\"", 0, Some((0, 1, 2, 3, 7))),
            ("\\\"p\\\": \\\\\"", 0, None),
            ("\\\"p\\\"\\\":\\\"", 0, None),
            ("\\\"p\\\\\": \\\"x", 0, None),
            ("\\\"p\\\": \\\\\"x", 0, None),
            ("x\\\"p\\\": \\\"x", 0, Some((1, 1, 3, 4, 8))),
            ("x\\\"p\\\": \\\"x", 1, Some((1, 1, 3, 4, 8))),
            ("x\\\"p\\\": \\\"x", 2, None),
            ("\\\\\"p\\\": \\\"x", 0, None),
            ("\\\\\"p\\\": \\\"x", 1, None),
            ("\\\\\"p\\\": \\\"x", 2, None),
            ("\\\"p\\\": \\\"x", 1, None),
            ("\\\"a'b\\\": \\\"x", 0, None),
            ("\\\"\\\": \\\"x", 0, None),
            ("\\\"\u{e9}\\\": \\\"x", 0, Some((0, 1, 2, 3, 7))),
            ("\\\"a\nb\\\": \\\"x", 0, None),
            ("\\\"a\\\":         \\\"x", 0, None),
            ("\\\"a\\\":        \\\"x", 0, Some((0, 1, 2, 3, 14))),
            ("\\\"a\\\"         :\\\"x", 0, None),
            (
                "\\\"a\\\": \\\"b\\\", \\\"password\\\": \\\"x\\\"",
                0,
                Some((0, 1, 2, 3, 7)),
            ),
            (
                "\\\"a\\\": \\\"b\\\", \\\"password\\\": \\\"x\\\"",
                6,
                Some((14, 1, 16, 24, 28)),
            ),
            (
                "\\\"a\\\": \\\"b\\\", \\\"password\\\": \\\"x\\\"",
                10,
                Some((14, 1, 16, 24, 28)),
            ),
            ("\\\"a\\\": \\\"b\\\"", 3, None),
            ("\\\"a\\\": \\\"b\\\"", 4, None),
            ("\"a\\\": \\\"b", 0, None),
            (
                "\\\\\\\\\"a\\\\\\\\\": \\\\\\\\\"b",
                0,
                Some((0, 4, 5, 6, 13)),
            ),
            ("\\\"a\\\\\\\": \\\"b", 0, None),
            ("\\", 0, None),
            ("\\\"", 0, None),
            ("", 0, None),
        ];
        for &(s, pos, want) in table {
            let got =
                esc_pair(&cs(s), pos).map(|m| (m.start, m.bs_len, m.name_start, m.name_end, m.end));
            assert_eq!(got, want, "{s:?} @{pos}");
        }
    }

    #[test]
    fn esc_pair_caps() {
        let bs = |k: usize| "\\".repeat(k);
        let got = |s: String, pos: usize| {
            esc_pair(&cs(&s), pos).map(|m| (m.start, m.bs_len, m.name_start, m.name_end, m.end))
        };
        let pair_of = |k: usize| format!("{}\"n{}\": {}\"", bs(k), bs(k), bs(k));
        // the backslash run is at most 31 long, and a longer run has no match from any start inside it
        assert_eq!(got(pair_of(31), 0), Some((0, 31, 32, 33, 67)));
        assert_eq!(got(pair_of(32), 0), None);
        assert_eq!(got(pair_of(33), 0), None);
        // the lookbehind sees the character before `pos`
        assert_eq!(
            got(format!("x{}", pair_of(31)), 1),
            Some((1, 31, 33, 34, 68))
        );
        assert_eq!(got(format!("x{}", pair_of(31)), 2), None);
        // the name is at most 80 characters
        let named = |n: usize| format!("\\\"{}\\\": \\\"", "n".repeat(n));
        assert_eq!(got(named(80), 0), Some((0, 1, 2, 82, 86)));
        assert_eq!(got(named(81), 0), None);
        assert_eq!(got(bs(1_000_000), 0), None);
        assert_eq!(got("\\\"a".repeat(300_000), 0), None);
    }
}
