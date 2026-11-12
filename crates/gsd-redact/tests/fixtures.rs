//! Drives [`gsd_redact::redact_text`] over the shared redaction fixtures that `tests/test_redact.py` and
//! `omp/test/swarm_bar.test.ts` already replay, so the Rust port is held to the same vectors as the Python oracle.
//!
//! Sections checked, one `#[test]` each so a failure names its section:
//!
//! * `tests/fixtures/redact_cases.json` `cases`: `input` and every needle are a string or a list of pieces joined with no separator,
//!   with `{{tok:KIND:INDEX[:LENGTH]}}` placeholders expanded (see [`expand_tokens`]). For each case, no `must_not_contain` needle
//!   may occur in the output, every `must_contain` needle must, the output must equal `expected` when the case has one, and the
//!   output must be a fixed point (`redact_text(out) == out`). This is `test_shared_vectors` and its TypeScript twin, unweakened.
//! * `tests/fixtures/redact_generated.json` `cases` and `wild`: `redact_text(input) == expected`, exactly.
//! * `tests/fixtures/redact_generated.json` `quoting`: `redact_text(input) == expected`, the output holds no `Zq` (the planted secret of
//!   every quoting vector), and the output is a fixed point, the three conditions the Python and TypeScript suites apply.
//!
//! Not covered here: the generated sections `property`, `noise` and `random`. They pin digests of outputs for inputs that need the
//! seeded xorshift/FNV generators (and `sha256`), which this crate does not port; the Python parity test owns them. The only generator
//! logic below is the minimal token expander `redact_cases.json` itself needs (kinds `plain`, `urlsafe`, `jwt`, `quoted`), a
//! transcription of `_token` in `tests/test_redact.py`.

use std::fmt::Write as _;
use std::fs;
use std::path::PathBuf;

use gsd_redact::redact_text;
use serde_json::Value;

/// How many mismatches a failure report spells out.
const SHOWN: usize = 5;
/// The longest text, in characters, a failure report quotes.
const QUOTE_LIMIT: usize = 120;

const ALNUM: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

/// A fixture parsed from `tests/fixtures/<name>`.
fn load(name: &str) -> Value {
    let path: PathBuf = [
        env!("CARGO_MANIFEST_DIR"),
        "..",
        "..",
        "tests",
        "fixtures",
        name,
    ]
    .iter()
    .collect();
    let text =
        fs::read_to_string(&path).unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));
    serde_json::from_str(&text)
        .unwrap_or_else(|e| panic!("{} is not valid JSON: {e}", path.display()))
}

/// The array under `key` of a fixture document.
fn section<'a>(doc: &'a Value, file: &str, key: &str) -> &'a [Value] {
    doc.get(key)
        .and_then(Value::as_array)
        .unwrap_or_else(|| panic!("{file} has no array section {key:?}"))
}

/// The string field `key` of a fixture item.
fn text_field<'a>(item: &'a Value, key: &str) -> &'a str {
    item.get(key)
        .and_then(Value::as_str)
        .unwrap_or_else(|| panic!("fixture item lacks the string field {key:?}: {item}"))
}

/// `value` as Python's `_join`/`flat` read it: a string, or a list of strings concatenated with no separator (placeholders unexpanded).
fn join(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Array(pieces) => pieces
            .iter()
            .map(|p| {
                p.as_str()
                    .unwrap_or_else(|| panic!("a fixture piece is not a string: {p}"))
            })
            .collect(),
        other => {
            panic!("a fixture needle or input is neither a string nor a list of strings: {other}")
        }
    }
}

/// `text` quoted with `{:?}`, cut to [`QUOTE_LIMIT`] characters (marked with `…` when cut).
fn quote(text: &str) -> String {
    let mut chars = text.chars();
    let head: String = chars.by_ref().take(QUOTE_LIMIT).collect();
    let cut = chars.next().is_some();
    let mut shown = format!("{head:?}");
    if cut {
        shown.pop();
        shown.push_str("…\"");
    }
    shown
}

/// Collects mismatches over one section and fails the test with their count and the first [`SHOWN`].
struct Check {
    section: &'static str,
    checked: usize,
    failures: Vec<String>,
}

