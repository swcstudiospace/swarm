//! The host `sec_gate` route (phase 14.5, unit U3.6e).
//!
//! Ports `scripts/sec_gate.py`: the file walk, the scanner regexes, the four dependency audits, the allow list and the
//! signed `security` verdict. The Python script stays the oracle and the default. Anything this module cannot prove
//! identical to Python is `E-DEP` with `write_state = not_started`, and the selector runs the Python body.
//!
//! What the port reproduces, and what it declines:
//! - The walk is `Path.rglob("*")` on CPython 3.12, which runs on `Path.walk()`: a stack DFS that lists each walked
//!   directory's child directories as starting points. [`walk_order`] emits in that order.
//! - `read_text(encoding="utf-8")` translates `\r\n` and `\r` to `\n`, then `str.splitlines` splits on the ASCII
//!   separators. A scanned file holding valid non-ASCII UTF-8 declines the call: Python's Unicode `\w`, `\s`, `(?i)` and
//!   `isspace` tables are not reproduced. Invalid UTF-8 is skipped, as Python skips it.
//! - Python's `\s` on str matches `\t \n \v \f \r \x1c-\x1f` and space. Inside a line that is `[\t \x1f]`.
//! - `sum()` of floats in CPython 3.12 is Neumaier-compensated; [`entropy`] reproduces it bit for bit.
//! - Audit output is served only when it is ASCII without `\r`, and parses with `serde_json` into the shapes the Python
//!   branches accept. A timeout is served with `TimeoutExpired`'s exact text. Any other exception declines.
//! - `--dry-run`, text mode, a per-target file with more than one target or with a `--task-id` (keys are checked
//!   against the store), an allow list the expiry parser cannot prove, and a self-scan of `sec_gate.py` decline.

use std::collections::HashMap;
use std::ffi::OsString;
use std::fs;
use std::io::Read;
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use gsd_envelope::KeyMaterial;
use gsd_failure::ErrorCode;
use gsd_route::plan_derive::sha256_hex;
use gsd_route::{jstr, to_python_json, Json, Reply, SeamError};
use gsd_taskwriter::{
    derive_gate_issue, open_for_write, Decline, GateIssueRequest, StoreFacts, SystemIds,
    TaskStoreWriter, WriterConfig,
};
use regex::Regex;
use serde_json::Value;

use crate::strangler_seam::{
    after_commit, before_any_write, canonical_or, env_value, resolve_swarm_dir, Invocation,
};

const SEVERITIES: &[&str] = &["info", "minor", "major", "critical", "blocker"];
const CODE_EXTS: &[&str] = &[
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs",
    ".vue", ".svelte",
];
const IAC_EXTS: &[&str] = &[".tf", ".yaml", ".yml"];
const PY_EXTS: &[&str] = &[".py"];
const JS_EXTS: &[&str] = &[".js", ".ts", ".tsx", ".jsx", ".vue", ".svelte"];
const SKIP_DIRS: &[&str] = &[
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".swarm",
];
const MAX_SCAN_BYTES: u64 = 2_000_000;
const KINDS: [&str; 3] = ["secrets", "sast", "iac"];

/// `(name, manifest globs, binary for shutil.which, argv run)`, in Python's `AUDITS` order.
const AUDITS: [(&str, &[&str], &str, &[&str]); 4] = [
    (
        "pip-audit",
        &["requirements*.txt", "pyproject.toml"],
        "pip-audit",
        &["pip-audit", "--format", "json", "--progress-spinner", "off"],
    ),
    (
        "npm-audit",
        &["package-lock.json", "package.json"],
        "npm",
        &["npm", "audit", "--json"],
    ),
    (
        "cargo-audit",
        &["Cargo.lock"],
        "cargo-audit",
        &["cargo", "audit", "--json"],
    ),
    (
        "govulncheck",
        &["go.mod"],
        "govulncheck",
        &["govulncheck", "-json", "./..."],
    ),
];

/// True when `argv` is the grammar this route serves. `--json` is required so Python's text mode stays Python's.
pub fn served(argv: &[String]) -> bool {
    parse(argv).is_some()
}

struct Args {
    root: Option<String>,
    timeout: u64,
    strict: bool,
    allow_list: Option<String>,
    per_target: Option<String>,
    task_id: Option<String>,
    correlation_id: Option<String>,
}

fn parse(argv: &[String]) -> Option<Args> {
    let mut out = Args {
        root: None,
        timeout: 600,
        strict: false,
        allow_list: None,
        per_target: None,
        task_id: None,
        correlation_id: None,
    };
    let mut json = false;
    let mut index = 0;
    while index < argv.len() {
        let token = argv[index].as_str();
        index += 1;
        match token {
            "--json" => {
                json = true;
                continue;
            }
            "--strict" => {
                out.strict = true;
                continue;
            }
            "--dry-run" => return None,
            _ => {}
        }
        let (flag, inline) = match token.split_once('=') {
            Some((flag, value)) => (flag, Some(value.to_string())),
            None => (token, None),
        };
        if !matches!(
            flag,
            "--root"
                | "--allow-list"
                | "--timeout"
                | "--per-target-findings"
                | "--task-id"
                | "--correlation-id"
        ) {
            return None;
        }
        let value = match inline {
            Some(value) => value,
            None => {
                let next = argv.get(index)?;
                if next.starts_with('-') {
                    return None;
                }
                index += 1;
                next.clone()
            }
        };
        match flag {
            "--root" => out.root = Some(value),
            "--allow-list" => out.allow_list = Some(value),
            "--per-target-findings" => out.per_target = Some(value),
            "--task-id" => out.task_id = Some(value),
            "--correlation-id" => out.correlation_id = Some(value),
            "--timeout" => {
                // Python's int() also takes signs, spaces, underscores and non-ASCII digits: only plain digits here.
                if value.is_empty() || !value.bytes().all(|b| b.is_ascii_digit()) {
                    return None;
                }
                let seconds: u64 = value.parse().ok()?;
                if seconds == 0 {
                    return None;
                }
                out.timeout = seconds;
            }
            _ => return None,
        }
    }
    json.then_some(out)
}

fn decline(reason: &str) -> SeamError {
    before_any_write(SeamError::new(ErrorCode::Dep, reason))
}

/// `check_task_id`: `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` fully matched, and no `..`.
fn valid_task_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes[0].is_ascii_alphanumeric()
        && bytes[1..]
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
        && !value.contains("..")
}

/// Python's `str.strip()` on ASCII text.
fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| {
        matches!(
            c,
            '\t' | '\n' | '\x0b' | '\x0c' | '\r' | '\x1c'..='\x1f' | ' '
        )
    })
}

