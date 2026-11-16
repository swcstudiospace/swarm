//! The quote lexer (levels 2 and 3 of the port): the role of every quote mark of one string.
//!
//! A port of `swarm/redact.py` `_LEX_RE`, `_COMMENT_LEAD`, `_prose_quote`, `_mate`, `_comment_at`, `_value_starts`, `_word_break`
//! and `class _Lexer`. The lexer walks the text once, left to right, and records for every `"`, `'` the role it plays
//! (content, open, close) in parallel arrays indexed by the mark's number; the derived arrays (`top`, `par`, `lost`, `nx0`, `nxl`)
//! are built on first use. Every other detector only queries it.
//!
//! Python keeps the arrays as `array('i')` (32-bit signed, `-1` for none), `bytearray` and `list`; so does this module (`Vec<i32>`,
//! `Vec<u8>`), which bounds the text length: [`Lexer::new`] rejects a text of more than [`MAX_LEN`] code points with
//! [`TextTooLong`] (Python raises `OverflowError` from `array.append` there), so no conversion in this module can wrap or truncate.
//!
//! Positions are code-point indices into the `&[char]`. Nothing here panics: every array access is bounds-checked and the few
//! invariants (a mark's `link` is a valid mark when `how` is closed or cut, `lvl <= pos`) are stated where they are used.

use std::cell::OnceCell;
use std::collections::HashMap;
use std::fmt;
use std::hash::{BuildHasherDefault, Hasher};

use crate::cls::is_alnum;
use crate::keys::{is_sensitive_key, name_hit};
use crate::patterns::{eol_search, esc_pair, flag_sep, name_sep, pair};

/// Role of a mark: a quote that is plain text (prose such as `it's`, or a quote inside a comment that no value covers).
pub const CONTENT: u8 = 0;
/// Role of a mark: a quote that opens a quoted string.
pub const OPEN: u8 = 1;
/// Role of a mark: a quote that closes a quoted string.
pub const CLOSE: u8 = 2;

/// `how` of an OPEN mark: nothing closes it (it runs to the end of its line as a value).
pub const UNCLOSED: u8 = 0;
/// `how` of an OPEN mark: closed by its own closing quote (`link` is that mark).
pub const CLOSED: u8 = 1;
/// `how` of an OPEN mark: ended by the close of an enclosing quote (`link` is the mark that ended it).
pub const CUT: u8 = 2;

/// The longest text, in code points, that a [`Lexer`] accepts: the largest value of Python's `array('i')`.
pub const MAX_LEN: usize = i32::MAX as usize;

/// The text is longer than [`MAX_LEN`] code points, so the 32-bit mark arrays could not hold its positions.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TextTooLong {
    /// Length of the rejected text in code points.
    pub len: usize,
}

impl fmt::Display for TextTooLong {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "text of {} code points exceeds the lexer limit of {MAX_LEN}",
            self.len
        )
    }
}

impl std::error::Error for TextTooLong {}

/// `max(v, 0)` as an index: the arrays hold non-negative values wherever this is used, `-1` (none) is mapped by the caller first.
pub(crate) fn us(v: i32) -> usize {
    usize::try_from(v).unwrap_or(0)
}

/// A mark number or position as stored in the 32-bit arrays. The text length is checked against [`MAX_LEN`] before any value is
/// made, so the saturation is unreachable.
fn i32_of(v: usize) -> i32 {
    i32::try_from(v).unwrap_or(i32::MAX)
}

/// Replace `v[i]` when it exists (every call site passes a mark number that the lexer made).
fn set<T>(v: &mut [T], i: usize, x: T) {
    if let Some(e) = v.get_mut(i) {
        *e = x;
    }
}

/// `key = (level << 1) | is_apostrophe` of redact.py:358 (a Python int, so it cannot overflow; `level < 2**31` here).
fn key_of(level: usize, apostrophe: bool) -> u64 {
    ((level as u64) << 1) | u64::from(apostrophe)
}

/// A multiplicative hasher for the small integer keys of the level counters (std's SipHash is several times slower and the keys
/// are not attacker-chosen collisions that matter: the table holds at most one entry per distinct escape level of the text).
#[derive(Default)]
struct KeyHasher(u64);

impl Hasher for KeyHasher {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.write_u64(u64::from(b));
        }
    }

    fn write_u64(&mut self, k: u64) {
        self.0 = (self.0.rotate_left(5) ^ k).wrapping_mul(0x517c_c1b7_2722_0a95);
    }
}

type KeyMap<V> = HashMap<u64, V, BuildHasherDefault<KeyHasher>>;

/// The Python `cnt` dict: for each key the number of open quotes with that key on the stack (a count of zero and an absent key
/// are the same, as `cnt.get(key)` is only tested for truth). Keys 0 and 1 (plain `"` and `'`) are the hot ones and live in an
/// array.
#[derive(Default)]
struct Counts {
    plain: [u32; 2],
    escaped: KeyMap<u32>,
}

impl Counts {
    fn cell(&mut self, key: u64) -> &mut u32 {
        match key {
            0 => &mut self.plain[0],
            1 => &mut self.plain[1],
            _ => self.escaped.entry(key).or_insert(0),
        }
    }

    fn get(&self, key: u64) -> u32 {
        match key {
            0 => self.plain[0],
            1 => self.plain[1],
            _ => self.escaped.get(&key).copied().unwrap_or(0),
        }
    }

    fn inc(&mut self, key: u64) {
        let c = self.cell(key);
        *c = c.saturating_add(1);
    }

    fn dec(&mut self, key: u64) {
        let c = self.cell(key);
        *c = c.saturating_sub(1);
    }
}

/// `_prose_quote` (redact.py:225): whether the plain quote at `s[p]` sits right after a letter or digit that is not part of a flag
/// word (`it's`, `users'`, but not `-p'x'`).
pub fn prose_quote(s: &[char], p: usize) -> bool {
    let Some(mut i) = p.checked_sub(1) else {
        return false;
    };
    if !s.get(i).copied().is_some_and(is_alnum) {
        return false;
    }
    while i > 0 && s.get(i - 1).copied().is_some_and(is_alnum) {
        i -= 1;
    }
    // `_PROSE_FLAG` is "-": a word that starts right after a dash is a flag (`-p'x'`), not prose.
    !(i > 0 && s.get(i - 1) == Some(&'-'))
}

