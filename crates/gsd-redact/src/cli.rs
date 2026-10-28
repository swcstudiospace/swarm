//! Command-line credential detection (`redact.py` lines 742-905): the reader-independent leaf.
//!
//! This half of the module holds what `_cli_option_spans` and `_cli_command_spans` use that
//! does not need the quote reader: the six CLI patterns as scanners, the character sets, the
//! kind bits and the command tables. Each scanner takes `&[char]` and reports code-point offsets
//! with the semantics of the Python pattern (`.search`/`.match`/`.finditer`/`.fullmatch` with
//! `pos` and `endpos`), so the detectors port line for line.
//!
//! None of the Python patterns uses `re.ASCII` or `re.IGNORECASE`; every class is spelled out,
//! so the negated classes accept every non-ASCII code point (NBSP, NEL, U+2028 ...) and only
//! the six ASCII blanks `[ \t\r\n\f\v]` delimit a word.

use crate::cls::{is_alnum, is_blank, is_digit};
use crate::reader::Reader;

// ---- character sets and kind bits ---------------------------------------------------------------

/// `_CLI_MYSQL`: the `mysql` family, whose bare `-p` is a prompt, not a value flag (`redact.py:763`).
pub const CLI_MYSQL: u8 = 1;
/// `_CLI_LOGIN`: `sshpass`, the `mongo*` tools, and the first word `login` of a two-word command.
pub const CLI_LOGIN: u8 = 2;
/// `_CLI_REDIS`: `redis-cli`, flag `-a`.
pub const CLI_REDIS: u8 = 4;
/// `_CLI_SQLCMD`: `sqlcmd` and `bcp`, flag `-P`.
pub const CLI_SQLCMD: u8 = 8;
/// `_CLI_KEYGEN`: `ssh-keygen`, flag `-N`.
pub const CLI_KEYGEN: u8 = 16;

/// `_CLI_BREAKS`: `\r\n\f\v;&|`, the characters that end a segment (line 753).
pub fn is_cli_break(c: char) -> bool {
    matches!(c, '\r' | '\n' | '\x0c' | '\x0b' | ';' | '&' | '|')
}

/// `_CLI_STOPS`: a blank or a [`is_cli_break`] character, the characters that end a word (line 754).
pub fn is_cli_stop(c: char) -> bool {
    is_blank(c) || is_cli_break(c)
}

/// `_CLI_NOT_LEAD`: `[A-Za-z0-9-]`. A word is scanned past the characters NOT in this set
/// (backtick, quote, `(`, `[` ...) to find an option's leading `-` (line 759).
pub fn is_cli_not_lead(c: char) -> bool {
    is_alnum(c) || c == '-'
}

/// `_CLI_NAME_CHARS`: `[A-Za-z0-9.-]`, the characters of a command name; no `_` (line 760).
pub fn is_cli_name_char(c: char) -> bool {
    is_alnum(c) || c == '.' || c == '-'
}

/// `_CLI_CLOSERS`: `` `"')]}>*_.,;: ``, the Markdown/prose closers (line 761).
pub fn is_cli_closer(c: char) -> bool {
    matches!(
        c,
        '`' | '"' | '\'' | ')' | ']' | '}' | '>' | '*' | '_' | '.' | ',' | ';' | ':'
    )
}

/// `_GLUE_STOPS`: `` `"')]}>*,;/?&| ``, the characters that stop a bare continuation after a
/// quoted segment (line 779). `_Reader.read` (line 625) uses it too.
pub fn is_glue_stop(c: char) -> bool {
    matches!(
        c,
        '`' | '"' | '\'' | ')' | ']' | '}' | '>' | '*' | ',' | ';' | '/' | '?' | '&' | '|'
    )
}

/// `_CLI_COMMANDS`: command name to kind bit (lines 765-772). Every kind is non-zero.
pub static CLI_COMMANDS: [(&str, u8); 24] = [
    ("mysql", CLI_MYSQL),
    ("mysqladmin", CLI_MYSQL),
    ("mysqldump", CLI_MYSQL),
    ("mysqlcheck", CLI_MYSQL),
    ("mysqlimport", CLI_MYSQL),
    ("mysqlshow", CLI_MYSQL),
    ("mysqlpump", CLI_MYSQL),
    ("mysqlsh", CLI_MYSQL),
    ("mariadb", CLI_MYSQL),
    ("mariadb-dump", CLI_MYSQL),
    ("mariadb-admin", CLI_MYSQL),
    ("sshpass", CLI_LOGIN),
    ("mongo", CLI_LOGIN),
    ("mongosh", CLI_LOGIN),
    ("mongodump", CLI_LOGIN),
    ("mongorestore", CLI_LOGIN),
    ("mongoexport", CLI_LOGIN),
    ("mongoimport", CLI_LOGIN),
    ("mongostat", CLI_LOGIN),
    ("mongotop", CLI_LOGIN),
    ("redis-cli", CLI_REDIS),
    ("sqlcmd", CLI_SQLCMD),
    ("bcp", CLI_SQLCMD),
    ("ssh-keygen", CLI_KEYGEN),
];

/// `_CLI_TWO_WORD`: the first words of a two-word command; the value is the `pending` state
/// (1: `login` follows, 2: `registry login` follows) (line 773).
pub static CLI_TWO_WORD: [(&str, u8); 9] = [
    ("docker", 1),
    ("podman", 1),
    ("buildah", 1),
    ("skopeo", 1),
    ("crane", 1),
    ("oras", 1),
    ("nerdctl", 1),
    ("az", 1),
    ("helm", 2),
];

/// `_CLI_COMMAND_HINTS`: if none of these occurs in the text, no command can be active (line 780).
pub static CLI_COMMAND_HINTS: [&str; 9] = [
    "mysql",
    "mariadb",
    "sshpass",
    "login",
    "mongo",
    "redis-cli",
    "sqlcmd",
    "bcp",
    "ssh-keygen",
];

/// Does `name` spell the ASCII literal `lit` exactly?
fn chars_are(name: &[char], lit: &str) -> bool {
    name.len() == lit.chars().count() && lit.chars().zip(name).all(|(a, &b)| a == b)
}

/// `_CLI_COMMANDS.get(name)`: the kind bit of the command `name`, `None` for any other word.
pub fn cli_command_kind(name: &[char]) -> Option<u8> {
    CLI_COMMANDS
        .iter()
        .find(|(cmd, _)| chars_are(name, cmd))
        .map(|&(_, kind)| kind)
}

/// `_CLI_TWO_WORD.get(name)`: the `pending` state `name` opens (1 or 2), `None` for any other word.
pub fn cli_two_word_state(name: &[char]) -> Option<u8> {
    CLI_TWO_WORD
        .iter()
        .find(|(cmd, _)| chars_are(name, cmd))
        .map(|&(_, state)| state)
}

/// `_CLI_FLAG_KINDS.get(s[at + 1:at + 2], 0)`: the command kinds that take `flag` as their
/// password flag; `flag` is the character after the `-` (`None` at the end of the text) and the
/// result is 0 for any other character.
pub fn cli_flag_kinds(flag: Option<char>) -> u8 {
    match flag {
        Some('p') => CLI_MYSQL | CLI_LOGIN,
        Some('a') => CLI_REDIS,
        Some('P') => CLI_SQLCMD,
        Some('N') => CLI_KEYGEN,
        _ => 0,
    }
}

// ---- text helpers -------------------------------------------------------------------------------

/// `lit in s`: does the ASCII literal `lit` occur anywhere in `s`?
pub fn contains_lit(s: &[char], lit: &str) -> bool {
    let n = lit.chars().count();
    n == 0 || s.windows(n).any(|w| chars_are(w, lit))
}