/// Python's `str.splitlines()` on text whose `\r` has already been translated away.
fn py_splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    for (index, byte) in text.bytes().enumerate() {
        if matches!(byte, b'\n' | 0x0b | 0x0c | 0x1c | 0x1d | 0x1e | b'\r') {
            lines.push(&text[start..index]);
            start = index + 1;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// Python's `Path.suffix` for one file name.
fn py_suffix(name: &str) -> &str {
    match name.rfind('.') {
        Some(index) if index > 0 && index < name.len() - 1 => &name[index..],
        _ => "",
    }
}

/// `_entropy` of `sec_gate.py`: Counter values in first-occurrence order, summed with CPython 3.12's compensated `sum`.
fn entropy(text: &str) -> f64 {
    let n = text.len() as f64;
    let mut counts: Vec<(u8, usize)> = Vec::new();
    for &byte in text.as_bytes() {
        match counts.iter_mut().find(|(seen, _)| *seen == byte) {
            Some(slot) => slot.1 += 1,
            None => counts.push((byte, 1)),
        }
    }
    let mut sum = 0.0_f64;
    let mut compensation = 0.0_f64;
    for (index, (_, count)) in counts.iter().enumerate() {
        let p = *count as f64 / n;
        let term = p * p.log2();
        if index == 0 {
            sum = 0.0 + term;
            continue;
        }
        let total = sum + term;
        if sum.abs() >= term.abs() {
            compensation += (sum - total) + term;
        } else {
            compensation += (term - total) + sum;
        }
        sum = total;
    }
    if compensation != 0.0 && compensation.is_finite() {
        sum += compensation;
    }
    -sum
}

/// `"SEC-" + sha1("|".join(parts))[:8]`.
fn fid(parts: &[&str]) -> String {
    format!("SEC-{}", &sha1_hex(parts.join("|").as_bytes())[..8])
}

fn sha1_hex(data: &[u8]) -> String {
    let mut h: [u32; 5] = [
        0x6745_2301,
        0xEFCD_AB89,
        0x98BA_DCFE,
        0x1032_5476,
        0xC3D2_E1F0,
    ];
    let mut message = data.to_vec();
    let bit_len = (data.len() as u64).wrapping_mul(8);
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bit_len.to_be_bytes());
    for chunk in message.chunks(64) {
        let mut w = [0u32; 80];
        for (i, word) in w.iter_mut().enumerate().take(16) {
            *word = u32::from_be_bytes([
                chunk[4 * i],
                chunk[4 * i + 1],
                chunk[4 * i + 2],
                chunk[4 * i + 3],
            ]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, word) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | (!b & d), 0x5A82_7999),
                20..=39 => (b ^ c ^ d, 0x6ED9_EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1B_BCDC),
                _ => (b ^ c ^ d, 0xCA62_C1D6),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(*word);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        for (slot, value) in h.iter_mut().zip([a, b, c, d, e]) {
            *slot = slot.wrapping_add(value);
        }
    }
    h.iter().map(|word| format!("{word:08x}")).collect()
}

/// One finding as `make_finding` builds it. `owner`, `location` and `ac_ref` are Python `None` when absent.
#[derive(Clone)]
struct Finding {
    id: String,
    severity: String,
    kind: String,
    summary: String,
    evidence: String,
    ac_ref: Option<String>,
    owner: Option<String>,
    location: Option<String>,
    justification: Option<String>,
}

impl Finding {
    /// `make_finding(id, severity, kind, summary, evidence=...)` with the default `None` members.
    fn plain(id: String, severity: &str, kind: &str, summary: String, evidence: String) -> Self {
        Self {
            id,
            severity: severity.into(),
            kind: kind.into(),
            summary,
            evidence,
            ac_ref: None,
            owner: None,
            location: None,
            justification: None,
        }
    }

    fn to_json(&self) -> Json {
        let mut fields = vec![
            ("id".to_string(), jstr(&self.id)),
            ("severity".to_string(), jstr(&self.severity)),
            ("kind".to_string(), jstr(&self.kind)),
            ("summary".to_string(), jstr(&self.summary)),
            ("evidence".to_string(), jstr(&self.evidence)),
            ("ac_ref".to_string(), opt_json(self.ac_ref.as_deref())),
            (
                "owner_suggestion".to_string(),
                opt_json(self.owner.as_deref()),
            ),
            ("location".to_string(), opt_json(self.location.as_deref())),
        ];
        if let Some(justification) = &self.justification {
            fields.push(("justification".to_string(), jstr(justification)));
        }
        Json::Obj(fields)
    }
}

fn opt_json(value: Option<&str>) -> Json {
    value.map_or(Json::Null, jstr)
}

struct Patterns {
    secrets: Vec<(&'static str, Regex)>,
    placeholder: Regex,
    sast: Vec<(&'static str, Regex, &'static [&'static str], &'static str)>,
    yaml_call: Regex,
    yaml_loader: Regex,
    iac: Vec<(&'static str, Regex, &'static str)>,
    comment: Regex,
}

fn compile(pattern: &str) -> Regex {
    // The patterns are fixed; a failure here is a programming error in this file, caught by the unit tests.
    Regex::new(pattern).expect("sec_gate pattern compiles")
}

/// The scanner regexes. `\s` is written `[\t \x1f]` and `\w`, `\b` are the ASCII forms (the scanned text is ASCII).
/// The Python PLACEHOLDER alternative `…` cannot match ASCII text and is dropped.
fn patterns() -> &'static Patterns {
    static PATTERNS: LazyLock<Patterns> = LazyLock::new(|| {
        Patterns {
        secrets: [
            ("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b"),
            (
                "github-token",
                r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})",
            ),
            ("slack-token", r"\bxox[baprs]-[0-9A-Za-z-]{10,}"),
            ("stripe-live-key", r"\bsk_live_[0-9a-zA-Z]{24,}"),
            ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
            (
                "private-key",
                r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY( BLOCK)?-----",
            ),
            (
                "jwt",
                r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
            ),
            (
                "aws-secret-key",
                r#"(?i)aws_secret_access_key[\t \x1f]*[:=][\t \x1f]*['"]?([A-Za-z0-9/+=]{40})"#,
            ),
            (
                "generic-credential",
                r#"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd)\b[\t \x1f]*[:=][\t \x1f]*['"]([^'"\t \x1f]{16,})['"]"#,
            ),
        ]
        .into_iter()
        .map(|(name, pattern)| (name, compile(pattern)))
        .collect(),
        placeholder: compile(
            r"(?i)example|placeholder|xxx|changeme|dummy|redacted|<[^>]*>|\$\{|\{\{|your[_-]|\.\.\.|\*{3}|sample|fake",
        ),
        sast: vec![
            (
                "py.eval-exec",
                compile(r#"\b(eval|exec)\([\t \x1f]*[^'")\t \x1f]"#),
                PY_EXTS,
                "major",
            ),
            (
                "py.shell-injection",
                compile(
                    r#"(subprocess\.\w+|os\.system|os\.popen)\(.*(f['"]|%[\t \x1f]*\(|\.format\(|\+[\t \x1f]*\w).*shell[\t \x1f]*=[\t \x1f]*True|(os\.system|os\.popen)\([\t \x1f]*(f['"]|\w+[\t \x1f]*\+)"#,
                ),
                PY_EXTS,
                "major",
            ),
            ("py.pickle-load", compile(r"\bpickle\.loads?\("), PY_EXTS, "major"),
            // Two-step in `yaml_unsafe`: the Python negative look-ahead cannot be expressed here.
            ("py.yaml-unsafe-load", compile(r"\byaml\.load\("), PY_EXTS, "major"),
            (
                "js.eval",
                compile(r#"\beval\([\t \x1f]*[^'")\t \x1f]|new Function\("#),
                JS_EXTS,
                "major",
            ),
            (
                "js.inner-html",
                compile(r"\.innerHTML[\t \x1f]*=|dangerouslySetInnerHTML[\t \x1f]*="),
                JS_EXTS,
                "major",
            ),
            (
                "sql.string-build",
                compile(
                    r#"(?i)(f['"][^'"]*\b(select|insert|update|delete)\b[^'"]*\{|['"][\t \x1f]*\b(select|insert|update|delete)\b[^'"]*['"][\t \x1f]*(\+|%|\|\|)[\t \x1f]*\w|\.(execute|query|raw)\([\t \x1f]*(f['"]|`[^`]*\$\{|['"][^'"]*['"][\t \x1f]*(\+|%)))"#,
                ),
                CODE_EXTS,
                "major",
            ),
        ],
        yaml_call: compile(r"\byaml\.load\("),
        yaml_loader: compile(r"Loader[\t \x1f]*="),
        iac: vec![
            ("iac.open-ingress", compile(r"0\.0\.0\.0/0|::/0"), "major"),
            (
                "iac.privileged",
                compile(r"privileged[\t \x1f]*[:=][\t \x1f]*true"),
                "major",
            ),
            (
                "iac.host-network",
                compile(r"host(Network|PID|IPC)[\t \x1f]*[:=][\t \x1f]*true"),
                "major",
            ),
            (
                "iac.priv-escalation",
                compile(
                    r"allowPrivilegeEscalation[\t \x1f]*:[\t \x1f]*true|runAsUser[\t \x1f]*:[\t \x1f]*0\b",
                ),
                "minor",
            ),
        ],
        comment: compile(r"^[\t \x1f]*(#|//|/\*|\*|--)"),
    }
    });
    &PATTERNS
}

/// `py.yaml-unsafe-load`: `\byaml\.load\((?![^)]*Loader\s*=)` matches when some `yaml.load(` occurrence has no
/// `Loader =` before the next `)`. Every occurrence is tried, as Python's `search` does.
fn yaml_unsafe(patterns: &Patterns, line: &str) -> bool {
    patterns.yaml_call.find_iter(line).any(|found| {
        let rest = &line[found.end()..];
        let before_close = rest.find(')').map_or(rest, |index| &rest[..index]);
        !patterns.yaml_loader.is_match(before_close)
    })
}

#[derive(Default)]
struct Scan {
    findings: Vec<Finding>,
    /// `seen` of `scan_files`: raw hits per kind, before the allow list.
    seen: [i64; 3],
}

impl Scan {
    fn add(
        &mut self,
        kind: &str,
        (rule, severity): (&str, &str),
        summary: &str,
        loc: &str,
        evidence: String,
        cwe: &str,
    ) {
        let slot = KINDS.iter().position(|item| *item == kind).unwrap_or(0);
        self.seen[slot] += 1;
        self.findings.push(Finding {
            id: fid(&[kind, rule, loc]),
            severity: severity.into(),
            kind: kind.into(),
            summary: format!("{rule}: {summary}"),
            evidence,
            ac_ref: Some(cwe.into()),
            owner: Some("A05".into()),
            location: Some(loc.into()),
            justification: None,
        });
    }
}

fn is_alpha(text: &str) -> bool {
    !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_alphabetic())
}

fn scan_line(patterns: &Patterns, scan: &mut Scan, ext: &str, rel: &str, no: usize, line: &str) {
    let loc = format!("{rel}:{no}");
    for (name, regex) in &patterns.secrets {
        let Some(captures) = regex.captures(line) else {
            continue;
        };
        let val = if *name == "generic-credential" || *name == "aws-secret-key" {
            captures.get(captures.len() - 1).map_or("", |m| m.as_str())
        } else {
            captures.get(0).map_or("", |m| m.as_str())
        };
        // The placeholder test runs on the captured value. A placeholder word elsewhere on the line, such as a
        // comment that says `example`, does not make a real token a placeholder.
        if patterns.placeholder.is_match(val) {
            continue;
        }
        if *name == "generic-credential" && (entropy(val) < 3.2 || is_alpha(val)) {
            continue;
        }
        scan.add(
            "secrets",
            (name, "critical"),
            "credential material committed (value redacted)",
            &loc,
            format!("value not recorded ({} chars)", val.chars().count()),
            "CWE-798",
        );
    }
    if CODE_EXTS.contains(&ext) && !patterns.comment.is_match(line) {
        for (name, regex, exts, severity) in &patterns.sast {
            if !exts.contains(&ext) {
                continue;
            }
            let hit = if *name == "py.yaml-unsafe-load" {
                yaml_unsafe(patterns, line)
            } else {
                regex.is_match(line)
            };
            if !hit {
                continue;
            }
            let mut severity = *severity;
            if *name == "sql.string-build" && (line.contains('?') || line.contains("%s")) {
                severity = "minor";
            }
            let cwe = match *name {
                "sql.string-build" => "CWE-89",
                "js.inner-html" => "CWE-79",
                "py.pickle-load" | "py.yaml-unsafe-load" => "CWE-502",
                _ => "CWE-94",
            };
            scan.add(
                "sast",
                (name, severity),
                "dangerous pattern",
                &loc,
                py_strip(line).chars().take(160).collect(),
                cwe,
            );
        }
    }
    if IAC_EXTS.contains(&ext) && !patterns.comment.is_match(line) {
        for (name, regex, severity) in &patterns.iac {
            if regex.is_match(line) {
                scan.add(
                    "iac",
                    (name, severity),
                    "insecure infrastructure setting",
                    &loc,
                    py_strip(line).chars().take(160).collect(),
                    "CWE-284",
                );
            }
        }
    }
}

/// One directory listing as `scandir` returns it: entry name and whether it is a real directory (not a symlink).
/// `Ok(None)` when the directory cannot be opened, which Python's walk and selector skip silently.
fn list_dir(dir: &Path) -> Result<Option<Vec<(OsString, bool)>>, SeamError> {
    let Ok(read) = fs::read_dir(dir) else {
        return Ok(None);
    };
    let mut entries = Vec::new();
    for entry in read {
        let entry =
            entry.map_err(|_| decline("a directory entry could not be read; Python answers"))?;
        let is_dir = entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false);
        entries.push((entry.file_name(), is_dir));
    }
    Ok(Some(entries))
}

