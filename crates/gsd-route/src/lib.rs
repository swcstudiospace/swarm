//! Pure route logic for the Rust engine's read-only `orch_status` route (P5 slice 2, D-P5-11).
//!
//! Every function here turns plain data into the Python-shaped output: the JSON writer and parser,
//! the seam error object, the `orch_status` argv parser, and the shaping of Task Store rows into the
//! list and history answers. Nothing here opens a database, a file, a process, an async runtime or
//! a clock. `gsd-adapter` reads the Task Store with sqlx, turns each row into [`Fields`] in column
//! order, and calls these functions in the order the Python scripts run them, so error precedence
//! matches the Python engine.

use std::collections::HashMap;

use gsd_codes::ErrorCode;

mod orch_status_wire;
pub mod pydumps;
pub mod transition_plan;

pub use orch_status_wire::{decode_reply_wire, orch_status_invoke, orch_status_reply};

/// Ordered columns of one Task Store row, typed as SQLite stored them (Python `dict(row)`).
pub type Fields = Vec<(String, Json)>;

/// Latest verdict per gate: gate name and the full verdict row.
pub type LatestRow = (String, Fields);

/// Deepest JSON nesting the parser accepts.
const MAX_JSON_DEPTH: usize = 128;

/// Null returned by reference for absent fields.
static NULL_JSON: Json = Json::Null;

/// Order-preserving JSON value that renders exactly as Python's `json.dumps(value, indent=2)`.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    /// JSON `null` (Python `None`).
    Null,
    /// JSON boolean (Python `bool`).
    Bool(bool),
    /// JSON integer (Python `int`).
    Int(i64),
    /// JSON number with a fraction or exponent (Python `float`).
    Float(f64),
    /// JSON string (Python `str`).
    Str(String),
    /// JSON array (Python `list`).
    Arr(Vec<Json>),
    /// JSON object with insertion-ordered keys (Python `dict`).
    Obj(Vec<(String, Json)>),
}

/// Builds a string value.
pub fn jstr(text: &str) -> Json {
    Json::Str(text.to_string())
}

/// Renders `value` exactly as Python's `json.dumps(value, indent=2)` with `ensure_ascii`.
pub fn to_python_json(value: &Json) -> String {
    let mut out = String::new();
    write_value(value, 0, &mut out);
    out
}

fn push_indent(out: &mut String, level: usize) {
    for _ in 0..level {
        out.push_str("  ");
    }
}

fn write_value(value: &Json, level: usize, out: &mut String) {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Int(number) => out.push_str(&number.to_string()),
        Json::Float(number) => out.push_str(&py_float(*number)),
        Json::Str(text) => write_str(text, out),
        Json::Arr(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push('\n');
                push_indent(out, level + 1);
                write_value(item, level + 1, out);
            }
            out.push('\n');
            push_indent(out, level);
            out.push(']');
        }
        Json::Obj(fields) => {
            if fields.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            for (index, (key, item)) in fields.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push('\n');
                push_indent(out, level + 1);
                write_str(key, out);
                out.push_str(": ");
                write_value(item, level + 1, out);
            }
            out.push('\n');
            push_indent(out, level);
            out.push('}');
        }
    }
}

/// Writes a JSON string with Python's `ensure_ascii` escaping.
fn write_str(text: &str, out: &mut String) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) >= 0x20 && (c as u32) < 0x7f => out.push(c),
            c => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units).iter() {
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
        }
    }
    out.push('"');
}

/// Formats a float the way Python's `float.__repr__` does.
///
/// The digits are the shortest that read back as `number`; exponent form is used below `1e-4` and from `1e16` on.
pub fn py_float(number: f64) -> String {
    if number.is_nan() {
        return "NaN".to_string();
    }
    if number.is_infinite() {
        return if number > 0.0 {
            "Infinity".to_string()
        } else {
            "-Infinity".to_string()
        };
    }
    let scientific = shortest_scientific(number);
    let Some((mantissa, exponent)) = scientific.split_once('e') else {
        return scientific;
    };
    let Ok(exponent) = exponent.parse::<i32>() else {
        return scientific;
    };
    if !(-4..16).contains(&exponent) {
        let sign = if exponent < 0 { '-' } else { '+' };
        return format!("{mantissa}e{sign}{:02}", exponent.unsigned_abs());
    }
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let mut out = String::new();
    if mantissa.starts_with('-') {
        out.push('-');
    }
    match usize::try_from(exponent) {
        Ok(point) if digits.len() > point + 1 => {
            out.push_str(&digits[..=point]);
            out.push('.');
            out.push_str(&digits[point + 1..]);
        }
        Ok(point) => {
            out.push_str(&digits);
            out.extend(std::iter::repeat_n('0', point + 1 - digits.len()));
            out.push_str(".0");
        }
        Err(_) => {
            out.push_str("0.");
            out.extend(std::iter::repeat_n(
                '0',
                exponent.unsigned_abs() as usize - 1,
            ));
            out.push_str(&digits);
        }
    }
    out
}

/// Rust's shortest `{:e}` text for a finite `number`, moved to the even digit where Python would pick it.
///
/// When `number` lies exactly halfway between two candidates of the shortest length (`-749382848194079.25`), Rust rounds the
/// tie up and Python's `repr` rounds it to the even last digit (`-749382848194079.2`). A tie leaves one odd and one even
/// neighbour, so only a result with an odd last digit can be the wrong one: for those the correctly rounded (half to even)
/// text of the same length replaces it, if it still reads back as `number`.
fn shortest_scientific(number: f64) -> String {
    let shortest = format!("{number:e}");
    let Some((mantissa, _)) = shortest.split_once('e') else {
        return shortest;
    };
    if mantissa.bytes().last().is_none_or(|digit| digit % 2 == 0) {
        return shortest;
    }
    let digits = mantissa.bytes().filter(u8::is_ascii_digit).count();
    let rounded = format!("{number:.prec$e}", prec = digits - 1);
    match rounded.parse::<f64>() {
        Ok(read_back) if read_back == number => rounded,
        _ => shortest,
    }
}

/// Parses JSON text into an order-preserving [`Json`], keeping Python's int/float split.
pub fn parse_json(text: &str) -> Result<Json, SeamError> {
    parse_json_checked(text).map_err(|_| invalid_json())
}

/// Why [`parse_json_checked`] refused a text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum JsonFault {
    /// Not JSON that Python's `json` accepts.
    Invalid,
    /// JSON whose only defect is an integer outside i64; Python's `int` holds it, so the host decides.
    IntegerOutOfRange,
}

fn parse_json_checked(text: &str) -> Result<Json, JsonFault> {
    let mut parser = Parser {
        bytes: text.as_bytes(),
        pos: 0,
        overflowed: false,
    };
    let value = parser.value(0).ok_or(JsonFault::Invalid)?;
    parser.skip_ws();
    if parser.pos != parser.bytes.len() {
        return Err(JsonFault::Invalid);
    }
    if parser.overflowed {
        return Err(JsonFault::IntegerOutOfRange);
    }
    Ok(value)
}

