//! The YAML detector of `swarm/redact.py` (lines 905-953): `- name: DB_PASSWORD` / `value: x` pairs, block-scalar keys (`password: |`)
//! and the extent of the value that follows.
//!
//! This file holds the three regex scanners (`_YAML_NAME_RE`, `_YAML_BLOCK_KEY_RE`, `_YAML_INDICATOR_RE`) and `_yaml_value_end`.
//! None of the Python patterns carries a flag, so `[ \t]`, `[^\r\n]` and the name classes are literal ASCII sets, and a negated
//! class accepts every non-ASCII code point.
//!
//! Every pattern here is deterministic once its greedy choices are made: after a greedy run the next element can never start with a
//! character the run could have given back (a blank is neither `-`, `n`, a quote, a name start nor `:`), so no scanner backtracks
//! and each attempt reads at most the line it starts on plus the next one. The `(?<![^\n])` lookbehind restricts attempts to
//! line starts, so a scan is linear in the text.

use crate::cls::is_blank;

/// One hit of [`name_matches`] or [`block_key_matches`], as offsets into the scanned text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct YamlMatch {
    /// Start of the whole match (`m.start()`).
    pub start: usize,
    /// End of the whole match (`m.end()`): where the value begins.
    pub end: usize,
    /// Start of the key name (group 2).
    pub name_start: usize,
    /// End of the key name (group 2).
    pub name_end: usize,
    /// Start of the indentation group (group 3 of the name pattern, group 1 of the block-key pattern).
    pub indent_start: usize,
    /// End of the indentation group.
    pub indent_end: usize,
}

impl YamlMatch {
    /// `len(m.group(indent_group))`: the width of the indentation the pattern captured.
    pub fn indent_len(&self) -> usize {
        self.indent_end - self.indent_start
    }
}

/// `[A-Za-z_]`, the first character of a key name.
fn is_name_start(c: char) -> bool {
    c == '_' || c.is_ascii_alphabetic()
}

/// `[A-Za-z0-9_.-]`, the rest of a key name.
fn is_name_char(c: char) -> bool {
    c == '_' || c == '.' || c == '-' || c.is_ascii_alphanumeric()
}

/// `(?<![^\n])` at `p`: the start of the text or just after a newline.
fn at_line_start(s: &[char], p: usize) -> bool {
    p == 0 || s.get(p - 1) == Some(&'\n')
}

/// End of the run of `[ \t]` starting at `i`.
fn skip_blanks(s: &[char], mut i: usize) -> usize {
    while s.get(i).is_some_and(|&c| is_blank(c)) {
        i += 1;
    }
    i
}

/// `-[ \t]+` when it matches at `i`, as the end of its match; otherwise `i` (the optional group `(?:-[ \t]+)?` is skipped).
fn skip_dash(s: &[char], i: usize) -> usize {
    if s.get(i) == Some(&'-') {
        let j = skip_blanks(s, i + 1);
        if j > i + 1 {
            return j;
        }
    }
    i
}

/// Whether `s[i..]` starts with the ASCII literal `lit`.
fn starts_with(s: &[char], i: usize, lit: &str) -> bool {
    lit.chars()
        .enumerate()
        .all(|(k, c)| s.get(i + k) == Some(&c))
}

/// `[A-Za-z_][A-Za-z0-9_.-]{0,127}` at `i`, as the end of the name. A run longer than 128 characters cannot be shortened into a
/// match (the character after a shorter name is itself a name character, which none of the continuations accept), so it fails.
fn name_end(s: &[char], i: usize) -> Option<usize> {
    if !s.get(i).is_some_and(|&c| is_name_start(c)) {
        return None;
    }
    let mut j = i + 1;
    while s.get(j).is_some_and(|&c| is_name_char(c)) {
        j += 1;
    }
    (j - i <= 128).then_some(j)
}

/// `(?:#[^\r\n]*)?\r?\n` at `i`, as the end of the newline; the comment is greedy and cannot be shortened (the next character would
/// be neither `\r` nor `\n`).
fn comment_newline(s: &[char], mut i: usize) -> Option<usize> {
    if s.get(i) == Some(&'#') {
        while s.get(i).is_some_and(|&c| c != '\r' && c != '\n') {
            i += 1;
        }
    }
    if s.get(i) == Some(&'\r') {
        i += 1;
    }
    (s.get(i) == Some(&'\n')).then_some(i + 1)
}