fn is_skip_name(name: &std::ffi::OsStr) -> bool {
    name.to_str().is_some_and(|text| SKIP_DIRS.contains(&text))
}

/// The emission order of `root.rglob("*")` on CPython 3.12.
///
/// `_iterate_directories` yields the root, then the child directories of each directory `Path.walk()` yields, in walk
/// order. The wildcard selector lists each of those starting points in `scandir` order. Walk order is a stack DFS that
/// pushes children reversed. Directories named in [`SKIP_DIRS`] are not descended: every path under them is filtered.
fn walk_order(root: &Path) -> Result<Vec<PathBuf>, SeamError> {
    let mut out = Vec::new();
    if !root.is_dir() {
        return Ok(out);
    }
    if let Some(entries) = list_dir(root)? {
        out.extend(entries.iter().map(|(name, _)| root.join(name)));
    }
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Some(entries) = list_dir(&dir)? else {
            continue;
        };
        let dirnames: Vec<OsString> = entries
            .iter()
            .filter(|(name, is_dir)| *is_dir && !is_skip_name(name))
            .map(|(name, _)| name.clone())
            .collect();
        for name in &dirnames {
            let child = dir.join(name);
            if let Some(children) = list_dir(&child)? {
                out.extend(children.iter().map(|(entry, _)| child.join(entry)));
            }
        }
        for name in dirnames.iter().rev() {
            stack.push(dir.join(name));
        }
    }
    Ok(out)
}

