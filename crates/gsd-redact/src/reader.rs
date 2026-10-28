//! The quote-aware value reader (level 4 of the port): where the value of a secret-taking name or option ends.
//!
//! A port of `swarm/redact.py` `class _Reader`. One [`Reader`] reads one string. It keeps no state that depends on the order of the
//! questions: the [`Lexer`] is immutable once built (lazily, on the first question that needs it) and the end of a line is a pure
//! memo, so every method takes `&self`.
//!
//! Python's `-1` results become `Option` (`enclosing_end`, [`Lexer::index`]); its `(str, int, int)` opener becomes an [`Opener`]
//! tuple, which is exactly what [`Reader::quoted`] takes as its three trailing arguments. Python's `bare_re` argument of
//! [`Reader::read`] is a function `(text, pos) -> Option<end>` with the semantics of `pattern.match(text, pos)`:
//! [`name_bare`](crate::patterns::name_bare), [`flag_bare`](crate::patterns::flag_bare) or a caller's own scanner.

use std::cell::{Cell, OnceCell};

use crate::lexer::{us, Lexer, TextTooLong, CLOSE, CLOSED, CONTENT, MAX_LEN, OPEN, UNCLOSED};
use crate::patterns::{eol_positions, eol_search};

/// A quote opener at some offset: `(quote, length of the opener, backslashes before the quote)`, as `_Reader.opener` returns it.
pub type Opener = (char, usize, usize);

/// A bare-word scanner: the end of the word that starts at the given offset, `None` when it is empty (`pattern.match(s, pos)`).
pub type Bare = fn(&[char], usize) -> Option<usize>;

/// `_GLUE_STOPS` (redact.py:779): characters that stop a bare continuation after a quoted segment.
fn is_glue_stop(c: char) -> bool {
    matches!(
        c,
        '`' | '"' | '\'' | ')' | ']' | '}' | '>' | '*' | ',' | ';' | '/' | '?' | '&' | '|'
    )
}

/// The closing-quote length of a quote with `run` backslashes before it: `run + 1` for an escaped quote, 1 for a plain one. Python
/// spells it `run + 1 if run else 1` inside a tuple (`return ..., run + 1 if run else 1`), which parses as the conditional on its own.
fn close_len(run: usize) -> usize {
    if run > 0 {
        run + 1
    } else {
        1
    }
}

/// The reader of one string (see the module documentation).
#[derive(Debug)]
pub struct Reader<'a> {
    /// The text.
    pub s: &'a [char],
    lex: OnceCell<Lexer>,
    /// The single-entry memo of [`Reader::line_end`]: every offset in `from..=to` has the line end `to`. Python starts with the
    /// empty range `(1, 0)`.
    eol_memo: Cell<(usize, usize)>,
    eols: OnceCell<Vec<usize>>,
}

impl<'a> Reader<'a> {
    /// `_Reader.__init__` (redact.py:498): a reader of `s`. The lexer is built on the first question that needs it.
    ///
    /// # Errors
    ///
    /// [`TextTooLong`] when `s` has more than [`MAX_LEN`] code points: the lexer's 32-bit arrays could not hold its positions. The
    /// check is made here, so the lazy lexer build cannot fail.
    pub fn new(s: &'a [char]) -> Result<Self, TextTooLong> {
        if s.len() > MAX_LEN {
            return Err(TextTooLong { len: s.len() });
        }
        Ok(Reader {
            s,
            lex: OnceCell::new(),
            eol_memo: Cell::new((1, 0)),
            eols: OnceCell::new(),
        })
    }

    /// The lexer of the text (`_Reader.lex`, redact.py:504), built on first use.
    pub fn lex(&self) -> &Lexer {
        self.lex.get_or_init(|| {
            // `Reader::new` checked the length, the only thing `Lexer::new` checks.
            Lexer::build(self.s)
        })
    }

    fn at(&self, i: usize) -> Option<char> {
        self.s.get(i).copied()
    }

    /// `opener` (redact.py:511): `(quote, length of the opener, backslashes before the quote)` when a quote opens at `s[i]` (`"`,
    /// `'`, `$'`, `$"`, `\"`, `$\"` ...), else `None`.
    pub fn opener(&self, i: usize) -> Option<Opener> {
        let mut c = self.at(i);
        let mut j = i;
        if c == Some('$') {
            j += 1;
            c = self.at(j);
        }
        if let Some(q @ ('"' | '\'')) = c {
            return Some((q, j + 1 - i, 0));
        }
        if c != Some('\\') {
            return None;
        }
        let mut r = j;
        while self.at(r) == Some('\\') {
            r += 1;
        }
        let run = r - j;
        match self.at(r) {
            Some(q @ ('"' | '\'')) if run % 2 == 1 => Some((q, r + 1 - i, run)),
            _ => None,
        }
    }