/// One attempt of `_YAML_NAME_RE` (redact.py 909-910) at the line start `p`.
fn name_at(s: &[char], p: usize) -> Option<YamlMatch> {
    let mut i = skip_dash(s, skip_blanks(s, p));
    if !starts_with(s, i, "name") {
        return None;
    }
    i = skip_blanks(s, i + 4);
    if s.get(i) != Some(&':') {
        return None;
    }
    i = skip_blanks(s, i + 1);
    // `(["']?)`: a quote is taken when present; the empty alternative cannot succeed then, a quote is no name start.
    let quote = s.get(i).copied().filter(|&c| c == '"' || c == '\'');
    if quote.is_some() {
        i += 1;
    }
    let name_start = i;
    let name_stop = name_end(s, i)?;
    i = name_stop;
    if let Some(q) = quote {
        if s.get(i) != Some(&q) {
            return None;
        }
        i += 1;
    }
    i = comment_newline(s, skip_blanks(s, i))?;
    let indent_start = i;
    i = skip_blanks(s, i);
    let indent_end = i;
    if !starts_with(s, i, "value") {
        return None;
    }
    i = skip_blanks(s, i + 5);
    if s.get(i) != Some(&':') {
        return None;
    }
    i = skip_blanks(s, i + 1);
    // `(?=[^ \t\r\n#])`: some character must follow, and not one of those.
    match s.get(i) {
        Some(&c) if !matches!(c, ' ' | '\t' | '\r' | '\n' | '#') => Some(YamlMatch {
            start: p,
            end: i,
            name_start,
            name_end: name_stop,
            indent_start,
            indent_end,
        }),
        _ => None,
    }
}

/// One attempt of `_YAML_BLOCK_KEY_RE` (redact.py 911) at the line start `p`.
fn block_key_at(s: &[char], p: usize) -> Option<YamlMatch> {
    let indent_end = skip_blanks(s, p);
    let mut i = skip_dash(s, indent_end);
    if matches!(s.get(i), Some('"' | '\'')) {
        i += 1;
    }
    let name_start = i;
    let name_stop = name_end(s, i)?;
    i = name_stop;
    if matches!(s.get(i), Some('"' | '\'')) {
        i += 1;
    }
    i = skip_blanks(s, i);
    if s.get(i) != Some(&':') {
        return None;
    }
    i = skip_blanks(s, i + 1);
    // `(?=[|>])`
    matches!(s.get(i), Some('|' | '>')).then_some(YamlMatch {
        start: p,
        end: i,
        name_start,
        name_end: name_stop,
        indent_start: p,
        indent_end,
    })
}

/// `finditer` over `attempt`: matches are never empty, so the search resumes at each match end.
fn find_all(s: &[char], attempt: fn(&[char], usize) -> Option<YamlMatch>) -> Vec<YamlMatch> {
    let mut out = Vec::new();
    let mut p = 0;
    while p < s.len() {
        if at_line_start(s, p) {
            if let Some(m) = attempt(s, p) {
                p = m.end;
                out.push(m);
                continue;
            }
        }
        p += 1;
    }
    out
}

/// `_YAML_NAME_RE.finditer(s)`: `name: KEY` (optionally `- name:`, quoted, with a trailing comment) on one line and `value: X` on the
/// next. Group 2 is the key name, group 3 the indentation of the `value` line, and `end` is where the value begins.
pub fn name_matches(s: &[char]) -> Vec<YamlMatch> {
    find_all(s, name_at)
}

/// `_YAML_BLOCK_KEY_RE.finditer(s)`: `key:` (optionally `- key:`, quoted) followed by `|` or `>`. Group 1 is the indentation, group 2
/// the key name, and `end` is where the block indicator begins.
pub fn block_key_matches(s: &[char]) -> Vec<YamlMatch> {
    find_all(s, block_key_at)
}

/// `_YAML_INDICATOR_RE.match(s, start)` (redact.py 912): a block-scalar header `|` or `>` with chomping/indentation indicators and an
/// optional comment, ending at a line end or the end of the text. Returns the match end.
pub fn indicator_end(s: &[char], start: usize) -> Option<usize> {
    // Python clamps a `pos` past the end to the end, where nothing can match.
    if !matches!(s.get(start), Some('|' | '>')) {
        return None;
    }
    let is_sign = |c: Option<&char>| matches!(c, Some('+' | '-'));
    let mut i = start + 1;
    if is_sign(s.get(i)) {
        i += 1;
    }
    if s.get(i).is_some_and(char::is_ascii_digit) {
        i += 1;
    }
    if is_sign(s.get(i)) {
        i += 1;
    }
    i = skip_blanks(s, i);
    if s.get(i) == Some(&'#') {
        while s.get(i).is_some_and(|&c| c != '\r' && c != '\n') {
            i += 1;
        }
    }
    // `(?=\r?\n|\Z)`: no shorter prefix can satisfy it, the next character would be a blank, `#`, sign, digit or comment text.
    let at_eol = match s.get(i) {
        None | Some('\n') => true,
        Some('\r') => s.get(i + 1) == Some(&'\n'),
        Some(_) => false,
    };
    at_eol.then_some(i)
}

