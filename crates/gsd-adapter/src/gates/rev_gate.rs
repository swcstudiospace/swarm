//! The host `rev_gate` route (phase 14.5, unit U3.6d).
//!
//! Ports `scripts/rev_gate.py`: the change set (`git diff -U0` and `git ls-files --others`, or a walk of the tree when there is
//! no base), the seven mechanical checks, the findings files and the verdict tail ([`gsd_taskwriter`]'s `issue_gate`).
//!
//! The Python text rules are reproduced where this module answers: the universal newlines of `subprocess` text mode and of
//! `read_text`, `str.splitlines` on its ASCII separators, `str.strip` on ASCII whitespace (which includes U+001C to U+001F),
//! `\b`, `\w` and `\s` on ASCII, and each regex as a hand-written matcher that returns the leftmost match. Those rules are
//! the Python rules only for ASCII text, so every git output and every scanned file must be ASCII: a non-ASCII byte declines
//! the whole call, because `splitlines` also splits on U+0085, U+2028 and U+2029 and `\w`, `\s` and case folding are Unicode.
//!
//! Everything else that Python would answer differently is `E-DEP` `not_started`, and the selector runs the Python body: a
//! `--dry-run`, an argv outside [`served`], non-UTF-8 git output (Python raises), a missing or unparsable findings file, a
//! finding Python refuses, a per-target object that names a target outside `gate_for`, a symbolic link in the scanned set,
//! and a changed file named `rev_gate.py` (Python exempts its own script from three checks).
//!
//! The diff parser keeps the defects `scripts/rev_gate.py` has (a `++ ` added line is read as a file header, a `-- ` deleted
//! line is not counted, a binary change marks the previous file): the port reproduces them, it does not fix them.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use gsd_envelope::KeyMaterial;
use gsd_failure::ErrorCode;
use gsd_route::pydumps::parse_python_json;
use gsd_route::{is_safe_id, jstr, obj_get, to_python_json, Fields, Json, Reply, SeamError};
use gsd_taskwriter::{
    derive_gate_issue, open_for_write, Decline, GateIssueRequest, StoreFacts, SystemIds,
    TaskStoreWriter, WriteState, WriterConfig,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{ConnectOptions, Row};

use crate::strangler_seam::{
    after_commit, before_any_write, canonical_or, env_value, resolve_swarm_dir, Invocation,
};

/// Suffixes that make a changed file a code file (`CODE_EXTS`).
const CODE_EXTS: [&str; 16] = [
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".c",
    ".cc", ".cpp", ".h",
];
/// Path parts the untracked-file listing skips (`rev_gate.py` `collect_changes`).
const DIFF_SKIP: [&str; 7] = [
    ".swarm",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
];
/// Path parts `iter_files` skips, matched against the absolute path (`swarm/script_base.py`).
const WALK_SKIP: [&str; 8] = [
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".swarm",
];
const SEVERITIES: [&str; 5] = ["info", "minor", "major", "critical", "blocker"];
const BLOCKING: [&str; 3] = ["major", "critical", "blocker"];
const EXPIRES_S: i64 = 172_800;
const DEFAULT_MAX_LINES: usize = 800;
/// Python's `str.isspace` on ASCII: tab to carriage return, U+001C to U+001F, and space.
const PY_SPACE: [u8; 10] = [0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20];
/// The keywords of `CODE_LIKE_RE`'s first alternative.
const KEYWORDS: [&str; 13] = [
    "if", "for", "while", "return", "import", "from", "def", "class", "const", "let", "var",
    "function", "else",
];
/// The `(?i)` key/value heads of the sixth secret pattern: `head[_-]?tail`.
const SEP_WORDS: [(&[u8], &[u8]); 4] = [
    (b"api", b"key"),
    (b"secret", b"key"),
    (b"access", b"token"),
    (b"auth", b"token"),
];
/// The plain words of the sixth secret pattern.
const PLAIN_WORDS: [&[u8]; 2] = [b"password", b"passwd"];

/// True when `argv` is the grammar this route serves. `--json` is required, so Python's text mode stays Python's.
pub fn served(argv: &[String]) -> bool {
    parse(argv).is_some()
}

#[derive(Debug, Default)]
struct Args {
    root: Option<String>,
    diff_base: Option<String>,
    findings_file: Option<String>,
    per_target: Option<String>,
    max_lines: Option<usize>,
    task_id: Option<String>,
    correlation_id: Option<String>,
}

fn parse(argv: &[String]) -> Option<Args> {
    let mut out = Args::default();
    let mut json = false;
    let mut seen: Vec<&str> = Vec::new();
    let mut index = 0;
    while index < argv.len() {
        let token = argv[index].as_str();
        index += 1;
        if token == "--json" {
            if json {
                return None;
            }
            json = true;
            continue;
        }
        let (flag, inline) = match token.split_once('=') {
            Some((flag, value)) => (flag, Some(value.to_string())),
            None => (token, None),
        };
        if !matches!(
            flag,
            "--root"
                | "--diff-base"
                | "--findings-file"
                | "--per-target-findings"
                | "--max-lines"
                | "--task-id"
                | "--correlation-id"
        ) || seen.contains(&flag)
        {
            return None;
        }
        seen.push(flag);
        let value = match inline {
            Some(value) => value,
            None => {
                let next = argv.get(index)?;
                index += 1;
                next.clone()
            }
        };
        if value.is_empty() || value.starts_with('-') {
            return None;
        }
        match flag {
            "--root" => out.root = Some(value),
            "--diff-base" => out.diff_base = Some(value),
            "--findings-file" => out.findings_file = Some(value),
            "--per-target-findings" => out.per_target = Some(value),
            "--task-id" => out.task_id = Some(value),
            "--correlation-id" => out.correlation_id = Some(value),
            "--max-lines" => {
                if value.len() > 9 || !value.bytes().all(|b| b.is_ascii_digit()) {
                    return None;
                }
                out.max_lines = value.parse().ok();
            }
            _ => return None,
        }
    }
    json.then_some(out)
}

/// An `E-DEP` `not_started` refusal: nothing was written and the selector runs the Python body.
fn decline(reason: &str) -> SeamError {
    before_any_write(SeamError::new(ErrorCode::Dep, reason))
}

/// One finding in the order `make_finding` gives its keys.
#[derive(Debug, Clone)]
struct Finding {
    id: String,
    severity: String,
    kind: Json,
    summary: Json,
    evidence: Json,
    ac_ref: Json,
    owner: Json,
    location: Json,
}

impl Finding {
    fn json(&self) -> Json {
        Json::Obj(vec![
            ("id".into(), jstr(&self.id)),
            ("severity".into(), jstr(&self.severity)),
            ("kind".into(), self.kind.clone()),
            ("summary".into(), self.summary.clone()),
            ("evidence".into(), self.evidence.clone()),
            ("ac_ref".into(), self.ac_ref.clone()),
            ("owner_suggestion".into(), self.owner.clone()),
            ("location".into(), self.location.clone()),
        ])
    }

    fn blocking(&self) -> bool {
        BLOCKING.contains(&self.severity.as_str())
    }
}

/// `add(...)` of `mechanical_checks`: the id is `RF-<n>` in emission order.
fn add(
    out: &mut Vec<Finding>,
    severity: &str,
    kind: &str,
    summary: &str,
    evidence: &str,
    location: &str,
) {
    out.push(Finding {
        id: format!("RF-{:03}", out.len() + 1),
        severity: severity.to_string(),
        kind: jstr(kind),
        summary: jstr(summary),
        evidence: jstr(evidence),
        ac_ref: Json::Null,
        owner: jstr("A05"),
        location: jstr(location),
    });
}

/// One changed file: `{added, deleted, lines, binary}` of `collect_changes`.
#[derive(Debug, Default)]
struct Change {
    added: i64,
    deleted: i64,
    lines: Vec<(usize, String)>,
    binary: bool,
}

fn is_word(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn is_py_space(byte: u8) -> bool {
    PY_SPACE.contains(&byte)
}

/// `str.splitlines` on ASCII text: `\n`, `\v`, `\f`, `\r`, U+001C, U+001D and U+001E end a line; no empty last line.
fn py_splitlines(text: &str) -> Vec<&str> {
    let bytes = text.as_bytes();
    let mut out = Vec::new();
    let mut start = 0;
    for (index, &byte) in bytes.iter().enumerate() {
        if matches!(byte, 0x0a..=0x0d | 0x1c..=0x1e) {
            out.push(&text[start..index]);
            start = index + 1;
        }
    }
    if start < bytes.len() {
        out.push(&text[start..]);
    }
    out
}

/// The universal newlines of text mode: `\r\n` and lone `\r` become `\n`.
fn translate_newlines(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

/// `str.strip()` on ASCII text.
fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_ascii() && is_py_space(c as u8))
}

/// `text.strip()[:120]`.
fn preview(text: &str) -> String {
    py_strip(text).chars().take(120).collect()
}

/// `Path(rel).suffix`: the last component's text from its last dot, when the dot is neither first nor last.
fn py_suffix(rel: &str) -> &str {
    let name = rel
        .split('/')
        .rfind(|part| !part.is_empty() && *part != ".")
        .unwrap_or("");
    match name.rfind('.') {
        Some(index) if index > 0 && index < name.len() - 1 => &name[index..],
        _ => "",
    }
}

/// `TEST_RE.search(rel)`.
fn test_path(rel: &str) -> bool {
    let bytes = rel.as_bytes();
    // (^|/)(tests?|__tests__|spec)/
    let component_starts = (0..bytes.len()).filter(|&p| p == 0 || bytes[p - 1] == b'/');
    for start in component_starts {
        let rest = &bytes[start..];
        let words: [&[u8]; 4] = [b"test/", b"tests/", b"__tests__/", b"spec/"];
        if words.iter().any(|word| rest.starts_with(word)) {
            return true;
        }
    }
    // (_test|\.test|\.spec|_spec)\.[a-z]+$
    if let Some(dot) = rel.rfind('.') {
        let ext = &rel[dot + 1..];
        let stem = &rel[..dot];
        if !ext.is_empty()
            && ext.bytes().all(|b| b.is_ascii_lowercase())
            && ["_test", ".test", ".spec", "_spec"]
                .iter()
                .any(|word| stem.ends_with(word))
        {
            return true;
        }
    }
    // (^|/)test_[^/]+\.py$
    let name = rel.rsplit('/').next().unwrap_or("");
    name.starts_with("test_") && name.ends_with(".py") && name.len() >= 9
}

/// `\b(TODO|FIXME|XXX)\b`-style search: a word, bounded on both sides by non-word characters or the ends.
fn word_hit(text: &str, words: &[&str]) -> bool {
    let bytes = text.as_bytes();
    (0..bytes.len()).any(|start| {
        (start == 0 || !is_word(bytes[start - 1]))
            && words.iter().any(|word| {
                let end = start + word.len();
                end <= bytes.len()
                    && &bytes[start..end] == word.as_bytes()
                    && (end == bytes.len() || !is_word(bytes[end]))
            })
    })
}

/// `DEBUG_RE.search(text)`.
fn debug_hit(text: &str) -> bool {
    const PLAIN: [&str; 6] = [
        "console.log(",
        "console.debug(",
        "console.trace(",
        "breakpoint()",
        "pdb.set_trace(",
        "var_dump(",
    ];
    if PLAIN.iter().any(|needle| text.contains(needle)) || word_hit(text, &["ipdb"]) {
        return true;
    }
    let bytes = text.as_bytes();
    (0..bytes.len()).any(|start| {
        if start > 0 && is_word(bytes[start - 1]) {
            return false;
        }
        let rest = &bytes[start..];
        // \bdd\(
        if rest.starts_with(b"dd(") {
            return true;
        }
        // \bdebugger\b;?$
        rest.starts_with(b"debugger") && (rest.len() == 8 || (rest.len() == 9 && rest[8] == b';'))
    })
}

/// `PRINT_RE.match(text)`: `^\s*print\(`.
fn print_hit(text: &str) -> bool {
    let bytes = text.as_bytes();
    let start = bytes
        .iter()
        .position(|&b| !is_py_space(b))
        .unwrap_or(bytes.len());
    bytes[start..].starts_with(b"print(")
}

/// `CODE_LIKE_RE.match(text)`: a comment marker, then a keyword on a word boundary, or a comment that ends in `;{}()`.
fn code_like(text: &str) -> bool {
    let bytes = text.as_bytes();
    let first = bytes
        .iter()
        .position(|&b| !is_py_space(b))
        .unwrap_or(bytes.len());
    let marker = if bytes[first..].first() == Some(&b'#') {
        1
    } else if bytes[first..].starts_with(b"//") {
        2
    } else {
        return false;
    };
    let after_marker = first + marker;
    let keyword_at = after_marker
        + bytes[after_marker..]
            .iter()
            .take_while(|&&b| is_py_space(b))
            .count();
    let keyword_hit = KEYWORDS.iter().any(|keyword| {
        let rest = &bytes[keyword_at..];
        rest.starts_with(keyword.as_bytes())
            && (rest.len() == keyword.len() || !is_word(rest[keyword.len()]))
    });
    if keyword_hit {
        return true;
    }
    let rest = &bytes[after_marker..];
    let trimmed_len = rest
        .iter()
        .rposition(|&b| !is_py_space(b))
        .map_or(0, |last| last + 1);
    trimmed_len > 0 && matches!(rest[trimmed_len - 1], b';' | b'{' | b'}' | b'(' | b')')
}

/// `CONTROL_RE.search(text)` on ASCII text.
fn control_hit(text: &str) -> bool {
    text.bytes()
        .any(|b| matches!(b, 0x00..=0x08 | 0x0b | 0x0c | 0x0e..=0x1f))
}

/// `PLACEHOLDER_RE.search(text)` (`(?i)` on ASCII text).
fn placeholder(text: &str) -> bool {
    const WORDS: [&str; 12] = [
        "example",
        "placeholder",
        "xxx",
        "changeme",
        "dummy",
        "redacted",
        "${",
        "{{",
        "your_",
        "your-",
        "...",
        "***",
    ];
    let lower = text.to_ascii_lowercase();
    if WORDS.iter().any(|word| lower.contains(word)) {
        return true;
    }
    // <[^>]*>: some '>' after the first '<'.
    text.find('<')
        .is_some_and(|index| text[index + 1..].contains('>'))
}

/// `_entropy(s)`: the terms in first-occurrence order, summed the way Python's `sum` adds them.
fn entropy(value: &[u8]) -> f64 {
    let total = value.len() as f64;
    let mut order: Vec<(u8, usize)> = Vec::new();
    for &byte in value {
        match order.iter_mut().find(|(seen, _)| *seen == byte) {
            Some(slot) => slot.1 += 1,
            None => order.push((byte, 1)),
        }
    }
    let mut sum = 0.0_f64;
    for (_, count) in order {
        let probability = count as f64 / total;
        sum += probability * probability.log2();
    }
    -sum
}

/// `AKIA[0-9A-Z]{16}`.
fn akia(bytes: &[u8]) -> Option<&[u8]> {
    (0..bytes.len()).find_map(|start| {
        let rest = bytes.get(start..)?;
        if !rest.starts_with(b"AKIA") {
            return None;
        }
        let tail = rest.get(4..20)?;
        tail.iter()
            .all(|b| b.is_ascii_digit() || b.is_ascii_uppercase())
            .then(|| &bytes[start..start + 20])
    })
}

/// `\bgh[pousr]_[A-Za-z0-9]{36,}`.
fn github_token(bytes: &[u8]) -> Option<&[u8]> {
    (0..bytes.len()).find_map(|start| {
        if start > 0 && is_word(bytes[start - 1]) {
            return None;
        }
        let rest = bytes.get(start..)?;
        if !(rest.starts_with(b"gh")
            && matches!(rest.get(2), Some(&(b'p' | b'o' | b'u' | b's' | b'r')))
            && rest.get(3) == Some(&b'_'))
        {
            return None;
        }
        let run = rest[4..]
            .iter()
            .take_while(|b| b.is_ascii_alphanumeric())
            .count();
        (run >= 36).then(|| &bytes[start..start + 4 + run])
    })
}

/// `-----BEGIN [A-Z ]*PRIVATE KEY-----`.
fn private_key_header(bytes: &[u8]) -> Option<&[u8]> {
    const START: &[u8] = b"-----BEGIN ";
    const END: &[u8] = b"PRIVATE KEY-----";
    (0..bytes.len()).find_map(|start| {
        if !bytes.get(start..)?.starts_with(START) {
            return None;
        }
        let body = start + START.len();
        let run_end = body
            + bytes[body..]
                .iter()
                .take_while(|b| b.is_ascii_uppercase() || **b == b' ')
                .count();
        (body..=run_end)
            .rev()
            .find(|&at| bytes[at..].starts_with(END))
            .map(|at| &bytes[start..at + END.len()])
    })
}

/// `\bxox[baprs]-[0-9A-Za-z-]{10,}`.
fn slack_token(bytes: &[u8]) -> Option<&[u8]> {
    (0..bytes.len()).find_map(|start| {
        if start > 0 && is_word(bytes[start - 1]) {
            return None;
        }
        let rest = bytes.get(start..)?;
        if !(rest.starts_with(b"xox")
            && matches!(rest.get(3), Some(&(b'b' | b'a' | b'p' | b'r' | b's')))
            && rest.get(4) == Some(&b'-'))
        {
            return None;
        }
        let run = rest[5..]
            .iter()
            .take_while(|b| b.is_ascii_alphanumeric() || **b == b'-')
            .count();
        (run >= 10).then(|| &bytes[start..start + 5 + run])
    })
}

/// `eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`.
fn jwt(bytes: &[u8]) -> Option<&[u8]> {
    let segment_end = |from: usize| -> Option<usize> {
        let run = bytes
            .get(from..)?
            .iter()
            .take_while(|b| b.is_ascii_alphanumeric() || matches!(**b, b'_' | b'-'))
            .count();
        (run >= 10).then_some(from + run)
    };
    (0..bytes.len()).find_map(|start| {
        if !bytes.get(start..)?.starts_with(b"eyJ") {
            return None;
        }
        let first_end = segment_end(start + 3)?;
        if bytes.get(first_end) != Some(&b'.')
            || bytes.get(first_end + 1..first_end + 4) != Some(&b"eyJ"[..])
        {
            return None;
        }
        let second_end = segment_end(first_end + 4)?;
        if bytes.get(second_end) != Some(&b'.') {
            return None;
        }
        let third_end = segment_end(second_end + 1)?;
        Some(&bytes[start..third_end])
    })
}

/// The key/value pattern's `head[_-]?tail` or plain word starting at `start`, on lower-cased ASCII; returns the end
/// position when a `\b` follows it.
fn keyword_end(lower: &[u8], start: usize) -> Option<usize> {
    let rest = &lower[start..];
    let end = SEP_WORDS.iter().find_map(|(head, tail)| {
        if !rest.starts_with(head) {
            return None;
        }
        let after_head = start + head.len();
        if matches!(lower.get(after_head), Some(b'_' | b'-'))
            && lower[after_head + 1..].starts_with(tail)
        {
            return Some(after_head + 1 + tail.len());
        }
        lower[after_head..]
            .starts_with(tail)
            .then(|| after_head + tail.len())
    });
    let end = end.or_else(|| {
        PLAIN_WORDS
            .iter()
            .find(|word| rest.starts_with(word))
            .map(|word| start + word.len())
    })?;
    (end == lower.len() || !is_word(lower[end])).then_some(end)
}

/// `(?i)\b(api[_-]?key|...|passwd)\b\s*[:=]\s*['"]([^'"\s]{16,})['"]`: the captured value of the leftmost match.
fn key_value_secret(bytes: &[u8]) -> Option<&[u8]> {
    let lower = bytes.to_ascii_lowercase();
    for start in 0..bytes.len() {
        if start > 0 && is_word(bytes[start - 1]) {
            continue;
        }
        let Some(end) = keyword_end(&lower, start) else {
            continue;
        };
        let mut at = end + bytes[end..].iter().take_while(|&&b| is_py_space(b)).count();
        if !matches!(bytes.get(at), Some(b':' | b'=')) {
            continue;
        }
        at += 1;
        at += bytes[at..].iter().take_while(|&&b| is_py_space(b)).count();
        if !matches!(bytes.get(at), Some(b'\'' | b'"')) {
            continue;
        }
        let value_start = at + 1;
        let run = bytes[value_start..]
            .iter()
            .take_while(|&&b| !matches!(b, b'\'' | b'"') && !is_py_space(b))
            .count();
        let value_end = value_start + run;
        if run >= 16 && matches!(bytes.get(value_end), Some(b'\'' | b'"')) {
            return Some(&bytes[value_start..value_end]);
        }
    }
    None
}

/// The evidence string of a redacted secret: its first four characters and an ellipsis.
fn redacted(value: &[u8]) -> String {
    let head = &value[..value.len().min(4)];
    format!("{}…", String::from_utf8_lossy(head))
}

/// The evidence of every secret pattern that hits `text`, in `SECRET_RES` order.
fn secret_evidence(text: &str) -> Vec<String> {
    if placeholder(text) {
        return Vec::new();
    }
    let bytes = text.as_bytes();
    let mut hits: Vec<String> = [
        akia(bytes),
        github_token(bytes),
        private_key_header(bytes),
        slack_token(bytes),
        jwt(bytes),
    ]
    .into_iter()
    .flatten()
    .map(redacted)
    .collect();
    if let Some(value) = key_value_secret(bytes) {
        if entropy(value) >= 3.2 {
            hits.push(redacted(value));
        }
    }
    hits
}

/// `code_like` over the streak of lines, `mechanical_checks` and its `size`, `encoding`, `todo`, `debug`, `dead-code`,
/// `secret` and `coverage` kinds in emission order.
fn mechanical_checks(changes: &BTreeMap<String, Change>, max_lines: usize) -> Vec<Finding> {
    let mut out: Vec<Finding> = Vec::new();
    let mut src_changed = false;
    let mut tests_changed = false;
    for (rel, change) in changes {
        let ext = py_suffix(rel);
        let is_code = CODE_EXTS.contains(&ext);
        let is_test = test_path(rel);
        src_changed |= is_code && !is_test;
        tests_changed |= is_test;
        if change.binary {
            add(
                &mut out,
                "info",
                "structure",
                "binary/undecodable file — structural checks only",
                "",
                rel,
            );
            continue;
        }
        let total = change.added + change.deleted;
        if total > max_lines as i64 {
            add(
                &mut out,
                "major",
                "size",
                &format!("{total} changed lines exceeds {max_lines}; request a split"),
                "",
                rel,
            );
        }
        let cli = ext != ".py"
            || change
                .lines
                .iter()
                .any(|(_, text)| text.contains("__main__") || text.contains("argparse"));
        let mut run = 0_usize;
        for (number, text) in &change.lines {
            let location = format!("{rel}:{number}");
            if control_hit(text) {
                add(
                    &mut out,
                    "major",
                    "encoding",
                    "invisible/bidi control character (possible Trojan-source)",
                    "",
                    &location,
                );
            }
            if is_code && word_hit(text, &["TODO", "FIXME", "XXX"]) {
                add(
                    &mut out,
                    "minor",
                    "todo",
                    "TODO/FIXME/XXX marker added",
                    &preview(text),
                    &location,
                );
            }
            if is_code && (debug_hit(text) || (!cli && print_hit(text))) {
                add(
                    &mut out,
                    "minor",
                    "debug",
                    "debug statement left in code",
                    &preview(text),
                    &location,
                );
            }
            run = if is_code && code_like(text) {
                run + 1
            } else {
                0
            };
            if run == 3 {
                add(
                    &mut out,
                    "minor",
                    "dead-code",
                    "commented-out code block (>=3 lines)",
                    "",
                    &location,
                );
            }
            for evidence in secret_evidence(text) {
                add(
                    &mut out,
                    "major",
                    "secret",
                    "secret-looking string added (value redacted)",
                    &evidence,
                    &location,
                );
            }
        }
    }
    if src_changed && !tests_changed {
        add(
            &mut out,
            "minor",
            "coverage",
            "source files changed but no test files changed",
            "",
            "tests/",
        );
    }
    out
}

/// True when a `/`-separated path has a part named in `skip`.
fn has_part(path: &str, skip: &[&str]) -> bool {
    path.split('/').any(|part| skip.contains(&part))
}

/// The text of a git output or a read: strict UTF-8, ASCII only, universal newlines. Anything else declines.
fn ascii_text(bytes: &[u8]) -> Result<String, SeamError> {
    let text = std::str::from_utf8(bytes)
        .map_err(|_| decline("git output is not UTF-8; Python raises"))?;
    if !text.is_ascii() {
        return Err(decline("non-ASCII text in the scan; Python answers"));
    }
    Ok(translate_newlines(text))
}

/// One file read as `whole()` does: UTF-8 with universal newlines; a read or decode failure is a binary entry.
fn whole(path: &Path) -> Result<Change, SeamError> {
    let binary = Change {
        binary: true,
        ..Change::default()
    };
    let Ok(bytes) = fs::read(path) else {
        return Ok(binary);
    };
    let Ok(text) = std::str::from_utf8(&bytes) else {
        return Ok(binary);
    };
    if !text.is_ascii() {
        return Err(decline("non-ASCII file content; Python answers"));
    }
    let translated = translate_newlines(text);
    let lines: Vec<(usize, String)> = py_splitlines(&translated)
        .into_iter()
        .enumerate()
        .map(|(index, line)| (index + 1, line.to_string()))
        .collect();
    Ok(Change {
        added: lines.len() as i64,
        deleted: 0,
        lines,
        binary: false,
    })
}

/// The header-driven parse of `git diff -U0` (`collect_changes`), defects included.
fn parse_diff(diff: &str, changes: &mut BTreeMap<String, Change>) -> Result<(), SeamError> {
    let mut current: Option<String> = None;
    let mut new_no = 0_usize;
    for line in py_splitlines(diff) {
        if let Some(header) = line.strip_prefix("+++ ") {
            let rel = py_strip(header.strip_prefix("b/").unwrap_or(header));
            current = if rel == "/dev/null" {
                None
            } else {
                changes.entry(rel.to_string()).or_default();
                Some(rel.to_string())
            };
            continue;
        }
        let Some(key) = current.as_ref() else {
            continue;
        };
        let Some(change) = changes.get_mut(key) else {
            continue;
        };
        if line.starts_with("@@") {
            new_no = hunk_start(line)?;
        } else if line.starts_with('+') && !line.starts_with("+++") {
            change.added += 1;
            change.lines.push((new_no, line[1..].to_string()));
            new_no += 1;
        } else if line.starts_with('-') && !line.starts_with("---") {
            change.deleted += 1;
        } else if line.starts_with("Binary files") {
            change.binary = true;
        }
    }
    Ok(())
}

/// `re.search(r"\+(\d+)", line)` as `int(...)`, or 0 when there is no match.
fn hunk_start(line: &str) -> Result<usize, SeamError> {
    let bytes = line.as_bytes();
    for index in 0..bytes.len().saturating_sub(1) {
        if bytes[index] == b'+' && bytes[index + 1].is_ascii_digit() {
            let digits = bytes[index + 1..]
                .iter()
                .take_while(|b| b.is_ascii_digit())
                .count();
            if digits > 15 {
                return Err(decline("a hunk number beyond 15 digits; Python answers"));
            }
            return Ok(line[index + 1..index + 1 + digits]
                .parse()
                .unwrap_or_default());
        }
    }
    Ok(0)
}

/// `iter_files(root)` for the base-less mode: regular files reached without a symbolic link, with the `iter_files` skip
/// test on the absolute path. A symbolic link anywhere in a walked directory declines (Python's `rglob` treatment of
/// links is not reproduced).
fn walk_files(root: &Path) -> Result<Vec<(String, PathBuf)>, SeamError> {
    let root_text = root
        .to_str()
        .ok_or_else(|| decline("a non-UTF-8 root; Python answers"))?;
    if has_part(root_text, &WALK_SKIP) {
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    let mut stack: Vec<(PathBuf, String)> = vec![(root.to_path_buf(), String::new())];
    while let Some((dir, prefix)) = stack.pop() {
        let entries = fs::read_dir(&dir)
            .map_err(|_| decline("a directory of the tree could not be listed; Python answers"))?;
        for entry in entries {
            let entry = entry.map_err(|_| decline("a directory entry could not be read"))?;
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| decline("a non-UTF-8 file name; Python answers"))?;
            let file_type = entry
                .file_type()
                .map_err(|_| decline("a file type could not be read"))?;
            if file_type.is_symlink() {
                return Err(decline("a symbolic link in the tree; Python answers"));
            }
            let rel = if prefix.is_empty() {
                name.clone()
            } else {
                format!("{prefix}/{name}")
            };
            if file_type.is_dir() {
                if WALK_SKIP.contains(&name.as_str()) {
                    continue;
                }
                stack.push((entry.path(), rel));
            } else if file_type.is_file() && !has_part(&rel, &WALK_SKIP) {
                out.push((rel, entry.path()));
            }
        }
    }
    Ok(out)
}

/// One `git` call with the default environment and the call's cwd: `(succeeded, stdout)`.
fn git(root: &Path, args: &[&str]) -> Result<(bool, Vec<u8>), SeamError> {
    let output = Command::new("git")
        .args(args)
        .current_dir(root)
        .output()
        .map_err(|_| decline("git could not be started; Python answers"))?;
    Ok((output.status.success(), output.stdout))
}

/// `resolve_base`: the requested ref, else `HEAD~1` then `origin/main`, else none (the whole tree). An invalid requested
/// ref is none, as in Python.
fn resolve_base(root: &Path, requested: Option<&str>) -> Result<Option<String>, SeamError> {
    let (inside, _) = git(root, &["rev-parse", "--is-inside-work-tree"])?;
    if !inside {
        return Ok(None);
    }
    let candidates: Vec<&str> = match requested {
        Some(name) => vec![name],
        None => vec!["HEAD~1", "origin/main"],
    };
    for candidate in candidates {
        let spec = format!("{candidate}^{{commit}}");
        let (found, _) = git(root, &["rev-parse", "--verify", "-q", &spec])?;
        if found {
            return Ok(Some(candidate.to_string()));
        }
    }
    Ok(None)
}

/// `collect_changes(root, base)`: the diff parse and the untracked files, or the walk of the tree when there is no base.
fn collect_changes(root: &Path, base: Option<&str>) -> Result<BTreeMap<String, Change>, SeamError> {
    let mut changes = BTreeMap::new();
    let Some(base) = base else {
        for (rel, path) in walk_files(root)? {
            changes.insert(rel, whole(&path)?);
        }
        return Ok(changes);
    };
    let (_, diff) = git(root, &["diff", "-U0", "--no-color", base, "--", "."])?;
    parse_diff(&ascii_text(&diff)?, &mut changes)?;
    let (_, others) = git(root, &["ls-files", "--others", "--exclude-standard", "."])?;
    let others = ascii_text(&others)?;
    for rel in py_splitlines(&others) {
        if rel.is_empty() || has_part(rel, &DIFF_SKIP) {
            continue;
        }
        changes.insert(rel.to_string(), whole(&root.join(rel))?);
    }
    Ok(changes)
}

/// `json.loads(text)` after the universal newlines of `read_text`, with the dict semantics of Python: a repeated key keeps
/// its first position and takes its last value. Anything Python refuses declines.
fn read_json(base: &Path, path: &str) -> Result<Json, SeamError> {
    let bytes = fs::read(base.join(path))
        .map_err(|_| decline("the findings file could not be read; Python answers"))?;
    let text = std::str::from_utf8(&bytes)
        .map_err(|_| decline("the findings file is not UTF-8; Python answers"))?;
    let value = parse_python_json(&translate_newlines(text))
        .map_err(|_| decline("the findings file is not JSON this route proves; Python answers"))?;
    Ok(normalize(value))
}

fn normalize(value: Json) -> Json {
    match value {
        Json::Arr(items) => Json::Arr(items.into_iter().map(normalize).collect()),
        Json::Obj(fields) => {
            let mut out: Fields = Vec::new();
            for (key, value) in fields {
                let value = normalize(value);
                match out.iter_mut().find(|(seen, _)| *seen == key) {
                    Some(slot) => slot.1 = value,
                    None => out.push((key, value)),
                }
            }
            Json::Obj(out)
        }
        other => other,
    }
}

/// The items Python's `enumerate` yields for `value`, when they are all that the finding loop can take: a list, or an
/// empty container. A non-empty dict or string yields keys or characters, which Python refuses.
fn iterable(value: &Json) -> Option<Vec<Json>> {
    match value {
        Json::Arr(items) => Some(items.clone()),
        Json::Obj(fields) if fields.is_empty() => Some(Vec::new()),
        Json::Str(text) if text.is_empty() => Some(Vec::new()),
        _ => None,
    }
}

/// A finding field that is text or null, with its default when absent.
fn text_or(fields: &[(String, Json)], key: &str, default: Json) -> Result<Json, SeamError> {
    match obj_get(fields, key) {
        None => Ok(default),
        Some(value @ (Json::Str(_) | Json::Null)) => Ok(value.clone()),
        Some(_) => Err(decline(
            "a finding field that is not text or null; Python answers",
        )),
    }
}

/// `agent_finding(f, i, path)` for one object of a findings file.
fn agent_finding(
    fields: &[(String, Json)],
    index: usize,
    owner: &str,
) -> Result<Finding, SeamError> {
    let severity = match obj_get(fields, "severity") {
        None => "minor".to_string(),
        Some(Json::Str(text)) if SEVERITIES.contains(&text.as_str()) => text.clone(),
        Some(_) => {
            return Err(decline(
                "a finding with a severity outside the five; Python answers",
            ))
        }
    };
    let default_id = format!("RF-{index:03}");
    let id = match obj_get(fields, "id") {
        None | Some(Json::Null) => default_id,
        Some(Json::Str(text)) if text.is_empty() => default_id,
        Some(Json::Str(text)) => text.clone(),
        Some(_) => return Err(decline("a finding id that is not text; Python answers")),
    };
    Ok(Finding {
        id,
        severity,
        kind: text_or(fields, "kind", jstr("semantic"))?,
        summary: text_or(fields, "summary", jstr(""))?,
        evidence: text_or(fields, "evidence", jstr(""))?,
        ac_ref: text_or(fields, "ac_ref", Json::Null)?,
        owner: text_or(fields, "owner_suggestion", jstr(owner))?,
        location: text_or(fields, "location", Json::Null)?,
    })
}

/// `load_extra(path, offset)`: the findings file's list, or its `findings` member, numbered from `offset + 1`.
fn load_extra(base: &Path, path: &str, offset: usize) -> Result<Vec<Finding>, SeamError> {
    let raw = read_json(base, path)?;
    let items = match &raw {
        Json::Obj(fields) => match obj_get(fields, "findings") {
            Some(value) => iterable(value),
            None => iterable(&raw),
        },
        other => iterable(other),
    }
    .ok_or_else(|| decline("the findings file is not a list of findings; Python answers"))?;
    let mut out = Vec::new();
    for (position, item) in items.iter().enumerate() {
        let Json::Obj(fields) = item else {
            return Err(decline("a finding is not an object; Python answers"));
        };
        out.push(agent_finding(fields, offset + 1 + position, "A05")?);
    }
    Ok(out)
}

/// `load_per_target(path, offset)`: `{target: [findings]}`, numbered across the targets in order.
fn load_per_target(
    base: &Path,
    path: &str,
    offset: usize,
) -> Result<Vec<(String, Vec<Finding>)>, SeamError> {
    let Json::Obj(fields) = read_json(base, path)? else {
        return Err(decline(
            "the per-target findings are not an object; Python answers",
        ));
    };
    let mut out = Vec::new();
    let mut index = offset;
    for (target, items) in fields {
        let Json::Arr(list) = items else {
            return Err(decline("a per-target value is not a list; Python answers"));
        };
        let mut parsed = Vec::new();
        for item in &list {
            index += 1;
            let Json::Obj(fields) = item else {
                return Err(decline("a finding is not an object; Python answers"));
            };
            parsed.push(agent_finding(fields, index, "A05")?);
        }
        out.push((target, parsed));
    }
    Ok(out)
}

/// `check_per_target_keys`: a per-target id outside the gate task's `gate_for` is Python's `E-INPUT`. Reads the Task Store
/// only; declines whatever it cannot prove.
async fn check_per_target_keys(
    swarm: &Path,
    task_id: Option<&str>,
    own: &[(String, Vec<Finding>)],
) -> Result<(), SeamError> {
    let Some(task_id) = task_id else {
        return Ok(());
    };
    if own.is_empty() {
        return Ok(());
    }
    let db = swarm.join("tasks.db");
    if !db.is_file() {
        return Err(decline("the task store is absent; Python answers"));
    }
    let mut conn = SqliteConnectOptions::new()
        .filename(&db)
        .read_only(true)
        .connect()
        .await
        .map_err(|_| decline("the task store could not be opened; Python answers"))?;
    let row = sqlx::query("SELECT notes FROM tasks WHERE task_id=?")
        .bind(task_id)
        .fetch_optional(&mut conn)
        .await
        .map_err(|_| decline("the task store could not be read; Python answers"))?;
    let Some(row) = row else {
        return Ok(());
    };
    let notes: Option<String> = row
        .try_get(0)
        .map_err(|_| decline("the notes could not be read; Python answers"))?;
    let Some(notes) = notes else {
        return Ok(());
    };
    let notes = normalize(
        parse_python_json(&notes).map_err(|_| decline("the notes are not JSON; Python answers"))?,
    );
    let fields = match notes {
        Json::Obj(fields) => fields,
        Json::Null => return Ok(()),
        _ => return Err(decline("the notes are not an object; Python answers")),
    };
    let Some(gate_for) = obj_get(&fields, "gate_for") else {
        return Ok(());
    };
    let targets: Vec<&str> = match gate_for {
        Json::Null | Json::Bool(false) | Json::Int(0) => return Ok(()),
        Json::Str(text) if text.is_empty() => return Ok(()),
        Json::Arr(items) if items.is_empty() => return Ok(()),
        Json::Arr(items) => items
            .iter()
            .map(|item| match item {
                Json::Str(text) => Ok(text.as_str()),
                _ => Err(decline(
                    "gate_for holds a value that is not text; Python answers",
                )),
            })
            .collect::<Result<_, _>>()?,
        _ => return Err(decline("gate_for is not a list; Python answers")),
    };
    if own
        .iter()
        .any(|(target, _)| !targets.contains(&target.as_str()))
    {
        return Err(decline(
            "per-target findings name a non-target; Python raises E-INPUT",
        ));
    }
    Ok(())
}

/// The commit condition of `issue_gate`: a store call (`task_id` given) whose plan has no error and writes rows. The no-task
/// path derives the plan without a store and commits nothing, so its writes are not recorded.
fn committed(request: &GateIssueRequest, plan: &gsd_taskwriter::GatePlan) -> bool {
    request.task_id.is_some() && plan.error.is_none() && !plan.writes.is_empty()
}

/// Writes the verdict file and its directory. A failure after a commit is final: the verdict is recorded and must not be
/// recorded again. Otherwise nothing was committed and the failure is not_started, so Python answers.
fn write_plan_file(
    swarm: &Path,
    plan: &gsd_taskwriter::GatePlan,
    committed: bool,
) -> Result<(), SeamError> {
    let refuse = |reason: &str| {
        if committed {
            after_commit(SeamError::new(
                ErrorCode::Internal,
                format!("the verdict is recorded in the Task Store, but {reason}; the outcome is final and is not run again"),
            ))
        } else {
            before_any_write(SeamError::new(
                ErrorCode::Dep,
                format!("{reason}; Python answers"),
            ))
        }
    };
    if plan.ensure_verdicts_dir {
        fs::create_dir_all(swarm.join("verdicts"))
            .map_err(|_| refuse("the verdicts directory could not be created"))?;
    }
    if let Some(file) = &plan.file {
        fs::write(swarm.join("verdicts").join(&file.name), &file.bytes)
            .map_err(|_| refuse("the verdict file could not be written"))?;
    }
    Ok(())
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `payload.verdict` of an envelope.
fn payload_verdict(envelope: &Json) -> Option<String> {
    let Json::Obj(fields) = envelope else {
        return None;
    };
    let Some(Json::Obj(payload)) = obj_get(fields, "payload") else {
        return None;
    };
    match obj_get(payload, "verdict") {
        Some(Json::Str(verdict)) => Some(verdict.clone()),
        _ => None,
    }
}

/// The verdict of the envelope a committed plan returned. The verdict is already recorded when this runs, so a missing one is
/// final: the outcome is not run again.
fn committed_verdict(envelope: &Json) -> Result<String, SeamError> {
    payload_verdict(envelope).ok_or_else(|| {
        after_commit(SeamError::new(
            ErrorCode::Internal,
            "the verdict is recorded in the Task Store, but the reply could not be built: the envelope carries no verdict; the outcome is final and is not run again",
        ))
    })
}

/// Answers one served `rev_gate` call. A [`SeamError`] of `E-DEP` is not-started: the selector runs Python.
pub async fn answer(call: &Invocation) -> Result<Reply, SeamError> {
    let Some(args) = parse(&call.argv) else {
        return Err(decline(
            "rev_gate argv is outside the host grammar; Python answers",
        ));
    };
    let root = canonical_or(&call.root.join(args.root.as_deref().unwrap_or(".")));
    if !root.is_dir() {
        return Err(decline("the root is not a directory; Python answers"));
    }
    let task_id = args
        .task_id
        .clone()
        .or_else(|| env_value(&call.env, "SWARM_TASK_ID").map(str::to_string));
    let correlation_id = args
        .correlation_id
        .clone()
        .or_else(|| env_value(&call.env, "SWARM_CORRELATION_ID").map(str::to_string));
    let unsafe_id = |id: &Option<String>| id.as_deref().is_some_and(|value| !is_safe_id(value));
    if unsafe_id(&task_id) || unsafe_id(&correlation_id) {
        return Err(decline("an id that is not safe; Python answers E-INPUT"));
    }

    let base = resolve_base(&root, args.diff_base.as_deref())?;
    let changes = collect_changes(&root, base.as_deref())?;
    // Python exempts its own script (`self_path`) from three checks; the port declines any changed file that could be it.
    for rel in changes.keys() {
        let named = rel.rsplit('/').next() == Some("rev_gate.py");
        let linked =
            fs::symlink_metadata(root.join(rel)).is_ok_and(|meta| meta.file_type().is_symlink());
        if named || linked {
            return Err(decline(
                "a changed file may be rev_gate.py itself; Python answers",
            ));
        }
    }
    let max_lines = args.max_lines.unwrap_or(DEFAULT_MAX_LINES);
    let mechanical = mechanical_checks(&changes, max_lines);
    let extra = match args.findings_file.as_deref() {
        Some(path) => load_extra(&call.root, path, mechanical.len())?,
        None => Vec::new(),
    };
    let own = match args.per_target.as_deref() {
        Some(path) => load_per_target(&call.root, path, mechanical.len() + extra.len())?,
        None => Vec::new(),
    };
    if args.per_target.is_some() && own.is_empty() {
        return Err(decline(
            "an empty per-target object; Python's per_target is {}",
        ));
    }
    let swarm = resolve_swarm_dir(&root, env_value(&call.env, "SWARM_DIR"));
    check_per_target_keys(&swarm, task_id.as_deref(), &own).await?;

    let files = changes.len() as i64;
    let added: i64 = changes.values().map(|change| change.added).sum();
    let deleted: i64 = changes.values().map(|change| change.deleted).sum();
    let per_file: Vec<Json> = changes
        .iter()
        .map(|(file, change)| {
            Json::Obj(vec![
                ("file".into(), jstr(file)),
                ("added".into(), Json::Int(change.added)),
                ("deleted".into(), Json::Int(change.deleted)),
            ])
        })
        .collect();

    let semantic: Vec<Finding> = extra
        .iter()
        .cloned()
        .chain(own.iter().flat_map(|(_, items)| items.iter().cloned()))
        .collect();
    let llm = args.findings_file.is_some() || args.per_target.is_some();
    let rules = if mechanical.iter().any(Finding::blocking) {
        "fail"
    } else {
        "pass"
    };
    let semantic_run = if !llm {
        "skipped:no-llm-findings"
    } else if semantic.iter().any(Finding::blocking) {
        "fail"
    } else {
        "pass"
    };
    let runs: Fields = vec![
        ("rules".into(), jstr(rules)),
        ("semantic".into(), jstr(semantic_run)),
    ];
    let all: Vec<Finding> = mechanical.iter().chain(semantic.iter()).cloned().collect();
    let per_target: Vec<(String, Vec<Json>)> = match args.per_target {
        Some(_) => own
            .iter()
            .map(|(target, items)| {
                let mut list: Vec<Json> = mechanical
                    .iter()
                    .chain(extra.iter())
                    .map(Finding::json)
                    .collect();
                list.extend(items.iter().map(Finding::json));
                (target.clone(), list)
            })
            .collect(),
        None => Vec::new(),
    };
    let base_label = base.clone().unwrap_or_else(|| "whole-tree".to_string());
    let mode = if llm { "rules+semantic" } else { "rules-only" };

    let request = GateIssueRequest {
        gate: "review".into(),
        agent_id: "A09@local".into(),
        findings: all.iter().map(Finding::json).collect(),
        runs: runs.clone(),
        expires_s: EXPIRES_S,
        extra: vec![
            ("mode".into(), jstr(mode)),
            ("diff_base".into(), jstr(&base_label)),
            (
                "stats".into(),
                Json::Obj(vec![
                    ("files".into(), Json::Int(files)),
                    ("added".into(), Json::Int(added)),
                    ("deleted".into(), Json::Int(deleted)),
                ]),
            ),
        ],
        simulate: false,
        per_target,
        task_id: task_id.clone(),
        correlation_id: correlation_id.clone(),
        session: env_value(&call.env, "SWARM_AGENT_SESSION") == Some("1"),
        dryrun_fail: std::env::var("SWARM_DRYRUN_FAIL").unwrap_or_default(),
        now: now_secs(),
    };
    let keys =
        KeyMaterial::from_env().map_err(|_| decline("the signing configuration is Python's"))?;
    let mut ids = SystemIds::open()
        .map_err(|_| decline("the id source could not be opened; Python answers"))?;
    let plan = if request.task_id.is_none() {
        derive_gate_issue(&request, &StoreFacts::default(), &keys, &mut ids)
            .map_err(|Decline(reason)| decline(reason))?
    } else {
        let store = open_for_write(&WriterConfig::new(swarm.join("tasks.db")))
            .await
            .map_err(|error| {
                if error.write_state() == WriteState::NotStarted {
                    before_any_write(error.to_seam_error())
                } else {
                    error.to_seam_error()
                }
            })?;
        TaskStoreWriter::new(store)
            .issue_gate(&request, &keys, &mut ids)
            .await
            .map_err(|error| {
                if error.write_state() == WriteState::NotStarted {
                    before_any_write(error.to_seam_error())
                } else {
                    error.to_seam_error()
                }
            })?
    };
    if let Some(error) = &plan.error {
        write_plan_file(&swarm, &plan, committed(&request, &plan))?;
        return Err(error.clone());
    }
    write_plan_file(&swarm, &plan, committed(&request, &plan))?;
    let envelope = plan.envelope.clone().unwrap_or(Json::Obj(vec![]));
    let verdict = committed_verdict(&envelope)?;
    let mut recorded: Vec<String> = plan.recorded.iter().map(|(id, _)| id.clone()).collect();
    recorded.sort();
    let summary = format!(
        "review gate {} — {files} files, +{added}/-{deleted}, {} findings (base: {base_label})",
        verdict.to_uppercase(),
        all.len()
    );
    let status = if verdict == "pass" { "ok" } else { "fail" };
    let body = Json::Obj(vec![
        (
            "recorded".into(),
            Json::Arr(recorded.iter().map(|id| jstr(id)).collect()),
        ),
        ("status".into(), jstr(status)),
        ("verdict".into(), jstr(&verdict)),
        (
            "findings".into(),
            Json::Arr(all.iter().map(Finding::json).collect()),
        ),
        (
            "stats".into(),
            Json::Obj(vec![
                ("files".into(), Json::Int(files)),
                ("added".into(), Json::Int(added)),
                ("deleted".into(), Json::Int(deleted)),
                ("per_file".into(), Json::Arr(per_file)),
            ]),
        ),
        ("runs".into(), Json::Obj(runs)),
        ("diff_base".into(), jstr(&base_label)),
        ("envelope".into(), envelope),
        ("summary".into(), jstr(&summary)),
        ("agent".into(), jstr("A09")),
        ("script".into(), jstr("rev_gate")),
    ]);
    let exit_code = if verdict == "fail" || all.iter().any(Finding::blocking) {
        1
    } else {
        0
    };
    Ok(Reply {
        exit_code,
        stdout: format!("{}\n", to_python_json(&body)),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use gsd_taskwriter::{GateFile, GatePlan, GateWrite};

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn served_needs_json_and_the_known_flags_only() {
        assert!(served(&strings(&["--json"])));
        assert!(served(&strings(&[
            "--root",
            "r",
            "--diff-base=HEAD~2",
            "--max-lines",
            "40",
            "--json"
        ])));
        assert!(!served(&strings(&["--root", "r"])), "text mode is Python's");
        assert!(!served(&strings(&["--dry-run", "--json"])));
        assert!(!served(&strings(&["--json", "--json"])));
        assert!(!served(&strings(&["--diff-base", "-x", "--json"])));
        assert!(!served(&strings(&["--diff-base", "", "--json"])));
        assert!(!served(&strings(&["--max-lines", "+4", "--json"])));
        assert!(!served(&strings(&["--root", "a", "--root", "b", "--json"])));
    }

    #[test]
    fn splitlines_and_strip_follow_python_on_ascii() {
        assert_eq!(py_splitlines("a\nb\n"), vec!["a", "b"]);
        assert_eq!(py_splitlines("a\n\nb"), vec!["a", "", "b"]);
        assert_eq!(
            py_splitlines("a\x0bb\x0cc\x1cd\x1ee\x1ff"),
            vec!["a", "b", "c", "d", "e\x1ff"]
        );
        assert_eq!(py_strip("\x1c\x1fa b\x1d "), "a b");
        assert_eq!(translate_newlines("a\r\nb\rc"), "a\nb\nc");
    }

    #[test]
    fn suffix_follows_pathlib() {
        assert_eq!(py_suffix("src/a.py"), ".py");
        assert_eq!(py_suffix("src/.bashrc"), "");
        assert_eq!(py_suffix("src/a."), "");
        assert_eq!(py_suffix("src/a..py"), ".py");
        assert_eq!(py_suffix("sub/"), "");
    }

    #[test]
    fn test_paths_follow_test_re() {
        assert!(test_path("tests/x.py"));
        assert!(test_path("a/test/x.js"));
        assert!(test_path("src/x_test.go"));
        assert!(test_path("src/x.spec.ts"));
        assert!(test_path("pkg/test_mod.py"));
        assert!(!test_path("src/test_mod.txt"));
        assert!(!test_path("src/latest/x.py"));
        assert!(!test_path("test_.py"));
    }

    #[test]
    fn word_and_debug_matchers_respect_boundaries() {
        assert!(word_hit("x = 1  # TODO later", &["TODO"]));
        assert!(!word_hit("TODOS", &["TODO"]));
        assert!(!word_hit("aTODO", &["TODO"]));
        assert!(debug_hit("print_debugger = 1; debugger;"));
        assert!(debug_hit("debugger;"));
        assert!(!debug_hit("debugger x"));
        assert!(debug_hit("if x: dd(y)"));
        assert!(!debug_hit("abdd(y)"));
    }

    #[test]
    fn code_like_matches_comment_keywords_and_terminators() {
        assert!(code_like("  # import os"));
        assert!(code_like("// return x"));
        assert!(!code_like("  # importance"));
        assert!(code_like("# foo();"));
        assert!(!code_like("# a comment."));
        assert!(!code_like("x = 1;"));
    }

    #[test]
    fn secret_hits_use_leftmost_matches_and_the_entropy_gate() {
        let key = format!("AKIA{}", "ABCDEFGHIJKLMNOP");
        assert_eq!(secret_evidence(&key), vec!["AKIA…".to_string()]);
        assert!(secret_evidence(&format!("{key} example")).is_empty());
        let low = "password = 'aaaaaaaaaaaaaaaaaaaa'";
        assert!(
            secret_evidence(low).is_empty(),
            "entropy below 3.2 is skipped"
        );
        let high = "password = 'Zk3mQ9xL2vB7nR4tY8wE'";
        assert_eq!(secret_evidence(high), vec!["Zk3m…".to_string()]);
    }

    #[test]
    fn hunk_numbers_come_from_the_first_plus_digit_group() {
        assert_eq!(hunk_start("@@ -1,2 +7,3 @@ fn x+9").unwrap(), 7);
        assert_eq!(hunk_start("@@ -1 @@").unwrap(), 0);
    }

    fn write_state(error: &SeamError) -> Option<&str> {
        error.details().iter().find_map(|(key, value)| match value {
            Json::Str(state) if key == "write_state" => Some(state.as_str()),
            _ => None,
        })
    }

    fn plan(writes: Vec<GateWrite>) -> GatePlan {
        GatePlan {
            file: Some(GateFile {
                name: "T-1.review.json".into(),
                bytes: "{}".into(),
            }),
            writes,
            ..GatePlan::default()
        }
    }

    #[test]
    fn a_verdict_file_that_fails_after_the_commit_is_final() {
        let dir = tempfile::tempdir().expect("tempdir");
        // `verdicts` is a file, so the verdict file cannot be created under it.
        std::fs::write(dir.path().join("verdicts"), "not a directory").expect("collide");
        let committed = plan(vec![GateWrite::Notes {
            task_id: "T-1".into(),
            text: "{}".into(),
            updated_at: 1.0,
        }]);
        let error = write_plan_file(dir.path(), &committed, true).expect_err("the write fails");
        assert_eq!(write_state(&error), Some("final"));
        assert_eq!(error.code(), ErrorCode::Internal);
        assert!(
            error.message().contains("the verdict is recorded"),
            "{}",
            error.message()
        );
    }

    #[test]
    fn a_verdict_file_that_fails_without_a_commit_is_not_started() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(dir.path().join("verdicts"), "not a directory").expect("collide");
        let error =
            write_plan_file(dir.path(), &plan(Vec::new()), false).expect_err("the write fails");
        assert_eq!(write_state(&error), Some("not_started"));
        assert_eq!(error.code(), ErrorCode::Dep);
    }

    fn no_task_request() -> GateIssueRequest {
        GateIssueRequest {
            gate: "review".into(),
            agent_id: "A09".into(),
            findings: Vec::new(),
            runs: Vec::new(),
            expires_s: 3600,
            extra: Vec::new(),
            simulate: false,
            per_target: Vec::new(),
            task_id: None,
            correlation_id: None,
            session: false,
            dryrun_fail: String::new(),
            now: 1_700_000_000.0,
        }
    }

    #[test]
    fn a_no_task_verdict_file_failure_is_not_started() {
        let dir = tempfile::tempdir().expect("tempdir");
        // `verdicts` is a file, so the verdict file cannot be created under it.
        std::fs::write(dir.path().join("verdicts"), "not a directory").expect("collide");
        let derived = plan(vec![GateWrite::Notes {
            task_id: "T-1".into(),
            text: "{}".into(),
            updated_at: 1.0,
        }]);
        let commits = committed(&no_task_request(), &derived);
        assert!(!commits, "the no-task path writes no store rows");
        let error = write_plan_file(dir.path(), &derived, commits).expect_err("the write fails");
        assert_eq!(write_state(&error), Some("not_started"));
        assert_eq!(error.code(), ErrorCode::Dep);
    }

    #[test]
    fn a_missing_verdict_after_the_commit_is_final() {
        let no_verdict = Json::Obj(vec![("payload".into(), Json::Obj(Vec::new()))]);
        let error = committed_verdict(&no_verdict).expect_err("no verdict");
        assert_eq!(write_state(&error), Some("final"));
        let passed = Json::Obj(vec![(
            "payload".into(),
            Json::Obj(vec![("verdict".into(), jstr("pass"))]),
        )]);
        assert_eq!(committed_verdict(&passed).expect("verdict"), "pass");
    }
}