/// `scan_files`: the secrets, SAST and IAC findings of every scanned file, in Python's order.
fn scan(root: &Path) -> Result<Scan, SeamError> {
    let mut scan = Scan::default();
    if root.components().any(|part| match part {
        Component::Normal(name) => is_skip_name(name),
        _ => false,
    }) {
        // `iter_files` tests the absolute parts, so a root under such a directory yields no file at all.
        return Ok(scan);
    }
    let patterns = patterns();
    for path in walk_order(root)? {
        if path.components().any(|part| match part {
            Component::Normal(name) => is_skip_name(name),
            _ => false,
        }) {
            continue;
        }
        let Ok(meta) = fs::metadata(&path) else {
            continue;
        };
        if !meta.is_file() {
            continue;
        }
        let resolved = fs::canonicalize(&path)
            .map_err(|_| decline("a path could not be resolved; Python answers"))?;
        if resolved.file_name().and_then(|name| name.to_str()) == Some("sec_gate.py") {
            return Err(decline(
                "a file named sec_gate.py may be the script itself; Python answers",
            ));
        }
        let relative = path
            .strip_prefix(root)
            .map_err(|_| decline("a scanned path is outside the root; Python answers"))?;
        let mut parts = Vec::new();
        for part in relative.components() {
            let Component::Normal(name) = part else {
                return Err(decline("an unexpected path component; Python answers"));
            };
            let name = name
                .to_str()
                .ok_or_else(|| decline("a file name is not UTF-8; Python answers"))?;
            parts.push(name);
        }
        let rel = parts.join("/");
        // An in-scope input over the cap is not read. It is one major `scan-incomplete` finding (AGENTS.md).
        if meta.len() > MAX_SCAN_BYTES {
            let mut finding = Finding::plain(
                fid(&["secrets", "scan-incomplete", rel.as_str()]),
                "major",
                "secrets",
                format!(
                    "scan-incomplete: scan incomplete, status is fail until every in-scope input is analysed: {rel} is {} bytes, over the {MAX_SCAN_BYTES}-byte scan cap",
                    meta.len()
                ),
                format!("{} bytes exceeds the {MAX_SCAN_BYTES}-byte scan cap; not analysed", meta.len()),
            );
            finding.location = Some(rel.clone());
            scan.findings.push(finding);
            continue;
        }
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(text) = std::str::from_utf8(&bytes) else {
            continue;
        };
        if !text.is_ascii() {
            return Err(decline(
                "a scanned file holds non-ASCII text; Python answers",
            ));
        }
        let ext = py_suffix(parts.last().copied().unwrap_or(""));
        let translated = text.replace("\r\n", "\n").replace('\r', "\n");
        for (index, line) in py_splitlines(&translated).into_iter().enumerate() {
            scan_line(patterns, &mut scan, ext, &rel, index + 1, line);
        }
    }
    Ok(scan)
}

