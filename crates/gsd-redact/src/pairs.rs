//! Quoted-pair and header detectors: `_pair_spans` (`"name": "value"` and escaped JSON), `_header_end` and `_header_spans` (Cookie and
//! Authorization header values), ported from `swarm/redact.py` lines 1038-1081.
//!
//! The pattern scanners live in [`crate::patterns`] ([`pair`], [`esc_pair`]) and [`crate::tokens`] ([`header_prefix_search`]); the
//! detectors here decide where each value ends with the quote-aware [`Reader`]. Every detector appends `(start, end)` code-point spans to
//! a `&mut Vec<(usize, usize)>` in the order Python calls `add`, and takes the reader of the same text as its last argument.

use crate::keys::{credential_pair, is_sensitive_key};
use crate::patterns::{esc_pair, pair};
use crate::reader::Reader;
use crate::tokens::header_prefix_search;

/// `_pair_spans` (redact.py:1042): `"name": "value"` / `'name': 'value'` and the twice-serialised `\"name\": \"value\"` (any odd number of
/// backslashes before the quotes).
///
/// The value of a credential name is read to its closing quote (to the end of the line when it never closes); a benign pair does not
/// hide a pair inside its value (`{"message": "login 'password': 'x'"}`): the scan goes on inside it. Plain pairs are reported first,
/// then the escaped ones. Each search resumes past the previous match start or accepted value, so both passes are linear.
pub fn pair_spans(s: &[char], spans: &mut Vec<(usize, usize)>, rd: &Reader<'_>) {
    let mut pos = 0;
    while let Some(m) = pair(s, pos) {
        let p = m.end;
        // `_PAIR_RE` ends in a lookahead for a quote, so `s[p]` exists; `get` keeps the port panic-free regardless.
        let Some(&q) = s.get(p) else { break };
        let (end, close) = rd.quoted(p, q, 1, 0);
        // `end - close > p + 1` and the value `s[p + 1:end - close]`: the value is never empty, and `close <= end` for every quote.
        if end > close + p + 1
            && credential_pair(&s[m.name_start..m.name_end], &s[p + 1..end - close])
        {
            spans.push((p + 1, end - close));
            pos = end;
        } else {
            pos = p + 1;
        }
    }
    pos = 0;
    while let Some(m) = esc_pair(s, pos) {
        let p = m.end;
        let run = m.bs_len;
        pos = p + run + 1;
        if run % 2 == 0 || !is_sensitive_key(&s[m.name_start..m.name_end]) {
            continue;
        }
        let (end, close) = rd.quoted(p, '"', run + 1, run);
        if end > close + p + run + 1 {
            spans.push((p + run + 1, end - close));
            pos = end;
        }
    }
}

/// `_header_end` (redact.py:1068): the end of the header value that begins at `value_start` (the first non-blank, non-quote character).
///
/// That is the end of its line, or the quote that closes the quote it sits in (`-H "Cookie: a=b"`), whichever comes first. A quote inside
/// the value (`a="b c"; d=e`, a stray apostrophe) is part of it: the lexer, not a local scan for the next quote, says which quote
/// encloses the header.
pub fn header_end(rd: &Reader<'_>, value_start: usize) -> usize {
    let end = rd.line_end(value_start);
    // A header that starts at the closing quote of an enclosing one is ambiguous: its line.
    match rd.enclosing_end(value_start) {
        Some(close) if value_start < close && close < end => close,
        _ => end,
    }
}