fn invalid_json() -> SeamError {
    SeamError::new(ErrorCode::Input, "invalid JSON")
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
    /// Set when an integer literal exceeds i64; parsing goes on so that a later syntax error still reports as invalid JSON.
    overflowed: bool,
}

impl Parser<'_> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn eat(&mut self, byte: u8) -> Option<()> {
        if self.peek() == Some(byte) {
            self.pos += 1;
            Some(())
        } else {
            None
        }
    }

    fn value(&mut self, depth: usize) -> Option<Json> {
        if depth > MAX_JSON_DEPTH {
            return None;
        }
        self.skip_ws();
        match self.peek()? {
            b'{' => self.object(depth),
            b'[' => self.array(depth),
            b'"' => self.string().map(Json::Str),
            b't' => self.literal(b"true", Json::Bool(true)),
            b'f' => self.literal(b"false", Json::Bool(false)),
            b'n' => self.literal(b"null", Json::Null),
            b'-' | b'0'..=b'9' => self.number(),
            _ => None,
        }
    }

    fn literal(&mut self, word: &[u8], value: Json) -> Option<Json> {
        if self.bytes.get(self.pos..)?.starts_with(word) {
            self.pos += word.len();
            Some(value)
        } else {
            None
        }
    }

    fn object(&mut self, depth: usize) -> Option<Json> {
        self.eat(b'{')?;
        let mut fields: Vec<(String, Json)> = Vec::new();
        let mut index: HashMap<String, usize> = HashMap::new();
        self.skip_ws();
        if self.eat(b'}').is_some() {
            return Some(Json::Obj(fields));
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
            if self.eat(b',').is_some() {
                continue;
            }
            self.eat(b'}')?;
            return Some(Json::Obj(fields));
        }
    }

    fn array(&mut self, depth: usize) -> Option<Json> {
        self.eat(b'[')?;
        let mut items: Vec<Json> = Vec::new();
        self.skip_ws();
        if self.eat(b']').is_some() {
            return Some(Json::Arr(items));
        }
        loop {
            items.push(self.value(depth + 1)?);
            self.skip_ws();
            if self.eat(b',').is_some() {
                continue;
            }
            self.eat(b']')?;
            return Some(Json::Arr(items));
        }
    }

    fn string(&mut self) -> Option<String> {
        self.eat(b'"')?;
        let mut buf: Vec<u8> = Vec::new();
        loop {
            let byte = self.peek()?;
            self.pos += 1;
            match byte {
                b'"' => break,
                b'\\' => {
                    let escape = self.peek()?;
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
                        _ => return None,
                    }
                }
                0x00..=0x1f => return None,
                other => buf.push(other),
            }
        }
        String::from_utf8(buf).ok()
    }

    fn unicode_escape(&mut self) -> Option<char> {
        let first = self.hex4()?;
        if (0xD800..0xDC00).contains(&first) {
            if self.bytes.get(self.pos..self.pos + 2) == Some(&b"\\u"[..]) {
                let save = self.pos;
                self.pos += 2;
                let second = self.hex4()?;
                if (0xDC00..0xE000).contains(&second) {
                    let code = 0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00);
                    return Some(char::from_u32(code).unwrap_or('\u{FFFD}'));
                }
                self.pos = save;
            }
            return Some('\u{FFFD}');
        }
        Some(char::from_u32(first).unwrap_or('\u{FFFD}'))
    }

    fn hex4(&mut self) -> Option<u32> {
        let slice = self.bytes.get(self.pos..self.pos + 4)?;
        let value = slice.iter().try_fold(0u32, |acc, &byte| {
            Some(acc * 16 + char::from(byte).to_digit(16)?)
        })?;
        self.pos += 4;
        Some(value)
    }

    fn number(&mut self) -> Option<Json> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek()? {
            b'0' => self.pos += 1,
            b'1'..=b'9' => {
                self.digits();
            }
            _ => return None,
        }
        let mut is_float = false;
        if self.peek() == Some(b'.') {
            is_float = true;
            self.pos += 1;
            if !self.digits() {
                return None;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            is_float = true;
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if !self.digits() {
                return None;
            }
        }
        let text = std::str::from_utf8(self.bytes.get(start..self.pos)?).ok()?;
        if is_float {
            text.parse::<f64>().ok().map(Json::Float)
        } else if let Ok(number) = text.parse::<i64>() {
            Some(Json::Int(number))
        } else {
            // The grammar above admits only digits here, so the only failure is i64 overflow. The value is
            // not used: the text is refused once it has been checked whole.
            self.overflowed = true;
            text.parse::<f64>().ok().map(Json::Float)
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

/// Inserts or replaces `key`, keeping the first insertion position (Python `dict` semantics).
fn upsert<V>(items: &mut Vec<(String, V)>, key: String, value: V) {
    match items.iter().position(|(existing, _)| *existing == key) {
        Some(index) => items[index].1 = value,
        None => items.push((key, value)),
    }
}

/// Looks up `key` in an ordered object's fields.
pub fn obj_get<'a>(fields: &'a [(String, Json)], key: &str) -> Option<&'a Json> {
    fields
        .iter()
        .find(|(name, _)| name.as_str() == key)
        .map(|(_, value)| value)
}

/// A failure carrying one taxonomy code, the Python-shaped message and the `details` object.
///
/// `details` holds what `SwarmError(code, message, **details)` carries (`task_id=tid` on the write routes). It is [`Json`]
/// data, so the type is `PartialEq` only: a float inside `details` has no `Eq`.
#[derive(Debug, Clone, PartialEq)]
pub struct SeamError {
    code: ErrorCode,
    message: String,
    details: Fields,
}

impl SeamError {
    /// Builds an error whose message is prefixed `CODE: ` the way `SwarmError` formats it.
    pub fn new(code: ErrorCode, message: impl AsRef<str>) -> Self {
        Self {
            code,
            message: format!("{}: {}", code.as_str(), message.as_ref()),
            details: Vec::new(),
        }
    }

    /// Builds an error whose message is used verbatim (generic Python exceptions carry no prefix).
    pub fn unprefixed(code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            details: Vec::new(),
        }
    }

    /// Sets the `details` object, as `SwarmError(code, message, **details)` does: keys keep the order given and a repeated key
    /// keeps its first position with the last value, like the `dict` Python builds from keyword arguments.
    ///
    /// A call replaces any details set before. Without a call `details` stays `{}`, so an error built by [`SeamError::new`]
    /// or [`SeamError::unprefixed`] renders as it always did.
    #[must_use]
    pub fn with_details(mut self, details: Fields) -> Self {
        self.details = Vec::with_capacity(details.len());
        for (key, value) in details {
            upsert(&mut self.details, key, value);
        }
        self
    }

    /// Returns the taxonomy code.
    pub fn code(&self) -> ErrorCode {
        self.code
    }

    /// Returns the message as Python would print it.
    pub fn message(&self) -> &str {
        &self.message
    }

    /// Returns the `details` fields in the order they render.
    pub fn details(&self) -> &[(String, Json)] {
        &self.details
    }

    /// Renders the `SwarmError.to_dict()` shape: `code`, `message`, `details`, `handling`.
    pub fn to_json(&self) -> Json {
        Json::Obj(vec![
            ("code".to_string(), jstr(self.code.as_str())),
            ("message".to_string(), jstr(&self.message)),
            ("details".to_string(), Json::Obj(self.details.clone())),
            ("handling".to_string(), jstr(handling(self.code))),
        ])
    }
}