/// Index of the first `\n` at or after `from`.
fn find_newline(s: &[char], from: usize) -> Option<usize> {
    s.get(from..)?
        .iter()
        .position(|&c| c == '\n')
        .map(|k| from + k)
}

/// `_yaml_value_end` (redact.py 915-935): the end of the scalar that begins at `s[start]`. A block scalar runs through the following
/// lines indented deeper than `indent` (blank lines inside it included); any other value is the rest of its line, minus a `\r`.
pub fn value_end(s: &[char], start: usize, indent: usize) -> usize {
    let Some(end) = indicator_end(s, start) else {
        let eol = find_newline(s, start).unwrap_or(s.len());
        // `s[eol - 1:eol] == "\r"` is empty when `eol == 0`.
        return if eol > 0 && s[eol - 1] == '\r' {
            eol - 1
        } else {
            eol
        };
    };
    let mut end = end;
    let mut pos = find_newline(s, end);
    while let Some(p) = pos {
        let ls = p + 1;
        let le = find_newline(s, ls).unwrap_or(s.len());
        let line = &s[ls..le];
        // `line.strip(" \t\r")` is non-empty.
        if line.iter().any(|&c| !matches!(c, ' ' | '\t' | '\r')) {
            let lead = line.iter().take_while(|&&c| is_blank(c)).count();
            if lead <= indent {
                break;
            }
            end = if line.last() == Some(&'\r') {
                le - 1
            } else {
                le
            };
        }
        pos = (le < s.len()).then_some(le);
    }
    end
}