    /// `line_end` (redact.py:532): the offset of the first CR or LF at or after `i`, or the length of the text. Memoised for the
    /// last line asked about, which keeps a flood of unclosed quotes on one long line linear.
    pub fn line_end(&self, i: usize) -> usize {
        let (from, to) = self.eol_memo.get();
        if from <= i && i <= to {
            return to;
        }
        let end = eol_search(self.s, i).unwrap_or(self.s.len());
        self.eol_memo.set((i, end));
        end
    }

    /// `quoted` (redact.py:540): `(end, length of the closing quote)` of the quoted value that opens at `s[i]` with the opener
    /// `(q, oplen, run)`; the closing length is 0 for a value that ends at the end of its line or before the quote that closes an
    /// enclosing one.
    pub fn quoted(&self, i: usize, q: char, oplen: usize, run: usize) -> (usize, usize) {
        let lex = self.lex();
        // `i + oplen - 1`; Python's `index(-1)` finds nothing, which `checked_sub` reproduces for `i == 0 && oplen == 0`.
        let Some(m) = i
            .saturating_add(oplen)
            .checked_sub(1)
            .and_then(|p| lex.index(p))
        else {
            return (self.line_end(i), 0);
        };
        if lex.role_at(m) == OPEN && lex.lvl_at(m) == run {
            let how = lex.how_at(m);
            if how == UNCLOSED {
                return (self.line_end(i), 0);
            }
            // `how` is closed or cut, so `link` is a mark (the one that closed or cut it).
            let t = lex.link_at(m).unwrap_or(m);
            if how == CLOSED {
                return (lex.pos_at(t) + 1, close_len(run));
            }
            let end = lex.start_at(t);
            if run > 0 {
                // An escaped quote ends before the plain quote that closes the JSON string holding it (`"mysql -p\\"x"`); any
                // other escaped quote that was cut by an enclosing one is ambiguous (`OPTS='sshpass -p "x'y' z'`): its value
                // runs at least to the end of its line.
                let end = if self.at(lex.pos_at(t)) == Some(q) {
                    end
                } else {
                    end.max(self.line_end(i))
                };
                return (end, 0);
            }
            // A plain quote cut by an enclosing close (`echo a 'x` ... `P="x 'y` ... `s3cr3t"`: the stray `'` closes the earlier
            // one) is a value that runs to its matching quote, the next plain quote of its kind, which the fallback below finds.
        }
        // The lexer takes this quote for a closer or for prose: read it as an opener of its own kind (the next quote that ends it).
        let (nx0, nxl) = lex.next_marks(self.s);
        let t0 = nx0.get(m).copied().and_then(|v| usize::try_from(v).ok());
        let t1 = if run > 0 && lex.lvl_at(m) == run {
            nxl.get(m).copied().and_then(|v| usize::try_from(v).ok())
        } else {
            None
        };
        if let Some(t1) = t1 {
            if t0.is_none_or(|t0| t1 < t0) {
                return (lex.pos_at(t1) + 1, run + 1);
            }
        }
        if let Some(t0) = t0 {
            return if run == 0 {
                (lex.pos_at(t0) + 1, 1)
            } else {
                (lex.pos_at(t0), 0)
            };
        }
        (self.line_end(i), 0)
    }

