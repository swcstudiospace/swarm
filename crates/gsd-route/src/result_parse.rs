//! `task.result` parsing and validation (`swarm/results.py::parse_result` and `validate_result`), for the write routes.
//!
//! Principle (14.5 Design 1): Rust answers only what it can prove byte-identical to Python, and every other input is refused with a typed
//! [`NotStarted`] so the Python body answers. The contract of this module is "Rust accepts only what Python accepts, and accepts the same
//! parsed value".
//!
//! # `parse_result`
//!
//! Python takes the LAST parseable ```` ```json ```` block, else the whole text as a bare JSON object:
//!
//! * [`JSON_BLOCK_PATTERN`] is `` ```json\s*(\{.*?\})\s*``` `` with `re.S`. It is implemented by hand (`fenced_objects`) with the same
//!   matching order as `re.findall`: leftmost start, `\s*` then `{`, then the SHORTEST object text whose closing `}` is followed by optional
//!   whitespace and three backticks, and the next search resumes after those backticks. Consequences kept on purpose: only the exact spelling
//!   ```` ```json ```` opens a block (not `JSON`, `json5`, `~~~json` or ```` ``` json ````); the fence may sit on one line (`` ```json{...}``` ``);
//!   a `}` inside a string followed by backticks ends the block early (the cut text then fails to parse and the previous block is tried); an
//!   unterminated fence swallows everything up to the next closing fence, and one with no closing fence matches nothing at all.
//! * `\s` is Python's Unicode whitespace, which is Rust's `char::is_whitespace` plus U+001C to U+001F (`py_space`).
//! * Each captured text is parsed from the last block to the first; one that is not valid JSON is skipped, the first that is valid wins.
//!   With no block left the whole text is parsed and kept only when it is an object; anything else is `None` (Python's `None`).
//!
//! The JSON parser here is private and not [`crate::parse_json`]: it has to tell "Python rejects this" from "Python accepts this and Rust does
//! not reproduce it", which `parse_json` (one error for both) cannot. It follows `json.loads` for a `str`: whitespace is space, tab, LF and CR;
//! the number grammar is `-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?`; a control character in a string, a trailing comma, a comment, a quote other
//! than `"` and a byte order mark are syntax errors; duplicate keys keep the first position and the last value; a surrogate pair `\ud83d\ude00`
//! is one character. A syntax error always wins over a feature below (Python rejects the text as a whole).
//!
//! What Python accepts and Rust refuses ([`NotStarted`]), all of it for parsed values that Rust cannot represent or print like Python:
//!
//! * [`NotStarted::IntegerOutOfRange`]: an integer outside the `i64` range (Python keeps any size; above 4300 digits `int()` raises
//!   `ValueError`, above 4000 digits this parser stops at once rather than guess the interpreter's limit);
//! * [`NotStarted::NonFiniteNumber`]: `NaN`, `Infinity`, `-Infinity` (Python's `json` accepts them) and a literal that overflows to infinity
//!   such as `1e999`;
//! * [`NotStarted::LoneSurrogate`]: a `\uD800`..`\uDFFF` escape that is not half of a valid pair (Python keeps the lone surrogate, a Rust
//!   `String` cannot);
//! * [`NotStarted::NestingTooDeep`]: containers nested deeper than 100 (Python recurses without a fixed limit of its own; Rust stops here and Python answers).
//!
//! # `validate_result`
//!
//! Python runs `swarm/schema.py::validate` over `swarm/schemas/task.result.v1.json` and then compares `task_id`. That validator is a general
//! interpreter of a Draft 2020-12 subset (`type`, `required`, `enum`, `const`, `properties`, `items`, `additionalProperties`, `minimum`,
//! `maximum`, `minLength`, `allOf`, `if/then`). This file USES only `type` (one name or a list), `required`, `enum` (of strings),
//! `properties`, `items` (a schema) and `additionalProperties` (`true` or a schema). Those, and nothing else, are the fields of the private
//! `Node` type, and `TASK_RESULT` is that schema written out as a static tree. A keyword the validator supports but this schema does not use
//! cannot be expressed, and no schema text is interpreted at run time. [`SCHEMA_SHA256`] is the digest of the schema file the tree was written
//! from; the vectors record the digest of the file on disk and a test compares the two, so editing the schema fails a test until the tree is
//! reviewed.
//!
//! Error texts are reproduced exactly: the order of Python's checks (type, enum, required, properties in the object's own key order, items), the
//! JSON-pointer paths, `; ` joins, the 500 character cut (characters, not bytes) and the `E-CONTRACT: ` prefix. Python's messages embed the
//! `repr` of an object in two places: an enum failure (`/state: 'DONE' not one of [...]`) and a `task_id` mismatch. Rust formats the repr of
//! `None`, `bool`, `int` and ASCII `str` only; a float, a list, a dict or a non-ASCII string there is [`NotStarted::ReprNotReproducible`]
//! (Python's rule for printable non-ASCII needs the Unicode database).
//!
//! Python's `details` (`{"task_id": ...}`) are not part of [`SeamError`]; the caller attaches them.