/// `_header_spans` (redact.py:1077): the value of every `Cookie:` / `Set-Cookie:` / `Authorization:` / `Proxy-Authorization:` header
/// except a `Bearer` / `Basic` scheme (those belong to the token detectors), from its first character to [`header_end`].
///
/// Searching resumes at the end of each value, so a value is never scanned twice.
pub fn header_spans(s: &[char], spans: &mut Vec<(usize, usize)>, rd: &Reader<'_>) {
    let mut pos = 0;
    while let Some(m) = header_prefix_search(s, pos) {
        pos = header_end(rd, m.value_start);
        spans.push((m.value_start, pos));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::assert_scales;

    /// A detector of this module.
    type Detector = fn(&[char], &mut Vec<(usize, usize)>, &Reader<'_>);

    fn run(s: &str, detector: Detector) -> Vec<(usize, usize)> {
        let t: Vec<char> = s.chars().collect();
        let rd = Reader::new(&t).unwrap();
        let mut spans = Vec::new();
        detector(&t, &mut spans, &rd);
        spans
    }

    fn check(detector: Detector, table: &[(&str, &[(usize, usize)])]) {
        for (input, want) in table {
            assert_eq!(run(input, detector), *want, "input {input:?}");
        }
    }

    // Every expected value below comes from `swarm.redact._pair_spans(s, add, _Reader(s))`, in `add` order.
    #[test]
    fn pair_spans_match_python() {
        check(
            pair_spans,
            &[
        ("{\"password\": \"hunter2\"}", &[(14, 21)]),
        ("{'password': 'hunter2'}", &[(14, 21)]),
        ("{\"password\":\"hunter2\"}", &[(13, 20)]),
        ("{\"password\" : \"hunter2\"}", &[(15, 22)]),
        ("{\"password\"        :        \"hunter2\"}", &[(29, 36)]),
        ("{\"password\"         : \"hunter2\"}", &[]),
        ("{\"password\":          \"hunter2\"}", &[]),
        ("{\"password\"\t:\t\"hunter2\"}", &[(15, 22)]),
        ("{\"password\": \"hunter2", &[(14, 21)]),
        ("{\"password\": \"hunter2\nnext\": \"x\"}", &[(14, 26)]),
        ("{\"password\": \"\"}", &[]),
        ("{\"password\": \"a\"}", &[(14, 15)]),
        ("{\"password\": \"a\\\"b\"} tail", &[(14, 18)]),
        ("{\"password\": \"a\\\\\"} tail", &[(14, 17)]),
        ("{\"password\": 'it's'}", &[(14, 16)]),
        ("{\"password\": \"it's fine\"}", &[(14, 23)]),
        ("{\"message\": \"login 'password': 'x'\"}", &[(32, 33)]),
        ("{\"message\": \"see \\\"token\\\": \\\"abc\\\" now\"}", &[(30, 33)]),
        ("{\"msg\": \"hi\", \"token\": \"abc\", \"n\": \"x\"}", &[(24, 27)]),
        ("{\"password\": \"a\", \"token\": \"b\", \"secret\": \"c\"}", &[(14, 15), (28, 29), (43, 44)]),
        ("{\"host\": \"h\", \"name\": \"n\"}", &[]),
        ("{\"api_key\": \"AbCdEfGhIjKlMnOpQrSt0123456789\"}", &[(13, 43)]),
        ("{\"ssh_key\": \"AbCdEfGhIjKlMnOpQrSt0123456789xyzXYZ\"}", &[(13, 49)]),
        ("{\"ssh_key\": \"short\"}", &[(13, 18)]),
        ("{\"key\": \"AbCdEfGhIjKlMnOpQrSt0123456789xyzXYZ\"}", &[]),
        ("{\"a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_password\": \"x\"}", &[(86, 87)]),
        ("{\"a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a__password\": \"x\"}", &[]),
        ("{\"a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_password\": \"x\"}", &[(84, 85)]),
        ("{\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\": \"x\"}", &[]),
        ("{\"\u{e9}password\": \"x\"}", &[(15, 16)]),
        ("{\"pass word\": \"x\"}", &[]),
        ("{\"\": \"x\"}", &[]),
        ("{\"password\\n\": \"x\"}", &[]),
        ("{\"pass'word\": \"x\"}", &[]),
        ("{\"password': \"x\"}", &[]),
        ("{\\\"password\\\": \\\"hunter2\\\"}", &[(17, 24)]),
        ("{\\\"password\\\" : \\\"hunter2\\\"}", &[(18, 25)]),
        ("{\\\"password\\\":\\\"hunter2\\\"}", &[(16, 23)]),
        ("{\\\"password\\\": \\\"hunter2", &[(17, 24)]),
        ("{\\\"password\\\": \\\"hunter2\\\", \\\"n\\\": 1}", &[(17, 24)]),
        ("{\\\"password\\\": \\\"a\\\"b\\\"}", &[(17, 18)]),
        ("{\\\"password\\\": \\\"\\\"}", &[]),
        ("{\\\"host\\\": \\\"x\\\"}", &[]),
        ("{\\\\\"password\\\\\": \\\\\"x\\\\\"}", &[]),
        ("{\\\\\\\"password\\\\\\\": \\\\\\\"hunter2\\\\\\\"}", &[(23, 30)]),
        ("{\\\\\\\\\\\"password\\\\\\\\\\\": \\\\\\\\\\\"hunter2\\\\\\\\\\\"}", &[(29, 36)]),
        ("{\\\"password\\\": \\\\\"x\\\\\"}", &[]),
        ("{\\\"password\\\": \\\"x\\\\\"}", &[(17, 22)]),
        ("{\\\"password\\\\\": \\\"x\\\"}", &[]),
        ("{\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"password\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\": \\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"hunter2\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"}", &[(107, 114)]),
        ("{\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"password\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\": \\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"hunter2\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"}", &[]),
        ("{\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"password\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\": \\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"hunter2\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\\"}", &[]),
        ("{\\\"msg\\\": \\\"login \\\\\\\"password\\\\\\\": \\\\\\\"x\\\\\\\"\\\"}", &[(40, 41)]),
        ("{\\\"msg\\\": \\\"\\\\\\\"password\\\\\\\": \\\\\\\"x\\\\\\\"\\\"}", &[(34, 35)]),
        ("{\\\"msg\\\": \\\"\\\\\\\"msg\\\\\\\": \\\\\\\"\\\\\\\\\\\\\\\"token\\\\\\\\\\\\\\\": \\\\\\\\\\\\\\\"y\\\\\\\\\\\\\\\"\\\\\\\"\\\"}", &[(60, 61)]),
        ("{\"a\": \"{\\\"password\\\": \\\"x\\\"}\"}", &[(24, 25)]),
        ("{\"password\": \"x\", \\\"token\\\": \\\"y\\\"}", &[(14, 15), (31, 32)]),
        ("{\\\"token\\\": \\\"y\\\", \"password\": \"x\"}", &[(32, 33), (14, 15)]),
        ("'password': 'x' \\'token\\': \\'y\\'", &[(13, 14)]),
        ("{\\\"password\\\": \\\"x\n  \\\"y\\\": 1", &[(17, 21)]),
        ("{\\\"a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a__password\\\": \\\"x\\\"}", &[]),
        ("{\\\"a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_a_password\\\": \\\"x\\\"}", &[(89, 90)]),
        ("{\\\"password\\\"        :        \\\"x\\\"}", &[(32, 33)]),
        ("{\\\"password\\\"         :\\\"x\\\"}", &[]),
        ("\"password\": \"x\" \"token\": \"y\" 'secret': 'z'", &[(13, 14), (26, 27), (40, 41)]),
            ],
        );
    }

    // Every expected value below comes from `swarm.redact._header_spans(s, add, _Reader(s))`, in `add` order.
    #[test]
    fn header_spans_match_python() {
        check(
            header_spans,
            &[
                ("Cookie: a=b", &[(8, 11)]),
                ("Cookie: a=b; c=d\nNext: x", &[(8, 16)]),
                ("Cookie: a=b\r\nNext: x", &[(8, 11)]),
                ("Cookie:a=b", &[(7, 10)]),
                ("Cookie :\ta=b", &[(9, 12)]),
                ("cookie: a=b", &[(8, 11)]),
                ("COOKIE: a=b", &[(8, 11)]),
                ("Set-Cookie: sid=abc; Path=/; HttpOnly", &[(12, 37)]),
                ("set-cookie: sid=abc", &[(12, 19)]),
                ("Authorization: token abc", &[(15, 24)]),
                ("Authorization: Digest user=\"x\", nonce=\"y\"", &[(15, 41)]),
                ("Proxy-Authorization: Negotiate abc", &[(21, 34)]),
                ("Authorization: Bearer abcdef", &[]),
                ("Authorization: bearer abcdef", &[]),
                ("Authorization: Bearerx abc", &[(15, 26)]),
                ("Authorization: Bearer", &[]),
                ("Authorization: Basic dXNlcjpwYXNz", &[]),
                ("Authorization: Basic", &[]),
                ("Authorization: BasicAuth abc", &[(15, 28)]),
                ("Authorization:  \t Basic x", &[]),
                ("Authorization: Basics x", &[(15, 23)]),
                ("Authorization: \"quoted\"", &[]),
                ("Authorization: 'quoted'", &[]),
                ("Authorization: \\\"x\\\"", &[(15, 20)]),
                ("Authorization:", &[]),
                ("Authorization:\nx: y", &[]),
                ("Authorization: \nx: y", &[]),
                ("Authorization:  ", &[]),
                ("Cookie: ;", &[(8, 9)]),
                ("X-Cookie: a=b", &[]),
                ("-Cookie: a=b", &[]),
                ("xCookie: a=b", &[]),
                ("_Cookie: a=b", &[]),
                ("Cookies: a=b", &[]),
                ("Set-Set-Cookie: a=b", &[]),
                ("Proxy-Cookie: a=b", &[(14, 17)]),
                ("Set-Authorization: a=b", &[(19, 22)]),
                ("a\nCookie: x\nb\nSet-Cookie: y\n", &[(10, 11), (26, 27)]),
                ("Cookie: a=b Cookie: c=d\n", &[(8, 23)]),
                ("curl -H \"Cookie: a=b\" https://h", &[(17, 20)]),
                ("curl -H 'Cookie: a=b' https://h", &[(17, 20)]),
                (
                    "curl -H 'Authorization: x y' -H 'Cookie: z'",
                    &[(24, 27), (41, 42)],
                ),
                (
                    "curl -H \"Authorization: Bearer abc\" -H \"Cookie: sid=1\"",
                    &[(48, 53)],
                ),
                ("-H \"Cookie: a=\\\"b c\\\"; d=e\"", &[(12, 26)]),
                ("Cookie: a=\"b c\"; d=e\nnext", &[(8, 20)]),
                ("Cookie: a='b c'; d=e", &[(8, 20)]),
                ("Cookie: it's here\nnext: x", &[(8, 17)]),
                ("\"Cookie: a=b\nnext: y\"", &[(9, 12)]),
                ("\"Cookie: a=b\"\nnext: y", &[(9, 12)]),
                ("x=\"Cookie: a=b\" y", &[(11, 14)]),
                ("echo \"Cookie: session=\"abc\"; other\" done", &[(14, 40)]),
                (
                    "echo 'Cookie: a=b' && echo 'Set-Cookie: c=d'",
                    &[(14, 17), (40, 43)],
                ),
                ("{\"headers\": \"Cookie: a=b\"}", &[(21, 24)]),
                ("{\"Cookie\": \"a=b\"}", &[]),
                ("'Cookie: a=\"b\"'", &[(9, 14)]),
                ("\"Cookie: a=\"\nb\"", &[(9, 12)]),
                ("Cookie: \u{e9}=b", &[(8, 11)]),
                ("Cookie: a\u{a0}b", &[(8, 11)]),
                ("Cookie:\u{3000}a", &[(7, 9)]),
                ("Cookie:\u{b}a", &[]),
                ("Cookie: \u{b}", &[]),
                ("\tCookie: a", &[(9, 10)]),
                ("Cookie: a\rCookie: b", &[(8, 9), (18, 19)]),
                ("Cookie: a\n\nCookie: b\n", &[(8, 9), (19, 20)]),
                ("Cookie: a=bCookie: a=bCookie: a=b", &[(8, 33)]),
                (
                    "Cookie: x\nCookie: x\nCookie: x\n",
                    &[(8, 9), (18, 19), (28, 29)],
                ),
            ],
        );
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
    fn pair_and_header_spans_are_linear() {
        for shape in [
            "{\"a\": \"",
            "{\"password\": \"x",
            "\\\"password\\\": \\\"x",
            "'a': '\n",
            "{\"msg\": \"login 'password': 'x' ",
        ] {
            assert_linear(shape, &|n| shape.repeat(n / shape.len() * 4), &|s| {
                run(s, pair_spans);
            });
        }
        for shape in [
            "Cookie: \"",
            "\"Cookie: a=b ",
            "Cookie: a=\"b c\"; d='e\n",
            "-H 'Authorization: x' ",
        ] {
            assert_linear(shape, &|n| shape.repeat(n / shape.len() * 4), &|s| {
                run(s, header_spans);
            });
        }
    }
}