/// `any(h in s for h in _CLI_COMMAND_HINTS)`, the pre-filter of `_cli_command_spans` (line 858).
pub fn has_cli_command_hint(s: &[char]) -> bool {
    CLI_COMMAND_HINTS.iter().any(|h| contains_lit(s, h))
}

/// Is `s[span]` the ASCII literal `lit`? `span` is a `(start, end)` pair such as the one
/// [`cli_core_search`] returns; `core[0] == "login"` in Python.
pub fn span_is(s: &[char], span: (usize, usize), lit: &str) -> bool {
    s.get(span.0..span.1).is_some_and(|t| chars_are(t, lit))
}

/// Does the ASCII literal `lit` start at `s[i]`?
fn lit_at(s: &[char], i: usize, lit: &str) -> bool {
    lit.chars()
        .enumerate()
        .all(|(k, c)| s.get(i + k) == Some(&c))
}

// ---- `_CLI_WORD_RE`, `_CLI_BLANKS_RE`, `_CLI_UNQUOTED_RE`, `_CLI_UID_GID_RE`, `_CLI_CORE_RE` -----

/// `_CLI_WORD_RE.search(s, pos)`: `[^ \t\r\n\f\v;&|]+`, the leftmost maximal word starting at or
/// after `pos`, as `(start, end)`.
pub fn cli_word_search(s: &[char], pos: usize) -> Option<(usize, usize)> {
    let tail = s.get(pos..)?;
    let skip = tail.iter().take_while(|&&c| is_cli_stop(c)).count();
    let start = pos + skip;
    let len = s
        .get(start..)?
        .iter()
        .take_while(|&&c| !is_cli_stop(c))
        .count();
    (len > 0).then_some((start, start + len))
}

/// `_CLI_WORD_RE.finditer(s)`: the words of `s` in order, as `(start, end)`.
pub fn cli_words(s: &[char]) -> CliWords<'_> {
    CliWords { s, pos: 0 }
}

/// The iterator [`cli_words`] returns.
#[derive(Debug, Clone)]
pub struct CliWords<'a> {
    s: &'a [char],
    pos: usize,
}

impl Iterator for CliWords<'_> {
    type Item = (usize, usize);

    fn next(&mut self) -> Option<Self::Item> {
        let (start, end) = cli_word_search(self.s, self.pos)?;
        self.pos = end;
        Some((start, end))
    }
}

/// `_CLI_BLANKS_RE.match(s, pos).end()`: `[ \t]*` always matches; the end of the blank run that
/// starts at `pos`. A `pos` past the end of the text gives the text length, as `re` clamps it.
pub fn cli_blanks_end(s: &[char], pos: usize) -> usize {
    let pos = pos.min(s.len());
    pos + s[pos..].iter().take_while(|&&c| is_blank(c)).count()
}

/// `_CLI_UNQUOTED_RE.match(s, pos)`: `(?:[^ \t\r\n\f\v;&|"'\\]|\\[ \t]|\\(?!"))+`. The end of the
/// run, or `None` when no character can start it (a stop, a quote, a backslash before `"`).
///
/// The run is greedy and nothing follows it, so no backtracking is ever needed: a backslash
/// takes a blank with it, stays alone before anything else (also at the end of the text) and
/// ends the run before a `"`.
pub fn cli_unquoted_match(s: &[char], pos: usize) -> Option<usize> {
    let mut i = pos;
    while let Some(&c) = s.get(i) {
        if c == '\\' {
            match s.get(i + 1) {
                Some(&d) if is_blank(d) => i += 2,
                Some('"') => break,
                _ => i += 1,
            }
        } else if is_cli_stop(c) || c == '"' || c == '\'' {
            break;
        } else {
            i += 1;
        }
    }
    (i > pos).then_some(i)
}

/// `_CLI_UID_GID_RE.fullmatch(s, start, endpos)`: `[0-9]{1,10}:[0-9]{1,10}` over exactly
/// `s[start:endpos]`; the match is a bool because the detector only tests it.
pub fn cli_uid_gid_fullmatch(s: &[char], start: usize, endpos: usize) -> bool {
    let Some(seg) = s.get(start..endpos.min(s.len())) else {
        return false;
    };
    // The digits and the colon are disjoint classes, so the colon is the first (and only) one.
    let Some(colon) = seg.iter().position(|&c| c == ':') else {
        return false;
    };
    let (left, right) = (&seg[..colon], &seg[colon + 1..]);
    let digits = |t: &[char]| (1..=10).contains(&t.len()) && t.iter().all(|&c| is_digit(c));
    digits(left) && digits(right)
}

/// `_CLI_CORE_RE.search(s, start, end)`: `[A-Za-z0-9_./-]+`, the leftmost maximal run inside
/// `s[start:end]` (`end` is `endpos`: nothing past it is seen), as `(start, end)`.
pub fn cli_core_search(s: &[char], start: usize, end: usize) -> Option<(usize, usize)> {
    let end = end.min(s.len());
    let window = s.get(start..end)?;
    let is_core = |c: char| is_alnum(c) || matches!(c, '_' | '.' | '/' | '-');
    let skip = window.iter().take_while(|&&c| !is_core(c)).count();
    let len = window[skip..].iter().take_while(|&&c| is_core(c)).count();
    (len > 0).then_some((start + skip, start + skip + len))
}

// ---- `_CLI_OPTION_RE` ---------------------------------------------------------------------------

/// One match of `_CLI_OPTION_RE`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OptionMatch {
    /// `m.start()`: the leading `-` of the option.
    pub start: usize,
    /// `m.end()`: just past the option, or past the `=` of `-u=`. The value is read from here.
    pub end: usize,
    /// `m.group(1) == "="`: the value is attached with `=`. False for a `-u`/`--user`/`--proxy-user`
    /// followed by a blank (group 1 is the empty string) and for the `-pass` forms (group 1 is `None`).
    pub eq: bool,
    /// `m.group(2) is not None`: an openssl `-pass`, `-passin` or `-passout`.
    pub pass: bool,
}

/// The match of `_CLI_OPTION_RE` that starts exactly at `s[i]`, with its lookbehind.
///
/// `(?<![A-Za-z0-9-])(?:(?:--user|--proxy-user|-u)(=|(?=[ \t]))|(-pass(?:in|out)?)(?=[ \t]))`.
/// The alternatives are tried in pattern order, and a failed group 1 falls through to the next
/// one (`--userx` fails them all).
fn option_at(s: &[char], i: usize) -> Option<OptionMatch> {
    if s.get(i) != Some(&'-') {
        return None;
    }
    if i.checked_sub(1)
        .and_then(|p| s.get(p))
        .is_some_and(|&c| is_alnum(c) || c == '-')
    {
        return None;
    }
    for lit in ["--user", "--proxy-user", "-u"] {
        if !lit_at(s, i, lit) {
            continue;
        }
        let end = i + lit.len();
        match s.get(end) {
            Some('=') => {
                return Some(OptionMatch {
                    start: i,
                    end: end + 1,
                    eq: true,
                    pass: false,
                })
            }
            Some(&c) if is_blank(c) => {
                return Some(OptionMatch {
                    start: i,
                    end,
                    eq: false,
                    pass: false,
                })
            }
            _ => {}
        }
    }
    if lit_at(s, i, "-pass") {
        // `(?:in|out)?` is greedy: `in`, then `out`, then nothing, each followed by a blank.
        for suffix in ["in", "out", ""] {
            let end = i + "-pass".len() + suffix.len();
            if lit_at(s, i + "-pass".len(), suffix) && s.get(end).is_some_and(|&c| is_blank(c)) {
                return Some(OptionMatch {
                    start: i,
                    end,
                    eq: false,
                    pass: true,
                });
            }
        }
    }
    None
}