use std::collections::HashMap;

use gsd_codes::ErrorCode;

use crate::{obj_get, Json, SeamError};

/// The regular expression of `swarm/results.py::JSON_BLOCK` (flags `re.S`) that `fenced_objects` implements.
pub const JSON_BLOCK_PATTERN: &str = r"```json\s*(\{.*?\})\s*```";

/// SHA-256 of `swarm/schemas/task.result.v1.json`, the file `TASK_RESULT` was written from.
pub const SCHEMA_SHA256: &str = "56c5bf8677406a651e3b32724d71ff3bf9362ea26e0552f8e754f380f2fa7638";

/// The `state` values the schema allows, in the schema's order.
pub const STATES: [&str; 4] = ["IN_PROGRESS", "IN_REVIEW", "FAILED", "BLOCKED"];

/// Deepest container nesting that is parsed; a deeper text is [`NotStarted::NestingTooDeep`].
pub const MAX_NESTING: usize = 100;

/// `validate_result` cuts the `task.result invalid: ...` text to this many characters.
pub const MESSAGE_CAP: usize = 500;

/// Integers with more digits than this stop the parse: Python's `int()` limit is 4300 and may be configured.
const MAX_INT_DIGITS: usize = 4000;

const FENCE: &str = "```";
const FENCE_OPEN: &str = "```json";

/// Why Rust declines an input so that Python answers it (the route turns this into `E-DEP` with `write_state = "not_started"`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotStarted {
    /// Containers nested more than [`MAX_NESTING`] deep.
    NestingTooDeep,
    /// An integer outside the `i64` range.
    IntegerOutOfRange,
    /// `NaN`, `Infinity`, `-Infinity` or a literal that overflows to infinity.
    NonFiniteNumber,
    /// A `\uD800`..`\uDFFF` escape that is not half of a valid surrogate pair.
    LoneSurrogate,
    /// Python's error text would embed a `repr` that Rust does not format.
    ReprNotReproducible,
}

impl NotStarted {
    /// A stable kebab-case name for logs and `error.details`.
    pub fn as_str(self) -> &'static str {
        match self {
            NotStarted::NestingTooDeep => "nesting-too-deep",
            NotStarted::IntegerOutOfRange => "integer-out-of-range",
            NotStarted::NonFiniteNumber => "non-finite-number",
            NotStarted::LoneSurrogate => "lone-surrogate",
            NotStarted::ReprNotReproducible => "repr-not-reproducible",
        }
    }
}

/// Why [`validate_result`] did not return the result.
#[derive(Debug, Clone, PartialEq)]
pub enum ResultError {
    /// Python raises this `SwarmError(E_CONTRACT, ...)`; the message equals `str(exc)`.
    Contract(SeamError),
    /// Python's text cannot be reproduced; Python answers.
    NotStarted(NotStarted),
}

// ---------------------------------------------------------------------------------------------------------------------------------------
// Fence detection

/// Python's `\s` for a `str` pattern: the Unicode White_Space characters plus U+001C to U+001F.
fn py_space(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\u{1c}'..='\u{1f}')
}

/// Byte length of the whitespace run at the start of `text`.
fn space_len(text: &str) -> usize {
    text.chars()
        .take_while(|&ch| py_space(ch))
        .map(char::len_utf8)
        .sum()
}

/// `JSON_BLOCK.findall(text)`: the captured `{...}` texts in match order.
fn fenced_objects(text: &str) -> Vec<&str> {
    let mut found = Vec::new();
    let mut from = 0;
    while let Some(offset) = text[from..].find(FENCE_OPEN) {
        let start = from + offset;
        let after_word = start + FENCE_OPEN.len();
        let brace = after_word + space_len(&text[after_word..]);
        if !text[brace..].starts_with('{') {
            from = start + 1;
            continue;
        }
        match object_end(text, brace + 1) {
            Some((end, resume)) => {
                found.push(&text[brace..end]);
                from = resume;
            }
            // Every later start would need a closing `}` after its own brace, which is a subset of the positions that just failed.
            None => break,
        }
    }
    found
}

/// The lazy `.*?\}\s*```` tail: the first `}` at or after `from` followed by optional whitespace and three backticks.
/// Returns the end of the object text (after the brace) and where the search resumes (after the backticks).
fn object_end(text: &str, from: usize) -> Option<(usize, usize)> {
    let mut at = from;
    while let Some(offset) = text[at..].find('}') {
        let after = at + offset + 1;
        let fence = after + space_len(&text[after..]);
        if text[fence..].starts_with(FENCE) {
            return Some((after, fence + FENCE.len()));
        }
        at = after;
    }
    None
}

// ---------------------------------------------------------------------------------------------------------------------------------------
// JSON text to value, with Python's accept/reject decision

/// Why a text did not produce a value.
enum Fail {
    /// `json.loads` raises `JSONDecodeError`.
    Syntax,
    /// Parsing stopped on something Rust will not follow.
    Abort(NotStarted),
}