    /// `read` (redact.py:571): `(end, closing quote length)` of the value at `s[i]`; `end == i` when there is none (no bare
    /// character, or a quote with nothing after it).
    ///
    /// The value is a shell WORD: segments that touch the one just read belong to it (`-p'a b'c`, `-p"x"'y'`, `x=ab'c'`): a quote the
    /// lexer opens (or one it takes for prose) continues it, a quote it closes ends it, and after a quoted segment a bare character
    /// continues it unless it is a closer of prose or Markdown or a joiner (`/`, `?`, `&`, `|`).
    pub fn read(&self, i: usize, bare: Bare) -> (usize, usize) {
        let s = self.s;
        let (mut end, mut close);
        if let Some((q, oplen, run)) = self.opener(i) {
            (end, close) = self.quoted(i, q, oplen, run);
            if !(close > 0 || end > i + oplen) {
                return (i, 0);
            }
        } else {
            match bare(s, i) {
                Some(e) => (end, close) = (e, 0),
                None => return (i, 0),
            }
        }
        let mut quoted = close > 0;
        let n = s.len();
        // The number of the mark that follows the last quoted segment: where the next quote is looked for first (Python keeps it only
        // after a closed quote and resets it after a bare word; here a stale value costs nothing, because every use checks the mark's
        // position, and a hit saves the binary search of `Lexer::index`, which a long run of `a'a'a'...` would pay once per quote).
        let mut nxt: Option<usize> = None;
        while end < n {
            if let Some(nx) = nxt {
                // The next quote starts exactly here and the lexer closes it: one step, no re-reading (`\\"a\\"\\"b\\"...`).
                let lex = self.lex();
                if nx < lex.len()
                    && lex.start_at(nx) == end
                    && lex.role_at(nx) == OPEN
                    && lex.how_at(nx) == CLOSED
                {
                    let t = lex.link_at(nx).unwrap_or(nx);
                    (end, close, nxt) = (lex.pos_at(t) + 1, close_len(lex.lvl_at(nx)), Some(t + 1));
                    // Python gets here only right after a quoted segment; a stale `nxt` also arrives after a bare word.
                    quoted = true;
                    continue;
                }
            }
            let Some(c) = self.at(end) else { break };
            if matches!(c, '"' | '\'' | '\\' | '$') {
                let Some((q, oplen, run)) = self.opener(end) else {
                    match bare(s, end) {
                        Some(e) => {
                            (end, quoted) = (e, false);
                            continue;
                        }
                        None => break,
                    }
                };
                let lex = self.lex();
                let qpos = end + oplen - 1;
                let k = match nxt {
                    Some(nx) if nx < lex.len() && lex.pos_at(nx) == qpos => Some(nx),
                    _ => lex.index(qpos),
                };
                let role = k.map_or(CONTENT, |k| lex.role_at(k));
                if role == CLOSE {
                    break;
                }
                match k {
                    Some(k) if role == OPEN && lex.lvl_at(k) == run => {
                        if lex.how_at(k) == CLOSED {
                            let t = lex.link_at(k).unwrap_or(k);
                            (end, close, nxt) = (lex.pos_at(t) + 1, close_len(run), Some(t + 1));
                        } else {
                            (end, close) = self.quoted(end, q, oplen, run);
                            nxt = None;
                        }
                    }
                    _ => {
                        // A quote that is prose (`it's`): part of the word, unless its mate on the line closes a quoted segment
                        // (`x'a b'`). The mark that follows is where the next quote is looked for first (a shortcut: the answer
                        // is the same whichever mark is tried first).
                        if run == 0 {
                            ((end, close), nxt) = self.prose_from(k, qpos);
                        } else {
                            (end, close, nxt) = (qpos + 1, 0, k.map(|k| k + 1));
                        }
                    }
                }
                quoted = true;
            } else if quoted && !is_glue_stop(c) {
                match bare(s, end) {
                    Some(e) => (end, quoted) = (e, false),
                    None => break,
                }
            } else {
                break;
            }
        }
        (end, close)
    }

    /// `prose_end` (redact.py:631): `(end, closing quote length)` of a PLAIN quote the lexer takes for prose (`x'a b'`,
    /// `ab'c d'ef`): it opens a quoted segment when its mate, the next plain quote of its kind, lies on the same line, and the
    /// blanks between them are inside the quotes; else it is part of the word, `(qpos + 1, 0)`.
    pub fn prose_end(&self, qpos: usize) -> (usize, usize) {
        self.prose_from(self.lex().index(qpos), qpos).0
    }

    /// [`Reader::prose_end`] for the quote at `qpos` whose mark number `m` the caller already has, and the number of the mark that
    /// follows the word read (`None` when `m` is not a mark).
    fn prose_from(&self, m: Option<usize>, qpos: usize) -> ((usize, usize), Option<usize>) {
        let lex = self.lex();
        let mate = m.and_then(|m| {
            lex.next_marks(self.s)
                .0
                .get(m)
                .and_then(|&v| usize::try_from(v).ok())
        });
        if let Some(t) = mate {
            if lex.pos_at(t) < self.line_end(qpos) {
                return ((lex.pos_at(t) + 1, 1), Some(t + 1));
            }
        }
        ((qpos + 1, 0), m.map(|m| m + 1))
    }