/// `_CLI_OPTION_RE.search(s, pos)`: the leftmost match starting at or after `pos`. The
/// lookbehind reads the real text before `pos`, as `re` does.
pub fn cli_option_search(s: &[char], pos: usize) -> Option<OptionMatch> {
    (pos..s.len()).find_map(|i| option_at(s, i))
}

/// `_CLI_OPTION_RE.finditer(s)`: every match in order. A match is never empty, so the next
/// search resumes at its end.
pub fn cli_options(s: &[char]) -> CliOptions<'_> {
    CliOptions { s, pos: 0 }
}

/// The iterator [`cli_options`] returns.
#[derive(Debug, Clone)]
pub struct CliOptions<'a> {
    s: &'a [char],
    pos: usize,
}

impl Iterator for CliOptions<'_> {
    type Item = OptionMatch;

    fn next(&mut self) -> Option<Self::Item> {
        let m = cli_option_search(self.s, self.pos)?;
        self.pos = m.end;
        Some(m)
    }
}

// ---- reader-dependent detectors -----------------------------------------------------------------
// `_cli_attached`, `_cli_option_spans`, `_cli_command_spans` and `_cli_spans` (lines 783-908): they need the quote reader and use only
// the items above.

/// `_cli_attached` (line 783): whether the flag at `s[at..at + 2]` (`-p`) of the word that ends at `end` carries its value attached:
/// `-pVALUE`, `-p'VALUE'`, `-p$'VALUE'`, `-p\"VALUE\"`.
///
/// A quote that opens the value (one that the lexer does not take for the CLOSE of an earlier quote of the line: `"mysql -p" ok` and
/// `"-p"` are prose) makes everything after it the value, even when it is only punctuation or blanks (`-p" x"`); an unquoted value
/// is one unless it is made only of closers (`-p`, `(-p)`, `**-p**`) that end the word.
fn cli_attached(rd: &Reader<'_>, at: usize, end: usize) -> bool {
    let mut v = at + 2;
    if v >= end {
        return false;
    }
    if let Some((_, oplen, run)) = rd.opener(v) {
        if !rd.closes(v, oplen, run) {
            return true;
        }
        // the quote CLOSES an earlier one: only what follows it can be a value (`"mysql -p" ok`)
        v += oplen;
    }
    // `any(ch not in _CLI_CLOSERS for ch in rd.s[v:end])`; an empty or inverted slice is `False`
    rd.s.get(v..end)
        .is_some_and(|t| t.iter().any(|&c| !is_cli_closer(c)))
}

/// `_cli_option_spans` (line 799): the values of `-u user:pass` (and `--user`, `--proxy-user`, also `=user:pass`) and of
/// `-passin pass:X`, appended to `out` as `(start, end)` in Python's `add()` order.
fn cli_option_spans(rd: &Reader<'_>, out: &mut Vec<(usize, usize)>) {
    let s = rd.s;
    if !contains_lit(s, "-u") && !contains_lit(s, "-pass") {
        return;
    }
    let n = s.len();
    // `[run.0, run.1)` holds no blank, `;&|` or quote: the unquoted run every start inside it ends at. Python starts with
    // `-1, -1`; the empty range `(1, 0)` holds no start either.
    let mut run = (1usize, 0usize);
    // no ':' in `[colon.0, colon.1)`, a ':' at `colon.1` (`n` when there is none); `(1, 0)` holds no start
    let mut colon = (1usize, 0usize);
    // end of the last value reported (Python `-1`): a later start (starts only grow) inside that run is covered
    let mut pushed_end = 0usize;
    for m in cli_options(s) {
        // `_CLI_BLANKS_RE` matches at every position
        let start = if m.eq {
            m.end
        } else {
            cli_blanks_end(s, m.end)
        };
        if start < pushed_end || start >= n || is_cli_stop(s[start]) {
            continue; // (a start inside a reported run is covered)
        }
        let op = rd.opener(start);
        if m.pass {
            // openssl: `-passin pass:SECRET`, `-passin "pass:SECRET"`, `-passin pass:"a b"`
            if let Some((q, oplen, run_len)) = op {
                let (end, close) = rd.quoted(start, q, oplen, run_len);
                let inner = start + oplen;
                if lit_at(s, inner, "pass:") {
                    if let Some(stop) = end.checked_sub(close).filter(|&e| e > inner + 5) {
                        out.push((inner + 5, stop));
                    }
                }
            } else if lit_at(s, start, "pass:") {
                let end = rd.read(start + 5, cli_unquoted_match).0;
                if end > start + 5 {
                    out.push((start + 5, end));
                }
            }
            continue;
        }
        if let Some((q, oplen, run_len)) = op {
            // the whole `user:pass` is quoted
            let end = rd.quoted(start, q, oplen, run_len).0;
            if s.get(start + oplen..end).is_some_and(|t| t.contains(&':')) {
                out.push((start, end));
                pushed_end = end;
            }
            continue;
        }
        if !(run.0 <= start && start < run.1) {
            // the opener check above excludes every quote and quote-escaping backslash, so a word starts here (Python asserts it)
            let Some(word_end) = cli_unquoted_match(s, start) else {
                continue;
            };
            run = (start, word_end);
        }
        if !(colon.0 <= start && start <= colon.1) {
            let at = s[start..]
                .iter()
                .position(|&c| c == ':')
                .map_or(n, |k| start + k);
            colon = (start, at);
        }
        let (run_to, colon_at) = (run.1, colon.1);
        if start >= colon_at || colon_at >= run_to {
            continue; // no user, or no `:` inside the word
        }
        // a user, `:`, a password (both non-empty): bare, or quoted in any form (`curl -u me:'a b'`); a docker uid:gid pair is not a
        // credential
        let p = colon_at + 1;
        let end = if rd.opener(p).is_some() {
            rd.read(p, cli_unquoted_match).0
        } else if p < run_to
            && !(colon_at - start <= 10
                && run_to - colon_at <= 11
                && cli_uid_gid_fullmatch(s, start, run_to))
        {
            run_to
        } else {
            continue;
        };
        if end > p {
            out.push((start, end));
            pushed_end = end;
        }
    }
}

