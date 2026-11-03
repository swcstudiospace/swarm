//! Secret-span detection for run-log text: a Rust port of `swarm/redact.py`.
//!
//! The Python module is the parity oracle. [`redact_spans`] returns exactly the spans that `swarm.redact.redact_spans` returns for every
//! input, as `(start, end)` pairs of Python code-point offsets, so every scanner works on `&[char]`, not on UTF-8 bytes.
//!
//! The crate is std-only and `forbid(unsafe_code)`. Python's patterns use lookbehind, lookahead and backreferences, which Rust's `regex`
//! crate lacks, so each pattern is a hand-written scanner with the same leftmost, greedy and backtracking behaviour.
//!
//! # Modules
//!
//! | Module | Port of |
//! |---|---|
//! | [`cls`] | the ASCII character classes the Python patterns spell out or compile with `re.ASCII` |
//! | [`keys`] | credential-name classification (`is_sensitive_key`, `_key_name`, `_credential_pair`) |
//! | [`patterns`] | the shared regex scanners: `NAME=`, `--flag`, quoted pairs, line ends |
//! | [`tokens`] | the stateless token detectors: PEM, JWT, vendor tokens, Bearer, Basic, URL userinfo, header prefix |
//! | [`lexer`], [`reader`] | the quote lexer and the quote-aware value reader every other detector queries |
//! | [`scan`] | the `NAME=value` and `--flag value` detector |
//! | [`cli`] | command-line password flags (`mysql -p`, `docker login -p`, `curl -u`) |
//! | [`yaml`] | YAML `name:` / `value:` pairs and block scalars |
//! | [`pairs`] | JSON `"name": "value"` pairs and Cookie or Authorization header values |
//!
//! # Pipeline
//!
//! Every detector reads the original text and only reports spans; nothing is rewritten while detecting, so no detector can measure a
//! value another one has already shortened, and the union does not depend on the order in which they run. [`redact_spans`] runs them
//! in the order of `_spans` (redact.py:1084) and unions the result: empty spans are dropped, the rest sorted, and overlapping or
//! touching spans become one range.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

pub mod cli;
pub mod cls;
pub mod keys;
pub mod lexer;
pub mod pairs;
pub mod patterns;
pub mod reader;
pub mod scan;
#[cfg(test)]
mod testing;
pub mod tokens;
pub mod yaml;

pub use lexer::TextTooLong;
use reader::Reader;
use scan::ScanKind;

/// The mask that replaces every detected span.
pub const REDACTED: &str = "[REDACTED]";

/// A text shorter than this holds nothing worth masking: no span is reported for it (redact.py:1167).
const MIN_LEN: usize = 6;

/// Every `(start, end)` code-point range of `text` that must be masked: the union of all detectors, ascending and disjoint.
///
/// # Errors
///
/// [`TextTooLong`] when `text` has more than [`lexer::MAX_LEN`] code points, which the quote lexer's 32-bit arrays cannot index. The
/// text is then not scanned at all: the caller must not treat the error as "nothing to redact".
pub fn redact_spans(text: &str) -> Result<Vec<(usize, usize)>, TextTooLong> {
    // The byte length bounds the code-point count from above, so the exact count is only needed when the bytes exceed the limit.
    let len = if text.len() <= lexer::MAX_LEN {
        text.len()
    } else {
        text.chars().count()
    };
    spans_of_bounded(len, || text.chars().collect())
}

/// `text` with every span of [`redact_spans`] replaced by [`REDACTED`]; the rest is returned unchanged.
///
/// # Errors
///
/// [`TextTooLong`], as for [`redact_spans`].
pub fn redact_text(text: &str) -> Result<String, TextTooLong> {
    let chars: Vec<char> = text.chars().collect();
    let spans = spans_of(&chars)?;
    if spans.is_empty() {
        return Ok(text.to_owned());
    }
    let mut out = String::with_capacity(text.len());
    let mut at = 0;
    for (start, end) in spans {
        // The spans are ascending, disjoint and inside the text (`merge_spans` and the detectors' bounds guarantee it); `get` keeps a
        // violated invariant from panicking, and the worst it can do is drop text, never expose a secret.
        out.extend(chars.get(at..start).unwrap_or_default());
        out.push_str(REDACTED);
        at = end;
    }
    out.extend(chars.get(at..).unwrap_or_default());
    Ok(out)
}