impl Check {
    fn new(section: &'static str) -> Self {
        Self {
            section,
            checked: 0,
            failures: Vec::new(),
        }
    }

    /// Counts one fixture item; `problems` are the conditions it violated (one mismatch per item, all problems on one line).
    fn item(&mut self, label: &str, problems: Vec<String>) {
        self.checked += 1;
        if !problems.is_empty() {
            self.failures
                .push(format!("{label}: {}", problems.join("; ")));
        }
    }

    /// Fails with `<section>: N of M items mismatch` and the first [`SHOWN`], unless nothing mismatched.
    fn finish(self, at_least: usize) {
        assert!(self.checked >= at_least, "{}: only {} items checked, expected at least {at_least}: fixture shrank or changed shape", self.section, self.checked);
        if self.failures.is_empty() {
            return;
        }
        let mut report = format!(
            "{}: {} of {} items mismatch; first {}:",
            self.section,
            self.failures.len(),
            self.checked,
            SHOWN.min(self.failures.len())
        );
        for failure in self.failures.iter().take(SHOWN) {
            let _ = write!(report, "\n  {failure}");
        }
        panic!("{report}");
    }
}

/// `redact_text(input)` against `expected`, as a problem line when they differ.
fn expect_equal(problems: &mut Vec<String>, input: &str, out: &str, expected: &str) {
    if out != expected {
        problems.push(format!(
            "input {} gave {}, expected {}",
            quote(input),
            quote(out),
            quote(expected)
        ));
    }
}

// ---- secret placeholders: `{{tok:KIND:INDEX}}` / `{{tok:KIND:INDEX:LENGTH}}` ------------------------------------------------------
// Transcription of `_Xorshift`, `_fnv1a32`, `_pick`, `_token` and `expand_tokens` of tests/test_redact.py, for the kinds the
// `redact_cases.json` `cases` use. An unknown kind panics, so a new kind in the fixture cannot silently go unchecked.

/// xorshift32 (tests/test_redact.py `_Xorshift`).
struct Xorshift(u32);

impl Xorshift {
    fn new(seed: u32) -> Self {
        Self(if seed == 0 { 1 } else { seed })
    }

    fn next(&mut self, bound: usize) -> usize {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        self.0 = x;
        // `bound` is a small alphabet size or a length; `u32 -> usize` never truncates on the supported targets.
        (x as usize) % bound
    }
}

/// FNV-1a 32-bit over the UTF-8 bytes of `text` (`_fnv1a32`).
fn fnv1a32(text: &str) -> u32 {
    text.bytes().fold(0x811C_9DC5_u32, |h, byte| {
        (h ^ u32::from(byte)).wrapping_mul(0x0100_0193)
    })
}

/// `n` characters of `alphabet`, each drawn with `rng.next(len)` (`_pick`).
fn pick(rng: &mut Xorshift, alphabet: &str, n: usize) -> String {
    let chars: Vec<char> = alphabet.chars().collect();
    (0..n).map(|_| chars[rng.next(chars.len())]).collect()
}

/// The token for `kind:index[:length]` (`_token`); `length` is `None` when omitted or zero, as Python's `length or default` reads it.
fn token(kind: &str, index: u64, length: Option<usize>) -> String {
    let mut rng = Xorshift::new(fnv1a32(&format!("{kind}:{index}:{}", length.unwrap_or(0))));
    let urlsafe = format!("{ALNUM}_-");
    match kind {
        "plain" => {
            let n = length.unwrap_or(24);
            let first = pick(&mut rng, ALNUM, 1);
            first + &pick(&mut rng, &format!("{ALNUM}_.-+/=~"), n - 1)
        }
        "urlsafe" => pick(&mut rng, &urlsafe, length.unwrap_or(32)),
        "jwt" => {
            let rest = length.unwrap_or(80).max(15) - 15; // `eyJ` + 5, `.` + 5, `.`
            let a = rng.next(rest + 1);
            let b = rng.next(rest + 1);
            let (first, second) = (a.min(b), a.max(b));
            let head = pick(&mut rng, &urlsafe, 5 + first);
            let middle = pick(&mut rng, &urlsafe, 5 + second - first);
            let tail = pick(&mut rng, &urlsafe, rest - second);
            format!("eyJ{head}.{middle}.{tail}")
        }
        "quoted" => {
            let n = length.unwrap_or(24);
            let head = pick(&mut rng, ALNUM, 1);
            if n == 1 {
                return head;
            }
            let body = pick(&mut rng, &format!("{ALNUM} ,;:=/+.-"), n - 2);
            head + &body + &pick(&mut rng, ALNUM, 1)
        }
        other => panic!("fixture placeholder of kind {other:?} has no expander in tests/fixtures.rs; port it from `_token` in tests/test_redact.py"),
    }
}