/// `_mate` (redact.py:235): the position of the next PLAIN quote (an even run of backslashes before it) of the kind of `s[p]` in
/// `s[p + 1..stop]`, or `None`. `stop` is the end of the comment line; a `stop` past the text is clamped, as Python's `find` does.
pub fn mate(s: &[char], p: usize, stop: usize) -> Option<usize> {
    let c = *s.get(p)?;
    let end = stop.min(s.len());
    let mut from = p + 1;
    while from < end {
        let q = from + s.get(from..end)?.iter().position(|&x| x == c)?;
        let mut r = q;
        while r > 0 && s.get(r - 1) == Some(&'\\') {
            r -= 1;
        }
        if (q - r).is_multiple_of(2) {
            return Some(q);
        }
        from = q + 1;
    }
    None
}

/// `_COMMENT_LEAD` (redact.py:219).
fn is_comment_lead(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\r' | '\n' | ';' | '&' | '|' | '(')
}

/// `_comment_at` (redact.py:249): whether the `#` at `s[p]` starts a word, so that it begins a shell comment (`echo x #note`, `#`
/// at the start of a line); not `a#b`, and not after an escaped blank or joiner (`\ #`).
pub fn comment_at(s: &[char], p: usize) -> bool {
    let Some(mut r) = p.checked_sub(1) else {
        return true;
    };
    if !s.get(r).copied().is_some_and(is_comment_lead) {
        return false;
    }
    let lead = r;
    while r > 0 && s.get(r - 1) == Some(&'\\') {
        r -= 1;
    }
    (lead - r) % 2 == 0
}

/// `_value_starts` (redact.py:265): `(offset, ends at ; and & too)` for every value of a SENSITIVE name in `line` (one comment, from
/// its `#`), ascending. The caller passes the comment as its own slice, so the lookbehinds of the patterns cannot see the text
/// before the `#`, exactly as in Python (`line = s[p:skip_to]`).
pub fn value_starts(line: &[char]) -> Vec<(usize, bool)> {
    let mut out: Vec<(usize, bool)> = Vec::new();
    let mut at = 0;
    while let Some(m) = name_sep(line, at) {
        at = m.end;
        // `NAME:` followed by `//` is a URL scheme, not a value (redact.py:273).
        if !m.eq && line.get(m.end..m.end + 2) == Some(&['/', '/'][..]) {
            continue;
        }
        if line
            .get(m.start..m.name_end)
            .is_some_and(|name| name_hit(name, m.eq))
        {
            out.push((m.end, true));
        }
    }
    at = 0;
    while let Some(m) = flag_sep(line, at) {
        at = m.end;
        if line
            .get(m.start..m.name_end)
            .is_some_and(|name| name_hit(name, false))
        {
            out.push((m.end, false));
        }
    }
    at = 0;
    while let Some(m) = pair(line, at) {
        at = m.end;
        if line
            .get(m.name_start..m.name_end)
            .is_some_and(is_sensitive_key)
        {
            out.push((m.end, true));
        }
    }
    at = 0;
    while let Some(m) = esc_pair(line, at) {
        at = m.end;
        if m.bs_len % 2 == 1
            && line
                .get(m.name_start..m.name_end)
                .is_some_and(is_sensitive_key)
        {
            out.push((m.end, true));
        }
    }
    out.sort_unstable();
    out
}

/// `_word_break` (redact.py:290): whether `s[a..b]`, outside quotes, ends a value word: a blank that no backslash escapes, or
/// (`joiners`) a `;` or `&`. Python searches with `endpos = b`; `b` past the text is clamped.
pub fn word_break(s: &[char], a: usize, b: usize, joiners: bool) -> bool {
    let end = b.min(s.len());
    let mut k = a;
    while k < end {
        let Some(off) = s.get(k..end).and_then(|w| {
            w.iter()
                .position(|&c| c == ' ' || c == '\t' || (joiners && (c == ';' || c == '&')))
        }) else {
            return false;
        };
        k += off;
        let c = s.get(k).copied();
        // A `;` or `&` always breaks; a blank breaks unless the single character before it is a backslash (redact.py:296).
        if matches!(c, Some(';' | '&')) || k == 0 || s.get(k - 1) != Some(&'\\') {
            return true;
        }
        k += 1;
    }
    false
}

/// Every quote mark of one string with its role (see the module documentation). The arrays are indexed by the mark's number
/// (ascending position).
///
/// `role` is [`CONTENT`], [`OPEN`] or [`CLOSE`]. An OPEN mark has `how` ([`UNCLOSED`], [`CLOSED`] or [`CUT`]) and `link` (the mark
/// that ended it; an unclosed quote that an escaped-quote rule dropped from the stack keeps `how` unclosed but names the mark that
/// dropped it). A CLOSE mark's `link` is the mark it closes; every other `link` is `-1`.
#[derive(Debug)]
pub struct Lexer {
    /// Code-point offset of every mark.
    pub pos: Vec<i32>,
    /// Escape level of every mark: the length of the run of backslashes before the quote when that run is odd, else 0.
    pub lvl: Vec<i32>,
    /// [`CONTENT`], [`OPEN`] or [`CLOSE`] for every mark.
    pub role: Vec<u8>,
    /// See the type documentation; `-1` for none.
    pub link: Vec<i32>,
    /// [`UNCLOSED`], [`CLOSED`] or [`CUT`] for every mark (meaningful for OPEN marks).
    pub how: Vec<u8>,
    derived: OnceCell<(Vec<i32>, Vec<i32>)>,
    lost: OnceCell<Vec<i32>>,
    next: OnceCell<(Vec<i32>, Vec<i32>)>,
}

/// State of the comment scan of [`Lexer::new`]: the value spans of the comment in progress and the cursor through them.
struct CommentScan {
    /// Where each value starts and whether it also ends at `;`/`&`, ascending.
    starts: Vec<(usize, bool)>,
    si: usize,
    /// A value word of the comment is open ...
    vact: bool,
    /// ... and the text before here is checked for the word's end (no break in it); the text from here on is not yet.
    vscan: usize,
    /// Whether that word ends at a `;` or `&` as well as at a blank.
    vjoin: bool,
    /// The mate of a prose quote that opened a quoted segment of that word: the blanks up to it are inside the quotes.
    vseg: Option<usize>,
}

impl Lexer {
    /// `_Lexer.__init__` (redact.py:320): lex `s` once.
    ///
    /// # Errors
    ///
    /// [`TextTooLong`] when `s` has more than [`MAX_LEN`] code points.
    pub fn new(s: &[char]) -> Result<Self, TextTooLong> {
        if s.len() > MAX_LEN {
            return Err(TextTooLong { len: s.len() });
        }
        Ok(Self::build(s))
    }

    /// The lexer of `s`, whose length the caller has checked against [`MAX_LEN`] (a longer text would saturate the 32-bit arrays).
    pub(crate) fn build(s: &[char]) -> Self {
        let n = s.len();
        // Every mark is a `"` or `'` (a `#` makes none): counting them first sizes the arrays exactly, so a text of a million marks
        // builds them without the copying of repeated growth.
        let marks = s.iter().filter(|&&c| c == '"' || c == '\'').count();
        let mut pos: Vec<i32> = Vec::with_capacity(marks);
        let mut lvl: Vec<i32> = Vec::with_capacity(marks);
        let mut link: Vec<i32> = Vec::with_capacity(marks);
        let mut role: Vec<u8> = Vec::with_capacity(marks);
        let mut how: Vec<u8> = Vec::with_capacity(marks);
        // The open quotes as (mark, key): `stack` and `kstack` of the Python are always the same length, so they are one vector.
        let mut stack: Vec<(usize, u64)> = Vec::new();
        let mut cnt = Counts::default();
        let mut i = 0usize;
        // Python starts at -1; every test is `p >= skip_to` or `p < skip_to` with `p >= 0`, which 0 answers the same way.
        let mut skip_to = 0usize;
        let mut cs = CommentScan {
            starts: Vec::new(),
            si: 0,
            vact: false,
            vscan: 0,
            vjoin: true,
            vseg: None,
        };
        // A comment began and no mark has come after its line yet: the first mark after that line closes the quotes it opened.
        let mut comment_open = false;
        for (p, &c) in s.iter().enumerate() {
            if !matches!(c, '"' | '\'' | '#') {
                continue;
            }
            let mark = i32_of(i);
            // Rule (a): the comment's line has ended; no quote it opened outlives that line.
            if comment_open && p >= skip_to {
                comment_open = false;
                for (o, k) in stack.drain(..) {
                    set(&mut link, o, mark); // `how` stays UNCLOSED: the value it opened runs to the end of its line
                    cnt.dec(k);
                }
            }
            if c == '#' {
                // A `#` inside a comment, or inside a quote, is plain text.
                if p >= skip_to && stack.is_empty() && comment_at(s, p) {
                    skip_to = eol_search(s, p).unwrap_or(n);
                    cs.starts = s
                        .get(p..skip_to)
                        .map(value_starts)
                        .unwrap_or_default()
                        .into_iter()
                        .map(|(k, j)| (p + k, j))
                        .collect();
                    cs.si = 0;
                    cs.vact = false;
                    comment_open = true;
                }
                continue;
            }
            let mut r = p;
            while r > 0 && s.get(r - 1) == Some(&'\\') {
                r -= 1;
            }
            let q1 = c == '\'';
            let level = if (p - r) & 1 == 1 { p - r } else { 0 };
            let key = key_of(level, q1);
            pos.push(i32_of(p));
            lvl.push(i32_of(level));
            role.push(CONTENT);
            link.push(-1);
            how.push(UNCLOSED);
            let in_comment = p < skip_to;
            if in_comment && Self::comment_prose(s, p, level, skip_to, stack.is_empty(), &mut cs) {
                i += 1;
                continue;
            }
            if level == 0 {
                // A plain quote ends the escaped quotes of its kind on top of the stack.
                while let Some(&(o, k)) = stack.last() {
                    if k < 2 || (k & 1) != u64::from(q1) {
                        break;
                    }
                    stack.pop();
                    cnt.dec(k);
                    set(&mut link, o, mark);
                    // The plain quote closes a quote that holds the escaped one (`"mysql -p\\"x"`); with none it ends nothing.
                    if cnt.get(key) > 0 {
                        set(&mut how, o, CUT);
                    }
                }
            }
            if cnt.get(key) > 0 {
                while let Some((o, k)) = stack.pop() {
                    cnt.dec(k);
                    set(&mut link, o, mark);
                    if k == key {
                        set(&mut how, o, CLOSED);
                        set(&mut role, i, CLOSE);
                        set(&mut link, i, i32_of(o));
                        break;
                    }
                    set(&mut how, o, CUT);
                }
            } else if level > 0 || !prose_quote(s, p) {
                set(&mut role, i, OPEN);
                stack.push((i, key));
                cnt.inc(key);
            }
            if in_comment {
                cs.vscan = cs.vscan.max(p + 1);
            }
            i += 1;
        }
        Lexer {
            pos,
            lvl,
            role,
            link,
            how,
            derived: OnceCell::new(),
            lost: OnceCell::new(),
            next: OnceCell::new(),
        }
    }

    /// Redact.py:365-388: a quote inside a comment is prose unless it lies in the value span of a sensitive name. Advances the
    /// comment cursor and returns whether the mark is comment prose (a CONTENT mark that opens and closes nothing).
    fn comment_prose(
        s: &[char],
        p: usize,
        level: usize,
        skip_to: usize,
        no_stack: bool,
        cs: &mut CommentScan,
    ) -> bool {
        while let Some(&(st, joins)) = cs.starts.get(cs.si) {
            if st > p {
                break;
            }
            if no_stack {
                // Only the text past the cursor is scanned: every character of the comment is scanned once.
                if cs.vact && st > cs.vscan {
                    cs.vact = !word_break(s, cs.vscan, st, cs.vjoin);
                    cs.vscan = st;
                }
                if cs.vact {
                    // A second reading of the same word (`--password :;` is also `password :`): the union, the longer word.
                    cs.vjoin = cs.vjoin && joins;
                } else {
                    cs.vact = true;
                    cs.vscan = st;
                    cs.vjoin = joins;
                }
            }
            cs.si += 1;
        }
        if cs.vact && no_stack && p > cs.vscan {
            cs.vact = !word_break(s, cs.vscan, p, cs.vjoin);
            cs.vscan = p;
        }
        if !cs.vact {
            return true;
        }
        if no_stack && level == 0 && cs.vseg.is_none_or(|v| p > v) && prose_quote(s, p) {
            // `ab'c d'ef` in a comment: the prose quote opens a quoted segment that its mate on the line closes; the blanks in
            // between are inside the quotes, so the word goes on past them.
            if let Some(m) = mate(s, p, skip_to) {
                cs.vseg = Some(m);
                cs.vscan = m;
            }
        }
        false
    }

    /// Number of marks.
    pub fn len(&self) -> usize {
        self.pos.len()
    }

    /// Whether the text has no quote mark.
    pub fn is_empty(&self) -> bool {
        self.pos.is_empty()
    }

    /// `pos[m]` as an offset (0 for a number that is not a mark).
    pub fn pos_at(&self, m: usize) -> usize {
        self.pos.get(m).map_or(0, |&v| us(v))
    }

    /// `lvl[m]` (0 for a number that is not a mark).
    pub fn lvl_at(&self, m: usize) -> usize {
        self.lvl.get(m).map_or(0, |&v| us(v))
    }

    /// `role[m]` ([`CONTENT`] for a number that is not a mark).
    pub fn role_at(&self, m: usize) -> u8 {
        self.role.get(m).copied().unwrap_or(CONTENT)
    }

    /// `how[m]` ([`UNCLOSED`] for a number that is not a mark).
    pub fn how_at(&self, m: usize) -> u8 {
        self.how.get(m).copied().unwrap_or(UNCLOSED)
    }

    /// `link[m]` as a mark number, `None` for `-1`.
    pub fn link_at(&self, m: usize) -> Option<usize> {
        self.link.get(m).and_then(|&v| usize::try_from(v).ok())
    }

    /// `pos[m] - lvl[m]`: where the escaped quote's backslash run starts (`lvl[m] <= pos[m]` by construction, the run lies before
    /// the mark).
    pub fn start_at(&self, m: usize) -> usize {
        self.pos_at(m).saturating_sub(self.lvl_at(m))
    }

    /// `_derive` (redact.py:423): `top` and `par` for every mark, one pass over the roles and links.
    fn derive(&self) -> (Vec<i32>, Vec<i32>) {
        let n = self.pos.len();
        let mut top: Vec<i32> = Vec::with_capacity(n);
        let mut par: Vec<i32> = Vec::with_capacity(n);
        let mut stack: Vec<usize> = Vec::new();
        for (i, &role) in self.role.iter().enumerate() {
            // The quotes that end at mark i.
            while stack
                .last()
                .is_some_and(|&t| self.link.get(t) == Some(&i32_of(i)))
            {
                stack.pop();
            }
            if role == OPEN {
                par.push(stack.last().map_or(-1, |&t| i32_of(t)));
                stack.push(i);
            } else {
                par.push(-1);
            }
            top.push(stack.last().map_or(-1, |&t| i32_of(t)));
        }
        (top, par)
    }

    /// `top[m]`: the innermost quote open after mark `m`, `-1` for none. Built on first use together with [`Lexer::par`].
    pub fn top(&self) -> &[i32] {
        &self.derived.get_or_init(|| self.derive()).0
    }

    /// `par[m]`: the quote an OPEN mark is nested in, `-1` for none (and for every other mark).
    pub fn par(&self) -> &[i32] {
        &self.derived.get_or_init(|| self.derive()).1
    }

    /// `lost`: the ascending numbers of the quotes that nothing closes (OPEN with `how` unclosed).
    pub fn lost(&self) -> &[i32] {
        self.lost.get_or_init(|| {
            self.role
                .iter()
                .zip(&self.how)
                .enumerate()
                .filter(|&(_, (&r, &h))| r == OPEN && h == UNCLOSED)
                .map(|(i, _)| i32_of(i))
                .collect()
        })
    }

    /// `index` (redact.py:461): the number of the mark at `s[p]`, or `None` (Python `-1`).
    pub fn index(&self, p: usize) -> Option<usize> {
        let i = self
            .pos
            .partition_point(|&x| usize::try_from(x).map_or(true, |x| x < p));
        (self.pos.get(i).map(|&x| us(x)) == Some(p)).then_some(i)
    }

    /// `next_marks` (redact.py:466): `(nx0, nxl)`, for every mark the next PLAIN mark of its kind and the next mark of its kind
    /// and level (`-1` for none). Built on first use from `s`, the text this lexer was made from.
    pub fn next_marks(&self, s: &[char]) -> (&[i32], &[i32]) {
        let (nx0, nxl) = self.next.get_or_init(|| {
            let n = self.pos.len();
            let mut nx0: Vec<i32> = Vec::with_capacity(n);
            let mut nxl: Vec<i32> = Vec::with_capacity(n);
            let mut last0 = [-1i32; 2];
            let mut lastk: KeyMap<i32> = KeyMap::default();
            for i in (0..n).rev() {
                let apostrophe = s.get(self.pos_at(i)) == Some(&'\'');
                let level = self.lvl_at(i);
                let k = key_of(level, apostrophe);
                let kind = usize::from(apostrophe);
                nx0.push(last0[kind]);
                nxl.push(lastk.get(&k).copied().unwrap_or(-1));
                if k < 2 {
                    last0[kind] = i32_of(i);
                } else {
                    lastk.insert(k, i32_of(i));
                }
            }
            // Built from the last mark to the first.
            nx0.reverse();
            nxl.reverse();
            (nx0, nxl)
        });
        (nx0, nxl)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The expected arrays of one text, all taken from `swarm.redact._Lexer(text)`: `pos`, `lvl`, `role` (`.` content, `O` open,
    /// `C` close), `link`, `how` (`u` unclosed, `c` closed, `x` cut), then `top`, `par`, `lost`, `nx0` and `nxl`.
    type LexCase = (
        &'static str,
        &'static [i32],
        &'static [i32],
        &'static str,
        &'static [i32],
        &'static str,
        &'static [i32],
        &'static [i32],
        &'static [i32],
        &'static [i32],
        &'static [i32],
    );

    fn chars(s: &str) -> Vec<char> {
        s.chars().collect()
    }

    fn role_str(lx: &Lexer) -> String {
        lx.role
            .iter()
            .map(|&r| match r {
                OPEN => 'O',
                CLOSE => 'C',
                _ => '.',
            })
            .collect()
    }

    fn how_str(lx: &Lexer) -> String {
        lx.how
            .iter()
            .map(|&h| match h {
                CLOSED => 'c',
                CUT => 'x',
                _ => 'u',
            })
            .collect()
    }

    #[rustfmt::skip]
    const LEX_CASES: &[LexCase] = &[
    ("", &[], &[], "", &[], "",
     &[], &[], &[], &[], &[]),
    ("no quotes at all", &[], &[], "", &[], "",
     &[], &[], &[], &[], &[]),
    ("'", &[0], &[0], "O", &[-1], "u",
     &[0], &[-1], &[0], &[-1], &[-1]),
    ("\"", &[0], &[0], "O", &[-1], "u",
     &[0], &[-1], &[0], &[-1], &[-1]),
    ("''", &[0, 1], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("\"\"", &[0, 1], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("'\"'", &[0, 1, 2], &[0, 0, 0], "OOC", &[2, 2, 0], "cxu",
     &[0, 1, -1], &[-1, 0, -1], &[], &[2, -1, -1], &[-1, -1, -1]),
    ("\"'\"", &[0, 1, 2], &[0, 0, 0], "OOC", &[2, 2, 0], "cxu",
     &[0, 1, -1], &[-1, 0, -1], &[], &[2, -1, -1], &[-1, -1, -1]),
    ("'\"'\"'", &[0, 1, 2, 3, 4], &[0, 0, 0, 0, 0], "OOCOO", &[2, 2, 0, -1, -1], "cxuuu",
     &[0, 1, -1, 3, 4], &[-1, 0, -1, -1, 3], &[3, 4], &[2, 3, 4, -1, -1], &[-1, -1, -1, -1, -1]),
    ("'a' \"b\" 'c'", &[0, 2, 4, 6, 8, 10], &[0, 0, 0, 0, 0, 0], "OCOCOC", &[1, 0, 3, 2, 5, 4], "cucucu",
     &[0, -1, 2, -1, 4, -1], &[-1, -1, -1, -1, -1, -1], &[], &[1, 4, 3, -1, 5, -1], &[-1, -1, -1, -1, -1, -1]),
    ("don't stop", &[3], &[0], ".", &[-1], "u",
     &[-1], &[-1], &[], &[-1], &[-1]),
    ("the users' list", &[9], &[0], ".", &[-1], "u",
     &[-1], &[-1], &[], &[-1], &[-1]),
    ("mysql -p'x'", &[8, 10], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("a 'b' c", &[2, 4], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("-p'x'", &[2, 4], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("it's 'x'", &[2, 5, 7], &[0, 0, 0], ".OC", &[-1, 2, 1], "ucu",
     &[-1, 1, -1], &[-1, -1, -1], &[], &[1, 2, -1], &[-1, -1, -1]),
    ("5\" wide", &[1], &[0], ".", &[-1], "u",
     &[-1], &[-1], &[], &[-1], &[-1]),
    ("a'b'c", &[1, 3], &[0, 0], "..", &[-1, -1], "uu",
     &[-1, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("\u{e9}'x'", &[1, 3], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("\u{1f600}'x'\u{1f600}", &[1, 3], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("x\u{a0}'y'", &[2, 4], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("echo \"it's ready\"", &[5, 8, 16], &[0, 0, 0], "O.C", &[2, -1, 0], "cuu",
     &[0, 0, -1], &[-1, -1, -1], &[], &[2, -1, -1], &[-1, -1, -1]),
    ("bash -c \"mysql -p'x' db\"", &[8, 17, 19, 23], &[0, 0, 0, 0], "OOCC", &[3, 2, 1, 0], "ccuu",
     &[0, 1, 0, -1], &[-1, 0, -1, -1], &[], &[3, 2, -1, -1], &[-1, -1, -1, -1]),
    ("\"a 'b\" c'", &[0, 3, 5, 8], &[0, 0, 0, 0], "OOC.", &[2, 2, 0, -1], "cxuu",
     &[0, 1, -1, -1], &[-1, 0, -1, -1], &[], &[2, 3, -1, -1], &[-1, -1, -1, -1]),
    ("'a \"b' c\"", &[0, 3, 5, 8], &[0, 0, 0, 0], "OOC.", &[2, 2, 0, -1], "cxuu",
     &[0, 1, -1, -1], &[-1, 0, -1, -1], &[], &[2, 3, -1, -1], &[-1, -1, -1, -1]),
    ("{\\\"k\\\": \\\"v\\\"}", &[2, 5, 9, 12], &[1, 1, 1, 1], "OCOC", &[1, 0, 3, 2], "cucu",
     &[0, -1, 2, -1], &[-1, -1, -1, -1], &[], &[-1, -1, -1, -1], &[1, 2, 3, -1]),
    ("\\\\\\\"x\\\\\\\"", &[3, 8], &[3, 3], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[-1, -1], &[1, -1]),
    ("mysql -p\\\"x\" y", &[9, 11], &[1, 0], "O.", &[1, -1], "uu",
     &[0, -1], &[-1, -1], &[0], &[1, -1], &[-1, -1]),
    ("OPTS='sshpass -p \"x'y' z'", &[5, 17, 19, 21, 24], &[0, 0, 0, 0, 0], "OOC..", &[2, 2, 0, -1, -1], "cxuuu",
     &[0, 1, -1, -1, -1], &[-1, 0, -1, -1, -1], &[], &[2, -1, 3, 4, -1], &[-1, -1, -1, -1, -1]),
    ("\\'x'", &[1, 3], &[1, 0], "O.", &[1, -1], "uu",
     &[0, -1], &[-1, -1], &[0], &[1, -1], &[-1, -1]),
    ("a\\\\'b'", &[3, 5], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"x\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"", &[31, 64], &[31, 31], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[-1, -1], &[1, -1]),
    ("\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"x\"", &[32, 34], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\'x'", &[33, 35], &[33, 0], "O.", &[1, -1], "uu",
     &[0, -1], &[-1, -1], &[0], &[1, -1], &[-1, -1]),
    ("echo a#b 'x' y\n# 'z\n'w'", &[9, 11, 17, 20, 22], &[0, 0, 0, 0, 0], "OC.OC", &[1, 0, -1, 4, 3], "cuucu",
     &[0, -1, -1, 3, -1], &[-1, -1, -1, -1, -1], &[], &[1, 2, 3, 4, -1], &[-1, -1, -1, -1, -1]),
    ("x#'y", &[2], &[0], "O", &[-1], "u",
     &[0], &[-1], &[0], &[-1], &[-1]),
    ("# PASSWORD=\"a\"'b'\"c\"", &[11, 13, 14, 16, 17, 19], &[0, 0, 0, 0, 0, 0], "OCOCOC", &[1, 0, 3, 2, 5, 4], "cucucu",
     &[0, -1, 2, -1, 4, -1], &[-1, -1, -1, -1, -1, -1], &[], &[1, 4, 3, -1, 5, -1], &[-1, -1, -1, -1, -1, -1]),
    ("# it's 'x' PASSWORD=\"a\"", &[4, 7, 9, 20, 22], &[0, 0, 0, 0, 0], "...OC", &[-1, -1, -1, 4, 3], "uuucu",
     &[-1, -1, -1, 3, -1], &[-1, -1, -1, -1, -1], &[], &[1, 2, -1, 4, -1], &[-1, -1, -1, -1, -1]),
    ("# PASSWORD=ab'c d' x'", &[13, 17, 20], &[0, 0, 0], "...", &[-1, -1, -1], "uuu",
     &[-1, -1, -1], &[-1, -1, -1], &[], &[1, 2, -1], &[-1, -1, -1]),
    ("# PASSWORD=\"example\"'\nTOKEN='alpha\ns3cr3t'", &[11, 19, 20, 28, 41], &[0, 0, 0, 0, 0], "OCOOC", &[1, 0, 3, 4, 3], "cuucu",
     &[0, -1, 2, 3, -1], &[-1, -1, -1, -1, -1], &[2], &[1, -1, 3, 4, -1], &[-1, -1, -1, -1, -1]),
    ("a;#c 'x'", &[5, 7], &[0, 0], "..", &[-1, -1], "uu",
     &[-1, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("\\ # 'x'", &[4, 6], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("# --password 'x y' z", &[13, 17], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("# password: \"a;b\" 'c'", &[12, 16, 18, 20], &[0, 0, 0, 0], "OC..", &[1, 0, -1, -1], "cuuu",
     &[0, -1, -1, -1], &[-1, -1, -1, -1], &[], &[1, -1, 3, -1], &[-1, -1, -1, -1]),
    ("$#'x'", &[2, 4], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("'# not a comment' 'x'", &[0, 16, 18, 20], &[0, 0, 0, 0], "OCOC", &[1, 0, 3, 2], "cucu",
     &[0, -1, 2, -1], &[-1, -1, -1, -1], &[], &[1, 2, 3, -1], &[-1, -1, -1, -1]),
    ("echo hi\t#it's\n'k'", &[11, 14, 16], &[0, 0, 0], ".OC", &[-1, 2, 1], "ucu",
     &[-1, 1, -1], &[-1, -1, -1], &[], &[1, 2, -1], &[-1, -1, -1]),
    ("(#a 'b')", &[4, 6], &[0, 0], "..", &[-1, -1], "uu",
     &[-1, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("'a\r\nb' c", &[0, 5], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("'a\rb'", &[0, 4], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    ("'a\nb' c'd", &[0, 4, 7], &[0, 0, 0], "OC.", &[1, 0, -1], "cuu",
     &[0, -1, -1], &[-1, -1, -1], &[], &[1, 2, -1], &[-1, -1, -1]),
    ("x'\n'y", &[1, 3], &[0, 0], ".O", &[-1, -1], "uu",
     &[-1, 1], &[-1, -1], &[1], &[1, -1], &[-1, -1]),
    ("'a\nb", &[0], &[0], "O", &[-1], "u",
     &[0], &[-1], &[0], &[-1], &[-1]),
    ("x=\"a\r\nb\"; y='c", &[2, 7, 12], &[0, 0, 0], "OCO", &[1, 0, -1], "cuu",
     &[0, -1, 2], &[-1, -1, -1], &[2], &[1, -1, -1], &[-1, -1, -1]),
    ("\u{1f600} \"a\u{1f600}\" '\u{e9}'", &[2, 5, 7, 9], &[0, 0, 0, 0], "OCOC", &[1, 0, 3, 2], "cucu",
     &[0, -1, 2, -1], &[-1, -1, -1, -1], &[], &[1, -1, 3, -1], &[-1, -1, -1, -1]),
    ("'\u{e9}' \u{fc}'s 'z'", &[0, 2, 5, 8, 10], &[0, 0, 0, 0, 0], "OCOC.", &[1, 0, 3, 2, -1], "cucuu",
     &[0, -1, 2, -1, -1], &[-1, -1, -1, -1, -1], &[], &[1, 2, 3, 4, -1], &[-1, -1, -1, -1, -1]),
    ("\"\u{fc}\" '\u{df}'", &[0, 2, 4, 6], &[0, 0, 0, 0], "OCOC", &[1, 0, 3, 2], "cucu",
     &[0, -1, 2, -1], &[-1, -1, -1, -1], &[], &[1, -1, 3, -1], &[-1, -1, -1, -1]),
    ("'password': 'alpha\n  beta'\nmysql -p'x y' db", &[0, 9, 12, 25, 35, 39], &[0, 0, 0, 0, 0, 0], "OCOCOC", &[1, 0, 3, 2, 5, 4], "cucucu",
     &[0, -1, 2, -1, 4, -1], &[-1, -1, -1, -1, -1, -1], &[], &[1, 2, 3, 4, 5, -1], &[-1, -1, -1, -1, -1, -1]),
    ("'password': 'alpha\n  beta'\nmysql -p'x y' db\n# sshpass -p'", &[0, 9, 12, 25, 35, 39, 56], &[0, 0, 0, 0, 0, 0, 0], "OCOCOC.", &[1, 0, 3, 2, 5, 4, -1], "cucucuu",
     &[0, -1, 2, -1, 4, -1, -1], &[-1, -1, -1, -1, -1, -1, -1], &[], &[1, 2, 3, 4, 5, 6, -1], &[-1, -1, -1, -1, -1, -1, -1]),
    ("'''''", &[0, 1, 2, 3, 4], &[0, 0, 0, 0, 0], "OCOCO", &[1, 0, 3, 2, -1], "cucuu",
     &[0, -1, 2, -1, 4], &[-1, -1, -1, -1, -1], &[4], &[1, 2, 3, 4, -1], &[-1, -1, -1, -1, -1]),
    ("\"'\"'\"'\"", &[0, 1, 2, 3, 4, 5, 6], &[0, 0, 0, 0, 0, 0, 0], "OOCOOCO", &[2, 2, 0, 5, 5, 3, -1], "cxucxuu",
     &[0, 1, -1, 3, 4, -1, 6], &[-1, 0, -1, -1, 3, -1, -1], &[6], &[2, 3, 4, 5, 6, -1, -1], &[-1, -1, -1, -1, -1, -1, -1]),
    ("echo \"it's\"; echo 'don\"t'", &[5, 8, 10, 18, 22, 24], &[0, 0, 0, 0, 0, 0], "O.CO.C", &[2, -1, 0, 5, -1, 3], "cuucuu",
     &[0, 0, -1, 3, 3, -1], &[-1, -1, -1, -1, -1, -1], &[], &[2, 3, 4, 5, -1, -1], &[-1, -1, -1, -1, -1, -1]),
    ("x='a\"b'c\"d'", &[2, 4, 6, 8, 10], &[0, 0, 0, 0, 0], "O.C..", &[2, -1, 0, -1, -1], "cuuuu",
     &[0, 0, -1, -1, -1], &[-1, -1, -1, -1, -1], &[], &[2, 3, 4, -1, -1], &[-1, -1, -1, -1, -1]),
    ("; # &--token =&//'x y'", &[17, 21], &[0, 0], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[1, -1], &[-1, -1]),
    (";#\"", &[2], &[0], ".", &[-1], "u",
     &[-1], &[-1], &[], &[-1], &[-1]),
    ("(# ='", &[4], &[0], ".", &[-1], "u",
     &[-1], &[-1], &[], &[-1], &[-1]),
    ("\\''", &[1, 2], &[1, 0], "OO", &[1, -1], "uu",
     &[0, 1], &[-1, -1], &[0, 1], &[1, -1], &[-1, -1]),
    ("\\\"'", &[1, 2], &[1, 0], "OO", &[-1, -1], "uu",
     &[0, 1], &[-1, 0], &[0, 1], &[-1, -1], &[-1, -1]),
    ("\n# -p=--password :;//\\\"token\\\": ", &[22, 29], &[1, 1], "OC", &[1, 0], "cu",
     &[0, -1], &[-1, -1], &[], &[-1, -1], &[1, -1]),
    ("(# \u{1f600}--token =&ab'c d'ef|;x;\"a b\"\n", &[16, 20, 27, 31], &[0, 0, 0, 0], "..OC", &[-1, -1, 3, 2], "uucu",
     &[-1, -1, 2, -1], &[-1, -1, -1, -1], &[], &[1, -1, 3, -1], &[-1, -1, -1, -1]),
    ];

    #[test]
    fn lexer_arrays_equal_the_python_oracle() {
        for &(text, pos, lvl, role, link, how, top, par, lost, nx0, nxl) in LEX_CASES {
            let s = chars(text);
            let lx = Lexer::new(&s).unwrap();
            assert_eq!(lx.pos, pos, "pos of {text:?}");
            assert_eq!(lx.lvl, lvl, "lvl of {text:?}");
            assert_eq!(role_str(&lx), role, "role of {text:?}");
            assert_eq!(lx.link, link, "link of {text:?}");
            assert_eq!(how_str(&lx), how, "how of {text:?}");
            assert_eq!(lx.top(), top, "top of {text:?}");
            assert_eq!(lx.par(), par, "par of {text:?}");
            assert_eq!(lx.lost(), lost, "lost of {text:?}");
            let (a, b) = lx.next_marks(&s);
            assert_eq!(a, nx0, "nx0 of {text:?}");
            assert_eq!(b, nxl, "nxl of {text:?}");
            assert_eq!(lx.len(), pos.len());
            assert_eq!(lx.is_empty(), pos.is_empty());
        }
    }

    /// The roles asserted by `tests/test_redact.py` (`test_lexer_roles_for_comments_and_words`, `test_quote_lexer_rules`).
    #[test]
    fn roles_pinned_by_the_python_suite() {
        for (text, roles) in [
            ("echo a#b 'x' y\n# 'z\n'w'", "OC.OC"),
            ("x#'y", "O"),
            ("# PASSWORD=\"a\"'b'\"c\"", "OCOCOC"),
            ("# it's 'x' PASSWORD=\"a\"", "...OC"),
            ("# PASSWORD=ab'c d' x'", "..."),
            ("TOKEN='alpha\ns3cr3t'", "OC"),
            ("echo \"it's ready\"", "O.C"),
            ("don't stop", "."),
            ("the users' list", "."),
            ("mysql -p'x'", "OC"),
            ("a 'b' c", "OC"),
            ("bash -c \"mysql -p'x' db\"", "OOCC"),
            ("{\\\"k\\\": \\\"v\\\"}", "OCOC"),
        ] {
            let s = chars(text);
            assert_eq!(role_str(&Lexer::new(&s).unwrap()), roles, "{text:?}");
        }
    }

    /// Rule (a): the quote that a comment's glued value leaves open is unclosed and linked to the first mark after the comment line;
    /// the next line's assignment opens its own quote (`tests/test_redact.py`, `test_a_quote_a_comment_leaves_open_...`).
    #[test]
    fn a_quote_left_open_by_a_comment_ends_at_the_next_mark() {
        let s = chars("# PASSWORD=\"example\"'\nTOKEN='alpha\ns3cr3t'");
        let lx = Lexer::new(&s).unwrap();
        assert_eq!(role_str(&lx), "OCOOC");
        assert_eq!((lx.how_at(2), lx.link_at(2)), (UNCLOSED, Some(3)));
        assert_eq!((lx.how_at(3), lx.link_at(3)), (CLOSED, Some(4)));
        assert_eq!(lx.lost(), [2]);
    }

    #[rustfmt::skip]
    const IDX_EXPECTED: &[i32] = &[-1, -1, 0, -1, 1, -1, 2, -1, -1, 3, -1, 4, -1, -1, 5, -1, -1, -1, -1];

    #[test]
    fn index_finds_exactly_the_marks() {
        let s = chars("x 'a' \"b\\\"c\" #'d");
        let lx = Lexer::new(&s).unwrap();
        for (p, &want) in IDX_EXPECTED.iter().enumerate() {
            assert_eq!(lx.index(p), usize::try_from(want).ok(), "p = {p}");
        }
    }

    #[test]
    fn accessors_answer_neutrally_for_a_number_that_is_not_a_mark() {
        let s = chars("x\\\"y");
        let lx = Lexer::new(&s).unwrap();
        assert_eq!((lx.pos_at(0), lx.lvl_at(0), lx.start_at(0)), (2, 1, 1));
        assert_eq!(
            (lx.role_at(0), lx.how_at(0), lx.link_at(0)),
            (OPEN, UNCLOSED, None)
        );
        assert_eq!((lx.pos_at(9), lx.lvl_at(9), lx.start_at(9)), (0, 0, 0));
        assert_eq!(
            (lx.role_at(9), lx.how_at(9), lx.link_at(9)),
            (CONTENT, UNCLOSED, None)
        );
    }

    #[test]
    fn text_too_long_names_its_length_and_the_limit() {
        let msg = TextTooLong { len: MAX_LEN + 1 }.to_string();
        assert_eq!(
            msg,
            "text of 2147483648 code points exceeds the lexer limit of 2147483647"
        );
    }

    #[rustfmt::skip]
    const PROSE: &[(&str, usize, bool)] = &[
    ("don't", 3, true),
    ("-p'x'", 2, false),
    ("a-b'c", 3, false),
    ("--ab'c", 4, false),
    ("x'", 1, true),
    ("'", 0, false),
    (" 'x", 1, false),
    ("\u{e9}'", 1, false),
    ("9'", 1, true),
    ("ab_cd'", 5, true),
    ("a-'", 2, false),
    ("-ab'", 3, false),
    ("--x y'", 5, true),
    ("x-y z'", 5, true),
    ("\u{a0}'", 1, false),
    ("12'ab", 2, true),
    ];

    #[test]
    fn prose_quote_matches_the_oracle() {
        for &(text, p, want) in PROSE {
            assert_eq!(prose_quote(&chars(text), p), want, "{text:?} at {p}");
        }
    }

    #[rustfmt::skip]
    const MATE: &[(&str, usize, usize, Option<usize>)] = &[
    ("'a'b'", 0, 5, Some(2)),
    ("'abc", 0, 4, None),
    ("'a\\'b' c", 0, 7, Some(5)),
    ("'a\\\\'b' c", 0, 8, Some(4)),
    ("'a'", 0, 2, None),
    ("'a'", 0, 3, Some(2)),
    ("'a'", 0, 99, Some(2)),
    ("\"a'b\"", 0, 5, Some(4)),
    ("''", 0, 2, Some(1)),
    ("'a\\\\\\'b'", 0, 8, Some(7)),
    ("x'a\nb'", 1, 3, None),
    ("x'a\nb'", 1, 6, Some(5)),
    ("'a'", 0, 0, None),
    ("'", 0, 1, None),
    ];

    #[test]
    fn mate_matches_the_oracle() {
        for &(text, p, stop, want) in MATE {
            assert_eq!(
                mate(&chars(text), p, stop),
                want,
                "{text:?} from {p} to {stop}"
            );
        }
    }

    #[rustfmt::skip]
    const COMMENT: &[(&str, usize, bool)] = &[
    ("#", 0, true),
    ("a#b", 1, false),
    ("a #b", 2, true),
    ("a\\ #", 3, false),
    ("a\\\\ #", 4, true),
    (";#", 1, true),
    ("(#", 1, true),
    ("\u{e9}#", 1, false),
    ("a\n#", 2, true),
    ("a\r#", 2, true),
    ("|#", 1, true),
    ("&#", 1, true),
    ("a\\\\\\ #", 5, false),
    ("\t#", 1, true),
    ("\\ #", 2, false),
    ("$#", 1, false),
    ("a\u{a0}#", 2, false),
    ];

    #[test]
    fn comment_at_matches_the_oracle() {
        for &(text, p, want) in COMMENT {
            assert_eq!(comment_at(&chars(text), p), want, "{text:?} at {p}");
        }
    }

    #[rustfmt::skip]
    const WORD: &[(&str, usize, usize, bool, bool)] = &[
    ("ab cd", 0, 5, false, true),
    ("ab cd", 0, 2, false, false),
    ("ab\\ cd", 0, 6, false, false),
    ("ab\\\\ cd", 0, 7, false, false),
    ("a;b", 0, 3, true, true),
    ("a;b", 0, 3, false, false),
    ("a&b", 0, 3, true, true),
    ("a\\;b", 0, 4, true, true),
    ("ab\tc", 0, 4, false, true),
    ("\\\tc", 0, 3, false, false),
    (" a", 0, 2, false, true),
    ("a b", 2, 3, false, false),
    ("a b", 3, 2, false, false),
    ("a b", 0, 99, false, true),
    ("a \\ b", 0, 5, false, true),
    ("a\\ \\ b", 0, 6, false, false),
    ("\n\r\u{b}\u{c}", 0, 4, true, false),
    ("ab", 0, 2, true, false),
    ("", 0, 0, true, false),
    ("a\u{a0}b", 0, 3, true, false),
    ];

    #[test]
    fn word_break_matches_the_oracle() {
        for &(text, a, b, joiners, want) in WORD {
            assert_eq!(
                word_break(&chars(text), a, b, joiners),
                want,
                "{text:?} {a}..{b} joiners={joiners}"
            );
        }
    }

    #[rustfmt::skip]
    const VALUES: &[(&str, &[(usize, bool)])] = &[
    ("password=hunter2", &[(9, true)]),
    ("--password x", &[(11, false)]),
    ("\"password\": \"x\"", &[(12, true)]),
    ("\\\"token\\\": \\\"x\\\"", &[(11, true)]),
    ("api_key://x", &[]),
    ("api_key: //x", &[]),
    ("name=x", &[]),
    ("a=b password=c --token d", &[(13, true), (23, false)]),
    ("PASSWORD: x token=y", &[(10, true), (18, true)]),
    ("'secret': 'a' \"key\": 'b'", &[(10, true)]),
    ("\\\\\"password\\\\\": \\\\\"x", &[]),
    ("-p x", &[]),
    ("password = x", &[(11, true)]),
    ("--secret=x --token  y", &[(9, true), (20, false)]),
    ("# x", &[]),
    ("", &[]),
    ];

    #[test]
    fn value_starts_matches_the_oracle() {
        for &(text, want) in VALUES {
            assert_eq!(value_starts(&chars(text)), want, "{text:?}");
        }
    }
}