/// A syntactically valid document and the first unsupported feature met while reading it.
struct Parsed {
    value: Json,
    flag: Option<NotStarted>,
}

struct Scan<'a> {
    bytes: &'a [u8],
    pos: usize,
    flag: Option<NotStarted>,
}

/// Parses `text` as `json.loads(text)` would.
fn parse_document(text: &str) -> Result<Parsed, Fail> {
    let mut scan = Scan {
        bytes: text.as_bytes(),
        pos: 0,
        flag: None,
    };
    let value = scan.value(1)?;
    scan.skip_ws();
    if scan.pos != scan.bytes.len() {
        return Err(Fail::Syntax);
    }
    Ok(Parsed {
        value,
        flag: scan.flag,
    })
}

impl Scan<'_> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn eat(&mut self, byte: u8) -> Result<(), Fail> {
        if self.peek() == Some(byte) {
            self.pos += 1;
            Ok(())
        } else {
            Err(Fail::Syntax)
        }
    }

    /// Remembers the first unsupported feature; a later one adds nothing.
    fn note(&mut self, why: NotStarted) {
        self.flag = self.flag.or(Some(why));
    }

    fn value(&mut self, depth: usize) -> Result<Json, Fail> {
        self.skip_ws();
        match self.peek().ok_or(Fail::Syntax)? {
            b'{' | b'[' if depth > MAX_NESTING => Err(Fail::Abort(NotStarted::NestingTooDeep)),
            b'{' => self.object(depth),
            b'[' => self.array(depth),
            b'"' => self.string().map(Json::Str),
            b't' => self.word(b"true", Json::Bool(true)),
            b'f' => self.word(b"false", Json::Bool(false)),
            b'n' => self.word(b"null", Json::Null),
            b'N' => self.non_finite(b"NaN", f64::NAN),
            b'I' => self.non_finite(b"Infinity", f64::INFINITY),
            b'-' if self.bytes.get(self.pos + 1) == Some(&b'I') => {
                self.non_finite(b"-Infinity", f64::NEG_INFINITY)
            }
            b'-' | b'0'..=b'9' => self.number(),
            _ => Err(Fail::Syntax),
        }
    }

    fn word(&mut self, word: &[u8], value: Json) -> Result<Json, Fail> {
        if self.bytes[self.pos..].starts_with(word) {
            self.pos += word.len();
            Ok(value)
        } else {
            Err(Fail::Syntax)
        }
    }

    /// `NaN`, `Infinity` and `-Infinity`: valid for Python, refused by Rust.
    fn non_finite(&mut self, word: &[u8], number: f64) -> Result<Json, Fail> {
        let value = self.word(word, Json::Float(number))?;
        self.note(NotStarted::NonFiniteNumber);
        Ok(value)
    }

    fn object(&mut self, depth: usize) -> Result<Json, Fail> {
        self.eat(b'{')?;
        let mut fields: Vec<(String, Json)> = Vec::new();
        let mut index: HashMap<String, usize> = HashMap::new();
        self.skip_ws();
        if self.eat(b'}').is_ok() {
            return Ok(Json::Obj(fields));
        }
        loop {
            self.skip_ws();
            let key = self.string()?;
            self.skip_ws();
            self.eat(b':')?;
            let value = self.value(depth + 1)?;
            match index.get(&key) {
                Some(&at) => fields[at].1 = value,
                None => {
                    index.insert(key.clone(), fields.len());
                    fields.push((key, value));
                }
            }
            self.skip_ws();
            if self.eat(b',').is_ok() {
                continue;
            }
            self.eat(b'}')?;
            return Ok(Json::Obj(fields));
        }
    }

    fn array(&mut self, depth: usize) -> Result<Json, Fail> {
        self.eat(b'[')?;
        let mut items = Vec::new();
        self.skip_ws();
        if self.eat(b']').is_ok() {
            return Ok(Json::Arr(items));
        }
        loop {
            items.push(self.value(depth + 1)?);
            self.skip_ws();
            if self.eat(b',').is_ok() {
                continue;
            }
            self.eat(b']')?;
            return Ok(Json::Arr(items));
        }
    }

    fn string(&mut self) -> Result<String, Fail> {
        self.eat(b'"')?;
        let mut buf: Vec<u8> = Vec::new();
        loop {
            let byte = self.peek().ok_or(Fail::Syntax)?;
            self.pos += 1;
            match byte {
                b'"' => break,
                b'\\' => {
                    let escape = self.peek().ok_or(Fail::Syntax)?;
                    self.pos += 1;
                    match escape {
                        b'"' => buf.push(b'"'),
                        b'\\' => buf.push(b'\\'),
                        b'/' => buf.push(b'/'),
                        b'b' => buf.push(0x08),
                        b'f' => buf.push(0x0c),
                        b'n' => buf.push(b'\n'),
                        b'r' => buf.push(b'\r'),
                        b't' => buf.push(b'\t'),
                        b'u' => {
                            let decoded = self.unicode_escape()?;
                            let mut encoded = [0u8; 4];
                            buf.extend_from_slice(decoded.encode_utf8(&mut encoded).as_bytes());
                        }
                        _ => return Err(Fail::Syntax),
                    }
                }
                0x00..=0x1f => return Err(Fail::Syntax),
                other => buf.push(other),
            }
        }
        // The input is a `&str` and escapes were encoded whole, so `buf` is valid UTF-8; a failure here is a syntax-level impossibility.
        String::from_utf8(buf).map_err(|_| Fail::Syntax)
    }

    /// The character of a `\uXXXX` escape (the `\u` is consumed): a valid surrogate pair is one character, a lone surrogate is noted and
    /// replaced by U+FFFD.
    fn unicode_escape(&mut self) -> Result<char, Fail> {
        let first = self.hex4()?;
        if (0xD800..0xDC00).contains(&first) {
            if self.bytes[self.pos..].starts_with(b"\\u") {
                let save = self.pos;
                self.pos += 2;
                let second = self.hex4()?;
                if (0xDC00..0xE000).contains(&second) {
                    let code = 0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00);
                    return Ok(char::from_u32(code).unwrap_or('\u{FFFD}'));
                }
                self.pos = save;
            }
            self.note(NotStarted::LoneSurrogate);
            return Ok('\u{FFFD}');
        }
        if (0xDC00..0xE000).contains(&first) {
            self.note(NotStarted::LoneSurrogate);
            return Ok('\u{FFFD}');
        }
        Ok(char::from_u32(first).unwrap_or('\u{FFFD}'))
    }

    fn hex4(&mut self) -> Result<u32, Fail> {
        let slice = self.bytes.get(self.pos..self.pos + 4).ok_or(Fail::Syntax)?;
        let value = slice
            .iter()
            .try_fold(0u32, |acc, &byte| {
                Some(acc * 16 + char::from(byte).to_digit(16)?)
            })
            .ok_or(Fail::Syntax)?;
        self.pos += 4;
        Ok(value)
    }

    fn number(&mut self) -> Result<Json, Fail> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek().ok_or(Fail::Syntax)? {
            b'0' => self.pos += 1,
            b'1'..=b'9' => {
                self.digits();
            }
            _ => return Err(Fail::Syntax),
        }
        let mut is_float = false;
        if self.peek() == Some(b'.') {
            is_float = true;
            self.pos += 1;
            if !self.digits() {
                return Err(Fail::Syntax);
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            is_float = true;
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if !self.digits() {
                return Err(Fail::Syntax);
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos]).map_err(|_| Fail::Syntax)?;
        if is_float {
            let number: f64 = text.parse().map_err(|_| Fail::Syntax)?;
            if number.is_infinite() {
                self.note(NotStarted::NonFiniteNumber);
            }
            return Ok(Json::Float(number));
        }
        match text.parse::<i64>() {
            Ok(number) => Ok(Json::Int(number)),
            Err(_) if text.trim_start_matches('-').len() > MAX_INT_DIGITS => {
                Err(Fail::Abort(NotStarted::IntegerOutOfRange))
            }
            Err(_) => {
                self.note(NotStarted::IntegerOutOfRange);
                Ok(Json::Int(0))
            }
        }
    }

    /// Consumes ASCII digits and returns whether at least one was consumed.
    fn digits(&mut self) -> bool {
        let start = self.pos;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.pos += 1;
        }
        self.pos > start
    }
}