/// `shutil.which`: some PATH entry holds a non-directory with an execute bit. Python's `os.access` check is the
/// real-uid form of the same test: a positive here is confirmed by the spawn, and a spawn that fails declines.
fn which(binary: &str) -> Result<bool, SeamError> {
    let path = std::env::var_os("PATH")
        .filter(|value| !value.is_empty())
        .ok_or_else(|| decline("PATH is unset or empty; Python answers"))?;
    let path = path
        .to_str()
        .ok_or_else(|| decline("PATH is not UTF-8; Python answers"))?;
    Ok(path.split(':').any(|dir| {
        fs::metadata(Path::new(dir).join(binary))
            .map(|meta| !meta.is_dir() && meta.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
    }))
}

/// `manifest` glob match of `dep_audit`: a literal name must exist (`Path.exists`, following links); a name with one
/// `*` matches any entry of the root by fnmatch.
fn manifest_present(root: &Path, glob: &str) -> bool {
    match glob.split_once('*') {
        None => root.join(glob).exists(),
        Some((prefix, suffix)) => list_dir(root).ok().flatten().is_some_and(|entries| {
            entries.iter().any(|(name, _)| {
                name.to_str().is_some_and(|text| {
                    text.len() >= prefix.len() + suffix.len()
                        && text.starts_with(prefix)
                        && text.ends_with(suffix)
                })
            })
        }),
    }
}

enum AuditRun {
    Done {
        code: Option<i32>,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
    },
    TimedOut,
}

/// `subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=t)`: inherited environment and stdin, both
/// pipes read while the child runs, and a direct-child kill at the deadline.
fn run_audit(argv: &[&str], cwd: &Path, timeout: u64) -> Result<AuditRun, SeamError> {
    let mut child = Command::new(argv[0])
        .args(&argv[1..])
        .current_dir(cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|_| decline("an audit tool could not be started; Python answers"))?;
    let mut out_pipe = child
        .stdout
        .take()
        .ok_or_else(|| decline("no stdout pipe"))?;
    let mut err_pipe = child
        .stderr
        .take()
        .ok_or_else(|| decline("no stderr pipe"))?;
    let out_reader = std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = out_pipe.read_to_end(&mut buffer);
        buffer
    });
    let err_reader = std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = err_pipe.read_to_end(&mut buffer);
        buffer
    });
    let deadline = Instant::now() + Duration::from_secs(timeout);
    let mut status = None;
    loop {
        if status.is_none() {
            status = child
                .try_wait()
                .map_err(|_| decline("an audit tool could not be waited; Python answers"))?;
        }
        if status.is_some() && out_reader.is_finished() && err_reader.is_finished() {
            break;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            // The reader threads are left to finish on their own, as Python leaves a grandchild's open pipe.
            return Ok(AuditRun::TimedOut);
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    Ok(AuditRun::Done {
        code: status.and_then(|value| value.code()),
        stdout: out_reader.join().unwrap_or_default(),
        stderr: err_reader.join().unwrap_or_default(),
    })
}

/// `json.loads` branches of `dep_audit`, read with the shapes the Python branches accept. Any other shape is an
/// exception in Python (uncaught, exit 2), so it declines here.
fn audit_outcome(
    name: &str,
    out: &str,
    nonzero: bool,
) -> Result<(&'static str, Vec<Finding>), SeamError> {
    let stripped = py_strip(out);
    let data: Option<Value> = if stripped.starts_with('{') || stripped.starts_with('[') {
        match serde_json::from_str::<Value>(out) {
            Ok(value) => Some(value),
            // Both parsers reject the text: `data` is None, as Python's JSONDecodeError makes it.
            Err(_) if python_json_rejects(out) => None,
            Err(_) => {
                return Err(decline(
                    "the audit output may be JSON Python accepts; Python answers",
                ))
            }
        }
    } else {
        None
    };
    let data_truthy = match &data {
        Some(Value::Object(map)) => !map.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        _ => false,
    };
    let mut crit: i64 = 0;
    let mut high: i64 = 0;
    let mut other: i64 = 0;
    let mut kev: i64 = 0;
    if name == "npm-audit" && data_truthy {
        let top = object_of(data.as_ref())?;
        let metadata = object_or_empty(top.get("metadata"))?;
        let vulnerabilities = object_or_empty(metadata.get("vulnerabilities"))?;
        crit = int_field(vulnerabilities, "critical")?;
        high = int_field(vulnerabilities, "high")?;
        other = int_field(vulnerabilities, "moderate")?
            .checked_add(int_field(vulnerabilities, "low")?)
            .ok_or_else(|| decline("an audit count overflows; Python answers"))?;
    } else if name == "pip-audit" && data_truthy {
        let items = match data.as_ref() {
            Some(Value::Object(top)) => match top.get("dependencies") {
                Some(Value::Array(items)) => items,
                _ => {
                    return Err(decline(
                        "pip-audit output has no dependency list; Python answers",
                    ))
                }
            },
            Some(Value::Array(items)) => items,
            _ => {
                return Err(decline(
                    "pip-audit output has an unexpected shape; Python answers",
                ))
            }
        };
        for dependency in items {
            let Value::Object(dependency) = dependency else {
                return Err(decline(
                    "a pip-audit dependency is not an object; Python answers",
                ));
            };
            let vulns = match dependency.get("vulns") {
                None => &[][..],
                Some(Value::Array(vulns)) => vulns.as_slice(),
                Some(_) => return Err(decline("pip-audit vulns is not a list; Python answers")),
            };
            for vuln in vulns {
                high += 1;
                if has_kev_word(vuln) {
                    kev += 1;
                }
            }
        }
    } else if name == "cargo-audit" && data_truthy {
        let top = object_of(data.as_ref())?;
        let vulnerabilities = object_or_empty(top.get("vulnerabilities"))?;
        crit = int_field(vulnerabilities, "count")?;
    } else if name == "govulncheck" {
        high = py_splitlines(out)
            .iter()
            .filter(|line| line.contains("\"finding\"") && line.contains("\"trace\""))
            .count() as i64;
    }
    if nonzero && crit == 0 && high == 0 && other == 0 {
        // A failed audit with no counted vulnerability did not complete, even when its output parsed.
        high = 1;
    }
    let state = if crit != 0 || high != 0 || other != 0 {
        "fail"
    } else {
        "pass"
    };
    let evidence = tail(out, 600);
    let mut findings = Vec::new();
    if kev != 0 || crit != 0 {
        let (severity, suffix) = if kev != 0 {
            ("blocker", " / KEV-listed")
        } else {
            ("critical", "")
        };
        findings.push(Finding {
            evidence,
            ..Finding::plain(
                fid(&["deps", name, "critical"]),
                severity,
                "deps",
                format!("{name}: {crit} critical{suffix} vulnerabilities"),
                String::new(),
            )
        });
    } else if high != 0 {
        findings.push(Finding::plain(
            fid(&["deps", name, "high"]),
            "major",
            "deps",
            format!("{name}: {high} high vulnerabilities"),
            evidence,
        ));
    } else if other != 0 {
        findings.push(Finding::plain(
            fid(&["deps", name, "other"]),
            "minor",
            "deps",
            format!("{name}: {other} moderate/low advisories"),
            evidence,
        ));
    }
    Ok((state, findings))
}

/// True when Python's `json.loads` also rejects text serde_json rejects. The text must hold none of the forms Python
/// accepts that serde does not: `NaN`, `Infinity`, escapes (lone surrogates), a number with an exponent (overflow to
/// `inf`), or nesting past serde's limit. Otherwise a serde failure is a Python failure too.
fn python_json_rejects(text: &str) -> bool {
    let exponent = text
        .as_bytes()
        .windows(2)
        .any(|pair| pair[0].is_ascii_digit() && matches!(pair[1], b'e' | b'E'));
    let nesting = text
        .bytes()
        .filter(|byte| matches!(byte, b'[' | b'{'))
        .count();
    !(text.contains("NaN")
        || text.contains("Infinity")
        || text.contains('\\')
        || exponent
        || nesting > 100)
}

fn tail(text: &str, chars: usize) -> String {
    let count = text.chars().count();
    text.chars().skip(count.saturating_sub(chars)).collect()
}

/// `dumps(vuln).lower()` contains `kev` or `exploited`. For ASCII input every such match lies inside one key or string
/// of the value, because the JSON escapes never complete one of those words.
fn has_kev_word(value: &Value) -> bool {
    let word = |text: &str| {
        let lower = text.to_ascii_lowercase();
        lower.contains("kev") || lower.contains("exploited")
    };
    match value {
        Value::String(text) => word(text),
        Value::Array(items) => items.iter().any(has_kev_word),
        Value::Object(map) => map
            .iter()
            .any(|(key, item)| word(key) || has_kev_word(item)),
        _ => false,
    }
}

fn object_of(value: Option<&Value>) -> Result<&serde_json::Map<String, Value>, SeamError> {
    match value {
        Some(Value::Object(map)) => Ok(map),
        _ => Err(decline(
            "an audit document is not an object; Python answers",
        )),
    }
}

fn object_or_empty(value: Option<&Value>) -> Result<&serde_json::Map<String, Value>, SeamError> {
    static EMPTY: LazyLock<serde_json::Map<String, Value>> = LazyLock::new(serde_json::Map::new);
    match value {
        None => Ok(&EMPTY),
        Some(Value::Object(map)) => Ok(map),
        Some(_) => Err(decline("an audit field is not an object; Python answers")),
    }
}

fn int_field(map: &serde_json::Map<String, Value>, key: &str) -> Result<i64, SeamError> {
    match map.get(key) {
        None => Ok(0),
        Some(Value::Number(number)) => number
            .as_i64()
            .ok_or_else(|| decline("an audit count is not an integer; Python answers")),
        Some(_) => Err(decline("an audit count is not a number; Python answers")),
    }
}

type DependencyAuditResults = (Vec<(String, String)>, Vec<Finding>);

/// `dep_audit`: one audit per manifest that exists. Returns the `runs` entries and the findings in Python's order.
fn dep_audits(root: &Path, timeout: u64) -> Result<DependencyAuditResults, SeamError> {
    let mut runs = Vec::new();
    let mut findings = Vec::new();
    for (name, globs, binary, argv) in AUDITS {
        if !globs.iter().any(|glob| manifest_present(root, glob)) {
            continue;
        }
        if !which(binary)? {
            runs.push((name.to_string(), "skipped:tool-missing".to_string()));
            continue;
        }
        match run_audit(argv, root, timeout)? {
            AuditRun::TimedOut => {
                runs.push((name.to_string(), "fail".to_string()));
                let repr = format!(
                    "[{}]",
                    argv.iter()
                        .map(|arg| format!("'{arg}'"))
                        .collect::<Vec<_>>()
                        .join(", ")
                );
                findings.push(Finding::plain(
                    fid(&["deps", name, "run"]),
                    "major",
                    "deps",
                    format!(
                        "{name} did not complete: Command '{repr}' timed out after {timeout} seconds"
                    ),
                    String::new(),
                ));
            }
            AuditRun::Done {
                code,
                stdout,
                stderr,
            } => {
                let out = String::from_utf8(stdout)
                    .map_err(|_| decline("audit output is not UTF-8; Python answers"))?;
                if !out.is_ascii() || out.contains('\r') {
                    return Err(decline(
                        "audit output is outside ASCII or has a carriage return; Python answers",
                    ));
                }
                if !stderr.is_ascii() {
                    return Err(decline("audit stderr is not ASCII; Python answers"));
                }
                let (state, found) = audit_outcome(name, &out, code != Some(0))?;
                runs.push((name.to_string(), state.to_string()));
                findings.extend(found);
            }
        }
    }
    Ok((runs, findings))
}

fn today_utc() -> (i64, u32, u32) {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0);
    civil_from_days(seconds.div_euclid(86_400))
}

/// Howard Hinnant's `civil_from_days`: the proleptic Gregorian date of a day count since 1970-01-01.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