/// One `{{tok:KIND:INDEX[:LENGTH]}}` at the start of `text` (`[a-z0-9_]+`, `\d+`, optional `:\d+`): its length in bytes and parts.
fn placeholder_at(text: &str) -> Option<(usize, &str, u64, Option<usize>)> {
    let body = text.strip_prefix("{{tok:")?;
    let kind_len = body
        .bytes()
        .take_while(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
        .count();
    let kind = &body[..kind_len];
    let rest = body[kind_len..].strip_prefix(':')?;
    let index_len = rest.bytes().take_while(u8::is_ascii_digit).count();
    if kind.is_empty() || index_len == 0 {
        return None;
    }
    let index: u64 = rest[..index_len]
        .parse()
        .expect("a placeholder index fits u64");
    let after_index = &rest[index_len..];
    let (length, tail) = match after_index.strip_prefix(':') {
        Some(more) => {
            let len_digits = more.bytes().take_while(u8::is_ascii_digit).count();
            if len_digits == 0 {
                (None, after_index)
            } else {
                let value: usize = more[..len_digits]
                    .parse()
                    .expect("a placeholder length fits usize");
                (Some(value), &more[len_digits..])
            }
        }
        None => (None, after_index),
    };
    let tail = tail.strip_prefix("}}")?;
    Some((
        text.len() - tail.len(),
        kind,
        index,
        length.filter(|n| *n != 0),
    ))
}

/// `text` with every placeholder expanded (`expand_tokens`); anything else is copied through.
fn expand_tokens(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(at) = rest.find("{{tok:") {
        out.push_str(&rest[..at]);
        rest = &rest[at..];
        match placeholder_at(rest) {
            Some((used, kind, index, length)) => {
                out.push_str(&token(kind, index, length));
                rest = &rest[used..];
            }
            None => {
                out.push_str("{{");
                rest = &rest["{{".len()..];
            }
        }
    }
    out.push_str(rest);
    out
}

// ---- the tests -------------------------------------------------------------------------------------------------------------------

#[test]
fn shared_vectors_redact_cases() {
    let doc = load("redact_cases.json");
    let mut check = Check::new("redact_cases.json cases");
    for case in section(&doc, "redact_cases.json", "cases") {
        let name = text_field(case, "name");
        let raw_input = join(&case["input"]);
        let input = expand_tokens(&raw_input);
        let out = redact_text(&input).expect("a fixture text is far below the length limit");
        let mut problems = Vec::new();
        for needle in case["must_not_contain"]
            .as_array()
            .map_or(&[][..], Vec::as_slice)
        {
            if out.contains(&expand_tokens(&join(needle))) {
                problems.push(format!(
                    "must_not_contain {} is in the output {}",
                    quote(&join(needle)),
                    quote(&out)
                ));
            }
        }
        for needle in case["must_contain"]
            .as_array()
            .map_or(&[][..], Vec::as_slice)
        {
            if !out.contains(&expand_tokens(&join(needle))) {
                problems.push(format!(
                    "must_contain {} is missing from the output {}",
                    quote(&join(needle)),
                    quote(&out)
                ));
            }
        }
        if let Some(expected) = case.get("expected") {
            let expected = expected
                .as_str()
                .unwrap_or_else(|| panic!("case {name:?}: `expected` is not a string"));
            expect_equal(&mut problems, &raw_input, &out, expected);
        }
        let again = redact_text(&out).expect("a fixture text is far below the length limit");
        if again != out {
            problems.push(format!(
                "not a fixed point: {} became {}",
                quote(&out),
                quote(&again)
            ));
        }
        check.item(name, problems);
    }
    check.finish(30);
}

/// Every `{input, expected}` item of a `redact_generated.json` section must satisfy `expected == redact_text(input)`.
fn check_generated(name: &'static str, at_least: usize, quoting: bool) {
    let doc = load("redact_generated.json");
    let mut check = Check::new(name);
    for (i, item) in section(&doc, "redact_generated.json", name)
        .iter()
        .enumerate()
    {
        let input = text_field(item, "input");
        let out = redact_text(input).expect("a fixture text is far below the length limit");
        let mut problems = Vec::new();
        expect_equal(&mut problems, input, &out, text_field(item, "expected"));
        if quoting {
            if out.contains("Zq") {
                problems.push(format!("the planted secret survives in {}", quote(&out)));
            }
            let again = redact_text(&out).expect("a fixture text is far below the length limit");
            if again != out {
                problems.push(format!(
                    "not a fixed point: {} became {}",
                    quote(&out),
                    quote(&again)
                ));
            }
        }
        check.item(&format!("{name}[{i}]"), problems);
    }
    check.finish(at_least);
}

#[test]
fn generated_cases_match_the_python_output() {
    check_generated("cases", 3000, false);
}

#[test]
fn generated_wild_shapes_match_the_python_output() {
    check_generated("wild", 3000, false);
}

#[test]
fn generated_quoting_forms_match_the_python_output_and_leave_no_secret() {
    check_generated("quoting", 2500, true);
}

/// The placeholder expander against `expand_tokens` of tests/test_redact.py: each row is the length and FNV-1a 32 of the Python expansion
/// (a hash, so the test stores no secret-shaped literal).
#[test]
fn token_expansion_matches_the_python_generator() {
    let tokens: [(&str, u64, Option<usize>, usize, u32); 18] = [
        ("plain", 0, None, 24, 0xcd9d_98e6),
        ("plain", 1, None, 24, 0x1395_3438),
        ("plain", 5, Some(1), 1, 0xe10c_2473),
        ("plain", 2, Some(40), 40, 0x62c1_0fd5),
        ("urlsafe", 0, None, 32, 0xc46b_797e),
        ("urlsafe", 714, Some(12), 12, 0x40fa_7343),
        ("urlsafe", 3, Some(513), 513, 0xf2c8_391d),
        ("jwt", 0, None, 80, 0x522b_1aa1),
        ("jwt", 1, Some(15), 15, 0xe51d_c430),
        ("jwt", 2, Some(16), 16, 0xd349_babb),
        ("jwt", 7, Some(60), 60, 0xb5d4_3cbe),
        ("jwt", 9, Some(200), 200, 0x735a_f432),
        ("quoted", 0, None, 24, 0x0c67_385c),
        ("quoted", 4, Some(1), 1, 0xf70c_4715),
        ("quoted", 4, Some(2), 2, 0x46f3_5f8c),
        ("quoted", 5, Some(10), 10, 0x829b_5940),
        ("quoted", 6, Some(40), 40, 0xd700_1b08),
        ("plain", 0, Some(0), 24, 0xcd9d_98e6), // a zero LENGTH reads as omitted
    ];
    for (kind, index, length, len, hash) in tokens {
        let placeholder = match length {
            Some(n) => format!("{{{{tok:{kind}:{index}:{n}}}}}"),
            None => format!("{{{{tok:{kind}:{index}}}}}"),
        };
        let expanded = expand_tokens(&placeholder);
        assert_eq!(
            (expanded.chars().count(), fnv1a32(&expanded)),
            (len, hash),
            "{placeholder}"
        );
    }
    // Text around placeholders is kept; text that is not a whole placeholder is copied through unchanged.
    let texts: [(&str, usize, u32); 4] = [
        ("a{{tok:plain:1}}b{{tok:urlsafe:714:12}}}", 39, 0x59e2_ef9e),
        ("{{{tok:quoted:5:10}}", 11, 0xa71a_250b),
        (
            "{{tok:plain:1:}} {{tok:Plain:1}} {{tok:plain:}} {{tok:",
            54,
            0x6f60_8f4d,
        ),
        ("{{tok:urlsafe:03:012}}x", 13, 0xdf48_ba82),
    ];
    for (text, len, hash) in texts {
        let expanded = expand_tokens(text);
        assert_eq!(
            (expanded.chars().count(), fnv1a32(&expanded)),
            (len, hash),
            "{text}"
        );
    }
}