/// `_yaml_spans` (redact.py 938-953): appends to `spans`, in Python's `add` order, the value of every sensitive YAML key: first the
/// `- name: DB_PASSWORD` / `value: x` pairs (`_YAML_NAME_RE`), then the block-scalar keys (`password: |`, `_YAML_BLOCK_KEY_RE`).
///
/// A match that starts inside the span the same pattern reported last is skipped, and so is one whose name is not sensitive. The value
/// runs to [`value_end`], extended to the closing quote when it opens with one (a quoted scalar runs across lines). `rd` must read `s`.
/// Both pattern scans visit each line start once and every value end reads only the lines it covers, so the pass is linear.
pub fn yaml_spans(s: &[char], spans: &mut Vec<(usize, usize)>, rd: &crate::reader::Reader<'_>) {
    if !s.contains(&'\n') || !s.contains(&':') {
        return;
    }
    for matches in [name_matches(s), block_key_matches(s)] {
        let mut last = 0;
        for m in matches {
            if m.start < last || !crate::keys::is_sensitive_key(&s[m.name_start..m.name_end]) {
                continue;
            }
            let start = m.end;
            let mut end = value_end(s, start, m.indent_len());
            // Both patterns end in a lookahead that guarantees `s[start]` exists; `get` keeps the port panic-free regardless.
            if let Some(&q @ ('"' | '\'')) = s.get(start) {
                end = end.max(rd.quoted(start, q, 1, 0).0); // a quoted scalar runs to its closing quote, across lines
            }
            if end > start {
                spans.push((start, end));
                last = end;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chars(s: &str) -> Vec<char> {
        s.chars().collect()
    }

    /// `(start, end, name_start, name_end, indent_start, indent_end)` of every match.
    type Spans = &'static [(usize, usize, usize, usize, usize, usize)];

    fn spans(ms: &[YamlMatch]) -> Vec<(usize, usize, usize, usize, usize, usize)> {
        ms.iter()
            .map(|m| {
                (
                    m.start,
                    m.end,
                    m.name_start,
                    m.name_end,
                    m.indent_start,
                    m.indent_end,
                )
            })
            .collect()
    }

    // Every expected value below comes from `swarm.redact._YAML_NAME_RE.finditer`.
    #[test]
    fn name_pattern_matches_python() {
        let cases: &[(&str, Spans)] = &[
        ("name: A\nvalue: x", &[(0, 15, 6, 7, 8, 8)]),
        ("- name: DB_PASSWORD\n  value: hunter2\n", &[(0, 29, 8, 19, 20, 22)]),
        ("  - name: 'TOKEN'\n    value: abc\n", &[(0, 29, 11, 16, 18, 22)]),
        ("- name: \"K\"\n  value: \"v\"\n", &[(0, 21, 9, 10, 12, 14)]),
        ("name: K\r\nvalue: x\r\n", &[(0, 16, 6, 7, 9, 9)]),
        ("name: K # c\nvalue: x", &[(0, 19, 6, 7, 12, 12)]),
        ("name: K #\nvalue: x", &[(0, 17, 6, 7, 10, 10)]),
        ("name:K\nvalue:x", &[(0, 13, 5, 6, 7, 7)]),
        ("name : K\nvalue : x", &[(0, 17, 7, 8, 9, 9)]),
        ("\tname:\tK\n\tvalue:\tx", &[(0, 17, 7, 8, 9, 10)]),
        ("name: 'K\nvalue: x", &[]),
        ("name: K'\nvalue: x", &[]),
        ("name: 'K\"\nvalue: x", &[]),
        ("name: K\nvalue:", &[]),
        ("name: K\nvalue: ", &[]),
        ("name: K\nvalue: # c", &[]),
        ("name: K\nvalue: \r\n", &[]),
        ("name: K\nvalue: #x", &[]),
        ("name: K\nvalue: |\n  x", &[(0, 15, 6, 7, 8, 8)]),
        ("name: K\nvalue: 'a\nb'", &[(0, 15, 6, 7, 8, 8)]),
        ("name: 9K\nvalue: x", &[]),
        ("name: _K\nvalue: x", &[(0, 16, 6, 8, 9, 9)]),
        ("name: a.b-c_d9\nvalue: x", &[(0, 22, 6, 14, 15, 15)]),
        ("name: \u{e9}\nvalue: x", &[]),
        ("name: K\n\nvalue: x", &[]),
        ("name: K\n  value: x\nname: L\nvalue: y", &[(0, 17, 6, 7, 8, 10), (19, 34, 25, 26, 27, 27)]),
        ("x name: K\nvalue: x", &[]),
        ("-name: K\nvalue: x", &[]),
        ("- name: K\n- value: x", &[]),
        ("name: K\nvalues: x", &[]),
        ("names: K\nvalue: x", &[]),
        ("- \tname: K\nvalue: x", &[(0, 18, 9, 10, 11, 11)]),
        ("-\nname: K\nvalue: x", &[(2, 17, 8, 9, 10, 10)]),
        ("name: K\rvalue: x", &[]),
        ("name: K\n\rvalue: x", &[]),
        ("name: K \t \nvalue: x", &[(0, 18, 6, 7, 11, 11)]),
        ("name: K# c\nvalue: x", &[(0, 18, 6, 7, 11, 11)]),
        ("name: K  # \u{e9}\u{2028} c\nvalue: \u{e9}", &[(0, 23, 6, 7, 16, 16)]),
        ("a\nname: K\nvalue: x\nname: L\nvalue: y\n", &[(2, 17, 8, 9, 10, 10), (19, 34, 25, 26, 27, 27)]),
        ("name: K\nvalue: x\n  name: L\n  value: y", &[(0, 15, 6, 7, 8, 8), (17, 36, 25, 26, 27, 29)]),
        ("name: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nvalue: x", &[(0, 141, 6, 133, 134, 134)]),
        ("name: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nvalue: x", &[(0, 142, 6, 134, 135, 135)]),
        ("name: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nvalue: x", &[]),
        ("name: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\nvalue: x", &[(0, 144, 7, 135, 137, 137)]),
        ("name: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\nvalue: x", &[]),
        ("name: a\nvalue: x", &[(0, 15, 6, 7, 8, 8)]),
        ("          name: K\n                                                                      value: x", &[(0, 95, 16, 17, 18, 88)]),
        ("name: K\nvalue: x\n\n\n", &[(0, 15, 6, 7, 8, 8)]),
        ];
        for (text, want) in cases {
            assert_eq!(spans(&name_matches(&chars(text))), *want, "{text:?}");
        }
    }

    // Every expected value below comes from `swarm.redact._YAML_BLOCK_KEY_RE.finditer`.
    #[test]
    fn block_key_pattern_matches_python() {
        let cases: &[(&str, Spans)] = &[
        ("key: |\n  x", &[(0, 5, 0, 3, 0, 0)]),
        ("key: >\n  x", &[(0, 5, 0, 3, 0, 0)]),
        ("- key: |-\n  x", &[(0, 7, 2, 5, 0, 0)]),
        ("  'key': |+\n x", &[(0, 9, 3, 6, 0, 2)]),
        ("\"key\": |\n x", &[(0, 7, 1, 4, 0, 0)]),
        ("'key\": |\n x", &[(0, 7, 1, 4, 0, 0)]),
        ("key '  :  |", &[]),
        ("key:|", &[(0, 4, 0, 3, 0, 0)]),
        ("key:>", &[(0, 4, 0, 3, 0, 0)]),
        ("key: |x", &[(0, 5, 0, 3, 0, 0)]),
        ("key:\t>\n", &[(0, 5, 0, 3, 0, 0)]),
        ("k : |\nv", &[(0, 4, 0, 1, 0, 0)]),
        ("1k: |", &[]),
        ("_k: >", &[(0, 4, 0, 2, 0, 0)]),
        ("a.b-c: |", &[(0, 7, 0, 5, 0, 0)]),
        ("\u{e9}: |", &[]),
        ("key: ||", &[(0, 5, 0, 3, 0, 0)]),
        ("a: |\nb: >\nc: x", &[(0, 3, 0, 1, 0, 0), (5, 8, 5, 6, 5, 5)]),
        ("\tkey: |", &[(0, 6, 1, 4, 0, 1)]),
        ("  - key:\t|", &[(0, 9, 4, 7, 0, 2)]),
        ("-key: |", &[]),
        ("- - key: |", &[]),
        ("key: |  # c", &[(0, 5, 0, 3, 0, 0)]),
        ("x\n  key: >", &[(2, 9, 4, 7, 2, 4)]),
        ("key: |\r\n  y", &[(0, 5, 0, 3, 0, 0)]),
        ("\nkey: |", &[(1, 6, 1, 4, 1, 1)]),
        ("key:: |", &[]),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa: |", &[(0, 129, 0, 127, 0, 0)]),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa: |", &[(0, 130, 0, 128, 0, 0)]),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa: |", &[]),
        ("'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa': |", &[(0, 132, 1, 129, 0, 0)]),
        ("'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa': |", &[]),
        ("- aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa: >", &[(0, 132, 2, 130, 0, 0)]),
        ("                                        a: |", &[(0, 43, 40, 41, 0, 40)]),
        ("key: |\r\n", &[(0, 5, 0, 3, 0, 0)]),
        ("key:  \n|", &[]),
        ];
        for (text, want) in cases {
            assert_eq!(spans(&block_key_matches(&chars(text))), *want, "{text:?}");
        }
    }

    // Every expected value below comes from `swarm.redact._YAML_INDICATOR_RE.match(text, 0)`.
    #[test]
    fn indicator_matches_python() {
        let cases: &[(&str, Option<usize>)] = &[
            ("|", Some(1)),
            ("|\n", Some(1)),
            ("|-\n", Some(2)),
            ("|+\n", Some(2)),
            ("|2\n", Some(2)),
            ("|-1\n", Some(3)),
            ("|1-\n", Some(3)),
            ("|-1+\n", Some(4)),
            ("|+-\n", Some(3)),
            ("|--\n", Some(3)),
            ("|12\n", None),
            ("|x\n", None),
            ("|\r\n", Some(1)),
            ("|\r", None),
            ("|\r x", None),
            ("|  \t \n", Some(5)),
            ("|  # c\n", Some(6)),
            ("|#c\n", Some(3)),
            ("|# c\r\n", Some(4)),
            ("| # c", Some(5)),
            ("| #\r", None),
            ("|-# c", Some(5)),
            ("|1 #", Some(4)),
            ("|+1-  # \u{e9}\n", Some(9)),
            ("> \n", Some(2)),
            (">\n", Some(1)),
            (">-", Some(2)),
            ("|-1-", Some(4)),
            ("|\u{2028}", None),
            ("|\u{b}\n", None),
            ("", None),
            ("a", None),
            ("|# c\r\r\n", None),
            ("|# c\r", None),
            ("||", None),
            ("|+1+\n", Some(4)),
            ("|\t#\t\n", Some(4)),
            ("|0\n", Some(2)),
            ("|9\n", Some(2)),
            ("|+9-\n", Some(4)),
        ];
        for (text, want) in cases {
            assert_eq!(indicator_end(&chars(text), 0), *want, "{text:?}");
        }
    }

    // `_YAML_INDICATOR_RE.match(text, pos)`: anchored at `pos`, `pos` past the end never matches.
    #[test]
    fn indicator_honours_start() {
        let cases: &[(&str, usize, Option<usize>)] = &[
            ("a: |\nb", 3, Some(4)),
            ("a: |-1 # c\r\nb", 3, Some(10)),
            ("xx|", 3, None),
            ("xx|", 2, Some(3)),
            ("|", 1, None),
            ("|", 5, None),
            ("a: >\n", 3, Some(4)),
        ];
        for (text, start, want) in cases {
            assert_eq!(
                indicator_end(&chars(text), *start),
                *want,
                "{text:?} at {start}"
            );
        }
    }

    // `_yaml_value_end(text, start, indent)`.
    #[test]
    fn value_end_matches_python() {
        let cases: &[(&str, usize, usize, usize)] = &[
            ("plain value\nnext", 0, 0, 11),
            ("plain value\r\nnext", 0, 0, 11),
            ("plain value\r", 0, 0, 11),
            ("plain", 0, 0, 5),
            ("", 0, 0, 0),
            ("\n", 0, 0, 0),
            ("\r\n", 0, 0, 0),
            ("\r", 0, 0, 0),
            ("ab\r", 1, 0, 2),
            ("ab\r\n", 2, 0, 2),
            ("a\r\nb", 1, 0, 1),
            ("ab\r\ncd", 2, 0, 2),
            ("x\n", 1, 0, 1),
            ("|\n  a\n  b\nc", 0, 0, 9),
            ("|\n  a\n  b\nc", 0, 1, 9),
            ("|\n  a\n  b\nc", 0, 2, 1),
            ("|\n  a\n  b\nc", 0, 3, 1),
            ("|\n  a\n\n  b\nc", 0, 0, 10),
            ("|\n  a\n   \n  b\n", 0, 0, 13),
            ("|\n  a\r\n  b\r\nc", 0, 0, 10),
            ("|\n  a\r\n\r\n  b\r\n", 0, 0, 12),
            ("|\n  a\n  \r\n", 0, 0, 5),
            ("|\n\n\n", 0, 0, 1),
            ("|\n", 0, 0, 1),
            ("|", 0, 0, 1),
            ("|\r\n", 0, 0, 1),
            ("|-\n  a\n  b", 0, 1, 10),
            ("|-\n  a\n  b\n", 0, 1, 10),
            ("|\n a\n  b\n c\n", 0, 1, 1),
            ("|\n a\n  b\n c\n", 0, 0, 11),
            ("|\n\ta\n\t\tb\nc", 0, 1, 1),
            ("|\n\ta\n\t\tb\nc", 0, 2, 1),
            ("|\n\u{b}\nx", 0, 0, 1),
            ("|\n\u{b}\nx", 0, 2, 1),
            ("|\n  a\n   #\nb: c", 0, 2, 1),
            ("|\n    a\n  b\n    c", 0, 2, 7),
            ("|\n    a\n  b\n    c", 0, 1, 17),
            ("|x\n  a", 0, 0, 2),
            ("| # c\n  a\n", 0, 0, 9),
            ("|# c\r\n  a\r\nb", 0, 0, 9),
            ("k: |\n  a", 3, 0, 8),
            ("k: v\r\nz", 3, 0, 4),
            ("k: v\r\nz", 9, 0, 7),
            ("k: v", 99, 0, 4),
            ("|\n  \u{e9}\u{2028}\n  b\n", 0, 0, 10),
            ("|\n  a\r", 0, 0, 5),
            ("|\n  a\r\r\n  b", 0, 0, 11),
            ("|\n  a\n  b\nc\n  d\n", 0, 1, 9),
            ("|\n \t a\n \t b\n", 0, 2, 11),
            ("|\n \t a\n \t b\n", 0, 3, 1),
        ];
        for (text, start, indent, want) in cases {
            assert_eq!(
                value_end(&chars(text), *start, *indent),
                *want,
                "{text:?} from {start} indent {indent}"
            );
        }
    }

    // The `{0,127}` bound: names of 127 and 128 characters match, 129 does not (Python: spans below).
    #[test]
    fn name_length_cap() {
        for (n, quote, want) in [
            (1, "", Some((0, 15, 6, 7))),
            (127, "", Some((0, 141, 6, 133))),
            (128, "", Some((0, 142, 6, 134))),
            (129, "", None),
            (127, "'", Some((0, 143, 7, 134))),
            (128, "'", Some((0, 144, 7, 135))),
            (129, "'", None),
        ] {
            let text = format!("name: {quote}{}{quote}\nvalue: x", "a".repeat(n));
            let got: Vec<_> = name_matches(&chars(&text))
                .iter()
                .map(|m| (m.start, m.end, m.name_start, m.name_end))
                .collect();
            assert_eq!(
                got,
                want.into_iter().collect::<Vec<_>>(),
                "n={n} quote={quote:?}"
            );
        }
        for (n, quote, want) in [
            (1, "", Some((0, 3, 0, 1))),
            (127, "", Some((0, 129, 0, 127))),
            (128, "", Some((0, 130, 0, 128))),
            (129, "", None),
            (127, "'", Some((0, 131, 1, 128))),
            (128, "'", Some((0, 132, 1, 129))),
            (129, "'", None),
        ] {
            let text = format!("{quote}{}{quote}: |", "a".repeat(n));
            let got: Vec<_> = block_key_matches(&chars(&text))
                .iter()
                .map(|m| (m.start, m.end, m.name_start, m.name_end))
                .collect();
            assert_eq!(
                got,
                want.into_iter().collect::<Vec<_>>(),
                "n={n} quote={quote:?}"
            );
        }
        let text = format!("- {}: >", "a".repeat(128));
        let ms = block_key_matches(&chars(&text));
        assert_eq!(spans(&ms), [(0, 132, 2, 130, 0, 0)]);
    }

    #[test]
    fn indent_group_widths() {
        let text = format!("{}a: |", " ".repeat(40));
        let ms = block_key_matches(&chars(&text));
        assert_eq!((ms[0].end, ms[0].indent_len()), (43, 40));
        let text = format!("{}name: K\n{}value: x", "  ".repeat(5), " ".repeat(70));
        let ms = name_matches(&chars(&text));
        assert_eq!(
            (ms[0].end, ms[0].indent_len(), ms[0].name_start),
            (95, 70, 16)
        );
    }

    // Floods that would be quadratic with a rescan: long blank runs, many near-matches, a huge block scalar.
    #[test]
    fn floods_complete_and_match() {
        let blanks = format!("{}name: K\nvalue: x", " ".repeat(200_000));
        assert_eq!(name_matches(&chars(&blanks)).len(), 1);
        let near = "name: K\n  valu: x\n".repeat(50_000);
        assert!(name_matches(&chars(&near)).is_empty());
        let keys = "k: |\n".repeat(50_000);
        assert_eq!(block_key_matches(&chars(&keys)).len(), 50_000);
        let scalar = format!("|\n{}", "  line\n".repeat(100_000));
        let s = chars(&scalar);
        assert_eq!(value_end(&s, 0, 0), s.len() - 1);
    }

    use crate::testing::assert_scales;

    fn detect(s: &str) -> Vec<(usize, usize)> {
        let t = chars(s);
        let rd = crate::reader::Reader::new(&t).unwrap();
        let mut spans = Vec::new();
        yaml_spans(&t, &mut spans, &rd);
        spans
    }

    // Every expected value below comes from `swarm.redact._yaml_spans(s, add, _Reader(s))`, in `add` order.
    #[test]
    fn yaml_spans_match_python() {
        let table: &[(&str, &[(usize, usize)])] = &[
        ("- name: DB_PASSWORD\n  value: hunter2\n", &[(29, 36)]),
        ("- name: DB_PASSWORD\n  value: hunter2", &[(29, 36)]),
        ("  - name: 'TOKEN'\n    value: abc\n", &[(29, 32)]),
        ("- name: \"API_KEY\"\n  value: \"v1\"\n", &[(27, 31)]),
        ("- name: DB_HOST\n  value: db.local\n", &[]),
        ("name: SECRET # comment\nvalue: x\n", &[(30, 31)]),
        ("name: SECRET\r\nvalue: x\r\nnext: y\r\n", &[(21, 22)]),
        ("name: SECRET\n\tvalue: x\n", &[(21, 22)]),
        ("- name: PASSWORD\n  value: \"multi\nline\" tail\nafter: z\n", &[(26, 38)]),
        ("- name: PASSWORD\n  value: 'it''s\nok'\n", &[(26, 32)]),
        ("- name: PASSWORD\n  value: \"unclosed\nnext: z\n", &[(26, 35)]),
        ("- name: PASSWORD\n  value:\nnext: z\n", &[]),
        ("- name: PASSWORD\n  value: # only comment\n", &[]),
        ("- name: PASSWORD\n  value:  \n", &[]),
        ("- name: PASSWORD\n  value: x\n- name: TOKEN\n  value: y\n", &[(26, 27), (51, 52)]),
        ("- name: PASSWORD\n  value: \"a\nname: TOKEN\nvalue: b\"\n", &[(26, 50)]),
        ("- name: A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_PASSWORD\n  value: x\n", &[(146, 147)]),
        ("- name: A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A_A__PASSWORD\n  value: x\n", &[]),
        ("password: |\n  line one\n  line two\nother: x\n", &[(10, 33)]),
        ("password: |-\n  a\n\n  b\nnext: 1\n", &[(10, 21)]),
        ("password: >+\n  a\n   b\n", &[(10, 21)]),
        ("password: |2\n  a\n", &[(10, 16)]),
        ("password: |+1 # c\n  a\n", &[(10, 21)]),
        ("password: |1+\n  a\n", &[(10, 17)]),
        ("password: |  # trailing comment\n  secret\n", &[(10, 40)]),
        ("password: |x\n  a\n", &[(10, 12)]),
        ("password: |\r\n  a\r\n  b\r\nz: 1\r\n", &[(10, 21)]),
        ("password: |\n  a\r\n\r\n  b\r\nz\n", &[(10, 22)]),
        ("password: |\n\t a\n \tb\nc\n", &[(10, 19)]),
        ("password: |\n   \n  a\n", &[(10, 19)]),
        ("password: |\n", &[(10, 11)]),
        ("password: |", &[]),
        ("  password: |\n    a\n   b\n  c\n", &[(12, 24)]),
        ("  password: |\n    a\n  b\n", &[(12, 19)]),
        ("  password: |\n    a\n\n\n  b\n    c\n", &[(12, 19)]),
        ("- password: |\n    deep\n  not-deep\n", &[(12, 33)]),
        ("- \"api_key\": |\n  x\n", &[(13, 18)]),
        ("'secret': >\n  x\n", &[(10, 15)]),
        ("\"secret': >\n  x\n", &[(10, 15)]),
        ("password : |\n  x\n", &[(11, 16)]),
        ("password:|\n  x\n", &[(9, 14)]),
        ("password: x\n  y\n", &[]),
        ("hostname: |\n  a\n", &[]),
        ("key: |\n  a\n", &[]),
        ("ssh_key: |\n  -----BEGIN\n  AAAA\n", &[(9, 30)]),
        ("password: |\n  a\ntoken: >\n  b\n", &[(10, 15), (23, 28)]),
        ("password: |\n  a\n  token: >\n    b\n", &[(10, 32)]),
        ("password: |\n  a\n  - name: SECRET\n    value: x\n", &[(44, 45), (10, 45)]),
        ("- name: SECRET\n  value: x\npassword: |\n  a\n", &[(24, 25), (36, 41)]),
        ("password: \"quoted\"\n- name: SECRET\n  value: x", &[(43, 44)]),
        ("no newline: PASSWORD", &[]),
        ("password: | \n  a\n", &[(10, 16)]),
        ("password: |\n\u{e9}a\n  b\n", &[(10, 11)]),
        ("name: caf\u{e9}\nvalue: x\n", &[]),
        ("- name: DB-PASSWORD\n  value: x\n", &[(29, 30)]),
        ("- name: db.password\n  value: x\n", &[(29, 30)]),
        ("- name: 1PASSWORD\n  value: x\n", &[]),
        ("-\tname: PASSWORD\n\tvalue: x\n", &[(25, 26)]),
        ("-  name:PASSWORD\nvalue:x\n", &[(23, 24)]),
        ("name : PASSWORD \nvalue : x\n", &[(25, 26)]),
        ];
        for (input, want) in table {
            assert_eq!(detect(input), *want, "input {input:?}");
        }
    }

    /// A text and 16 texts a sixteenth of its size (`make(12000)` against 16 times `make(750)`) must cost about the same: 1x for a linear
    /// detector, 16x for a quadratic one; the limit is 4x. The timing is the shared CPU-time one of `crate::testing`.
    fn assert_linear(shape: &str, make: &dyn Fn(usize) -> String, run: &dyn Fn(&str)) {
        let whole = make(12000);
        let pieces: Vec<String> = (0..16).map(|_| make(12000 / 16)).collect();
        assert_scales(
            &format!("{shape:?}"),
            4,
            || pieces.iter().for_each(|piece| run(piece)),
            || run(&whole),
        );
    }

    #[test]
    fn yaml_spans_are_linear() {
        for shape in [
            "- name: PASSWORD\n  value: \"x\n",
            "password: |\n  a\n",
            "password: |\n\n",
            "- name: SECRET\n  value: x\n",
        ] {
            assert_linear(shape, &|n| shape.repeat(n / shape.len() * 4), &|s| {
                detect(s);
            });
        }
    }
}
