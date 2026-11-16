//! Python's `json.dumps` with its default arguments, and the matching refusing reader.
//!
//! The write routes store JSON text in Task Store columns (`json.dumps(notes)`, `json.dumps(envelope)`,
//! `json.dumps(p["findings"])`, `swarm/taskstore.py`) and hash it (`json.dumps(rows, sort_keys=True)`,
//! `scripts/orch_plan.py`). Those bytes must be the ones Python writes, because Python reads them back and signs over
//! them. This module renders them:
//!
//! * [`to_python_dumps`]: `json.dumps(value)`: separators `", "` and `": "`, `ensure_ascii=True`, keys in the order given.
//! * [`to_python_dumps_sorted`]: `json.dumps(value, sort_keys=True)`, the same layout with keys sorted by code point.
//! * [`parse_python_json`]: [`parse_json`] behind a refusal of every text Python reads but [`Json`] cannot hold without
//!   changing the bytes `json.dumps` would print.
//!
//! The `indent=2` form that replies use (`script_base.AgentScript._print`) is [`to_python_json`](crate::to_python_json),
//! unchanged. `default=str` has no counterpart: [`Json`] is typed, so a value Python would stringify (a `Path`, a
//! timestamp) is turned into [`Json::Str`] by the caller before it gets here.
//!
//! # Why a new function and not an option on `to_python_json`
//!
//! `to_python_json` is the reply writer, called by every read route; a mode flag on it would put those byte-exact
//! outputs one wrong argument away from changing. The two writers share the parts that must never diverge, the string
//! escaping and [`py_float`], and differ only in layout, which is a dozen lines each.
//!
//! # What is refused, and why that is the whole list
//!
//! [`Json::Int`] is an `i64`, so a larger Python `int` has no representation. [`Json`] holds Rust `String`s, which
//! cannot carry a lone surrogate (`"\ud800"`). The reader therefore refuses an integer outside `i64`, an unpaired
//! surrogate escape, the literals `NaN`, `Infinity` and `-Infinity`, and text that is not JSON. Every refusal is
//! `E-DEP`, the code of "the rust engine cannot answer this; Python must": the route glue adds
//! `write_state = "not_started"` through [`SeamError::with_details`](crate::SeamError::with_details). Floats are not
//! refused: [`py_float`] prints `NaN`, `Infinity` and `-Infinity` exactly as Python does (`allow_nan` defaults to true), and
//! `1e400` reads as infinity in both languages.
//!
//! A [`Json::Obj`] with a repeated key cannot come from a Python `dict`. The writers do not check for one; the reader never
//! produces one (the last value wins at the first position, as in Python).

use gsd_codes::ErrorCode;

use crate::{parse_json, py_float, write_str, Json, SeamError};

/// Renders `value` as Python's `json.dumps(value)`: `", "` and `": "` separators, `ensure_ascii=True`, keys in the order given.
pub fn to_python_dumps(value: &Json) -> String {
    let mut out = String::new();
    write_compact(value, false, &mut out);
    out
}

/// Renders `value` as Python's `json.dumps(value, sort_keys=True)`: [`to_python_dumps`] with every object's keys sorted by code point.
pub fn to_python_dumps_sorted(value: &Json) -> String {
    let mut out = String::new();
    write_compact(value, true, &mut out);
    out
}

fn write_compact(value: &Json, sort_keys: bool, out: &mut String) {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Int(number) => out.push_str(&number.to_string()),
        Json::Float(number) => out.push_str(&py_float(*number)),
        Json::Str(text) => write_str(text, out),
        Json::Arr(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_compact(item, sort_keys, out);
            }
            out.push(']');
        }
        Json::Obj(fields) => {
            let mut ordered: Vec<&(String, Json)> = fields.iter().collect();
            if sort_keys {
                // `str` order is byte order of UTF-8, which is code point order: what Python's `sorted` uses.
                ordered.sort_by(|left, right| left.0.cmp(&right.0));
            }
            out.push('{');
            for (index, (key, item)) in ordered.into_iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_str(key, out);
                out.push_str(": ");
                write_compact(item, sort_keys, out);
            }
            out.push('}');
        }
    }
}