/// `_cli_command_spans` (line 855): the password flag of `mysql -pSECRET`, `docker login -p SECRET`, `sshpass -p`, `redis-cli -a`,
/// `sqlcmd -P`, `ssh-keygen -N`, appended to `out`. Once a command is seen in a segment its flag is checked in EVERY later word of
/// that segment, however long or numerous the arguments.
fn cli_command_spans(rd: &Reader<'_>, out: &mut Vec<(usize, usize)>) {
    let s = rd.s;
    if !has_cli_command_hint(s) {
        return;
    }
    // command kinds seen in this segment; `login` expected (1: `login`, 2: `registry login`, 3: after `registry`); a flag's value expected
    let (mut active, mut pending, mut want) = (0u8, 0u8, false);
    let mut prev_end = 0usize;
    for (start, end) in cli_words(s) {
        // `(start != prev_end + 1 or s[prev_end] not in " \t") and s[prev_end:start].strip(" \t")`: the gap holds a character that is
        // not a blank, i.e. a newline or `;&|` (the first clause only short-cuts the second)
        if s[prev_end..start].iter().any(|&c| !is_blank(c)) {
            active = 0;
            pending = 0;
            want = false; // a newline or `;&|`: the next command
        }
        prev_end = end;
        if want {
            // the value is taken whole, and its words are still read: a value can hold the next command (`redis-cli -a $(sshpass -p x)`)
            want = false;
            if s[start] != '-' {
                let value_end = rd.read(start, cli_unquoted_match).0;
                if value_end > start {
                    out.push((start, value_end));
                }
            }
        }
        // `, ", (, [, <, ** ... may precede an option
        let at = (start..end).find(|&k| is_cli_not_lead(s[k])).unwrap_or(end);
        if at < end && s[at] == '-' {
            pending = 0;
            let kinds = if active != 0 {
                active & cli_flag_kinds(s.get(at + 1).copied())
            } else {
                0
            };
            if kinds != 0 {
                if cli_attached(rd, at, end) {
                    // `-pSECRET`: the value touches the flag
                    let value_end = rd.read(at + 2, cli_unquoted_match).0;
                    if value_end > at + 2 {
                        out.push((at + 2, value_end));
                    }
                    continue;
                }
                if kinds != CLI_MYSQL {
                    want = true; // a bare `-p` is a prompt for mysql, not for the others: the value is the next word
                }
            }
        }
        if pending != 0 {
            let core = if end - start <= 24 {
                cli_core_search(s, start, end)
            } else {
                None
            };
            if pending == 2 {
                pending = if core.is_some_and(|c| span_is(s, c, "registry")) {
                    3
                } else {
                    0
                };
            } else {
                if core.is_some_and(|c| span_is(s, c, "login")) {
                    active |= CLI_LOGIN;
                }
                pending = 0;
            }
        }
        // the command name is the last run of name characters (`/usr/bin/mysql`, `$(mysql`), at most 40 of them
        let floor = start.max(end.saturating_sub(41));
        let mut base_start = end;
        while base_start > floor && is_cli_name_char(s[base_start - 1]) {
            base_start -= 1;
        }
        if base_start < end && end - base_start <= 40 {
            let base = &s[base_start..end];
            if let Some(kind) = cli_command_kind(base) {
                active |= kind;
            } else if let Some(state) = cli_two_word_state(base) {
                pending = state;
            }
        }
    }
}

/// `_cli_spans` (line 907): every command-line credential span of the text `rd` reads, appended to `out` as `(start, end)` in the
/// order Python calls `add`: the `-u`/`-passin` values first, then the command flags. The spans may overlap and are not sorted; the
/// caller merges them.
pub fn cli_spans(rd: &Reader<'_>, out: &mut Vec<(usize, usize)>) {
    cli_option_spans(rd, out);
    cli_command_spans(rd, out);
}

#[cfg(test)]
mod tests {
    //! Every vector below was produced by the real `swarm.redact` objects (`SWARM_PURE_PYTHON=1`),
    //! never derived from reading the scanners. They pin the boundaries a differential fuzz of the
    //! six scanners against the Python patterns found interesting (millions of random cases, with
    //! structured ones at every cap: the `{1,10}` uid/gid runs, the 24-character core).

    use super::*;
    use crate::testing::assert_scales;

    /// A `(start, end)` span.
    type Span = (usize, usize);
    /// An option match as `(start, end, group(1) == "=", group(2) is not None)`.
    type OptTuple = (usize, usize, bool, bool);

    fn cs(s: &str) -> Vec<char> {
        s.chars().collect()
    }

    /// Every code point that could tell two character sets apart: the BMP up to U+3000, an astral
    /// code point and the last one.
    fn probe_chars() -> impl Iterator<Item = char> {
        (0..0x3000u32)
            .filter_map(char::from_u32)
            .chain(['\u{1F600}', '\u{10FFFF}'])
    }