/// Python's `obj if isinstance(obj, dict) else None` applied to a document, with the unsupported-feature refusal.
fn object_or_none(parsed: Parsed) -> Result<Option<Json>, NotStarted> {
    match (parsed.value, parsed.flag) {
        (Json::Obj(_), Some(why)) => Err(why),
        (value @ Json::Obj(_), None) => Ok(Some(value)),
        _ => Ok(None),
    }
}

/// `swarm.results.parse_result(text)`: the last parseable fenced `json` object, else the whole text when it is an object.
///
/// `Ok(None)` is Python's `None` (no object found). `Err` means Python's answer is not reproduced here; see the module documentation.
pub fn parse_result(text: &str) -> Result<Option<Json>, NotStarted> {
    for raw in fenced_objects(text).into_iter().rev() {
        match parse_document(raw) {
            Ok(parsed) => return object_or_none(parsed),
            Err(Fail::Syntax) => continue,
            Err(Fail::Abort(why)) => return Err(why),
        }
    }
    match parse_document(text) {
        Ok(parsed) => object_or_none(parsed),
        Err(Fail::Syntax) => Ok(None),
        Err(Fail::Abort(why)) => Err(why),
    }
}

// ---------------------------------------------------------------------------------------------------------------------------------------
// Schema

/// The only JSON types the schema names.
#[derive(Clone, Copy)]
enum Ty {
    Str,
    Arr,
    Obj,
}