/// Reads JSON text the way Python's `json.loads` does, or refuses it with `E-DEP`.
///
/// What it returns dumps back, through [`to_python_dumps`], to exactly the text Python's `json.dumps(json.loads(text))`
/// prints. See the module documentation for the refused classes. The error message names the class and never quotes `text`.
pub fn parse_python_json(text: &str) -> Result<Json, SeamError> {
    if let Err(reason) = find_unrepresentable(text) {
        return Err(refusal(reason));
    }
    parse_json(text).map_err(|_| refusal(NOT_JSON))
}

/// Refusal reason: an integer literal outside `i64`.
const BIG_INT: &str = "integer outside the 64-bit range";
/// Refusal reason: a `\u` escape that is half of a surrogate pair.
const LONE_SURROGATE: &str = "unpaired surrogate escape";
/// Refusal reason: `NaN`, `Infinity` or `-Infinity` as a literal.
const NON_FINITE: &str = "non-finite number literal";
/// Refusal reason: text that is not JSON (Python raises `ValueError`).
const NOT_JSON: &str = "text is not JSON";

fn refusal(reason: &str) -> SeamError {
    SeamError::new(
        ErrorCode::Dep,
        format!("{reason}: the rust engine does not reproduce it; use GSD_ENGINE=python"),
    )
}

/// Scans `text` for what [`Json`] cannot hold. Text that is not JSON may pass here; [`parse_json`] rejects it.
fn find_unrepresentable(text: &str) -> Result<(), &'static str> {
    let bytes = text.as_bytes();
    let mut pos = 0;
    while let Some(&byte) = bytes.get(pos) {
        match byte {
            b'"' => pos = skip_string(bytes, pos + 1)?,
            b'N' | b'I'
                if bytes[pos..].starts_with(b"NaN") || bytes[pos..].starts_with(b"Infinity") =>
            {
                return Err(NON_FINITE)
            }
            b'-' | b'0'..=b'9' => {
                let end = pos
                    + bytes[pos..]
                        .iter()
                        .take_while(|&&b| {
                            matches!(b, b'-' | b'+' | b'.' | b'0'..=b'9' | b'e' | b'E')
                        })
                        .count();
                let token = &text[pos..end];
                let digits = token.strip_prefix('-').unwrap_or(token);
                let integral = !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit());
                if integral && token.parse::<i64>().is_err() {
                    return Err(BIG_INT);
                }
                pos = end;
            }
            _ => pos += 1,
        }
    }
    Ok(())
}

/// Advances past one string body (after its opening quote), refusing a lone surrogate escape. An unterminated string
/// ends the scan; the parser reports it.
fn skip_string(bytes: &[u8], mut pos: usize) -> Result<usize, &'static str> {
    while let Some(&byte) = bytes.get(pos) {
        pos += 1;
        match byte {
            b'"' => return Ok(pos),
            b'\\' if bytes.get(pos) == Some(&b'u') => {
                pos += 1;
                match hex_unit(bytes, pos) {
                    Some(0xD800..=0xDBFF) => {
                        match (bytes.get(pos + 4..pos + 6), hex_unit(bytes, pos + 6)) {
                            (Some(b"\\u"), Some(0xDC00..=0xDFFF)) => pos += 10,
                            (Some(b"\\u"), Some(_)) => return Err(LONE_SURROGATE),
                            (Some(b"\\u"), None) => pos += 4,
                            _ => return Err(LONE_SURROGATE),
                        }
                    }
                    Some(0xDC00..=0xDFFF) => return Err(LONE_SURROGATE),
                    _ => {}
                }
            }
            b'\\' => pos += 1,
            _ => {}
        }
    }
    Ok(pos)
}