/// `time.strptime(text, "%Y-%m-%d")` for the strict form only: four, two and two ASCII digits, a real date, year 1 or
/// later. Anything else declines, because Python's `%m` and `%d` also take one digit and a leading space.
fn parse_iso_date(text: &str) -> Option<(i64, u32, u32)> {
    let bytes = text.as_bytes();
    if bytes.len() != 10 || bytes[4] != b'-' || bytes[7] != b'-' {
        return None;
    }
    let digits = |range: std::ops::Range<usize>| -> Option<u32> {
        let slice = &bytes[range];
        if slice.iter().all(u8::is_ascii_digit) {
            std::str::from_utf8(slice).ok()?.parse().ok()
        } else {
            None
        }
    };
    let year = i64::from(digits(0..4)?);
    let month = digits(5..7)?;
    let day = digits(8..10)?;
    if year < 1 || !(1..=12).contains(&month) {
        return None;
    }
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let days = match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    (1..=days).contains(&day).then_some((year, month, day))
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

fn read_json(path: &str, what: &str) -> Result<Value, SeamError> {
    let bytes = fs::read(path)
        .map_err(|_| decline(&format!("{what} could not be read; Python answers")))?;
    let text = std::str::from_utf8(&bytes)
        .map_err(|_| decline(&format!("{what} is not UTF-8; Python answers")))?;
    if !text.is_ascii() {
        return Err(decline(&format!("{what} is outside ASCII; Python answers")));
    }
    serde_json::from_str(text).map_err(|_| {
        decline(&format!(
            "{what} is not JSON this route proves; Python answers"
        ))
    })
}

/// `apply_allow_list`: the entries are an array, or an object whose `allow` is one. Expired entries (date not after
/// today, UTC) are skipped; an entry without a justification is Python's E-POLICY and declines.
fn load_allow_list(path: &str) -> Result<HashMap<String, String>, SeamError> {
    let raw = read_json(path, "the allow list")?;
    let entries: Vec<&Value> = match &raw {
        Value::Array(items) => items.iter().collect(),
        Value::Object(map) => match map.get("allow") {
            Some(Value::Array(items)) => items.iter().collect(),
            None if map.is_empty() => Vec::new(),
            _ => return Err(decline("the allow list has no allow array; Python answers")),
        },
        _ => return Err(decline("the allow list is not a list; Python answers")),
    };
    let today = today_utc();
    let mut allow = HashMap::new();
    for entry in entries {
        let Value::Object(entry) = entry else {
            return Err(decline(
                "an allow-list entry is not an object; Python answers",
            ));
        };
        let justification = match entry.get("justification") {
            Some(Value::String(text)) if !text.is_empty() => text.clone(),
            _ => {
                return Err(decline(
                    "an allow-list entry has no justification; Python answers",
                ))
            }
        };
        if let Some(expires) = entry.get("expires").filter(|value| truthy(value)) {
            let Value::String(text) = expires else {
                return Err(decline(
                    "an allow-list expiry is not a string; Python answers",
                ));
            };
            let date = parse_iso_date(text).ok_or_else(|| {
                decline("an allow-list expiry is not a valid date; Python answers")
            })?;
            if date <= today {
                continue;
            }
        }
        let id = match entry.get("id") {
            Some(Value::String(text)) => text.clone(),
            _ => return Err(decline("an allow-list id is not a string; Python answers")),
        };
        allow.insert(id, justification);
    }
    Ok(allow)
}

fn apply_allow_list(
    findings: Vec<Finding>,
    path: Option<&str>,
) -> Result<(Vec<Finding>, Vec<Finding>), SeamError> {
    let Some(path) = path.filter(|value| !value.is_empty()) else {
        return Ok((findings, Vec::new()));
    };
    let allow = load_allow_list(path)?;
    let mut kept = Vec::new();
    let mut suppressed = Vec::new();
    for finding in findings {
        match allow.get(&finding.id) {
            Some(justification) => suppressed.push(Finding {
                justification: Some(justification.clone()),
                ..finding
            }),
            None => kept.push(finding),
        }
    }
    Ok((kept, suppressed))
}

/// `load_per_target` with prefix `SF` and owner `A10`. The object must hold at most one target, so the key order that
/// Python keeps and `serde_json` does not is never observed.
fn load_per_target(
    path: Option<&str>,
    offset: usize,
) -> Result<Vec<(String, Vec<Finding>)>, SeamError> {
    let Some(path) = path.filter(|value| !value.is_empty()) else {
        return Ok(Vec::new());
    };
    let Value::Object(map) = read_json(path, "the per-target findings")? else {
        return Err(decline(
            "the per-target findings are not an object; Python answers",
        ));
    };
    if map.len() > 1 {
        return Err(decline(
            "per-target findings name more than one target; Python answers",
        ));
    }
    let mut out = Vec::new();
    let mut index = offset;
    for (target, items) in map {
        let Value::Array(items) = items else {
            return Err(decline("a per-target value is not a list; Python answers"));
        };
        let mut parsed = Vec::new();
        for item in &items {
            index += 1;
            parsed.push(agent_finding(item, index)?);
        }
        out.push((target, parsed));
    }
    Ok(out)
}

/// `agent_finding` with prefix `SF` and owner `A10`.
fn agent_finding(item: &Value, index: usize) -> Result<Finding, SeamError> {
    let Value::Object(item) = item else {
        return Err(decline(
            "a per-target finding is not an object; Python answers",
        ));
    };
    let severity = match item.get("severity") {
        None => "minor".to_string(),
        Some(Value::String(text)) if SEVERITIES.contains(&text.as_str()) => text.clone(),
        _ => {
            return Err(decline(
                "a per-target severity is not valid; Python answers",
            ))
        }
    };
    let id = match item.get("id") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => format!("SF-{index:03}"),
        Some(Value::Number(number)) if number.as_f64() == Some(0.0) => format!("SF-{index:03}"),
        Some(Value::String(text)) if text.is_empty() => format!("SF-{index:03}"),
        Some(Value::String(text)) => text.clone(),
        _ => return Err(decline("a per-target id is not a string; Python answers")),
    };
    let text_field = |key: &str, default: &str| -> Result<String, SeamError> {
        match item.get(key) {
            None => Ok(default.to_string()),
            Some(Value::String(text)) => Ok(text.clone()),
            Some(_) => Err(decline(
                "a per-target field is not a string; Python answers",
            )),
        }
    };
    let optional_field = |key: &str| -> Result<Option<String>, SeamError> {
        match item.get(key) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(text)) => Ok(Some(text.clone())),
            Some(_) => Err(decline(
                "a per-target field is not a string; Python answers",
            )),
        }
    };
    let owner = match item.get("owner_suggestion") {
        None => Some("A10".to_string()),
        Some(Value::Null) => None,
        Some(Value::String(text)) => Some(text.clone()),
        Some(_) => {
            return Err(decline(
                "a per-target owner is not a string; Python answers",
            ))
        }
    };
    Ok(Finding {
        id,
        severity,
        kind: text_field("kind", "semantic")?,
        summary: text_field("summary", "")?,
        evidence: text_field("evidence", "")?,
        ac_ref: optional_field("ac_ref")?,
        owner,
        location: optional_field("location")?,
        justification: None,
    })
}

fn obj_str<'a>(value: &'a Json, key: &str) -> Option<&'a str> {
    let Json::Obj(fields) = value else {
        return None;
    };
    fields
        .iter()
        .find(|(name, _)| name == key)
        .and_then(|(_, value)| match value {
            Json::Str(text) => Some(text.as_str()),
            _ => None,
        })
}

/// `{'name': 'state', ...}`: Python's repr of the runs dict, whose values carry no quote characters.
fn py_dict_repr(runs: &[(String, String)]) -> String {
    let items: Vec<String> = runs
        .iter()
        .map(|(name, state)| format!("'{name}': '{state}'"))
        .collect();
    format!("{{{}}}", items.join(", "))
}