/// The standard handling text for a code (`swarm/errors.py::_HANDLING`).
fn handling(code: ErrorCode) -> &'static str {
    match code {
        ErrorCode::Input => "Nack + task.status FAILED; notify artifact producer",
        ErrorCode::Timeout => "Checkpoint, release lease, requeue once, then ESCALATED",
        ErrorCode::Dep => "Exponential backoff 1s→60s (jitter, max 5), then degraded mode",
        ErrorCode::Capacity => "Refuse bid; backpressure in next heartbeat",
        ErrorCode::Contract => "Bounded fix loop per agent spec",
        ErrorCode::Policy => "Fail-closed; escalate to A10/A01",
        ErrorCode::Internal => "Checkpoint + crash; A01 requeue; 3 strikes ⇒ ESCALATED",
    }
}

/// The stdout and exit code of one invocation, as the caller would see them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reply {
    /// Exit code: 0 success, 1 expected failure, 2 error.
    pub exit_code: i32,
    /// Stdout bytes: one JSON object followed by a newline.
    pub stdout: String,
}

/// Appends the `agent` and `script` keys every Python script adds, and renders stdout.
pub fn finish(mut fields: Fields, agent: &str, script: &str) -> Reply {
    fields.push(("agent".to_string(), jstr(agent)));
    fields.push(("script".to_string(), jstr(script)));
    Reply {
        exit_code: 0,
        stdout: format!("{}\n", to_python_json(&Json::Obj(fields))),
    }
}

/// Parsed flags of the read-only `orch_status` route (argparse-compatible for the accepted subset).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReadArgs {
    /// `--json` was given.
    pub json: bool,
    /// `--root` or `--repo` value.
    pub root: Option<String>,
    /// `--correlation-id` value.
    pub correlation_id: Option<String>,
    /// `--task-id` value.
    pub task_id: Option<String>,
    /// `--history` value.
    pub history: Option<String>,
}

/// Parses `orch_status` argv for the read-only route. Returns `None` for any flag outside the read
/// subset (`--transition`, `--ingest`, `--advisory`, `--reason`, unknown tokens), so the caller
/// routes that call to Python.
pub fn parse_orch_status_read(argv: &[String]) -> Option<ReadArgs> {
    let mut out = ReadArgs::default();
    let mut index = 0;
    while index < argv.len() {
        let token = argv[index].as_str();
        let (name, inline) = match token.split_once('=') {
            Some((head, value)) if head.starts_with("--") => (head, Some(value.to_string())),
            _ => (token, None),
        };
        match name {
            "--json" | "--dry-run" => {
                if inline.is_some() {
                    return None;
                }
                if name == "--json" {
                    out.json = true;
                }
            }
            "--root" | "--repo" | "--correlation-id" | "--task-id" | "--history" => {
                let value = match inline {
                    Some(value) => value,
                    None => {
                        index += 1;
                        let next = argv.get(index)?;
                        if next.starts_with('-') {
                            return None;
                        }
                        next.clone()
                    }
                };
                match name {
                    "--root" | "--repo" => out.root = Some(value),
                    "--correlation-id" => out.correlation_id = Some(value),
                    "--task-id" => out.task_id = Some(value),
                    _ => out.history = Some(value),
                }
            }
            _ => return None,
        }
        index += 1;
    }
    Some(out)
}

/// Returns true for an id matching `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` without `..`.
pub fn is_safe_id(value: &str) -> bool {
    let length = value.chars().count();
    (1..=128).contains(&length)
        && value
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphanumeric())
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
        && !value.contains("..")
}

fn broken_id_rule(value: &str) -> &'static str {
    if value.is_empty() {
        "is empty"
    } else if value.chars().count() > 128 {
        "is longer than 128 characters"
    } else if !value
        .chars()
        .next()
        .is_some_and(|c| c.is_ascii_alphanumeric())
    {
        "does not start with a letter or digit"
    } else if !value
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
    {
        "contains a character outside [A-Za-z0-9._-]"
    } else {
        "contains '..'"
    }
}

/// Rejects an id that [`is_safe_id`] refuses, naming the broken rule; `what` labels the id in the message.
pub fn check_safe_id(value: &str, what: &str) -> Result<(), SeamError> {
    if is_safe_id(value) {
        return Ok(());
    }
    Err(SeamError::new(
        ErrorCode::Input,
        format!(
            "invalid {what}: {} characters, {}; expected [A-Za-z0-9._-], starting alphanumeric, no '..', at most 128 chars",
            value.chars().count(),
            broken_id_rule(value)
        ),
    ))
}

/// A task row as `TaskStore._row` shapes it: JSON columns parsed, `notes_json` attached.
#[derive(Debug, Clone)]
pub struct Task {
    /// The `task_id` column.
    pub task_id: String,
    /// The `state` column.
    pub state: String,
    fields: Fields,
    notes: Json,
    deps: Vec<Json>,
}

/// Parses one JSON text column of a task row. An out-of-range integer in `budget` is refused as Dep;
/// every other column keeps the invalid-JSON refusal.
fn row_json(column: &str, text: &str) -> Result<Json, SeamError> {
    match parse_json_checked(text) {
        Ok(value) => Ok(value),
        Err(JsonFault::IntegerOutOfRange) if column == "budget" => Err(out_of_range_error(column)),
        Err(_) => Err(invalid_json()),
    }
}

/// The Dep refusal for a column holding an integer outside i64: Python's `int` holds it, so the Python host decides.
fn out_of_range_error(column: &str) -> SeamError {
    SeamError::new(
        ErrorCode::Dep,
        format!("{column} holds an integer outside the rust engine's range; use GSD_ENGINE=python"),
    )
}