    /// `(text, pos, _CLI_WORD_RE.search(text, pos))`
    const WORD_VECTORS: &[(&str, usize, Option<Span>)] = &[
        ("", 0, None),
        (" ", 0, None),
        ("a", 0, Some((0, 1))),
        ("a", 1, None),
        ("a", 2, None),
        ("  ab  cd", 0, Some((2, 4))),
        ("  ab  cd", 4, Some((6, 8))),
        ("  ab  cd", 3, Some((3, 4))),
        ("a;b", 0, Some((0, 1))),
        ("a;b", 1, Some((2, 3))),
        ("a&b|c", 0, Some((0, 1))),
        ("a&b|c", 2, Some((2, 3))),
        ("a\u{9}b\u{a}c\u{d}d\u{c}e\u{b}f", 0, Some((0, 1))),
        ("a\u{9}b\u{a}c\u{d}d\u{c}e\u{b}f", 2, Some((2, 3))),
        ("a\u{9}b\u{a}c\u{d}d\u{c}e\u{b}f", 9, Some((10, 11))),
        ("-u\u{a0}x y", 0, Some((0, 4))),
        ("x\u{85}y", 0, Some((0, 3))),
        ("x\u{2028}y z", 0, Some((0, 3))),
        ("\u{a0}", 0, Some((0, 1))),
        ("\u{a0} \u{a0}", 0, Some((0, 1))),
        ("\u{1c}ab\u{1d}", 0, Some((0, 4))),
        ("\u{1f}", 0, Some((0, 1))),
        ("\u{1f600} \u{1f600}\u{1f600}", 0, Some((0, 1))),
        ("\u{1f600} \u{1f600}\u{1f600}", 1, Some((2, 4))),
        ("\u{1f600} \u{1f600}\u{1f600}", 2, Some((2, 4))),
        ("\u{212a};\u{17f}", 0, Some((0, 1))),
        (";;;", 0, None),
        ("ab\u{a}", 0, Some((0, 2))),
        ("\u{a}ab", 0, Some((1, 3))),
        ("ab cd", 1, Some((1, 2))),
        ("ab cd", 5, None),
        ("ab cd", 6, None),
        ("`-p`", 0, Some((0, 4))),
        ("\\ x", 0, Some((0, 1))),
        (" a", 9, None),
        ("a b c", 2, Some((2, 3))),
        ("a b c", 3, Some((4, 5))),
        ("x\u{a0};\u{2028}y", 0, Some((0, 2))),
        ("x\u{a0};\u{2028}y", 3, Some((3, 5))),
    ];
    /// `(text, [m.span() for m in _CLI_WORD_RE.finditer(text)])`
    const WORD_ITER_VECTORS: &[(&str, &[Span])] = &[
        ("", &[]),
        ("a b", &[(0, 1), (2, 3)]),
        (
            "  a;b|c&d\u{a}e\u{9}f  ",
            &[(2, 3), (4, 5), (6, 7), (8, 9), (10, 11), (12, 13)],
        ),
        ("x\u{a0}y z", &[(0, 3), (4, 5)]),
        ("-u a:b -p c", &[(0, 2), (3, 6), (7, 9), (10, 11)]),
        (
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            &[(0, 50)],
        ),
        (";;a;;b;;", &[(2, 3), (5, 6)]),
        ("\u{1f600} \u{1f600}", &[(0, 1), (2, 3)]),
    ];
    /// `(text, pos, _CLI_BLANKS_RE.match(text, pos).end())`
    const BLANKS_VECTORS: &[(&str, usize, usize)] = &[
        ("", 0, 0),
        ("", 3, 0),
        ("  x", 0, 2),
        ("  x", 1, 2),
        ("  x", 2, 2),
        ("  x", 3, 3),
        ("  x", 9, 3),
        ("\u{9} \u{9}x", 0, 3),
        ("x  ", 1, 3),
        ("x  ", 3, 3),
        ("x\u{a}  ", 1, 1),
        ("x\u{d} ", 1, 1),
        (" \u{c} ", 0, 1),
        (" \u{b}", 0, 1),
        ("\u{a0} ", 0, 0),
        (" \u{a0}", 0, 1),
        ("\u{1f600}  ", 1, 3),
        ("   ", 1, 3),
    ];
    /// `(text, pos, _CLI_UNQUOTED_RE.match(text, pos).end())`
    const UNQUOTED_VECTORS: &[(&str, usize, Option<usize>)] = &[
        ("", 0, None),
        ("a", 0, Some(1)),
        ("a", 1, None),
        ("a", 5, None),
        ("abc def", 0, Some(3)),
        ("abc def", 3, None),
        ("abc def", 4, Some(7)),
        ("abc\u{9}def", 0, Some(3)),
        ("abc\u{d}def", 0, Some(3)),
        ("abc\u{a}def", 0, Some(3)),
        ("abc\u{c}def", 0, Some(3)),
        ("abc\u{b}def", 0, Some(3)),
        ("a;b", 0, Some(1)),
        ("a&b", 0, Some(1)),
        ("a|b", 0, Some(1)),
        ("a\"b", 0, Some(1)),
        ("a'b", 0, Some(1)),
        ("\"ab", 0, None),
        ("'ab", 0, None),
        (";ab", 0, None),
        ("\\", 0, Some(1)),
        ("\\\\", 0, Some(2)),
        ("\\ ", 0, Some(2)),
        ("\\\u{9}", 0, Some(2)),
        ("\\ x", 0, Some(3)),
        ("\\\u{9}x y", 0, Some(3)),
        ("\\\"", 0, None),
        ("\\'", 0, Some(1)),
        ("a\\\"b", 0, Some(1)),
        ("a\\'b", 0, Some(2)),
        ("a\\", 0, Some(2)),
        ("a\\\\\"b", 0, Some(2)),
        ("\\\\\"", 0, Some(1)),
        ("\\\\\\\"", 0, Some(2)),
        ("a\\ b c", 0, Some(4)),
        ("a\\ b\\ c d", 0, Some(7)),
        ("a\\\u{a}b", 0, Some(2)),
        ("\\;x", 0, Some(1)),
        ("\\x", 0, Some(2)),
        ("a\\\\ b", 0, Some(5)),
        ("\u{a0}x", 0, Some(2)),
        ("a\u{a0}b c", 0, Some(3)),
        ("a\u{85}\u{2028}b c", 0, Some(4)),
        ("\u{1f600}\u{1f600} x", 0, Some(2)),
        ("a=b:c/d", 0, Some(7)),
        ("p$(x)", 0, Some(5)),
        ("a\\\u{1f600}", 0, Some(3)),
        ("x\\", 1, Some(2)),
        ("x\\", 2, None),
        ("ab", 1, Some(2)),
        (" ab", 0, None),
        (" ab", 1, Some(3)),
    ];
    /// `(text, pos, endpos, _CLI_UID_GID_RE.fullmatch(text, pos, endpos) is not None)`
    const UID_GID_VECTORS: &[(&str, usize, usize, bool)] = &[
        ("", 0, 0, false),
        ("1:2", 0, 3, true),
        ("1:2", 0, 2, false),
        ("1:2", 0, 99, true),
        ("1:2", 1, 3, false),
        ("1:2", 3, 3, false),
        ("1:2", 2, 1, false),
        ("1000:1000", 0, 9, true),
        (":", 0, 1, false),
        ("1:", 0, 2, false),
        (":1", 0, 2, false),
        ("1:2:3", 0, 5, false),
        ("1:2:3", 0, 3, true),
        ("a1:2", 0, 4, false),
        ("a1:2", 1, 4, true),
        ("1:2a", 0, 4, false),
        ("1:2a", 0, 3, true),
        ("1 :2", 0, 4, false),
        ("1: 2", 0, 4, false),
        ("1::2", 0, 4, false),
        ("1\u{661}:2", 0, 4, false),
        ("1:\u{661}", 0, 3, false),
        ("\u{ff11}:2", 0, 3, false),
        ("12", 0, 2, false),
        ("1:2", 0, 4, true),
        ("1-2", 0, 3, false),
        ("1:2", 0, 3, true),
        ("1:222222222222", 0, 14, false),
        ("111111111:222222222", 0, 19, true),
        ("111111111:2222222222", 0, 20, true),
        ("1111111111:222222222", 0, 20, true),
        ("1111111111:2222222222", 0, 21, true),
        ("1111111111:22222222222", 0, 22, false),
        ("11111111111:2222222222", 0, 22, false),
        ("11111111111:22222222222", 0, 23, false),
        ("111111111111:2", 0, 14, false),
        ("1111111111:1111111111", 0, 20, true),
        ("1111111111:1111111111", 1, 21, true),
        ("x1111111111:1111111111y", 1, 22, true),
        ("x1111111111:1111111111y", 0, 23, false),
    ];
    /// `(text, start, end, _CLI_CORE_RE.search(text, start, end))`
    const CORE_VECTORS: &[(&str, usize, usize, Option<Span>)] = &[
        ("", 0, 0, None),
        ("login", 0, 5, Some((0, 5))),
        ("login", 0, 4, Some((0, 4))),
        ("login", 1, 5, Some((1, 5))),
        ("login", 0, 99, Some((0, 5))),
        ("login", 5, 5, None),
        ("login", 6, 99, None),
        ("login", 3, 2, None),
        ("registry", 0, 8, Some((0, 8))),
        ("registry", 0, 7, Some((0, 7))),
        ("-x/./a_b", 0, 8, Some((0, 8))),
        ("  a b", 0, 5, Some((2, 3))),
        ("  a b", 0, 1, None),
        ("  a b", 2, 3, Some((2, 3))),
        ("  a b", 2, 5, Some((2, 3))),
        ("  a b", 3, 5, Some((4, 5))),
        ("  a b", 3, 4, None),
        ("[docker]", 0, 8, Some((1, 7))),
        ("`login`", 0, 7, Some((1, 6))),
        ("`login`", 1, 6, Some((1, 6))),
        ("**login**", 0, 9, Some((2, 7))),
        ("a\u{9}b", 0, 3, Some((0, 1))),
        ("a:b", 0, 3, Some((0, 1))),
        ("a=b", 0, 3, Some((0, 1))),
        ("a~b", 0, 3, Some((0, 1))),
        ("\u{e9}a", 0, 2, Some((1, 2))),
        ("a\u{e9}b", 0, 3, Some((0, 1))),
        ("\u{212a}", 0, 1, None),
        ("\u{17f}", 0, 1, None),
        ("\u{1f600}a", 0, 2, Some((1, 2))),
        ("a\u{1f600}b", 0, 3, Some((0, 1))),
        ("-", 0, 1, Some((0, 1))),
        ("/", 0, 1, Some((0, 1))),
        (".", 0, 1, Some((0, 1))),
        ("_", 0, 1, Some((0, 1))),
        ("+", 0, 1, None),
        ("aaaaaaaaaaaaaaaaaaaaaaaa", 0, 24, Some((0, 24))),
        ("aaaaaaaaaaaaaaaaaaaaaaaaa", 0, 25, Some((0, 25))),
        ("aaaaaaaaaaaaaaaaaaaaaaa-", 0, 24, Some((0, 24))),
        ("xxaaaaaaaaaaaaaaaaaaaaaaaayy", 2, 26, Some((2, 26))),
        ("xxaaaaaaaaaaaaaaaaaaaaaaaayy", 2, 25, Some((2, 25))),
        ("xxaaaaaaaaaaaaaaaaaaaaaaaayy", 0, 28, Some((0, 28))),
        ("@@@@", 0, 4, None),
        ("...", 1, 3, Some((1, 3))),
    ];
    /// `(text, pos, _CLI_OPTION_RE.search(text, pos))` as `(start, end, group(1) == "=", group(2) is not None)`
    const OPTION_VECTORS: &[(&str, usize, Option<OptTuple>)] = &[
        ("", 0, None),
        ("-u", 0, None),
        ("-u ", 0, Some((0, 2, false, false))),
        ("-u\u{9}", 0, Some((0, 2, false, false))),
        ("-u=", 0, Some((0, 3, true, false))),
        ("-ux", 0, None),
        ("-u=x", 0, Some((0, 3, true, false))),
        ("-u a:b", 0, Some((0, 2, false, false))),
        ("--user a:b", 0, Some((0, 6, false, false))),
        ("--user=a:b", 0, Some((0, 7, true, false))),
        ("--user", 0, None),
        ("--user ", 0, Some((0, 6, false, false))),
        ("--userx ", 0, None),
        ("--users=", 0, None),
        ("--use r", 0, None),
        ("--proxy-user a:b", 0, Some((0, 12, false, false))),
        ("--proxy-user=a:b", 0, Some((0, 13, true, false))),
        ("--proxy-user", 0, None),
        ("--proxy-usr ", 0, None),
        ("--proxy ", 0, None),
        ("-pass ", 0, Some((0, 5, false, true))),
        ("-passin ", 0, Some((0, 7, false, true))),
        ("-passout ", 0, Some((0, 8, false, true))),
        ("-passin", 0, None),
        ("-passin=", 0, None),
        ("-pass=", 0, None),
        ("-passinx ", 0, None),
        ("-passoutx ", 0, None),
        ("-passi ", 0, None),
        ("-passou ", 0, None),
        ("-pass\u{9}", 0, Some((0, 5, false, true))),
        ("-pass pass:x", 0, Some((0, 5, false, true))),
        ("-passin pass:x", 0, Some((0, 7, false, true))),
        ("-passin\u{9}pass:x", 0, Some((0, 7, false, true))),
        ("-passwd ", 0, None),
        ("-password ", 0, None),
        ("-p ", 0, None),
        ("x-u a", 0, None),
        ("x -u a", 0, Some((2, 4, false, false))),
        ("a-u=b", 0, None),
        ("1-u ", 0, None),
        ("--u ", 0, None),
        ("---user a", 0, None),
        ("--user --user a", 0, Some((0, 6, false, false))),
        ("-u -u a", 0, Some((0, 2, false, false))),
        ("-u=-u=-u=", 0, Some((0, 3, true, false))),
        ("-u- ", 0, None),
        ("-u-u ", 0, None),
        ("--user=-u ", 0, Some((0, 7, true, false))),
        ("-uu ", 0, None),
        ("-U a", 0, None),
        ("-USER a", 0, None),
        ("_-u a", 0, Some((1, 3, false, false))),
        ("`-u a", 0, Some((1, 3, false, false))),
        ("'-u=a'", 0, Some((1, 4, true, false))),
        ("(-u a", 0, Some((1, 3, false, false))),
        ("[--user=a]", 0, Some((1, 8, true, false))),
        ("*-u a", 0, Some((1, 3, false, false))),
        ("=-u a", 0, Some((1, 3, false, false))),
        (":-u a", 0, Some((1, 3, false, false))),
        (".-u a", 0, Some((1, 3, false, false))),
        ("\u{e9}-u a", 0, Some((1, 3, false, false))),
        ("\u{a0}-u a", 0, Some((1, 3, false, false))),
        ("\u{1f600}-u a", 0, Some((1, 3, false, false))),
        ("-u\u{a0}a", 0, None),
        ("-u\u{1f600}", 0, None),
        ("a -u a:b -u c:d", 0, Some((2, 4, false, false))),
        ("-u a -passin x -pass y", 0, Some((0, 2, false, false))),
        ("curl -u me:pw https://x", 0, Some((5, 7, false, false))),
        (
            "curl --user=me:pw --proxy-user a:b",
            0,
            Some((5, 12, true, false)),
        ),
        ("-passin -passout ", 0, Some((0, 7, false, true))),
        ("-pass-pass ", 0, None),
        ("-pass -pass ", 0, Some((0, 5, false, true))),
        ("- u ", 0, None),
        ("-", 0, None),
        ("--", 0, None),
        ("-u\u{a}", 0, None),
        ("-u\u{d}", 0, None),
        ("-u -u", 1, None),
        ("-u -u", 2, None),
        ("-u -u", 3, None),
        ("--user -u", 2, None),
        ("a-u b", 1, None),
        ("a-u b", 2, None),
        ("x -u a", 3, None),
        ("x -u a", 2, Some((2, 4, false, false))),
        ("-u a", 9, None),
        ("-u a", 4, None),
        ("-passin -u ", 1, Some((8, 10, false, false))),
        ("-pass x -u y", 5, Some((8, 10, false, false))),
        ("a--user b", 1, None),
        ("a--user b", 2, None),
        ("a-pass b", 1, None),
        ("--proxy-user=", 2, None),
        ("--proxy-user=", 8, None),
        ("-u=-u ", 3, Some((3, 5, false, false))),
        ("-u-u ", 2, None),
    ];
    /// `(text, [(m.start(), m.end(), group(1) == "=", group(2) is not None) for m in _CLI_OPTION_RE.finditer(text)])`
    const OPTION_ITER_VECTORS: &[(&str, &[OptTuple])] = &[
        (
            "curl -u a:b --user=c:d --proxy-user e:f",
            &[
                (5, 7, false, false),
                (12, 19, true, false),
                (23, 35, false, false),
            ],
        ),
        ("-u -u -u", &[(0, 2, false, false), (3, 5, false, false)]),
        (
            "-u=-u=-u=",
            &[
                (0, 3, true, false),
                (3, 6, true, false),
                (6, 9, true, false),
            ],
        ),
        (
            "-passin x -passout y -pass z",
            &[
                (0, 7, false, true),
                (10, 18, false, true),
                (21, 26, false, true),
            ],
        ),
        ("x-u -u y", &[(4, 6, false, false)]),
        (
            "--user=--user=",
            &[(0, 7, true, false), (7, 14, true, false)],
        ),
        (
            "-u\u{9}-u\u{9}",
            &[(0, 2, false, false), (3, 5, false, false)],
        ),
        ("a -u b-u c -u", &[(2, 4, false, false)]),
    ];
    /// The members of `_CLI_BREAKS`, `_CLI_STOPS`, `_CLI_NOT_LEAD`, `_CLI_NAME_CHARS`, `_CLI_CLOSERS` and `_GLUE_STOPS`, dumped from the oracle.
    const BREAKS: &str = "\u{a}\u{b}\u{c}\u{d}&;|";
    const STOPS: &str = "\u{9}\u{a}\u{b}\u{c}\u{d} &;|";
    const NOT_LEAD: &str = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    const NAME_CHARS: &str = "-.0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
    const CLOSERS: &str = "\"')*,.:;>]_`}";
    const GLUE_STOPS: &str = "\"&')*,/;>?]`|}";
    /// `_CLI_COMMANDS` dumped from the oracle, sorted by name.
    const COMMANDS: &[(&str, u8)] = &[
        ("bcp", 8),
        ("mariadb", 1),
        ("mariadb-admin", 1),
        ("mariadb-dump", 1),
        ("mongo", 2),
        ("mongodump", 2),
        ("mongoexport", 2),
        ("mongoimport", 2),
        ("mongorestore", 2),
        ("mongosh", 2),
        ("mongostat", 2),
        ("mongotop", 2),
        ("mysql", 1),
        ("mysqladmin", 1),
        ("mysqlcheck", 1),
        ("mysqldump", 1),
        ("mysqlimport", 1),
        ("mysqlpump", 1),
        ("mysqlsh", 1),
        ("mysqlshow", 1),
        ("redis-cli", 4),
        ("sqlcmd", 8),
        ("ssh-keygen", 16),
        ("sshpass", 2),
    ];
    /// `_CLI_TWO_WORD` dumped from the oracle, sorted by name.
    const TWO_WORD: &[(&str, u8)] = &[
        ("az", 1),
        ("buildah", 1),
        ("crane", 1),
        ("docker", 1),
        ("helm", 2),
        ("nerdctl", 1),
        ("oras", 1),
        ("podman", 1),
        ("skopeo", 1),
    ];
    /// `_CLI_FLAG_KINDS` dumped from the oracle, sorted by flag.
    const FLAG_KINDS: &[(char, u8)] = &[('N', 16), ('P', 8), ('a', 4), ('p', 3)];
    /// `_CLI_COMMAND_HINTS` dumped from the oracle, in order.
    const HINTS: &[&str] = &[
        "mysql",
        "mariadb",
        "sshpass",
        "login",
        "mongo",
        "redis-cli",
        "sqlcmd",
        "bcp",
        "ssh-keygen",
    ];
    const KIND_BITS: [u8; 5] = [1, 2, 4, 8, 16];
    /// `(text, any(h in text for h in _CLI_COMMAND_HINTS))`
    const HINT_VECTORS: &[(&str, bool)] = &[
        ("", false),
        ("mysql", true),
        ("mysq", false),
        ("MYSQL", false),
        ("x mariadb-dump", true),
        ("sshpass -p", true),
        ("docker login", true),
        ("logi n", false),
        ("login", true),
        ("mongo", true),
        ("mongosh", true),
        ("redis-cli", true),
        ("redis cli", false),
        ("sqlcmd", true),
        ("bcp", true),
        ("bc p", false),
        ("ssh-keygen", true),
        ("ssh keygen", false),
        ("ssh-keyge", false),
        ("-u me:pw", false),
        ("mysq\u{212a}", false),
    ];