    /// `closes` (redact.py:642): whether the quote of the opener at `s[i]` CLOSES a quote that opened earlier on the same line
    /// (`"mysql -p" ok`: a prose quote, not the opener of a password).
    ///
    /// Two things make that reading unsafe, and the quote is then taken for an opener (over-redacting): the lexer pairs it with a
    /// quote on an earlier line, or a quote that nothing closes follows it on its line.
    pub fn closes(&self, i: usize, oplen: usize, run: usize) -> bool {
        let lex = self.lex();
        let Some(m) = i
            .saturating_add(oplen)
            .checked_sub(1)
            .and_then(|p| lex.index(p))
        else {
            return false;
        };
        if lex.role_at(m) != CLOSE || lex.lvl_at(m) != run {
            return false;
        }
        let eols = self.eols.get_or_init(|| eol_positions(self.s));
        let at = lex.pos_at(m);
        // `link` of a CLOSE mark is the mark it closes.
        let opened = lex.link_at(m).map_or(0, |o| lex.pos_at(o));
        // bisect_left: the number of line ends strictly before the offset.
        if eols.partition_point(|&e| e < opened) != eols.partition_point(|&e| e < at) {
            return false;
        }
        let lost = lex.lost();
        // bisect_right: the first lost quote after mark `m`.
        let j = lost.partition_point(|&x| usize::try_from(x).is_ok_and(|x| x <= m));
        !lost
            .get(j)
            .is_some_and(|&l| lex.pos_at(us(l)) < self.line_end(at))
    }