impl Task {
    /// Shapes one `tasks` row: JSON text columns are parsed, `depends_on` must be a list, and `notes` becomes an object.
    /// An integer outside i64 in `budget` or `notes` is refused as Dep (the Python host decides), not read as invalid or emptied.
    pub fn from_row(mut fields: Fields) -> Result<Self, SeamError> {
        for column in ["inputs", "outputs", "acceptance", "budget", "depends_on"] {
            let default = if column == "budget" { "{}" } else { "[]" };
            let parsed = match obj_get(&fields, column) {
                Some(Json::Str(text)) if !text.is_empty() => row_json(column, text)?,
                None | Some(Json::Null) | Some(Json::Str(_)) => row_json(column, default)?,
                Some(_) => {
                    return Err(SeamError::new(
                        ErrorCode::Internal,
                        format!("{column} is not text"),
                    ))
                }
            };
            upsert(&mut fields, column.to_string(), parsed);
        }
        let task_id = match obj_get(&fields, "task_id") {
            Some(Json::Str(id)) => id.clone(),
            _ => {
                return Err(SeamError::new(
                    ErrorCode::Internal,
                    "task row without task_id",
                ))
            }
        };
        let state = match obj_get(&fields, "state") {
            Some(Json::Str(state)) => state.clone(),
            _ => {
                return Err(SeamError::new(
                    ErrorCode::Internal,
                    "task row without state",
                ))
            }
        };
        let notes = match obj_get(&fields, "notes") {
            Some(Json::Str(text)) if !text.is_empty() => match parse_json_checked(text) {
                Ok(value @ Json::Obj(_)) => value,
                Err(JsonFault::IntegerOutOfRange) => return Err(out_of_range_error("notes")),
                _ => Json::Obj(Vec::new()),
            },
            _ => Json::Obj(Vec::new()),
        };
        let deps = match obj_get(&fields, "depends_on") {
            Some(Json::Arr(items)) => items.clone(),
            _ => {
                return Err(SeamError::new(
                    ErrorCode::Internal,
                    "depends_on is not a list",
                ))
            }
        };
        Ok(Self {
            task_id,
            state,
            fields,
            notes,
            deps,
        })
    }

    /// `float(notes.verdicts_since or 0)`: the lower `ts` bound of `TaskStore._latest_rows`.
    pub fn verdict_since(&self) -> Result<f64, SeamError> {
        float_or_zero(self.note("verdicts_since"))
    }

    fn field(&self, key: &str) -> &Json {
        obj_get(&self.fields, key).unwrap_or(&NULL_JSON)
    }

    fn note(&self, key: &str) -> &Json {
        match &self.notes {
            Json::Obj(fields) => obj_get(fields, key).unwrap_or(&NULL_JSON),
            _ => &NULL_JSON,
        }
    }
}

/// Python `float(value or 0)` for a stored `verdicts_since` note.
fn float_or_zero(value: &Json) -> Result<f64, SeamError> {
    match value {
        Json::Null | Json::Bool(false) | Json::Int(0) => Ok(0.0),
        Json::Float(number) if *number == 0.0 => Ok(0.0),
        Json::Str(text) if text.is_empty() => Ok(0.0),
        Json::Bool(true) => Ok(1.0),
        Json::Int(number) => Ok(*number as f64),
        Json::Float(number) => Ok(*number),
        Json::Str(text) => text.trim().parse::<f64>().map_err(|_| {
            SeamError::unprefixed(ErrorCode::Input, "could not convert string to float")
        }),
        _ => Err(SeamError::new(
            ErrorCode::Internal,
            "verdicts_since is not a number",
        )),
    }
}

/// `TaskStore._gates_of`: the `notes.gates` override, else the risk-class default.
fn gates_of(task: &Task) -> Result<Vec<String>, SeamError> {
    match task.note("gates") {
        Json::Null => match task.field("risk_class") {
            Json::Str(risk) => match risk.as_str() {
                "low" => Ok(strings(&["review"])),
                "medium" => Ok(strings(&["review", "quality"])),
                "high" => Ok(strings(&["review", "quality", "security", "release"])),
                _ => Err(SeamError::unprefixed(
                    ErrorCode::Input,
                    "KeyError: risk_class has no default gates",
                )),
            },
            Json::Null => Err(SeamError::unprefixed(ErrorCode::Input, "KeyError: None")),
            _ => Err(SeamError::new(
                ErrorCode::Internal,
                "risk_class is not text",
            )),
        },
        Json::Arr(items) => items
            .iter()
            .map(|gate| match gate {
                Json::Str(name) => Ok(name.clone()),
                _ => Err(SeamError::new(ErrorCode::Internal, "gate name is not text")),
            })
            .collect(),
        _ => Err(SeamError::new(
            ErrorCode::Internal,
            "unsupported notes.gates override",
        )),
    }
}

fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|item| item.to_string()).collect()
}

/// Adds one verdict row to the latest-per-gate list, as `TaskStore._latest_rows` does for each row in id order.
pub fn push_latest(latest: &mut Vec<LatestRow>, mut fields: Fields) -> Result<(), SeamError> {
    let findings = match obj_get(&fields, "findings") {
        Some(Json::Str(text)) => parse_json(text)?,
        _ => {
            return Err(SeamError::new(
                ErrorCode::Internal,
                "verdict row without findings",
            ))
        }
    };
    upsert(&mut fields, "findings".to_string(), findings);
    let gate = match obj_get(&fields, "gate") {
        Some(Json::Str(name)) => name.clone(),
        _ => {
            return Err(SeamError::new(
                ErrorCode::Internal,
                "verdict row without gate",
            ))
        }
    };
    upsert(latest, gate, fields);
    Ok(())
}

/// `TaskStore.missing_gate_reasons` for the rows already loaded.
///
/// A row carrying a signed envelope needs `verify_envelope`, which this engine does not implement,
/// so it refuses with `E-DEP` instead of guessing.
fn missing_reasons(
    gates: &[String],
    latest: &[LatestRow],
) -> Result<Vec<(String, String)>, SeamError> {
    let mut reasons: Vec<(String, String)> = Vec::new();
    for gate in gates {
        let reason = match latest.iter().find(|(name, _)| name == gate) {
            None => "absent",
            Some((_, fields)) => match obj_get(fields, "envelope_json") {
                None | Some(Json::Null) => "unsigned",
                Some(Json::Str(envelope)) if envelope.is_empty() => "unsigned",
                Some(_) => {
                    return Err(SeamError::new(
                        ErrorCode::Dep,
                        format!(
                            "gate {gate}: envelope signature verification is not implemented in the rust engine; use GSD_ENGINE=python"
                        ),
                    ))
                }
            },
        };
        upsert(&mut reasons, gate.clone(), reason.to_string());
    }
    Ok(reasons)
}

fn int_field(task: &Task, key: &str) -> Result<i64, SeamError> {
    match task.field(key) {
        Json::Int(number) => Ok(*number),
        _ => Err(SeamError::new(
            ErrorCode::Internal,
            format!("{key} is not an integer"),
        )),
    }
}

fn outputs_len(task: &Task) -> Result<i64, SeamError> {
    match task.field("outputs") {
        Json::Arr(items) => Ok(items.len() as i64),
        Json::Obj(fields) => Ok(fields.len() as i64),
        Json::Str(text) => Ok(text.chars().count() as i64),
        _ => Err(SeamError::new(ErrorCode::Internal, "outputs has no length")),
    }
}