/// The seam for `redact_spans`. `len` is an upper bound on the code-point count of the text that `collect` yields, and is exact
/// whenever it exceeds [`lexer::MAX_LEN`]. A text over the limit is refused before `collect` runs, so its characters are never
/// allocated.
fn spans_of_bounded(
    len: usize,
    collect: impl FnOnce() -> Vec<char>,
) -> Result<Vec<(usize, usize)>, TextTooLong> {
    if len > lexer::MAX_LEN {
        return Err(TextTooLong { len });
    }
    spans_of(&collect())
}

/// `redact_spans` for a text that is already a slice of code points.
fn spans_of(s: &[char]) -> Result<Vec<(usize, usize)>, TextTooLong> {
    if s.len() < MIN_LEN {
        return Ok(Vec::new());
    }
    let rd = Reader::new(s)?;
    let mut spans = Vec::new();
    // The order of `_spans` (redact.py:1084-1107). It does not change the union, but it keeps this function diffable against Python.
    tokens::pem_spans(s, &mut spans);
    tokens::bearer_spans(s, &mut spans);
    tokens::basic_spans(s, &mut spans);
    tokens::userinfo_spans(s, &mut spans);
    pairs::header_spans(s, &mut spans, &rd);
    cli::cli_spans(&rd, &mut spans);
    yaml::yaml_spans(s, &mut spans, &rd);
    scan::scan(ScanKind::Name, &mut spans, &rd);
    scan::scan(ScanKind::Flag, &mut spans, &rd);
    tokens::jwt_spans(s, &mut spans);
    tokens::vendor_token_spans(s, &mut spans);
    pairs::pair_spans(s, &mut spans, &rd);
    Ok(merge_spans(spans))
}