    #[test]
    fn word_search_matches_oracle() {
        for &(text, pos, want) in WORD_VECTORS {
            assert_eq!(cli_word_search(&cs(text), pos), want, "{text:?} @ {pos}");
        }
    }

    #[test]
    fn words_iterate_like_finditer() {
        for &(text, want) in WORD_ITER_VECTORS {
            let got: Vec<_> = cli_words(&cs(text)).collect();
            assert_eq!(got, want, "{text:?}");
        }
    }

    #[test]
    fn blanks_end_matches_oracle() {
        for &(text, pos, want) in BLANKS_VECTORS {
            assert_eq!(cli_blanks_end(&cs(text), pos), want, "{text:?} @ {pos}");
        }
    }

    #[test]
    fn unquoted_match_matches_oracle() {
        for &(text, pos, want) in UNQUOTED_VECTORS {
            assert_eq!(cli_unquoted_match(&cs(text), pos), want, "{text:?} @ {pos}");
        }
    }

    #[test]
    fn uid_gid_fullmatch_matches_oracle() {
        for &(text, pos, endpos, want) in UID_GID_VECTORS {
            assert_eq!(
                cli_uid_gid_fullmatch(&cs(text), pos, endpos),
                want,
                "{text:?} [{pos}:{endpos}]"
            );
        }
    }