/// Whether one non-blank run-log line counts toward the `orch_status` event total for `wanted`
/// (`None` counts every record). A line that is not a JSON object is an `E-INTERNAL` error.
pub fn run_log_counts(line: &str, wanted: Option<&str>) -> Result<bool, SeamError> {
    let record = parse_json(line)?;
    let Json::Obj(fields) = &record else {
        return Err(SeamError::new(
            ErrorCode::Internal,
            "run log record is not an object",
        ));
    };
    Ok(match wanted {
        Some(wanted) => {
            matches!(obj_get(fields, "correlation_id"), Some(Json::Str(found)) if found.as_str() == wanted)
        }
        None => true,
    })
}

/// Ids of the tasks whose state already satisfies a dependency (`IN_REVIEW`, `APPROVED`, `DONE`).
pub fn satisfied_ids(tasks: &[Task]) -> Vec<String> {
    tasks
        .iter()
        .filter(|t| matches!(t.state.as_str(), "IN_REVIEW" | "APPROVED" | "DONE"))
        .map(|t| t.task_id.clone())
        .collect()
}

/// Counts one task state in first-seen order (the `counts` table of `orch_status`).
pub fn count_state(counts: &mut Vec<(String, i64)>, state: &str) {
    let seen = counts
        .iter()
        .find(|(name, _)| name.as_str() == state)
        .map(|(_, count)| *count)
        .unwrap_or(0);
    upsert(counts, state.to_string(), seen + 1);
}

/// One row of the `orch_status` task table, in the Python output order.
pub struct StatusRow {
    task_id: String,
    agent: Json,
    state: String,
    depth: Json,
    attempt: i64,
    rework: i64,
    ready: bool,
    gates: Vec<String>,
    verdicts: Fields,
    missing: Vec<String>,
    depends_on: Json,
    outputs: i64,
}

/// Builds one `orch_status` task row: gates, missing reasons (for `IN_REVIEW` tasks), verdicts and
/// the ready flag. `latest` is the output of [`push_latest`] for the task; `satisfied` is the output
/// of [`satisfied_ids`] for the whole table.
pub fn status_row(
    task: &Task,
    latest: &[LatestRow],
    satisfied: &[String],
) -> Result<StatusRow, SeamError> {
    let gates = gates_of(task)?;
    let missing: Vec<String> = if task.state == "IN_REVIEW" {
        missing_reasons(&gates, latest)?
            .into_iter()
            .map(|(gate, _)| gate)
            .collect()
    } else {
        Vec::new()
    };
    let verdicts: Fields = latest
        .iter()
        .map(|(gate, row)| {
            (
                gate.clone(),
                obj_get(row, "verdict").cloned().unwrap_or(Json::Null),
            )
        })
        .collect();
    let ready = matches!(
        task.state.as_str(),
        "PLANNED" | "RETRY" | "CREATED" | "VALIDATED"
    ) && task.deps.iter().all(|dep| match dep {
        Json::Str(id) => satisfied.iter().any(|ok| ok == id),
        _ => false,
    });
    Ok(StatusRow {
        task_id: task.task_id.clone(),
        agent: task.field("agent_id").clone(),
        state: task.state.clone(),
        depth: task.field("dag_depth").clone(),
        attempt: int_field(task, "attempt")?,
        rework: int_field(task, "rework_loops")?,
        ready,
        gates,
        verdicts,
        missing,
        depends_on: task.field("depends_on").clone(),
        outputs: outputs_len(task)?,
    })
}

impl StatusRow {
    fn to_json(&self) -> Json {
        Json::Obj(vec![
            ("task_id".to_string(), jstr(&self.task_id)),
            ("agent".to_string(), self.agent.clone()),
            ("state".to_string(), jstr(&self.state)),
            ("depth".to_string(), self.depth.clone()),
            ("attempt".to_string(), Json::Int(self.attempt)),
            ("rework".to_string(), Json::Int(self.rework)),
            ("ready".to_string(), Json::Bool(self.ready)),
            (
                "gates_required".to_string(),
                Json::Arr(self.gates.iter().map(|g| jstr(g)).collect()),
            ),
            ("verdicts".to_string(), Json::Obj(self.verdicts.clone())),
            (
                "missing_gates".to_string(),
                Json::Arr(self.missing.iter().map(|g| jstr(g)).collect()),
            ),
            ("depends_on".to_string(), self.depends_on.clone()),
            ("outputs".to_string(), Json::Int(self.outputs)),
        ])
    }

    fn verdict_text(&self, gate: &str) -> String {
        match obj_get(&self.verdicts, gate) {
            Some(Json::Str(verdict)) => verdict.clone(),
            Some(Json::Null) => "None".to_string(),
            Some(other) => to_python_json(other),
            None => "·".to_string(),
        }
    }
}

fn pad(text: &str, width: usize) -> String {
    let length = text.chars().count();
    if length >= width {
        text.to_string()
    } else {
        format!("{}{}", text, " ".repeat(width - length))
    }
}