fn verdict_of(envelope: &Json, recorded: &[(String, Json)]) -> String {
    let mut fail = false;
    for env in std::iter::once(envelope).chain(recorded.iter().map(|(_, env)| env)) {
        if let Json::Obj(fields) = env {
            if let Some(Json::Obj(payload)) = fields
                .iter()
                .find(|(name, _)| name == "payload")
                .map(|(_, value)| value)
            {
                fail |= obj_str(&Json::Obj(payload.clone()), "verdict") == Some("fail");
            }
        }
    }
    if fail { "fail" } else { "pass" }.into()
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
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

fn reply_of(body: Json, exit_code: i32) -> Reply {
    Reply {
        exit_code,
        stdout: format!("{}\n", to_python_json(&body)),
    }
}

/// Answers one served `sec_gate` call. A [`SeamError`] of `E-DEP` is not-started: the selector runs Python.
pub async fn answer(call: &Invocation) -> Result<Reply, SeamError> {
    let Some(args) = parse(&call.argv) else {
        return Err(decline(
            "sec_gate argv is outside the host grammar; Python answers",
        ));
    };
    // Python takes --task-id and --correlation-id from the environment when the flag is absent: such a call is Python's.
    let env_set = |key: &str| {
        env_value(&call.env, key).is_some()
            || std::env::var(key).is_ok_and(|value| !value.is_empty())
    };
    if (args.task_id.is_none() && env_set("SWARM_TASK_ID"))
        || (args.correlation_id.is_none() && env_set("SWARM_CORRELATION_ID"))
    {
        return Err(decline(
            "a task or correlation id comes from the environment; Python answers",
        ));
    }
    for id in [args.task_id.as_deref(), args.correlation_id.as_deref()]
        .into_iter()
        .flatten()
    {
        if !valid_task_id(id) {
            return Err(decline("an id fails check_task_id; Python answers"));
        }
    }
    let root = canonical_or(&call.root.join(args.root.as_deref().unwrap_or(".")));
    if !root.is_dir() {
        return Err(decline("the root is not a directory; Python answers"));
    }
    let scanned = scan(&root)?;
    let mut findings = scanned.findings;
    let (mut runs, audit_findings) = dep_audits(&root, args.timeout)?;
    findings.extend(audit_findings);
    if args.strict {
        for (name, state) in &runs {
            if state.starts_with("skipped") {
                findings.push(Finding::plain(
                    fid(&["deps", name, "missing"]),
                    "major",
                    "deps",
                    format!("{name} unavailable ({state}); fail-closed under --strict"),
                    String::new(),
                ));
            }
        }
    }
    let (kept, suppressed) = apply_allow_list(findings, args.allow_list.as_deref())?;
    for (slot, kind) in KINDS.iter().enumerate() {
        let blocking = kept.iter().any(|finding| {
            finding.kind == *kind
                && matches!(finding.severity.as_str(), "major" | "critical" | "blocker")
        });
        let state = if blocking {
            "fail"
        } else if scanned.seen[slot] > 0 || *kind != "iac" {
            "pass"
        } else {
            "skipped:no-iac"
        };
        runs.push(((*kind).to_string(), state.to_string()));
    }
    let mut listed_ids: Vec<&str> = kept
        .iter()
        .chain(suppressed.iter())
        .map(|finding| finding.id.as_str())
        .collect();
    listed_ids.sort_unstable();
    let listed = format!(
        "[{}]",
        listed_ids
            .iter()
            .map(|id| format!("\"{id}\""))
            .collect::<Vec<_>>()
            .join(", ")
    );
    let digest = format!("sha256:{}", sha256_hex(listed.as_bytes()));
    let own = load_per_target(args.per_target.as_deref(), kept.len())?;
    if !own.is_empty() && args.task_id.is_some() {
        return Err(decline(
            "per-target findings with a task id are checked against the store; Python answers",
        ));
    }
    // Python exits 1 when a blocking finding of any kind is reported, and status `ok` when the verdict passes. The
    // engine's exit contract allows exit 1 only with status `fail`. Without a task id the verdict is derived from the
    // script's findings alone, so a blocking agent finding under a passing script verdict is Python's answer.
    let blocking =
        |finding: &Finding| matches!(finding.severity.as_str(), "major" | "critical" | "blocker");
    if own.iter().any(|(_, items)| items.iter().any(blocking)) && !kept.iter().any(blocking) {
        return Err(decline(
            "a blocking agent finding under a passing script verdict; Python answers",
        ));
    }
    let kept_json: Vec<Json> = kept.iter().map(Finding::to_json).collect();
    let per_target: Vec<(String, Vec<Json>)> = own
        .iter()
        .map(|(target, items)| {
            let mut list = kept_json.clone();
            list.extend(items.iter().map(Finding::to_json));
            (target.clone(), list)
        })
        .collect();
    let runs_fields: Vec<(String, Json)> = runs
        .iter()
        .map(|(name, state)| (name.clone(), jstr(state)))
        .collect();
    let suppressed_json: Vec<Json> = suppressed.iter().map(Finding::to_json).collect();
    let request = GateIssueRequest {
        gate: "security".into(),
        agent_id: "A10@local".into(),
        findings: kept_json.clone(),
        runs: runs_fields,
        expires_s: 86400,
        extra: vec![
            ("scan_digest".into(), jstr(&digest)),
            ("suppressed".into(), Json::Arr(suppressed_json.clone())),
        ],
        simulate: false,
        per_target,
        task_id: args.task_id.clone(),
        correlation_id: args.correlation_id.clone(),
        session: env_value(&call.env, "SWARM_AGENT_SESSION") == Some("1"),
        dryrun_fail: std::env::var("SWARM_DRYRUN_FAIL").unwrap_or_default(),
        now: now_secs(),
    };
    let keys = KeyMaterial::from_env().map_err(|_| {
        before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the signing configuration is Python's",
        ))
    })?;
    let mut ids = SystemIds::open().map_err(|_| {
        before_any_write(SeamError::new(
            ErrorCode::Dep,
            "the id source could not be opened; Python answers",
        ))
    })?;
    let swarm = resolve_swarm_dir(&root, env_value(&call.env, "SWARM_DIR"));
    let plan = if request.task_id.is_none() {
        derive_gate_issue(&request, &StoreFacts::default(), &keys, &mut ids)
            .map_err(|Decline(reason)| before_any_write(SeamError::new(ErrorCode::Dep, reason)))?
    } else {
        let store = open_for_write(&WriterConfig::new(swarm.join("tasks.db")))
            .await
            .map_err(|error| {
                if error.write_state() == gsd_taskwriter::WriteState::NotStarted {
                    before_any_write(error.to_seam_error())
                } else {
                    error.to_seam_error()
                }
            })?;
        TaskStoreWriter::new(store)
            .issue_gate(&request, &keys, &mut ids)
            .await
            .map_err(|error| {
                if error.write_state() == gsd_taskwriter::WriteState::NotStarted {
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
    let verdict = verdict_of(&envelope, &plan.recorded);
    let mut reported = kept_json;
    for (_, items) in &own {
        reported.extend(items.iter().map(Finding::to_json));
    }
    let major = reported.iter().any(|item| {
        matches!(
            obj_str(item, "severity"),
            Some("major" | "critical" | "blocker")
        )
    });
    let mut recorded_ids: Vec<&str> = plan.recorded.iter().map(|(id, _)| id.as_str()).collect();
    recorded_ids.sort_unstable();
    let summary = format!(
        "security gate {} — {} findings, {} suppressed; runs: {}",
        verdict.to_uppercase(),
        reported.len(),
        suppressed.len(),
        py_dict_repr(&runs)
    );
    let status = if verdict == "pass" { "ok" } else { "fail" };
    let body = Json::Obj(vec![
        (
            "recorded".into(),
            Json::Arr(recorded_ids.into_iter().map(jstr).collect()),
        ),
        ("status".into(), jstr(status)),
        ("verdict".into(), jstr(&verdict)),
        ("findings".into(), Json::Arr(reported)),
        (
            "runs".into(),
            Json::Obj(
                runs.iter()
                    .map(|(name, state)| (name.clone(), jstr(state)))
                    .collect(),
            ),
        ),
        ("suppressed".into(), Json::Arr(suppressed_json)),
        ("scan_digest".into(), jstr(&digest)),
        ("envelope".into(), envelope),
        ("summary".into(), jstr(&summary)),
        ("agent".into(), jstr("A10")),
        ("script".into(), jstr("sec_gate")),
    ]);
    let exit_code = if verdict == "fail" || major { 1 } else { 0 };
    Ok(reply_of(body, exit_code))
}

#[cfg(test)]
mod tests {
    use super::{
        civil_from_days, entropy, fid, has_kev_word, parse_iso_date, patterns, py_splitlines,
        py_strip, py_suffix, scan, sha1_hex, yaml_unsafe, MAX_SCAN_BYTES,
    };
    use super::{committed, write_plan_file};
    use gsd_failure::ErrorCode;
    use gsd_route::{Json, SeamError};
    use gsd_taskwriter::{GateFile, GateIssueRequest, GatePlan, GateWrite};
    use serde_json::json;

    #[test]
    fn sha1_matches_the_standard_vectors() {
        assert_eq!(sha1_hex(b""), "da39a3ee5e6b4b0d3255bfef95601890afd80709");
        assert_eq!(sha1_hex(b"abc"), "a9993e364706816aba3e25717850c26c9cd0d89d");
        assert_eq!(
            sha1_hex(b"The quick brown fox jumps over the lazy dog"),
            "2fd4e1c67a2d28fced849ee1bb76e7391b93eb12"
        );
    }

    #[test]
    fn a_finding_id_is_the_sha1_prefix_of_its_parts() {
        assert_eq!(
            fid(&["deps", "npm-audit", "run"]),
            format!("SEC-{}", &sha1_hex(b"deps|npm-audit|run")[..8])
        );
    }

    #[test]
    fn splitlines_breaks_on_python_separators_only_where_python_does() {
        assert_eq!(py_splitlines("a\n"), vec!["a"]);
        assert_eq!(py_splitlines(""), Vec::<&str>::new());
        assert_eq!(py_splitlines("\n"), vec![""]);
        assert_eq!(
            py_splitlines("a\x0bb\x0cc\x1cd\x1de\x1ef\x1fg"),
            vec!["a", "b", "c", "d", "e", "f\x1fg"]
        );
    }

    #[test]
    fn strip_removes_the_python_whitespace_set() {
        assert_eq!(py_strip("\x1f\t x \x1c"), "x");
    }

    #[test]
    fn suffix_follows_pathlib() {
        assert_eq!(py_suffix("foo."), "");
        assert_eq!(py_suffix("..py"), ".py");
        assert_eq!(py_suffix(".bashrc"), "");
        assert_eq!(py_suffix("a.b.py"), ".py");
        assert_eq!(py_suffix(".a.b"), ".b");
    }

    #[test]
    fn entropy_matches_the_python_values_bit_for_bit() {
        // CPython 3.12 `sec_gate._entropy`, with `sum()` compensated (Neumaier). The last vector differs from a
        // naive sum in the last place, so a naive accumulation fails here.
        assert_eq!(entropy("abcd").to_bits(), 2.0_f64.to_bits());
        assert_eq!(entropy("aaaa").to_bits(), (-0.0_f64).to_bits());
        assert_eq!(entropy("Zq3rT8vL2mN5pX7kB1wD"), 4.321928094887363);
        assert_eq!(entropy("0123456789abcdef0123"), 3.9219280948873627);
        assert_eq!(entropy("jIhKtJlgLKOmxgJeKdNnFI"), 3.970573095811685);
    }

    #[test]
    fn yaml_unsafe_load_needs_a_loader_before_the_close_paren() {
        let p = patterns();
        assert!(yaml_unsafe(p, "x = yaml.load(f)"));
        assert!(!yaml_unsafe(p, "x = yaml.load(f, Loader=SafeLoader)"));
        assert!(yaml_unsafe(p, "yaml.load(a) ; yaml.load(b, Loader=L)"));
        assert!(!yaml_unsafe(p, "yaml.load(a, Loader =L)"));
    }

    #[test]
    fn strict_dates_only() {
        assert_eq!(parse_iso_date("2024-02-29"), Some((2024, 2, 29)));
        assert_eq!(parse_iso_date("2023-02-29"), None);
        assert_eq!(parse_iso_date("2024-1-05"), None);
        assert_eq!(parse_iso_date("0000-01-01"), None);
    }

    #[test]
    fn civil_dates_from_day_counts() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(-1), (1969, 12, 31));
        assert_eq!(civil_from_days(20_000), (2024, 10, 4));
    }

    #[test]
    fn kev_words_are_found_in_keys_and_strings_only() {
        assert!(has_kev_word(&json!({"id": "x", "aliases": ["KEV-2024"]})));
        assert!(has_kev_word(&json!({"note": "actively Exploited"})));
        assert!(has_kev_word(
            &json!({"n": 1e5, "ok": true, "s": "kevlar-free"})
        ));
        assert!(!has_kev_word(&json!({"n": 12, "ok": true})));
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
                name: "T-1.security.json".into(),
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
            gate: "security".into(),
            agent_id: "A10".into(),
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
    fn github_token_with_example_comment_is_still_reported() {
        let dir = tempfile::tempdir().expect("tempdir");
        // A live-shaped token (`ghp_` + 36 alphanumerics) followed by a trailing comment. The placeholder test runs
        // on the whole line, so the comment's word `example` suppresses the token today.
        std::fs::write(
            dir.path().join("settings.env"),
            "TOKEN=\"ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ\"  # example\n",
        )
        .expect("write");
        let found = scan(dir.path()).expect("scan");
        assert!(
            found.findings.iter().any(|finding| {
                finding.kind == "secrets"
                    && finding.severity == "critical"
                    && finding.summary.starts_with("github-token:")
                    && finding.location.as_deref() == Some("settings.env:1")
            }),
            "no critical secrets finding for the token: {:?}",
            found
                .findings
                .iter()
                .map(|finding| format!("{}/{}/{}", finding.kind, finding.severity, finding.summary))
                .collect::<Vec<String>>()
        );
    }

    #[test]
    fn secret_in_file_over_scan_cap_is_not_silently_passed() {
        let dir = tempfile::tempdir().expect("tempdir");
        // The credential is on line 1. The rest pads the file past MAX_SCAN_BYTES, where the walk skips it silently.
        let mut body = String::from("password = \"Zq3rT8vL2mN5pX7kB1wD\"\n");
        body.push_str(&"a = 1\n".repeat(350_000));
        assert!(body.len() as u64 > MAX_SCAN_BYTES);
        std::fs::write(dir.path().join("big.env"), &body).expect("write");
        let found = scan(dir.path()).expect("scan");
        assert!(
            found.findings.iter().any(|finding| finding
                .location
                .as_deref()
                .is_some_and(|loc| loc.starts_with("big.env"))),
            "neither a finding nor a skip record names big.env: {:?}",
            found
                .findings
                .iter()
                .map(|finding| format!("{}/{}/{}", finding.kind, finding.severity, finding.summary))
                .collect::<Vec<String>>()
        );
    }
}