    #[test]
    fn core_search_matches_oracle() {
        for &(text, start, end, want) in CORE_VECTORS {
            assert_eq!(
                cli_core_search(&cs(text), start, end),
                want,
                "{text:?} [{start}:{end}]"
            );
        }
    }

    fn tuple(m: OptionMatch) -> OptTuple {
        (m.start, m.end, m.eq, m.pass)
    }

    #[test]
    fn option_search_matches_oracle() {
        for &(text, pos, want) in OPTION_VECTORS {
            let got = cli_option_search(&cs(text), pos).map(tuple);
            assert_eq!(got, want, "{text:?} @ {pos}");
        }
    }

    #[test]
    fn options_iterate_like_finditer() {
        for &(text, want) in OPTION_ITER_VECTORS {
            let got: Vec<_> = cli_options(&cs(text)).map(tuple).collect();
            assert_eq!(got, want, "{text:?}");
        }
    }

    #[test]
    fn character_sets_match_oracle() {
        type Set = (&'static str, fn(char) -> bool, &'static str);
        let sets: [Set; 6] = [
            ("_CLI_BREAKS", is_cli_break, BREAKS),
            ("_CLI_STOPS", is_cli_stop, STOPS),
            ("_CLI_NOT_LEAD", is_cli_not_lead, NOT_LEAD),
            ("_CLI_NAME_CHARS", is_cli_name_char, NAME_CHARS),
            ("_CLI_CLOSERS", is_cli_closer, CLOSERS),
            ("_GLUE_STOPS", is_glue_stop, GLUE_STOPS),
        ];
        for (name, pred, members) in sets {
            for c in probe_chars() {
                assert_eq!(pred(c), members.contains(c), "{name}: {c:?}");
            }
        }
    }

    #[test]
    fn kind_bits_match_oracle() {
        assert_eq!(
            [CLI_MYSQL, CLI_LOGIN, CLI_REDIS, CLI_SQLCMD, CLI_KEYGEN],
            KIND_BITS
        );
    }

    #[test]
    fn command_table_matches_oracle_entry_by_entry() {
        assert_eq!(CLI_COMMANDS.len(), COMMANDS.len());
        for &(name, kind) in COMMANDS {
            assert_eq!(cli_command_kind(&cs(name)), Some(kind), "{name}");
            // a near miss of every key is not a command
            assert_eq!(cli_command_kind(&cs(&name.to_uppercase())), None, "{name}");
            assert_eq!(cli_command_kind(&cs(&format!("{name} "))), None, "{name}");
            assert_eq!(cli_command_kind(&cs(&format!("{name}x"))), None, "{name}");
            assert_eq!(cli_command_kind(&cs(&name[1..])), None, "{name}");
            assert_eq!(cli_two_word_state(&cs(name)), None, "{name}");
        }
        assert_eq!(cli_command_kind(&[]), None);
    }

    #[test]
    fn two_word_table_matches_oracle_entry_by_entry() {
        assert_eq!(CLI_TWO_WORD.len(), TWO_WORD.len());
        for &(name, state) in TWO_WORD {
            assert_eq!(cli_two_word_state(&cs(name)), Some(state), "{name}");
            assert_eq!(
                cli_two_word_state(&cs(&name.to_uppercase())),
                None,
                "{name}"
            );
            assert_eq!(cli_two_word_state(&cs(&format!("{name}-"))), None, "{name}");
            assert_eq!(cli_command_kind(&cs(name)), None, "{name}");
        }
        assert_eq!(cli_two_word_state(&[]), None);
    }

    #[test]
    fn flag_table_matches_oracle() {
        let probes = (0..0x180u32)
            .filter_map(char::from_u32)
            .chain(['\u{1F600}', '\u{212a}']);
        for c in probes {
            let want = FLAG_KINDS
                .iter()
                .find(|&&(k, _)| k == c)
                .map_or(0, |&(_, kind)| kind);
            assert_eq!(cli_flag_kinds(Some(c)), want, "{c:?}");
        }
        assert_eq!(
            cli_flag_kinds(None),
            0,
            "s[at + 1:at + 2] is empty at the end"
        );
    }

    #[test]
    fn command_hints_match_oracle() {
        assert_eq!(CLI_COMMAND_HINTS, HINTS);
        for &(text, want) in HINT_VECTORS {
            assert_eq!(has_cli_command_hint(&cs(text)), want, "{text:?}");
        }
    }

    #[test]
    fn literal_helpers() {
        let s = cs("ab-pass-x");
        assert!(contains_lit(&s, "-pass") && contains_lit(&s, "x") && contains_lit(&s, ""));
        assert!(!contains_lit(&s, "-passx") && !contains_lit(&s, "ab-pass-xy"));
        assert!(!contains_lit(&[], "a") && contains_lit(&[], ""));
        assert!(span_is(&s, (0, 2), "ab") && span_is(&s, (2, 7), "-pass"));
        assert!(!span_is(&s, (0, 2), "abc") && !span_is(&s, (0, 3), "ab"));
        assert!(!span_is(&s, (5, 99), "x") && !span_is(&s, (4, 2), ""));
    }