/// Builds the human `summary` text exactly as `orch_status.run` does.
fn status_summary(
    rows: &[StatusRow],
    counts: &[(String, i64)],
    escalated: &[String],
    complete: bool,
) -> String {
    let mut lines = vec![format!(
        "{}{}{}{}{}{}{}",
        pad("TASK", 14),
        pad("AGENT", 7),
        pad("STATE", 19),
        pad("ATT", 4),
        pad("RW", 3),
        pad("READY", 6),
        "GATES"
    )];
    for row in rows {
        let gates = if row.gates.is_empty() {
            "-".to_string()
        } else {
            row.gates
                .iter()
                .map(|gate| format!("{}={}", gate, row.verdict_text(gate)))
                .collect::<Vec<_>>()
                .join(",")
        };
        let agent = match &row.agent {
            Json::Str(name) if !name.is_empty() => name.clone(),
            _ => "?".to_string(),
        };
        lines.push(format!(
            "{}{}{}{}{}{}{}",
            pad(&row.task_id, 14),
            pad(&agent, 7),
            pad(&row.state, 19),
            pad(&row.attempt.to_string(), 4),
            pad(&row.rework.to_string(), 3),
            pad(if row.ready { "yes" } else { "" }, 6),
            gates
        ));
    }
    let counts_repr = counts
        .iter()
        .map(|(state, count)| format!("'{state}': {count}"))
        .collect::<Vec<_>>()
        .join(", ");
    let escalated_repr = if escalated.is_empty() {
        "none".to_string()
    } else {
        format!(
            "[{}]",
            escalated
                .iter()
                .map(|id| format!("'{id}'"))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };
    lines.push(format!(
        "\ncounts: {{{counts_repr}}}   escalated: {escalated_repr}   complete: {}",
        if complete { "True" } else { "False" }
    ));
    lines.join("\n")
}

fn correlation_json(correlation: Option<&str>) -> Json {
    match correlation {
        Some(value) => jstr(value),
        None => Json::Null,
    }
}

/// The `orch_status` answer when the store holds no task for the correlation.
pub fn empty_status(correlation: Option<&str>, db_path: &str, swarm_dir: &str) -> Fields {
    vec![
        ("status".to_string(), jstr("ok")),
        ("correlation_id".to_string(), correlation_json(correlation)),
        ("tasks".to_string(), Json::Arr(Vec::new())),
        ("db".to_string(), jstr(db_path)),
        ("swarm_dir".to_string(), jstr(swarm_dir)),
        (
            "summary".to_string(),
            jstr("no tasks (run orch_plan.py first)"),
        ),
    ]
}

/// The `orch_status` list answer: task rows, state counts, escalated and complete flags, the run-log
/// event count, the store paths and the human summary.
pub fn status_list(
    correlation: Option<&str>,
    rows: &[StatusRow],
    counts: &[(String, i64)],
    events: i64,
    db_path: &str,
    swarm_dir: &str,
) -> Fields {
    let escalated: Vec<String> = rows
        .iter()
        .filter(|r| r.state == "ESCALATED")
        .map(|r| r.task_id.clone())
        .collect();
    let complete = rows
        .iter()
        .all(|r| r.state == "DONE" || r.state == "CANCELLED");
    let summary = status_summary(rows, counts, &escalated, complete);
    vec![
        ("status".to_string(), jstr("ok")),
        ("correlation_id".to_string(), correlation_json(correlation)),
        (
            "tasks".to_string(),
            Json::Arr(rows.iter().map(StatusRow::to_json).collect()),
        ),
        (
            "counts".to_string(),
            Json::Obj(
                counts
                    .iter()
                    .map(|(state, count)| (state.clone(), Json::Int(*count)))
                    .collect(),
            ),
        ),
        (
            "escalated".to_string(),
            Json::Arr(escalated.iter().map(|id| jstr(id)).collect()),
        ),
        ("complete".to_string(), Json::Bool(complete)),
        ("events".to_string(), Json::Int(events)),
        ("db".to_string(), jstr(db_path)),
        ("swarm_dir".to_string(), jstr(swarm_dir)),
        ("summary".to_string(), jstr(&summary)),
    ]
}

/// The `orch_status --history` answer for one task: transitions, latest verdicts and missing gate
/// reasons. `history` holds the transition rows in id order; `latest` is built with [`push_latest`].
pub fn history_fields(
    task_id: &str,
    history: Vec<Json>,
    latest: Vec<LatestRow>,
    task: &Task,
) -> Result<Fields, SeamError> {
    let gates = gates_of(task)?;
    let missing = missing_reasons(&gates, &latest)?;
    Ok(vec![
        ("status".to_string(), jstr("ok")),
        ("task_id".to_string(), jstr(task_id)),
        ("history".to_string(), Json::Arr(history)),
        (
            "verdicts".to_string(),
            Json::Obj(
                latest
                    .into_iter()
                    .map(|(gate, row)| (gate, Json::Obj(row)))
                    .collect(),
            ),
        ),
        (
            "missing_gate_reasons".to_string(),
            Json::Obj(
                missing
                    .into_iter()
                    .map(|(gate, reason)| (gate, jstr(&reason)))
                    .collect(),
            ),
        ),
        (
            "summary".to_string(),
            jstr(&format!("history for {task_id}")),
        ),
    ])
}

// Everything below is declared after the code above so that no line the default guest's panic locations name moves, and each
// group sits behind its own feature (see `Cargo.toml`): the guest of the first batch turns on `write-routes` only, so the
// planners of the second batch and the host-only route code do not move its digest while they change.
#[cfg(feature = "write-routes")]
pub mod invoke;
#[cfg(feature = "write-routes")]
pub mod transition_wire;

#[cfg(feature = "write-routes")]
pub use invoke::core_invoke;

#[cfg(feature = "write-routes-2")]
pub mod gate_facts;
#[cfg(feature = "write-routes-2")]
pub mod ingest_plan;
#[cfg(feature = "write-routes-2")]
pub mod plan_derive;
#[cfg(feature = "write-routes-2")]
pub mod reconcile_plan;
#[cfg(feature = "write-routes-2")]
pub mod result_parse;

#[cfg(feature = "host-routes")]
pub mod ingest_args;
#[cfg(feature = "host-routes")]
pub mod transition_args;

#[cfg(test)]
mod tests {
    use super::*;

    type TestResult = Result<(), String>;

    fn message(error: SeamError) -> String {
        error.message().to_string()
    }

    fn cols(pairs: &[(&str, Json)]) -> Fields {
        pairs
            .iter()
            .map(|(key, value)| (key.to_string(), value.clone()))
            .collect()
    }

    fn task_row(id: &str, state: &str, depends_on: &str) -> Result<Task, String> {
        Task::from_row(cols(&[
            ("task_id", jstr(id)),
            ("state", jstr(state)),
            ("agent_id", jstr("A05")),
            ("dag_depth", Json::Int(1)),
            ("attempt", Json::Int(0)),
            ("rework_loops", Json::Int(0)),
            ("risk_class", jstr("low")),
            ("depends_on", jstr(depends_on)),
            ("notes", jstr("{}")),
        ]))
        .map_err(message)
    }

    fn task_fields(notes: &str, budget: &str) -> Fields {
        cols(&[
            ("task_id", jstr("T-N")),
            ("state", jstr("PLANNED")),
            ("agent_id", jstr("A05")),
            ("dag_depth", Json::Int(1)),
            ("attempt", Json::Int(0)),
            ("rework_loops", Json::Int(0)),
            ("risk_class", jstr("low")),
            ("depends_on", jstr("[]")),
            ("notes", jstr(notes)),
            ("budget", jstr(budget)),
        ])
    }

    #[test]
    fn python_json_layout_matches_json_dumps_indent_2() -> TestResult {
        let value = Json::Obj(vec![
            ("a".to_string(), Json::Arr(vec![Json::Int(1), jstr("é")])),
            ("b".to_string(), Json::Obj(Vec::new())),
            ("c".to_string(), Json::Arr(Vec::new())),
        ]);
        let expected = "{\n  \"a\": [\n    1,\n    \"\\u00e9\"\n  ],\n  \"b\": {},\n  \"c\": []\n}";
        assert_eq!(to_python_json(&value), expected);
        Ok(())
    }

    #[test]
    fn python_float_repr_matches_float_repr() -> TestResult {
        assert_eq!(py_float(1.0), "1.0");
        assert_eq!(py_float(1781234567.5), "1781234567.5");
        assert_eq!(py_float(1e16), "1e+16");
        assert_eq!(py_float(1.5e-5), "1.5e-05");
        assert_eq!(py_float(0.0001), "0.0001");
        assert_eq!(py_float(-0.0), "-0.0");
        Ok(())
    }

    #[test]
    fn python_float_repr_places_the_point_and_rounds_halfway_digits_to_even() -> TestResult {
        // Expected texts are Python's `repr`. The first three are exactly halfway between two shortest candidates: Rust's own
        // formatting rounds them up, Python to the even digit.
        for (number, expected) in [
            (-(749382848194079.0 + 0.25), "-749382848194079.2"),
            (749382848194079.0 + 0.25, "749382848194079.2"),
            (749382848194079.0 + 0.75, "749382848194079.8"),
            (1e15, "1000000000000000.0"),
            (9999999999999998.0, "9999999999999998.0"),
            (123456.0, "123456.0"),
            (12345678.9, "12345678.9"),
            (0.001, "0.001"),
            (0.00012345, "0.00012345"),
            (9.9999e-5, "9.9999e-05"),
            (1.5e300, "1.5e+300"),
            (5e-324, "5e-324"),
            (f64::MAX, "1.7976931348623157e+308"),
            (f64::NAN, "NaN"),
            (f64::NEG_INFINITY, "-Infinity"),
        ] {
            assert_eq!(py_float(number), expected);
        }
        Ok(())
    }

    #[test]
    fn parser_keeps_key_order_and_int_float_split() -> TestResult {
        let parsed = parse_json("{\"b\": 1, \"a\": 2.0, \"c\": [true, null]}").map_err(message)?;
        let expected = Json::Obj(vec![
            ("b".to_string(), Json::Int(1)),
            ("a".to_string(), Json::Float(2.0)),
            (
                "c".to_string(),
                Json::Arr(vec![Json::Bool(true), Json::Null]),
            ),
        ]);
        assert_eq!(parsed, expected);
        assert!(parse_json("{\"a\": 1} trailing").is_err());
        if let Json::Obj(fields) = &parsed {
            assert_eq!(obj_get(fields, "a"), Some(&Json::Float(2.0)));
            assert_eq!(obj_get(fields, "missing"), None);
        }
        Ok(())
    }

    #[test]
    fn seam_error_renders_the_error_object() -> TestResult {
        let error = SeamError::new(ErrorCode::Input, "unknown task nope");
        assert_eq!(error.message(), "E-INPUT: unknown task nope");
        let expected = "{\n  \"code\": \"E-INPUT\",\n  \"message\": \"E-INPUT: unknown task nope\",\n  \"details\": {},\n  \"handling\": \"Nack + task.status FAILED; notify artifact producer\"\n}";
        assert_eq!(to_python_json(&error.to_json()), expected);
        Ok(())
    }

    #[test]
    fn seam_error_details_follow_swarm_error_keyword_arguments() -> TestResult {
        let error =
            SeamError::new(ErrorCode::Contract, "concurrent transition").with_details(cols(&[
                ("task_id", jstr("T-1")),
                ("write_state", jstr("not_started")),
                ("task_id", jstr("T-2")),
            ]));
        assert_eq!(
            error.details(),
            cols(&[
                ("task_id", jstr("T-2")),
                ("write_state", jstr("not_started"))
            ])
        );
        let expected = "{\n  \"code\": \"E-CONTRACT\",\n  \"message\": \"E-CONTRACT: concurrent transition\",\n  \"details\": {\n    \"task_id\": \"T-2\",\n    \"write_state\": \"not_started\"\n  },\n  \"handling\": \"Bounded fix loop per agent spec\"\n}";
        assert_eq!(to_python_json(&error.to_json()), expected);
        let replaced = error.with_details(cols(&[("n", Json::Float(1.0))]));
        assert_eq!(replaced.details(), cols(&[("n", Json::Float(1.0))]));
        let plain = SeamError::unprefixed(ErrorCode::Input, "ValueError: x");
        assert!(plain.details().is_empty());
        assert!(to_python_json(&plain.to_json()).contains("\"details\": {},"));
        Ok(())
    }

    #[test]
    fn finish_appends_agent_and_script_last() -> TestResult {
        let reply = finish(
            vec![("status".to_string(), jstr("ok"))],
            "A01",
            "orch_status",
        );
        assert_eq!(reply.exit_code, 0);
        assert_eq!(
            reply.stdout,
            "{\n  \"status\": \"ok\",\n  \"agent\": \"A01\",\n  \"script\": \"orch_status\"\n}\n"
        );
        Ok(())
    }

    #[test]
    fn id_check_names_the_broken_rule() -> TestResult {
        assert!(is_safe_id("T-A"));
        assert!(!is_safe_id("../x"));
        let error = check_safe_id("../x", "task id").err().map(message);
        assert_eq!(
            error.as_deref(),
            Some("E-INPUT: invalid task id: 4 characters, does not start with a letter or digit; expected [A-Za-z0-9._-], starting alphanumeric, no '..', at most 128 chars")
        );
        Ok(())
    }

    #[test]
    fn orch_status_read_flags_stop_at_write_flags() -> TestResult {
        let argv = |items: &[&str]| items.iter().map(|s| s.to_string()).collect::<Vec<String>>();
        assert!(parse_orch_status_read(&argv(&["--json"])).is_some());
        assert!(parse_orch_status_read(&argv(&["--transition", "T", "DONE", "--json"])).is_none());
        let parsed = parse_orch_status_read(&argv(&["--history=T-A", "--json"]))
            .ok_or_else(|| "unparsed".to_string())?;
        assert_eq!(parsed.history.as_deref(), Some("T-A"));
        assert!(parsed.json);
        Ok(())
    }

    #[test]
    fn run_log_counts_filters_by_correlation() -> TestResult {
        assert!(run_log_counts("{\"correlation_id\":\"C1\"}", Some("C1")).map_err(message)?);
        assert!(!run_log_counts("{\"correlation_id\":\"C2\"}", Some("C1")).map_err(message)?);
        assert!(run_log_counts("{\"type\":\"x\"}", None).map_err(message)?);
        let error = run_log_counts("[1]", None).err().map(message);
        assert_eq!(
            error.as_deref(),
            Some("E-INTERNAL: run log record is not an object")
        );
        Ok(())
    }

    #[test]
    fn count_state_keeps_first_seen_order() -> TestResult {
        let mut counts: Vec<(String, i64)> = Vec::new();
        for state in ["PLANNED", "DONE", "PLANNED"] {
            count_state(&mut counts, state);
        }
        let value = Json::Obj(
            counts
                .iter()
                .map(|(state, count)| (state.clone(), Json::Int(*count)))
                .collect(),
        );
        assert_eq!(
            to_python_json(&value),
            "{\n  \"PLANNED\": 2,\n  \"DONE\": 1\n}"
        );
        Ok(())
    }

    #[test]
    fn status_row_ready_needs_every_dependency_satisfied() -> TestResult {
        let task = task_row("T-B", "PLANNED", "[\"T-A\"]")?;
        let row = status_row(&task, &[], &["T-A".to_string()]).map_err(message)?;
        let expected = "{\n  \"task_id\": \"T-B\",\n  \"agent\": \"A05\",\n  \"state\": \"PLANNED\",\n  \"depth\": 1,\n  \"attempt\": 0,\n  \"rework\": 0,\n  \"ready\": true,\n  \"gates_required\": [\n    \"review\"\n  ],\n  \"verdicts\": {},\n  \"missing_gates\": [],\n  \"depends_on\": [\n    \"T-A\"\n  ],\n  \"outputs\": 0\n}";
        assert_eq!(to_python_json(&row.to_json()), expected);
        let blocked = status_row(&task, &[], &[]).map_err(message)?;
        assert!(!blocked.ready);
        Ok(())
    }

    #[test]
    fn status_list_keeps_python_key_order_and_counts() -> TestResult {
        let task = task_row("T-B", "PLANNED", "[]")?;
        let row = status_row(&task, &[], &[]).map_err(message)?;
        let fields = status_list(
            Some("C1"),
            &[row],
            &[("PLANNED".to_string(), 1)],
            2,
            "db",
            "swarm",
        );
        let text = to_python_json(&Json::Obj(fields));
        assert!(text.starts_with(
            "{\n  \"status\": \"ok\",\n  \"correlation_id\": \"C1\",\n  \"tasks\": [\n    {\n      \"task_id\": \"T-B\",\n"
        ));
        assert!(text.contains(
            "  \"counts\": {\n    \"PLANNED\": 1\n  },\n  \"escalated\": [],\n  \"complete\": false,\n  \"events\": 2,\n  \"db\": \"db\",\n  \"swarm_dir\": \"swarm\",\n  \"summary\": \""
        ));
        assert!(text.contains("escalated: none   complete: False"));
        Ok(())
    }

    #[test]
    fn empty_status_answers_no_tasks_with_the_python_keys() -> TestResult {
        let reply = finish(
            empty_status(Some("C9"), "/x/tasks.db", "/x"),
            "A01",
            "orch_status",
        );
        let expected = "{\n  \"status\": \"ok\",\n  \"correlation_id\": \"C9\",\n  \"tasks\": [],\n  \"db\": \"/x/tasks.db\",\n  \"swarm_dir\": \"/x\",\n  \"summary\": \"no tasks (run orch_plan.py first)\",\n  \"agent\": \"A01\",\n  \"script\": \"orch_status\"\n}\n";
        assert_eq!(reply.stdout, expected);
        Ok(())
    }

    #[test]
    fn history_fields_render_transitions_verdicts_and_reasons() -> TestResult {
        let task = task_row("T-A", "DONE", "[]")?;
        let mut latest: Vec<LatestRow> = Vec::new();
        push_latest(
            &mut latest,
            cols(&[
                ("task_id", jstr("T-A")),
                ("gate", jstr("review")),
                ("verdict", jstr("pass")),
                ("findings", jstr("[]")),
                ("envelope_json", Json::Null),
                ("ts", Json::Float(2.0)),
            ]),
        )
        .map_err(message)?;
        let history = vec![Json::Obj(cols(&[
            ("to_state", jstr("CREATED")),
            ("from_state", Json::Null),
        ]))];
        let fields = history_fields("T-A", history, latest, &task).map_err(message)?;
        let expected = "{\n  \"status\": \"ok\",\n  \"task_id\": \"T-A\",\n  \"history\": [\n    {\n      \"to_state\": \"CREATED\",\n      \"from_state\": null\n    }\n  ],\n  \"verdicts\": {\n    \"review\": {\n      \"task_id\": \"T-A\",\n      \"gate\": \"review\",\n      \"verdict\": \"pass\",\n      \"findings\": [],\n      \"envelope_json\": null,\n      \"ts\": 2.0\n    }\n  },\n  \"missing_gate_reasons\": {\n    \"review\": \"unsigned\"\n  },\n  \"summary\": \"history for T-A\"\n}";
        assert_eq!(to_python_json(&Json::Obj(fields)), expected);
        Ok(())
    }

    #[test]
    fn number_and_escape_grammar_is_strict_json() -> TestResult {
        for text in ["01", "1.", "-", "-01", "1e", "1e+", "1.e3", "+1"] {
            assert!(parse_json(text).is_err(), "{text:?}");
        }
        for text in [r#""\u+041""#, r#""\u00G1""#] {
            assert!(parse_json(text).is_err(), "{text:?}");
        }
        assert_eq!(parse_json("-0").map_err(message)?, Json::Int(0));
        assert_eq!(parse_json("0.5").map_err(message)?, Json::Float(0.5));
        assert_eq!(parse_json("1e3").map_err(message)?, Json::Float(1000.0));
        assert_eq!(parse_json(r#""\u0041""#).map_err(message)?, jstr("A"));
        Ok(())
    }

    #[test]
    fn duplicate_keys_keep_first_position_and_last_value() -> TestResult {
        let parsed = parse_json("{\"a\": 1, \"b\": 2, \"a\": 3}").map_err(message)?;
        let expected = Json::Obj(vec![
            ("a".to_string(), Json::Int(3)),
            ("b".to_string(), Json::Int(2)),
        ]);
        assert_eq!(parsed, expected);
        let mut text = String::from("{");
        for index in 0..2000 {
            text.push_str(&format!("\"k{index}\": {index}, "));
        }
        text.push_str("\"k0\": -1}");
        let Json::Obj(fields) = parse_json(&text).map_err(message)? else {
            return Err("object expected".to_string());
        };
        assert_eq!(fields.len(), 2000);
        assert_eq!(fields[0], ("k0".to_string(), Json::Int(-1)));
        assert_eq!(fields[1999], ("k1999".to_string(), Json::Int(1999)));
        Ok(())
    }

    #[test]
    fn task_notes_with_out_of_range_integer_is_refused_not_emptied() -> TestResult {
        let notes = r#"{"gates":["review"],"n":12345678901234567890}"#;
        let error = Task::from_row(task_fields(notes, "{}"))
            .err()
            .ok_or_else(|| "notes with an out-of-range integer were accepted".to_string())?;
        assert_eq!(error.code(), ErrorCode::Dep);
        Ok(())
    }

    #[test]
    fn task_row_with_out_of_range_budget_defers_to_python() -> TestResult {
        let budget = r#"{"max_tokens": 10000000000000000000}"#;
        let error = Task::from_row(task_fields("{}", budget))
            .err()
            .ok_or_else(|| "budget with an out-of-range integer was accepted".to_string())?;
        assert_eq!(error.code(), ErrorCode::Dep);
        Ok(())
    }
}

#[cfg(feature = "write-routes-2")]
pub mod catalog;

#[cfg(feature = "write-routes-2")]
pub mod approval_wire;
#[cfg(feature = "write-routes-2")]
pub mod catalog_wire;
#[cfg(feature = "write-routes-2")]
pub mod ingest_wire;
#[cfg(feature = "write-routes-2")]
pub mod plan_wire;
#[cfg(feature = "write-routes-2")]
pub mod reconcile_wire;