impl Ty {
    fn name(self) -> &'static str {
        match self {
            Ty::Str => "string",
            Ty::Arr => "array",
            Ty::Obj => "object",
        }
    }

    fn holds(self, value: &Json) -> bool {
        matches!(
            (self, value),
            (Ty::Str, Json::Str(_)) | (Ty::Arr, Json::Arr(_)) | (Ty::Obj, Json::Obj(_))
        )
    }
}

/// One schema node. Its fields are exactly the keywords `task.result.v1.json` uses; there is no way to state another one.
struct Node {
    /// `type`: one name or a list of names; empty means the keyword is absent.
    types: &'static [Ty],
    /// `enum` of strings.
    one_of: Option<&'static [&'static str]>,
    /// `required`.
    required: &'static [&'static str],
    /// `properties`.
    properties: &'static [(&'static str, &'static Node)],
    /// `items` as a schema.
    items: Option<&'static Node>,
    /// `additionalProperties` as a schema (`true`, the default, is `None`).
    additional: Option<&'static Node>,
}

const BARE: Node = Node {
    types: &[],
    one_of: None,
    required: &[],
    properties: &[],
    items: None,
    additional: None,
};

static STRING: Node = Node {
    types: &[Ty::Str],
    ..BARE
};

static ARRAY: Node = Node {
    types: &[Ty::Arr],
    ..BARE
};

static OBJECT: Node = Node {
    types: &[Ty::Obj],
    ..BARE
};

static STATE: Node = Node {
    one_of: Some(&STATES),
    ..BARE
};

static NEEDS: Node = Node {
    types: &[Ty::Str, Ty::Arr, Ty::Obj],
    ..BARE
};

static OUTPUT_ITEM: Node = Node {
    types: &[Ty::Obj],
    properties: &[
        ("kind", &STRING),
        ("uri", &STRING),
        ("version", &STRING),
        ("digest", &STRING),
    ],
    ..BARE
};

static OUTPUTS: Node = Node {
    types: &[Ty::Arr],
    items: Some(&OUTPUT_ITEM),
    ..BARE
};

static ERROR: Node = Node {
    types: &[Ty::Obj],
    properties: &[("code", &STRING), ("message", &STRING)],
    ..BARE
};

static VERDICT: Node = Node {
    types: &[Ty::Obj],
    properties: &[("verdict", &STRING), ("findings", &ARRAY)],
    ..BARE
};

static VERDICTS: Node = Node {
    types: &[Ty::Obj],
    additional: Some(&VERDICT),
    ..BARE
};

/// `swarm/schemas/task.result.v1.json` as a tree.
static TASK_RESULT: Node = Node {
    types: &[Ty::Obj],
    required: &["task_id", "state"],
    properties: &[
        ("task_id", &STRING),
        ("state", &STATE),
        ("outputs", &OUTPUTS),
        ("metrics", &OBJECT),
        ("summary_md", &STRING),
        ("needs", &NEEDS),
        ("error", &ERROR),
        ("verdicts", &VERDICTS),
        ("gate", &STRING),
    ],
    ..BARE
};

/// `type(value).__name__` of the Python object `json.loads` makes for `value`.
fn py_type_name(value: &Json) -> &'static str {
    match value {
        Json::Null => "NoneType",
        Json::Bool(_) => "bool",
        Json::Int(_) => "int",
        Json::Float(_) => "float",
        Json::Str(_) => "str",
        Json::Arr(_) => "list",
        Json::Obj(_) => "dict",
    }
}

/// `repr(text)` for an ASCII string, else `None`.
fn py_str_repr(text: &str) -> Option<String> {
    if !text.is_ascii() {
        return None;
    }
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    Some(out)
}

/// `repr(value)` for `None`, `bool`, `int` and ASCII `str`; `None` for everything else.
fn py_repr(value: &Json) -> Option<String> {
    match value {
        Json::Null => Some("None".to_string()),
        Json::Bool(true) => Some("True".to_string()),
        Json::Bool(false) => Some("False".to_string()),
        Json::Int(number) => Some(number.to_string()),
        Json::Str(text) => py_str_repr(text),
        Json::Float(_) | Json::Arr(_) | Json::Obj(_) => None,
    }
}

/// Python's `validate(obj, schema, path)`: appends the error texts for `value` in Python's order.
fn check(
    node: &Node,
    value: &Json,
    path: &str,
    errors: &mut Vec<String>,
) -> Result<(), NotStarted> {
    let at = if path.is_empty() { "/" } else { path };
    if !node.types.is_empty() && !node.types.iter().any(|ty| ty.holds(value)) {
        let names: Vec<&str> = node.types.iter().map(|ty| ty.name()).collect();
        errors.push(format!(
            "{at}: expected {}, got {}",
            names.join("|"),
            py_type_name(value)
        ));
        return Ok(());
    }
    if let Some(allowed) = node.one_of {
        let hit = matches!(value, Json::Str(text) if allowed.contains(&text.as_str()));
        if !hit {
            let shown = py_repr(value).ok_or(NotStarted::ReprNotReproducible)?;
            let listed: Vec<String> = allowed.iter().map(|name| format!("'{name}'")).collect();
            errors.push(format!("{at}: {shown} not one of [{}]", listed.join(", ")));
        }
    }
    if let Json::Obj(fields) = value {
        for key in node.required {
            if obj_get(fields, key).is_none() {
                errors.push(format!("{path}/{key}: required"));
            }
        }
        for (key, child) in fields {
            let named = node
                .properties
                .iter()
                .find(|(name, _)| *name == key.as_str())
                .map(|(_, schema)| *schema);
            if let Some(schema) = named.or(node.additional) {
                check(schema, child, &format!("{path}/{key}"), errors)?;
            }
        }
    }
    if let (Json::Arr(items), Some(schema)) = (value, node.items) {
        for (index, item) in items.iter().enumerate() {
            check(schema, item, &format!("{path}/{index}"), errors)?;
        }
    }
    Ok(())
}