    /// `enclosing_end` (redact.py:660): where the quote that encloses `s[p]` ends (before its closing quote), or `None` (Python
    /// `-1`) when none does or it never closes.
    ///
    /// A closing quote right after `=` is not taken for the close of the enclosing quote (`Cookie: a=1; session="x"`: it opens a
    /// cookie value): the next enclosing quote out is tried (at most 4 levels, then `None`: the caller reads to the end of the line).
    pub fn enclosing_end(&self, p: usize) -> Option<usize> {
        let lex = self.lex();
        // `bisect_left(pos, p) - 1`, which is -1 when no mark lies before `p`.
        let m = lex
            .pos
            .partition_point(|&x| usize::try_from(x).map_or(true, |x| x < p))
            .checked_sub(1);
        let mut o = m.and_then(|m| lex.top().get(m).copied());
        for _ in 0..4 {
            let cur = usize::try_from(o?).ok()?;
            if lex.how_at(cur) == UNCLOSED {
                return None;
            }
            let t = lex.link_at(cur)?;
            let end = lex.start_at(t);
            if end == 0 || self.at(end - 1) != Some('=') {
                return Some(end);
            }
            o = lex.par().get(cur).copied();
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::cli_unquoted_match;
    use crate::patterns::{flag_bare, name_bare};
    use crate::testing::assert_scales;
    use std::hint::black_box;

    /// `(text, i, quote, opener length, run, (end, closing length))`.
    type QuotedCase = (&'static str, usize, char, usize, usize, (usize, usize));
    /// `(text, i, bare scanner, (end, closing length))`.
    type ReadCase = (&'static str, usize, Bare, (usize, usize));

    fn chars(s: &str) -> Vec<char> {
        s.chars().collect()
    }

    // Every expected value below was produced by running the real `swarm.redact._Reader` on the same text and arguments.

    #[rustfmt::skip]
    const OPENERS: &[(&str, usize, Option<Opener>)] = &[
    ("", 0, None),
    ("\"x\"", 0, Some(('"', 1, 0))),
    ("'x'", 0, Some(('\'', 1, 0))),
    ("$'x'", 0, Some(('\'', 2, 0))),
    ("$\"x\"", 0, Some(('"', 2, 0))),
    ("$$'x'", 0, None),
    ("$", 0, None),
    ("x'y'", 0, None),
    ("x'y'", 1, Some(('\'', 1, 0))),
    ("\\\"x\\\"", 0, Some(('"', 2, 1))),
    ("\\\\\"x", 0, None),
    ("\\\\\\'x'", 0, Some(('\'', 4, 3))),
    ("$\\\"x", 0, Some(('"', 3, 1))),
    ("\\x", 0, None),
    ("\\", 0, None),
    ("ab", 5, None),
    ("\u{1f600}'x'", 1, Some(('\'', 1, 0))),
    ("\\\\\\\\\\\\\\\"x", 0, Some(('"', 8, 7))),
    ];

    #[test]
    fn opener_matches_the_oracle() {
        for &(text, i, want) in OPENERS {
            let s = chars(text);
            assert_eq!(Reader::new(&s).unwrap().opener(i), want, "{text:?} at {i}");
        }
    }

    #[rustfmt::skip]
    const LINE_ENDS: &[(&str, usize, usize)] = &[
    ("", 0, 0),
    ("abc", 0, 3),
    ("abc", 3, 3),
    ("abc", 9, 3),
    ("ab\ncd", 0, 2),
    ("ab\ncd", 2, 2),
    ("ab\ncd", 3, 5),
    ("ab\r\ncd", 0, 2),
    ("ab\r\ncd", 3, 3),
    ("ab\rcd\nef", 3, 5),
    ("\n\n", 1, 1),
    ("\u{1f600}\n\u{1f600}", 0, 1),
    ];

    #[test]
    fn line_end_matches_the_oracle() {
        for &(text, i, want) in LINE_ENDS {
            let s = chars(text);
            assert_eq!(
                Reader::new(&s).unwrap().line_end(i),
                want,
                "{text:?} at {i}"
            );
        }
    }

    /// The memo answers an offset inside the last asked line range with that range's end: whatever was asked before, the answers are the
    /// same (an offset past the text asks for the text's end, and its memo range never covers an earlier offset).
    #[test]
    fn line_end_memo_never_changes_an_answer() {
        let s = chars("ab\ncd\nef\r\ngh");
        let r = Reader::new(&s).unwrap();
        for (i, want) in [
            (3, 5),
            (4, 5),
            (1, 2),
            (0, 2),
            (5, 5),
            (6, 8),
            (9, 9),
            (12, 12),
            (40, 12),
            (7, 8),
            (12, 12),
            (13, 12),
        ] {
            assert_eq!(r.line_end(i), want, "at {i}");
        }
    }

    #[rustfmt::skip]
    const QUOTED: &[QuotedCase] = &[
    ("'x'", 0, '\'', 1, 0, (3, 1)),
    ("\"a b\"", 0, '"', 1, 0, (5, 1)),
    ("mysql -p'x y' db", 8, '\'', 1, 0, (13, 1)),
    ("'abc", 0, '\'', 1, 0, (4, 0)),
    ("'abc\ndef", 0, '\'', 1, 0, (4, 0)),
    ("'a\nb' c", 0, '\'', 1, 0, (5, 1)),
    ("echo 'x \"y\" z'", 5, '\'', 1, 0, (14, 1)),
    ("echo \"it's ready\" ok", 5, '"', 1, 0, (17, 1)),
    ("bash -c \"mysql -p'x' db\"", 17, '\'', 1, 0, (20, 1)),
    ("'password': 'alpha\n  beta'\nmysql -p'x y' db", 13, '\'', 1, 0, (18, 0)),
    ("'password': 'alpha\n  beta'\nmysql -p'x y' db", 36, '\'', 1, 0, (43, 0)),
    ("'password': 'alpha\n  beta'\nmysql -p'x y' db\n# sshpass -p'", 36, '\'', 1, 0, (43, 0)),
    ("'password': 'alpha\n  beta'\nmysql -p'x y' db\n# sshpass -p'", 13, '\'', 1, 0, (18, 0)),
    ("{\\\"k\\\": \\\"v\\\"}", 1, '"', 2, 1, (6, 2)),
    ("{\\\"k\\\": \\\"v\\\"}", 8, '"', 2, 1, (13, 2)),
    ("\\\\\\\"x\\\\\\\" y", 0, '"', 4, 3, (9, 4)),
    ("mysql -p\\\"x\" y", 8, '"', 2, 1, (14, 0)),
    ("OPTS='sshpass -p \"x'y' z'", 5, '\'', 1, 0, (20, 1)),
    ("OPTS='sshpass -p \"x'y' z'", 17, '"', 1, 0, (25, 0)),
    ("OPTS='sshpass -p \"x'y' z'", 19, '\'', 1, 0, (22, 1)),
    ("echo a 'x\nP=\"x 'y\ns3cr3t\"", 7, '\'', 1, 0, (16, 1)),
    ("echo a 'x\nP=\"x 'y\ns3cr3t\"", 15, '\'', 1, 0, (17, 0)),
    ("a'b c'd", 1, '\'', 1, 0, (6, 1)),
    ("it's x 'y'", 2, '\'', 1, 0, (8, 1)),
    ("'a' 'b'", 4, '\'', 1, 0, (7, 1)),
    ("x", 0, '\'', 1, 0, (1, 0)),
    ("x'", 1, '\'', 0, 0, (2, 0)),
    ("\u{1f600} '\u{e9} \u{fc}'", 2, '\'', 1, 0, (7, 1)),
    ("'a\\'b' c", 0, '\'', 1, 0, (6, 1)),
    ("'a\\\\'b' c", 0, '\'', 1, 0, (5, 1)),
    ];

    #[test]
    fn quoted_matches_the_oracle() {
        for &(text, i, q, oplen, run, want) in QUOTED {
            let s = chars(text);
            assert_eq!(
                Reader::new(&s).unwrap().quoted(i, q, oplen, run),
                want,
                "{text:?} at {i} ({q:?}, {oplen}, {run})"
            );
        }
    }

    /// `test_the_quote_lexer_is_a_pure_function_of_the_text`: the same text answers the same whatever was asked before, and a restarting line
    /// appended after it never changes how an EARLIER value is read (the expected pairs are the ones the Python test compares).
    #[test]
    fn quoted_is_a_pure_function_of_the_text() {
        let text = chars("'password': 'alpha\n  beta'\nmysql -p'x y' db");
        let longer = chars("'password': 'alpha\n  beta'\nmysql -p'x y' db\n# sshpass -p'");
        let first = Reader::new(&text).unwrap();
        let a = [first.quoted(13, '\'', 1, 0), first.quoted(36, '\'', 1, 0)];
        let second = Reader::new(&longer).unwrap();
        let b = [second.quoted(36, '\'', 1, 0), second.quoted(13, '\'', 1, 0)];
        assert_eq!(a, [b[1], b[0]]);
        assert_eq!(a, [(18, 0), (43, 0)]);
    }

    #[rustfmt::skip]
    const READS: &[ReadCase] = &[
    ("password=hunter2 x", 9, name_bare, (16, 0)),
    ("password=hunter2;x", 9, name_bare, (16, 0)),
    ("password=hunter2;x", 9, flag_bare, (18, 0)),
    ("--password hunter2 x", 11, flag_bare, (18, 0)),
    ("--password hunter2 x", 11, cli_unquoted_match, (18, 0)),
    ("x=", 2, name_bare, (2, 0)),
    ("x= y", 2, name_bare, (2, 0)),
    ("x=", 5, name_bare, (5, 0)),
    ("-p'a b'c d", 2, flag_bare, (8, 1)),
    ("-p\"x\"'y' z", 2, flag_bare, (8, 1)),
    ("x=ab'c' d", 2, name_bare, (7, 1)),
    ("x=ab'c d' e", 2, name_bare, (9, 1)),
    ("x='a b'/rest", 2, name_bare, (7, 1)),
    ("x='a b'abc", 2, name_bare, (10, 1)),
    ("x='a b'abc", 2, cli_unquoted_match, (10, 1)),
    ("x=\"a\"\"b\"\"c\" d", 2, name_bare, (11, 1)),
    ("x=\\\"a\\\"\\\"b\\\"\\\"c\\\" d", 2, name_bare, (17, 2)),
    ("pa\\ ss word", 0, name_bare, (6, 0)),
    ("x='unclosed value\nnext", 2, name_bare, (17, 0)),
    ("x='a\nb' c", 2, name_bare, (7, 1)),
    ("x=it's here", 2, name_bare, (6, 0)),
    ("x=a'b c'd e", 2, name_bare, (9, 1)),
    ("echo \"it's\"; x='y'", 6, name_bare, (17, 1)),
    ("x=$'a b'c d", 2, name_bare, (9, 1)),
    ("x=$\"a b\" c", 2, name_bare, (8, 1)),
    ("x=\"", 2, name_bare, (2, 0)),
    ("x=''", 2, name_bare, (4, 1)),
    ("x='' y", 2, name_bare, (4, 1)),
    ("# PASSWORD=ab'c d' x'", 12, name_bare, (18, 1)),
    ("x='a'\"b\"'c'd", 2, name_bare, (12, 1)),
    ("x=a\"b", 2, name_bare, (5, 0)),
    ("x=a'b", 2, cli_unquoted_match, (5, 0)),
    ("x=\\\"a b\\\" c", 2, name_bare, (9, 2)),
    ("x=\u{e9}'a'\u{1f600}", 2, name_bare, (7, 1)),
    ("'# PASSWORD=\u{1f600};it's\\\\\\\"\\\\''x'x--token=$x=\"curl -u me:users' ]", 0, flag_bare, (58, 1)),
    ("'# PASSWORD=\u{1f600};it's\\\\\\\"\\\\''x'x--token=$x=\"curl -u me:users' ]", 17, name_bare, (58, 1)),
    ("'# PASSWORD=\u{1f600};it's\\\\\\\"\\\\''x'x--token=$x=\"curl -u me:users' ]", 17, cli_unquoted_match, (58, 1)),
    ];

    #[test]
    fn read_matches_the_oracle() {
        for &(text, i, bare, want) in READS {
            let s = chars(text);
            assert_eq!(
                Reader::new(&s).unwrap().read(i, bare),
                want,
                "{text:?} at {i}"
            );
        }
    }

    #[rustfmt::skip]
    const PROSE_ENDS: &[(&str, usize, (usize, usize))] = &[
    ("x'a b'", 1, (6, 1)),
    ("ab'c d'ef", 2, (7, 1)),
    ("it's here", 2, (3, 0)),
    ("it's\nx'", 2, (3, 0)),
    ("it's x it's", 2, (10, 1)),
    ("it's 'q'", 2, (6, 1)),
    ("don't", 3, (4, 0)),
    ("a'", 1, (2, 0)),
    ("x", 0, (1, 0)),
    ("'a'", 0, (3, 1)),
    ("\u{e9}'a'", 1, (4, 1)),
    ("x'a b' y'", 1, (6, 1)),
    ];

    #[test]
    fn prose_end_matches_the_oracle() {
        for &(text, qpos, want) in PROSE_ENDS {
            let s = chars(text);
            assert_eq!(
                Reader::new(&s).unwrap().prose_end(qpos),
                want,
                "{text:?} at {qpos}"
            );
        }
    }

    #[rustfmt::skip]
    const CLOSES: &[(&str, usize, usize, usize, bool)] = &[
    ("\"mysql -p\" ok", 9, 1, 0, true),
    ("'x && mysql -p'***' db", 17, 1, 0, false),
    ("'unclosed && mysql -p'***' db", 23, 1, 0, false),
    ("'a\nb' -p'c' d", 4, 1, 0, false),
    ("a 'b' -p'c' d", 6, 1, 0, false),
    ("\"quoted ; mariadb -p\")]}\" db", 19, 1, 0, false),
    ("# note: ' ;docker login -p'***' --x db", 26, 1, 0, false),
    ("the users' list; sshpass -p'&;|' ssh h", 28, 1, 0, false),
    ("'x'", 2, 1, 0, true),
    ("'x'", 0, 1, 0, false),
    ("x", 0, 1, 0, false),
    ("\"a\" \"b\"", 4, 1, 0, false),
    ("\"a\" \"b\"", 6, 1, 0, true),
    ("'a\\'b'", 4, 1, 0, false),
    ("\\\"a\\\" x", 3, 2, 1, true),
    ];

    #[test]
    fn closes_matches_the_oracle() {
        for &(text, i, oplen, run, want) in CLOSES {
            let s = chars(text);
            assert_eq!(
                Reader::new(&s).unwrap().closes(i, oplen, run),
                want,
                "{text:?} at {i}"
            );
        }
    }

    #[rustfmt::skip]
    const ENCLOSING: &[(&str, usize, Option<usize>)] = &[
    ("", 0, None),
    ("abc", 1, None),
    ("'abc'", 2, Some(4)),
    ("'abc'", 0, None),
    ("'abc'", 5, None),
    ("'abc", 2, None),
    ("curl -H \"Cookie: a=1; s=\"alpha beta\"; x=2\" u", 30, None),
    ("curl -H 'Cookie: sid=alpha; theme=dark' https://h", 24, Some(38)),
    ("a=\"b 'c' d\" e", 7, Some(7)),
    ("x=\"a='b' c\"", 8, Some(10)),
    ("\"'\"'\"x\"'\"'\"", 5, Some(6)),
    ("'a' 'b'", 4, None),
    ("\"x=\"y\"=\"z\"", 6, None),
    ("\u{e9}'\u{1f600} x'", 4, Some(5)),
    ];

    #[test]
    fn enclosing_end_matches_the_oracle() {
        for &(text, p, want) in ENCLOSING {
            let s = chars(text);
            assert_eq!(
                Reader::new(&s).unwrap().enclosing_end(p),
                want,
                "{text:?} at {p}"
            );
        }
    }

    /// The quote-noise units of `tests/test_redact.py::test_quote_noise_floods_are_scanned_in_linear_time` (inventory section 6).
    const UNITS: [&str; 14] = [
        "'",
        "\"",
        "\\'",
        "\\\"",
        "a'",
        "-p'",
        "-p\"",
        "it's ",
        "x' ",
        "$'",
        "'\\'",
        "\"'",
        "mysql -p'a' ",
        "echo \"it's\"; ",
    ];
    /// The pieces one flood is compared against.
    const PIECES: usize = 16;
    const SMALL: usize = 64 * 1024;
    const LARGE: usize = 1 << 20;

    /// `lead` + the unit repeated up to `size` code points + a trailing line, as the Python test builds it.
    fn flood(lead: &str, unit: &str, size: usize) -> Vec<char> {
        let mut text = String::from(lead);
        text.push_str(&unit.repeat(size / unit.chars().count()));
        text.push_str("\npassword=hunter2");
        chars(&text)
    }

    /// One pass of lexer construction plus every read path over `s`: building the lexer; whole-text reads from the value start `start`
    /// (a read walks its word to the end, so a number of reads that grows with the text would be quadratic by design); a dense sweep of
    /// the line-end, enclosing-quote and prose queries; a dense sweep of the opener, quoted and closes queries.
    fn flood_pass(s: &[char], start: usize) {
        let r = Reader::new(s).unwrap();
        black_box(r.lex().len());
        let mut acc = 0;
        for bare in [name_bare as Bare, flag_bare, cli_unquoted_match] {
            let (end, close) = r.read(start, bare);
            acc += end + close;
        }
        for p in (start..s.len()).step_by(61) {
            acc += r.line_end(p) + r.enclosing_end(p).unwrap_or(0) + r.prose_end(p).0;
        }
        for p in (start..s.len()).step_by(61) {
            if let Some((q, oplen, run)) = r.opener(p) {
                acc += r.quoted(p, q, oplen, run).0 + usize::from(r.closes(p, oplen, run));
            }
        }
        black_box(acc);
    }

    /// One flood of `size` code points against `PIECES` floods of a sixteenth of that size (the same `lead` and unit): linear work
    /// takes about as long either way (1x), work quadratic in the text takes 16x as long on the whole; the limit is 4x. The timing is
    /// the shared CPU-time one of `crate::testing`, which repeats a pass until a timing spans 10 ms and keeps the least disturbed of
    /// several runs.
    fn assert_flood_scales(lead: &str, unit: &str, size: usize) {
        let whole = flood(lead, unit, size);
        let pieces: Vec<Vec<char>> = (0..PIECES)
            .map(|_| flood(lead, unit, size / PIECES))
            .collect();
        assert_scales(
            &format!("unit {unit:?}, {size} code points"),
            4,
            || {
                pieces
                    .iter()
                    .for_each(|piece| flood_pass(black_box(piece), lead.len()))
            },
            || flood_pass(black_box(&whole), lead.len()),
        );
    }

    /// Linear time, for every unit of the quote-noise family: one text of 64K code points takes about as long as 16 of 4K (linear 1x,
    /// quadratic 16x; checked first, so a quadratic path fails after seconds rather than hanging on the large text), and one of 1M as
    /// long as 16 of 64K.
    #[test]
    fn quote_noise_floods_scale_linearly() {
        let lead = "DB_PASSWORD=";
        // Lets the allocator keep the pages of the large arrays after they are freed (glibc raises its mmap and trim thresholds to the
        // size it saw freed): otherwise every run is charged a page fault per fresh page, which grows with the text but is not the algorithm's.
        drop(black_box(vec![1u8; 30 << 20]));
        for unit in UNITS {
            assert_flood_scales(lead, unit, SMALL);
            assert_flood_scales(lead, unit, LARGE);
        }
    }
}