/// The UTF-16 unit written as four hexadecimal digits at `pos`, if there are four.
fn hex_unit(bytes: &[u8], pos: usize) -> Option<u16> {
    let digits = bytes.get(pos..pos + 4)?;
    if !digits.iter().all(u8::is_ascii_hexdigit) {
        return None;
    }
    u16::from_str_radix(std::str::from_utf8(digits).ok()?, 16).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{to_python_json, Fields};

    type TestResult = Result<(), String>;

    /// The committed vectors: `tests/parity/gen_pydumps_vectors.py` writes them from Python's own `json.dumps`.
    const VECTORS: &str = include_str!("../tests/vectors/pydumps.json");

    fn text_of<'a>(fields: &'a [(String, Json)], key: &str) -> Result<&'a str, String> {
        match crate::obj_get(fields, key) {
            Some(Json::Str(text)) => Ok(text),
            other => Err(format!("{key}: expected a string, got {other:?}")),
        }
    }

    fn list_of<'a>(fields: &'a [(String, Json)], key: &str) -> Result<&'a [Json], String> {
        match crate::obj_get(fields, key) {
            Some(Json::Arr(items)) => Ok(items),
            other => Err(format!("{key}: expected an array, got {other:?}")),
        }
    }

    fn object_of(value: &Json) -> Result<&[(String, Json)], String> {
        match value {
            Json::Obj(fields) => Ok(fields),
            other => Err(format!("expected an object, got {other:?}")),
        }
    }

    /// Decodes the generator's tagged encoding: `["null"]`, `["bool", b]`, `["int", "<decimal>"]`, `["float", "<16 hex digits of
    /// the IEEE bits>"]`, `["str", s]`, `["arr", [..]]`, `["obj", [[key, value], ..]]`.
    fn decode(tagged: &Json) -> Result<Json, String> {
        let Json::Arr(parts) = tagged else {
            return Err(format!("tagged value is not an array: {tagged:?}"));
        };
        match parts.as_slice() {
            [Json::Str(tag)] if tag == "null" => Ok(Json::Null),
            [Json::Str(tag), Json::Bool(flag)] if tag == "bool" => Ok(Json::Bool(*flag)),
            [Json::Str(tag), Json::Str(digits)] if tag == "int" => digits
                .parse::<i64>()
                .map(Json::Int)
                .map_err(|error| format!("int {digits}: {error}")),
            [Json::Str(tag), Json::Str(bits)] if tag == "float" => u64::from_str_radix(bits, 16)
                .map(|raw| Json::Float(f64::from_bits(raw)))
                .map_err(|error| format!("float {bits}: {error}")),
            [Json::Str(tag), Json::Str(text)] if tag == "str" => Ok(Json::Str(text.clone())),
            [Json::Str(tag), Json::Arr(items)] if tag == "arr" => items
                .iter()
                .map(decode)
                .collect::<Result<Vec<_>, _>>()
                .map(Json::Arr),
            [Json::Str(tag), Json::Arr(pairs)] if tag == "obj" => pairs
                .iter()
                .map(|pair| match pair {
                    Json::Arr(entry) => match entry.as_slice() {
                        [Json::Str(key), value] => Ok((key.clone(), decode(value)?)),
                        _ => Err(format!("object entry is not [key, value]: {pair:?}")),
                    },
                    _ => Err(format!("object entry is not an array: {pair:?}")),
                })
                .collect::<Result<Vec<_>, _>>()
                .map(Json::Obj),
            _ => Err(format!("unknown tagged value {tagged:?}")),
        }
    }

    fn vectors() -> Result<Fields, String> {
        match parse_json(VECTORS).map_err(|error| error.message().to_string())? {
            Json::Obj(fields) => Ok(fields),
            other => Err(format!("vector file is not an object: {other:?}")),
        }
    }

    fn section<'a>(file: &'a [(String, Json)], name: &str) -> Result<&'a [Json], String> {
        let cases = list_of(file, name)?;
        if cases.len() < 10 {
            return Err(format!("section {name} has only {} cases", cases.len()));
        }
        Ok(cases)
    }

    /// Names where two renderings part, with a short window of each, so a long document does not drown the report.
    fn difference(expected: &str, actual: &str) -> String {
        let at = expected
            .chars()
            .zip(actual.chars())
            .take_while(|(left, right)| left == right)
            .count();
        let window = |text: &str| {
            text.chars()
                .skip(at.saturating_sub(24))
                .take(60)
                .collect::<String>()
        };
        format!(
            "differ at char {at}: expected ...{}..., got ...{}...",
            window(expected),
            window(actual)
        )
    }

    /// Fails with every mismatch of one section, naming the case, so one run shows the whole damage.
    fn report(section: &str, failures: Vec<String>) -> TestResult {
        if failures.is_empty() {
            return Ok(());
        }
        let shown: Vec<&str> = failures.iter().take(5).map(String::as_str).collect();
        Err(format!(
            "{section}: {} failing cases, first: {shown:#?}",
            failures.len()
        ))
    }

    #[test]
    fn dumps_match_python_json_dumps_for_every_vector() -> TestResult {
        let file = vectors()?;
        let mut failures = Vec::new();
        for case in section(&file, "dumps")? {
            let fields = object_of(case)?;
            let name = text_of(fields, "name")?;
            let value = decode(
                fields
                    .iter()
                    .find(|(key, _)| key == "input")
                    .map(|(_, v)| v)
                    .ok_or("no input")?,
            )?;
            for (field, actual) in [
                ("dumps", to_python_dumps(&value)),
                ("sorted", to_python_dumps_sorted(&value)),
                ("reply", format!("{}\n", to_python_json(&value))),
            ] {
                let expected = text_of(fields, field)?;
                if actual != expected {
                    failures.push(format!(
                        "{name} [{field}]: {}",
                        difference(expected, &actual)
                    ));
                }
            }
        }
        report("dumps", failures)
    }

    fn reason_text(class: &str) -> Result<&'static str, String> {
        match class {
            "bigint" => Ok(BIG_INT),
            "surrogate" => Ok(LONE_SURROGATE),
            "nonfinite" => Ok(NON_FINITE),
            "invalid" => Ok(NOT_JSON),
            other => Err(format!("unknown refusal class {other}")),
        }
    }

    #[test]
    fn reader_returns_what_python_reads_and_refuses_the_rest() -> TestResult {
        let file = vectors()?;
        let mut failures = Vec::new();
        for case in section(&file, "parse")? {
            let fields = object_of(case)?;
            let name = text_of(fields, "name")?;
            let text = text_of(fields, "text")?;
            let outcome = parse_python_json(text);
            match crate::obj_get(fields, "refuse") {
                Some(Json::Str(class)) => {
                    let reason = reason_text(class)?;
                    match outcome {
                        Err(error)
                            if error.code() == ErrorCode::Dep
                                && error.message().contains(reason) => {}
                        other => {
                            failures.push(format!("{name}: expected E-DEP {reason}, got {other:?}"))
                        }
                    }
                }
                _ => {
                    let value = outcome.map_err(|error| format!("{name}: {}", error.message()))?;
                    for (field, actual) in [
                        ("dumps", to_python_dumps(&value)),
                        ("sorted", to_python_dumps_sorted(&value)),
                    ] {
                        let expected = text_of(fields, field)?;
                        if actual != expected {
                            failures.push(format!(
                                "{name} [{field}]: {}",
                                difference(expected, &actual)
                            ));
                        }
                    }
                }
            }
        }
        report("parse", failures)
    }

    #[test]
    fn error_replies_match_swarm_error_to_dict() -> TestResult {
        let file = vectors()?;
        let mut failures = Vec::new();
        for case in section(&file, "errors")? {
            let fields = object_of(case)?;
            let name = text_of(fields, "name")?;
            let code = match text_of(fields, "code")? {
                "E-INPUT" => ErrorCode::Input,
                "E-TIMEOUT" => ErrorCode::Timeout,
                "E-DEP" => ErrorCode::Dep,
                "E-CAPACITY" => ErrorCode::Capacity,
                "E-CONTRACT" => ErrorCode::Contract,
                "E-POLICY" => ErrorCode::Policy,
                "E-INTERNAL" => ErrorCode::Internal,
                other => return Err(format!("{name}: unknown code {other}")),
            };
            let details = match decode(
                fields
                    .iter()
                    .find(|(key, _)| key == "details")
                    .map(|(_, v)| v)
                    .ok_or("no details")?,
            )? {
                Json::Obj(pairs) => pairs,
                other => return Err(format!("{name}: details is not an object: {other:?}")),
            };
            let error = SeamError::new(code, text_of(fields, "message")?).with_details(details);
            let body = Json::Obj(vec![
                ("agent".to_string(), crate::jstr("A01")),
                ("script".to_string(), crate::jstr("orch_status")),
                ("status".to_string(), crate::jstr("error")),
                ("error".to_string(), error.to_json()),
            ]);
            let actual = format!("{}\n", to_python_json(&body));
            let expected = text_of(fields, "reply")?;
            if actual != expected {
                failures.push(format!("{name}: {}", difference(expected, &actual)));
            }
        }
        report("errors", failures)
    }

    #[test]
    fn key_order_is_kept_unless_sorting_is_asked_for() -> TestResult {
        let value = Json::Obj(vec![
            ("b".to_string(), Json::Int(1)),
            (
                "a".to_string(),
                Json::Obj(vec![
                    ("z".to_string(), Json::Null),
                    ("y".to_string(), Json::Null),
                ]),
            ),
        ]);
        assert_eq!(
            to_python_dumps(&value),
            r#"{"b": 1, "a": {"z": null, "y": null}}"#
        );
        assert_eq!(
            to_python_dumps_sorted(&value),
            r#"{"a": {"y": null, "z": null}, "b": 1}"#
        );
        Ok(())
    }

    #[test]
    fn sorting_uses_code_points_not_utf16_units() -> TestResult {
        // U+E000 is one UTF-16 unit above the surrogate range, U+10000 is two units starting inside it: Python sorts by
        // code point, so U+E000 comes first, which UTF-16 order would reverse.
        let value = Json::Obj(vec![
            ("\u{10000}".to_string(), Json::Int(1)),
            ("\u{e000}".to_string(), Json::Int(2)),
        ]);
        assert_eq!(
            to_python_dumps_sorted(&value),
            "{\"\\ue000\": 2, \"\\ud800\\udc00\": 1}"
        );
        Ok(())
    }

    #[test]
    fn integers_at_the_i64_edges_read_and_one_beyond_is_refused() -> TestResult {
        for (text, expected) in [
            ("9223372036854775807", Some(Json::Int(i64::MAX))),
            ("-9223372036854775808", Some(Json::Int(i64::MIN))),
            ("9223372036854775808", None),
            ("-9223372036854775809", None),
            ("[1, 99999999999999999999]", None),
            ("{\"n\": -99999999999999999999}", None),
            ("1e30", Some(Json::Float(1e30))),
            ("100000000000000000000.0", Some(Json::Float(1e20))),
        ] {
            match (parse_python_json(text), expected) {
                (Ok(value), Some(want)) => assert_eq!(value, want, "{text}"),
                (Err(error), None) => {
                    assert_eq!(error.code(), ErrorCode::Dep, "{text}");
                    assert!(
                        error.message().contains(BIG_INT),
                        "{text}: {}",
                        error.message()
                    );
                }
                (got, want) => return Err(format!("{text}: got {got:?}, wanted {want:?}")),
            }
        }
        Ok(())
    }

    #[test]
    fn a_refusal_never_quotes_the_input() -> TestResult {
        let error = parse_python_json("[\"marker-text\", 99999999999999999999]")
            .err()
            .ok_or("accepted")?;
        assert_eq!(
            error.message(),
            "E-DEP: integer outside the 64-bit range: the rust engine does not reproduce it; use GSD_ENGINE=python"
        );
        Ok(())
    }

    #[test]
    fn digits_inside_a_string_are_not_integers() -> TestResult {
        let value = parse_python_json("[\"99999999999999999999\", \"a\\\"99999999999999999999\"]")
            .map_err(|e| e.message().to_string())?;
        assert_eq!(
            to_python_dumps(&value),
            "[\"99999999999999999999\", \"a\\\"99999999999999999999\"]"
        );
        Ok(())
    }
}