/// `swarm.results.validate_result(result, task_id=task_id)`: checks `result` against `task.result.v1` and binds it to the assigned task.
///
/// `None` is Python's missing result. `Ok` returns the same result, as Python does.
pub fn validate_result<'a>(
    result: Option<&'a Json>,
    task_id: &str,
) -> Result<&'a Json, ResultError> {
    let Some(result) = result else {
        return Err(ResultError::Contract(SeamError::new(
            ErrorCode::Contract,
            "no JSON result block from agent",
        )));
    };
    let mut errors = Vec::new();
    check(&TASK_RESULT, result, "", &mut errors).map_err(ResultError::NotStarted)?;
    if !errors.is_empty() {
        let text = format!("task.result invalid: {}", errors.join("; "));
        let cut: String = text.chars().take(MESSAGE_CAP).collect();
        return Err(ResultError::Contract(SeamError::new(
            ErrorCode::Contract,
            cut,
        )));
    }
    if let Json::Obj(fields) = result {
        if let Some(Json::Str(named)) = obj_get(fields, "task_id") {
            if named != task_id {
                let (Some(named), Some(assigned)) = (py_str_repr(named), py_str_repr(task_id))
                else {
                    return Err(ResultError::NotStarted(NotStarted::ReprNotReproducible));
                };
                let message =
                    format!("task_id mismatch: result names {named}, assigned {assigned}");
                return Err(ResultError::Contract(SeamError::new(
                    ErrorCode::Contract,
                    message,
                )));
            }
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{parse_json, to_python_json};

    const VECTORS: &str = include_str!("../tests/vectors/result_parse.json");

    fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
        match value {
            Json::Obj(fields) => {
                obj_get(fields, key).unwrap_or_else(|| panic!("missing key {key}"))
            }
            other => panic!("not an object looking for {key}: {other:?}"),
        }
    }

    fn text<'a>(value: &'a Json, key: &str) -> &'a str {
        match member(value, key) {
            Json::Str(found) => found,
            other => panic!("{key} is not a string: {other:?}"),
        }
    }

    fn optional_text<'a>(value: &'a Json, key: &str) -> Option<&'a str> {
        match member(value, key) {
            Json::Null => None,
            Json::Str(found) => Some(found),
            other => panic!("{key} is neither null nor a string: {other:?}"),
        }
    }

    fn array<'a>(value: &'a Json, key: &str) -> &'a [Json] {
        match member(value, key) {
            Json::Arr(items) => items,
            other => panic!("{key} is not an array: {other:?}"),
        }
    }

    fn vectors() -> Json {
        parse_json(VECTORS)
            .unwrap_or_else(|err| panic!("vector file does not parse: {}", err.message()))
    }

    /// What replaying the file exercised, so an empty or truncated file cannot pass.
    #[derive(Default)]
    struct Tally {
        value_exact: usize,
        none_exact: usize,
        validated_ok: usize,
        contract_exact: usize,
        parse_refused: usize,
        validate_refused: usize,
    }

    fn replay(case: &Json, tally: &mut Tally) {
        let id = text(case, "id");
        let input = text(case, "text");
        let task_id = text(case, "task_id");
        let parse = member(case, "parse");
        let validate = member(case, "validate");
        let expect_parse = text(member(case, "rust"), "parse");
        let expect_validate = text(member(case, "rust"), "validate");
        let outcome = text(parse, "outcome");
        let parsed = match (expect_parse, parse_result(input)) {
            ("exact", Ok(parsed)) => parsed,
            ("not_started", Err(_)) => {
                tally.parse_refused += 1;
                return;
            }
            (expected, got) => panic!("{id}: expected parse {expected}, got {got:?}"),
        };
        match (outcome, &parsed) {
            ("value", Some(value)) => {
                let want = optional_text(parse, "value")
                    .unwrap_or_else(|| panic!("{id}: exact parse without a recorded value"));
                assert_eq!(
                    to_python_json(value),
                    want,
                    "{id}: parsed value differs from Python's"
                );
                tally.value_exact += 1;
            }
            ("none", None) => tally.none_exact += 1,
            (python, got) => panic!("{id}: Python answered {python}, Rust answered {got:?}"),
        }
        let verdict = text(validate, "outcome");
        let checked = validate_result(parsed.as_ref(), task_id);
        match (expect_validate, verdict, checked) {
            ("exact", "ok", Ok(returned)) => {
                assert_eq!(
                    Some(returned),
                    parsed.as_ref(),
                    "{id}: validate_result must return the result it was given"
                );
                tally.validated_ok += 1;
            }
            ("exact", "error", Err(ResultError::Contract(err))) => {
                assert_eq!(err.code(), ErrorCode::Contract, "{id}");
                let want = optional_text(validate, "message")
                    .unwrap_or_else(|| panic!("{id}: error without a message"));
                assert_eq!(
                    err.message(),
                    want,
                    "{id}: error text differs from Python's"
                );
                tally.contract_exact += 1;
            }
            ("not_started", "error", Err(ResultError::NotStarted(_))) => {
                tally.validate_refused += 1
            }
            (expected, python, got) => {
                panic!("{id}: expected validate {expected}, Python {python}, Rust answered {got:?}")
            }
        }
    }

    #[test]
    fn vectors_replay_python_outcomes() {
        let doc = vectors();
        let mut tally = Tally::default();
        for case in array(&doc, "cases") {
            replay(case, &mut tally);
        }
        assert!(
            tally.value_exact >= 400,
            "dict answers replayed: {}",
            tally.value_exact
        );
        assert!(
            tally.none_exact >= 100,
            "None answers replayed: {}",
            tally.none_exact
        );
        assert!(
            tally.validated_ok >= 250,
            "accepted results replayed: {}",
            tally.validated_ok
        );
        assert!(
            tally.contract_exact >= 250,
            "E-CONTRACT texts replayed: {}",
            tally.contract_exact
        );
        assert!(
            tally.parse_refused >= 30,
            "unsupported parses refused: {}",
            tally.parse_refused
        );
        assert!(
            tally.validate_refused >= 15,
            "unreproducible repr texts refused: {}",
            tally.validate_refused
        );
    }

    #[test]
    fn oracle_constants_match_the_file_the_vectors_were_made_from() {
        let doc = vectors();
        let oracle = member(&doc, "oracle");
        assert_eq!(text(oracle, "json_block_pattern"), JSON_BLOCK_PATTERN);
        assert_eq!(
            text(oracle, "schema_sha256"),
            SCHEMA_SHA256,
            "task.result.v1.json changed: re-derive TASK_RESULT, then update SCHEMA_SHA256"
        );
        assert_eq!(
            member(oracle, "json_block_flags"),
            &Json::Int(48),
            "re.S (16) plus the str default re.UNICODE (32)"
        );
        assert_eq!(member(oracle, "depth_cap"), &Json::Int(MAX_NESTING as i64));
        assert_eq!(
            member(oracle, "message_cap"),
            &Json::Int(MESSAGE_CAP as i64)
        );
        let states: Vec<&str> = array(oracle, "state_enum")
            .iter()
            .map(|state| match state {
                Json::Str(name) => name.as_str(),
                other => panic!("state is not a string: {other:?}"),
            })
            .collect();
        assert_eq!(states, STATES);
    }

    #[test]
    fn space_matches_python_for_every_code_point() {
        let doc = vectors();
        let ranges: Vec<(u32, u32)> = array(member(&doc, "oracle"), "space_ranges")
            .iter()
            .map(|pair| match pair {
                Json::Arr(ends) => match ends.as_slice() {
                    [Json::Int(low), Json::Int(high)] => (*low as u32, *high as u32),
                    other => panic!("bad range {other:?}"),
                },
                other => panic!("bad range {other:?}"),
            })
            .collect();
        for code in 0..=0x10FFFFu32 {
            let Some(ch) = char::from_u32(code) else {
                continue;
            };
            let python = ranges
                .iter()
                .any(|(low, high)| (*low..=*high).contains(&code));
            assert_eq!(py_space(ch), python, "U+{code:04X}");
        }
    }

    #[test]
    fn fenced_objects_follow_the_regex_match_order() {
        let cases: [(&str, &[&str]); 12] = [
            ("```json\n{\"a\":1}\n```", &["{\"a\":1}"]),
            ("```json{\"a\":1}```", &["{\"a\":1}"]),
            (
                "```json\n{\"a\":1}\n```\ntext\n```json\n{\"b\":2}\n```",
                &["{\"a\":1}", "{\"b\":2}"],
            ),
            (
                "```json\n{\"a\":1}\n```\ntext\n```json\n{\"b\":2}",
                &["{\"a\":1}"],
            ),
            (
                "```json\n{bad\n```\n x ```json\n{\"a\":1}\n```",
                &["{bad\n```\n x ```json\n{\"a\":1}"],
            ),
            ("```json\n{\"a\":{\"b\":1}}\n```", &["{\"a\":{\"b\":1}}"]),
            ("```json\n{\"a\":\"x}\n```\"}\n```", &["{\"a\":\"x}"]),
            ("```JSON\n{\"a\":1}\n```", &[]),
            ("```json5\n{\"a\":1}\n```", &[]),
            ("```json\n[1]\n```", &[]),
            ("```json\u{2003}\u{1c}{}\u{3000}```", &["{}"]),
            ("```json\u{200b}{}```", &[]),
        ];
        for (input, expected) in cases {
            assert_eq!(fenced_objects(input), expected, "{input:?}");
        }
    }

    #[test]
    fn unsupported_features_are_refused_with_their_reason() {
        let wrap = |body: &str| {
            format!("```json\n{{\"task_id\": \"T\", \"state\": \"FAILED\", \"metrics\": {{\"n\": {body}}}}}\n```")
        };
        let cases: [(&str, NotStarted); 9] = [
            ("NaN", NotStarted::NonFiniteNumber),
            ("-Infinity", NotStarted::NonFiniteNumber),
            ("1e999", NotStarted::NonFiniteNumber),
            ("9223372036854775808", NotStarted::IntegerOutOfRange),
            ("-9223372036854775809", NotStarted::IntegerOutOfRange),
            ("\"\\ud800\"", NotStarted::LoneSurrogate),
            ("\"\\udc00\\ud83d\\ude00\"", NotStarted::LoneSurrogate),
            ("\"\\ud800\\u0041\"", NotStarted::LoneSurrogate),
            (
                "77777777777777777777777777777777777777",
                NotStarted::IntegerOutOfRange,
            ),
        ];
        for (body, why) in cases {
            assert_eq!(parse_result(&wrap(body)), Err(why), "{body}");
        }
        let long = "7".repeat(4001);
        assert_eq!(
            parse_result(&wrap(&long)),
            Err(NotStarted::IntegerOutOfRange)
        );
        let deep =
            |levels: usize| format!("{{\"a\": {}{}}}", "[".repeat(levels), "]".repeat(levels));
        assert!(parse_result(&deep(MAX_NESTING - 1)).is_ok_and(|found| found.is_some()));
        assert_eq!(
            parse_result(&deep(MAX_NESTING)),
            Err(NotStarted::NestingTooDeep)
        );
    }

    #[test]
    fn a_syntax_error_beats_an_unsupported_feature() {
        let cases = [
            "{\"n\": NaN,}",
            "{\"n\": 99999999999999999999 x}",
            "{\"n\": \"\\ud800\",}",
            "{\"n\": 1e999,}",
        ];
        for body in cases {
            assert_eq!(
                parse_result(&format!("```json\n{body}\n```")),
                Ok(None),
                "{body}"
            );
        }
    }

    #[test]
    fn repr_matches_python() {
        let cases: [(&str, Option<&str>); 9] = [
            ("a", Some("'a'")),
            ("it's", Some("\"it's\"")),
            ("say \"hi\"", Some("'say \"hi\"'")),
            ("it's \"x\"", Some("'it\\'s \"x\"'")),
            ("a\\b", Some("'a\\\\b'")),
            ("a\nb", Some("'a\\nb'")),
            ("a\tb\rc", Some("'a\\tb\\rc'")),
            ("a\u{1}\u{1f}\u{7f}b", Some("'a\\x01\\x1f\\x7fb'")),
            ("caf\u{e9}", None),
        ];
        for (input, expected) in cases {
            assert_eq!(py_str_repr(input).as_deref(), expected, "{input:?}");
        }
    }

    #[test]
    fn the_schema_tree_names_only_what_the_file_uses() {
        // Every property of the file is reachable and nothing else: a wrong type at each path gives Python's text.
        let cases: [(&str, &str); 6] = [
            (r#"{"task_id":"T","state":"FAILED","needs":1}"#, "/needs: expected string|array|object, got int"),
            (r#"{"task_id":"T","state":"FAILED","outputs":[{"uri":1}]}"#, "/outputs/0/uri: expected string, got int"),
            (r#"{"task_id":"T","state":"FAILED","error":{"message":null}}"#, "/error/message: expected string, got NoneType"),
            (r#"{"task_id":"T","state":"FAILED","verdicts":{"qa":{"findings":{}}}}"#, "/verdicts/qa/findings: expected array, got dict"),
            (r#"{"task_id":"T","state":"FAILED","verdicts":{"qa":3}}"#, "/verdicts/qa: expected object, got int"),
            (r#"{"state":"X","task_id":1,"gate":2.5}"#, "/state: 'X' not one of ['IN_PROGRESS', 'IN_REVIEW', 'FAILED', 'BLOCKED']; /task_id: expected string, got int; /gate: expected string, got float"),
        ];
        for (input, message) in cases {
            let parsed =
                parse_json(input).unwrap_or_else(|err| panic!("{input}: {}", err.message()));
            match validate_result(Some(&parsed), "T") {
                Err(ResultError::Contract(err)) => {
                    assert_eq!(
                        err.message(),
                        format!("E-CONTRACT: task.result invalid: {message}"),
                        "{input}"
                    )
                }
                other => panic!("{input}: {other:?}"),
            }
        }
    }
}