/// The union of `spans` (redact.py:1173-1186): empty ranges are dropped, the rest sorted, and ranges that overlap or touch merged.
fn merge_spans(mut spans: Vec<(usize, usize)>) -> Vec<(usize, usize)> {
    spans.retain(|&(start, end)| start < end);
    spans.sort_unstable();
    let mut merged: Vec<(usize, usize)> = Vec::with_capacity(spans.len());
    for (start, end) in spans {
        match merged.last_mut() {
            // `start > cur_end` starts a new range in Python, so a range that merely touches the previous one is absorbed.
            Some(last) if start <= last.1 => last.1 = last.1.max(end),
            _ => merged.push((start, end)),
        }
    }
    merged
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `(name, text pieces, expected spans)`. Every expectation comes from the real `swarm.redact.redact_spans`. The text is joined from
    /// pieces so that no secret-shaped literal is contiguous in this source (the project's fixtures do the same).
    #[allow(clippy::type_complexity)]
    const SPANS: &[(&str, &[&str], &[(usize, usize)])] = &[
        (
            "pem block",
            &[
                "x ",
                "-----BEGIN RSA PRI",
                "VATE KEY-----",
                "\nMIIEvQ\n",
                "-----END RSA PRI",
                "VATE KEY-----",
                " y",
            ],
            &[(2, 70)],
        ),
        (
            "jwt",
            &[
                "tok ",
                "eyJhbGci",
                "OiJIUzI1NiJ9",
                ".",
                "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                ".",
                "SflKxwRJSMeKKF2QT4fwpM",
                " end",
            ],
            &[(4, 75)],
        ),
        (
            "github pat",
            &["ran ", "ghp_", "a1B2c3D4a1B2c3D4a1B2c3D4a1B2", " ok"],
            &[(4, 36)],
        ),
        (
            "openai style key",
            &["key ", "sk-", "abcdefghijklmnopqrstuv", " z"],
            &[(4, 29)],
        ),
        (
            "aws access key id",
            &["id ", "AKIA", "ABCDEFGHIJKLMNOP", " z"],
            &[(3, 23)],
        ),
        (
            "slack bot token",
            &["t ", "xoxb-", "123456789012-abcdefghij", " z"],
            &[(2, 30)],
        ),
        (
            "bearer header",
            &[
                "Authorization: Bearer ",
                "abcdefghijklmnop1234567890",
                "\nnext",
            ],
            &[(22, 48)],
        ),
        (
            "basic header",
            &[
                "Proxy-Authorization: Basic ",
                "dXNlcjpwYXNzd29yZA==",
                "\nnext",
            ],
            &[(27, 47)],
        ),
        (
            "userinfo url",
            &["postgres://app:", "hunter2pass", "@db.internal:5432/x"],
            &[(11, 26)],
        ),
        (
            "cookie header",
            &["Cookie: session=abcdef123456; theme=dark\nnext"],
            &[(8, 40)],
        ),
        (
            "set-cookie header",
            &["Set-Cookie: sid=zzyyxx998877; Path=/; HttpOnly\n"],
            &[(12, 46)],
        ),
        ("mysql -p", &["mysql -u root -pSECRETPW1 shop"], &[(16, 25)]),
        (
            "docker login -p",
            &["docker login -u me -p SECRETPW2 registry.example.com"],
            &[(22, 31)],
        ),
        (
            "curl -u",
            &["curl -u admin:SECRETPW3 https://x.example.com/api"],
            &[(8, 23)],
        ),
        (
            "yaml name/value",
            &["- name: DB_PASSWORD\n  value: topsecret99\n- name: MODE\n  value: fast\n"],
            &[(29, 40)],
        ),
        (
            "yaml block scalar",
            &["password: |\n  line one secret\n  line two secret\nnext: 1\n"],
            &[(10, 47)],
        ),
        (
            "NAME=value",
            &["export API_TOKEN=abcdef123456 done"],
            &[(17, 29)],
        ),
        (
            "quoted NAME=value",
            &["db_password=\"p@ss w0rd with spaces\" tail"],
            &[(12, 35)],
        ),
        (
            "--flag value",
            &["run --password hunter2xyz --verbose"],
            &[(15, 25)],
        ),
        (
            "--flag=value",
            &["run --api-key=abcdef123456 --verbose"],
            &[(14, 26)],
        ),
        (
            "json pair",
            &["{\"user\": \"bob\", \"password\": \"hunter2hunter\", \"n\": 1}"],
            &[(29, 42)],
        ),
        (
            "escaped json pair",
            &["{\\\"password\\\": \\\"hunter2hunter\\\", \\\"n\\\": 1}"],
            &[(17, 30)],
        ),
        (
            "two spans one line",
            &["a password=one123 and token=two456 b"],
            &[(11, 17), (28, 34)],
        ),
        (
            "overlap merges",
            &[
                "Authorization: Bearer ",
                "abcdefghijklmnop1234567890",
                " token=abcdef123456",
            ],
            &[(22, 48), (55, 67)],
        ),
        (
            "astral before secret",
            &["\u{1F600}\u{1F600} password=hunter22 x"],
            &[(12, 20)],
        ),
        (
            "non-ascii name",
            &["caf\u{e9} secret=hunter2 \u{2603} x"],
            &[(12, 19)],
        ),
        ("crlf value", &["password=abc123\r\nnext=1"], &[(9, 15)]),
        ("six chars minimum", &["ab=cde"], &[]),
        ("under six chars", &["a=b"], &[]),
        (
            "no secrets",
            &["plain log line with nothing sensitive in it"],
            &[],
        ),
        ("empty", &[""], &[]),
    ];

    /// `(name, text pieces, expected redact_text output)`, from the real `swarm.redact.redact_text`.
    const TEXTS: &[(&str, &[&str], &str)] = &[
        (
            "github pat",
            &["ran ", "ghp_", "a1B2c3D4a1B2c3D4a1B2c3D4a1B2", " ok"],
            "ran [REDACTED] ok",
        ),
        (
            "bearer header",
            &[
                "Authorization: Bearer ",
                "abcdefghijklmnop1234567890",
                "\nnext",
            ],
            "Authorization: Bearer [REDACTED]\nnext",
        ),
        (
            "userinfo url",
            &["postgres://app:", "hunter2pass", "@db.internal:5432/x"],
            "postgres://[REDACTED]@db.internal:5432/x",
        ),
        (
            "mysql -p",
            &["mysql -u root -pSECRETPW1 shop"],
            "mysql -u root -p[REDACTED] shop",
        ),
        (
            "yaml name/value",
            &["- name: DB_PASSWORD\n  value: topsecret99\n- name: MODE\n  value: fast\n"],
            "- name: DB_PASSWORD\n  value: [REDACTED]\n- name: MODE\n  value: fast\n",
        ),
        (
            "json pair",
            &["{\"user\": \"bob\", \"password\": \"hunter2hunter\", \"n\": 1}"],
            "{\"user\": \"bob\", \"password\": \"[REDACTED]\", \"n\": 1}",
        ),
        (
            "escaped json pair",
            &["{\\\"password\\\": \\\"hunter2hunter\\\", \\\"n\\\": 1}"],
            "{\\\"password\\\": \\\"[REDACTED]\\\", \\\"n\\\": 1}",
        ),
        (
            "two spans one line",
            &["a password=one123 and token=two456 b"],
            "a password=[REDACTED] and token=[REDACTED] b",
        ),
        (
            "overlap merges",
            &[
                "Authorization: Bearer ",
                "abcdefghijklmnop1234567890",
                " token=abcdef123456",
            ],
            "Authorization: Bearer [REDACTED] token=[REDACTED]",
        ),
        (
            "astral before secret",
            &["\u{1F600}\u{1F600} password=hunter22 x"],
            "\u{1F600}\u{1F600} password=[REDACTED] x",
        ),
        (
            "crlf value",
            &["password=abc123\r\nnext=1"],
            "password=[REDACTED]\r\nnext=1",
        ),
        (
            "no secrets",
            &["plain log line with nothing sensitive in it"],
            "plain log line with nothing sensitive in it",
        ),
    ];

    #[test]
    fn every_detector_agrees_with_the_python_oracle() {
        for (name, pieces, expected) in SPANS {
            let text = pieces.concat();
            let got = redact_spans(&text).expect("a vector is far below the length limit");
            assert_eq!(got.as_slice(), *expected, "vector {name}");
        }
    }

    #[test]
    fn redact_text_replaces_each_union_with_one_mask_and_is_a_fixed_point() {
        for (name, pieces, expected) in TEXTS {
            let text = pieces.concat();
            let once = redact_text(&text).expect("a vector is far below the length limit");
            assert_eq!(once, *expected, "vector {name}");
            let twice = redact_text(&once).expect("a vector is far below the length limit");
            assert_eq!(twice, once, "redacting twice changed vector {name}");
        }
    }

    #[test]
    fn merge_spans_unions_sorted_overlapping_and_touching_ranges() {
        #[allow(clippy::type_complexity)]
        let cases: &[(&[(usize, usize)], &[(usize, usize)])] = &[
            (&[], &[]),
            (&[(3, 3)], &[]),
            (&[(0, 2), (5, 9)], &[(0, 2), (5, 9)]),
            (&[(0, 5), (3, 9)], &[(0, 9)]),
            (&[(0, 5), (5, 9)], &[(0, 9)]),
            (&[(0, 9), (2, 4)], &[(0, 9)]),
            (&[(5, 9), (0, 5), (2, 3)], &[(0, 9)]),
            (&[(4, 6), (4, 6), (4, 6)], &[(4, 6)]),
            (&[(7, 7), (1, 3), (3, 3), (10, 12)], &[(1, 3), (10, 12)]),
        ];
        for (input, expected) in cases {
            assert_eq!(
                merge_spans(input.to_vec()).as_slice(),
                *expected,
                "input {input:?}"
            );
        }
    }

    /// RED-01: the length is counted before the text is collected into `Vec<char>`. The seam `spans_of_bounded` takes the code-point
    /// count and the collect step as a closure, so a text over `MAX_LEN` is refused without any allocation of its characters. The
    /// counting closure records every call; it must stay at zero.
    #[test]
    fn redact_spans_does_not_collect_past_max_len() {
        let collects = std::cell::Cell::new(0_usize);
        let result = spans_of_bounded(lexer::MAX_LEN + 1, || {
            collects.set(collects.get() + 1);
            Vec::new()
        });
        assert_eq!(
            collects.get(),
            0,
            "the text was collected before its length was checked"
        );
        assert_eq!(
            result,
            Err(TextTooLong {
                len: lexer::MAX_LEN + 1
            })
        );
    }
}