    /// `(text, the spans `_cli_spans(text, add, _Reader(text))` passes to `add`, in call order)`: the `-u`/`-passin` values first, then
    /// the command flags, so the list is neither sorted nor disjoint.
    const SPAN_VECTORS: &[(&str, &[Span])] = &[
        ("mysql -pSECRET db", &[(8, 14)]),
        ("mysql -p SECRET db", &[]),
        ("mysql -p", &[]),
        ("mysql -u root -p", &[]),
        ("mysql -p'a b' x", &[(8, 13)]),
        ("mysql -p\"a b\" x", &[(8, 13)]),
        ("mysql -p$'a b'", &[(8, 14)]),
        ("mysql -p\\\"a b\\\" x", &[(8, 15)]),
        ("\"mysql -p\" ok", &[]),
        ("\"-p\"", &[]),
        ("mysql (-p)", &[]),
        ("mysql **-p**", &[]),
        ("mysql -p\" x\"", &[(8, 12)]),
        ("mysql -p;x", &[]),
        ("mysql -p'x", &[(8, 10)]),
        ("mysql -p\nmysql -pA", &[(17, 18)]),
        ("/usr/bin/mysql -pS", &[(17, 18)]),
        ("$(mysql -pS)", &[(10, 12)]),
        ("mysql.exe -pS", &[]),
        ("mysqlx -pS", &[]),
        ("echo mysql; ls -pS", &[]),
        ("docker login -p SECRET", &[(16, 22)]),
        ("docker login -u me -p SECRET registry", &[(22, 28)]),
        ("docker  login -pS", &[(16, 17)]),
        ("helm registry login -p S", &[(23, 24)]),
        ("helm login -p S", &[]),
        ("az acr login -p S", &[]),
        ("docker image login -p S", &[]),
        ("docker xxxxxxxxxxxxxxxxxxxxxxxx login -p S", &[]),
        ("docker ```````````````````login -p S", &[(35, 36)]),
        ("docker ``````````````````login` -p S", &[(35, 36)]),
        ("sshpass -p S ssh h", &[(11, 12)]),
        ("sshpass -p -x", &[]),
        ("sshpass -p\nx", &[]),
        ("sshpass -pS -a x", &[(10, 11)]),
        ("mongosh -p S", &[(11, 12)]),
        ("redis-cli -a S", &[(13, 14)]),
        ("redis-cli -aS", &[(12, 13)]),
        ("sqlcmd -P S", &[(10, 11)]),
        ("bcp -PS", &[(6, 7)]),
        ("ssh-keygen -N S", &[(14, 15)]),
        ("ssh-keygen -N '' -f x", &[(14, 16)]),
        ("redis-cli -a $(sshpass -p x)", &[(13, 22), (26, 28)]),
        ("redis-cli -a -a S", &[(16, 17)]),
        ("redis-cli -a x ; redis-cli -a y", &[(13, 14), (30, 31)]),
        ("redis-cli -a x && sshpass -pS", &[(13, 14), (28, 29)]),
        ("curl -u user:pass url", &[(8, 17)]),
        ("curl --user user:pass url", &[(12, 21)]),
        ("curl --proxy-user a:b", &[(18, 21)]),
        ("curl -u=user:pass", &[(8, 17)]),
        ("curl --user=a:b", &[(12, 15)]),
        ("curl -u user:", &[]),
        ("curl -u :pass", &[]),
        ("curl -u user", &[]),
        ("curl -u me:'a b' x", &[(8, 16)]),
        ("curl -u me:\"a b\" x", &[(8, 16)]),
        ("curl -u 'me:a b' x", &[(8, 16)]),
        ("curl -u \"me\" x", &[]),
        ("curl -u me:\\\"a b\\\" x", &[(8, 18)]),
        ("docker run -u 1000:1000 img", &[]),
        ("chown -u 1234567890:1234567890 x", &[]),
        ("docker run -u 12345678901:1 img", &[(14, 27)]),
        ("docker run -u 1:12345678901 img", &[(14, 27)]),
        ("curl -u 1000:abc", &[(8, 16)]),
        ("curl -u u:p -u v:q", &[(8, 11), (15, 18)]),
        ("x-u a:b", &[]),
        ("a -u a:b;c", &[(5, 8)]),
        ("-u a:b", &[(3, 6)]),
        ("-u a:b:c d", &[(3, 8)]),
        ("--userx a:b", &[]),
        ("curl --user a:'b", &[(12, 16)]),
        ("curl -u\ta:b", &[(8, 11)]),
        ("curl -u  a:b", &[(9, 12)]),
        ("openssl -passin pass:SECRET", &[(21, 27)]),
        ("openssl -passin \"pass:SECRET\"", &[(22, 28)]),
        ("openssl -passin pass:\"a b\"", &[(21, 26)]),
        ("openssl -passout pass:S x", &[(22, 23)]),
        ("openssl -pass pass:S", &[(19, 20)]),
        ("openssl -passin 'pass:'", &[]),
        ("openssl -passin pass:", &[]),
        ("openssl -passin file:p", &[]),
        ("openssl -passin pass:\"\"", &[(21, 23)]),
        ("openssl -passin \\\"pass:x y\\\" z", &[(23, 26)]),
        ("mysql -pS -u a:b", &[(13, 16), (8, 9)]),
        ("curl -u a:b; mysql -pS", &[(8, 11), (21, 22)]),
        ("docker ````````````````````login -p S", &[]),
        ("docker login -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -x -p S", &[(106, 107)]),
        ("mysql aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -pS", &[(49, 50)]),
        ("/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/mysql -pS", &[(40, 41)]),
        ("-u=-u=-u=a:b", &[(3, 12)]),
        ("curl -u 'a:b", &[(8, 12)]),
        ("curl -u 'a b'", &[]),
        ("sshpass -p \"x y\"z w", &[(11, 17)]),
        ("redis-cli -a 'x\ny'", &[(13, 18)]),
        ("mysql -p'a\nmysql -pB", &[(8, 10), (19, 20)]),
    ];

    #[test]
    fn cli_spans_match_oracle() {
        for &(text, want) in SPAN_VECTORS {
            let chars = cs(text);
            let rd = Reader::new(&chars).expect("short text");
            let mut got = Vec::new();
            cli_spans(&rd, &mut got);
            assert_eq!(got, want, "{text:?}");
        }
    }

    /// One `cli_spans` pass over `chars`, reader construction included.
    fn scan_once(chars: &[char]) {
        let rd = Reader::new(chars).expect("text within the lexer limit");
        let mut out = Vec::new();
        cli_spans(&rd, &mut out);
        std::hint::black_box(&out);
    }

    /// The Python module keeps both detectors linear (`run`, `colon` and `pushed_end` memos; the flag is checked in every later word
    /// of a segment without rescanning): a text of 16000 repeats must cost about what 16 texts of 1000 repeats cost (linear: 1x,
    /// quadratic: 16x; the limit is 4x). The timing is the shared CPU-time one of `crate::testing`.
    #[test]
    fn cli_spans_scale_linearly() {
        let shapes: [fn(usize) -> String; 4] = [
            |n| "x=-u=".repeat(n),
            |n| format!("mysql {}-p x", "a ".repeat(n)),
            |n| "-u a ".repeat(n) + "b:c",
            |n| "-u ".repeat(n) + &"'".repeat(n),
        ];
        for (i, shape) in shapes.into_iter().enumerate() {
            let whole = cs(&shape(16000));
            let pieces = vec![cs(&shape(1000)); 16];
            assert_scales(
                &format!("cli shape {i}"),
                4,
                || pieces.iter().for_each(|piece| scan_once(piece)),
                || scan_once(&whole),
            );
        }
    }
}
