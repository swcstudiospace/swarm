//! The canonical signing form and the envelope text entry point.
//!
//! The canonical form is Python's `json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()` of the
//! envelope without its top-level `sig` (`swarm/envelope.py:121-123`): keys in code point order, no whitespace, strings escaped
//! only where JSON requires it (`"`, `\`, the C0 controls) and otherwise left as UTF-8, floats through the one shared formatter
//! [`gsd_route::py_float`] (`NaN`, `Infinity` and `-Infinity` for the non-finite values).

use gsd_route::{parse_json, py_float, Json};

use crate::Declined;

/// Hex digits for the `\u00XX` escape of a C0 control character.
const HEX: &[u8; 16] = b"0123456789abcdef";

/// The bytes that are signed: the canonical form of `env` without its top-level `sig`.
///
/// # Errors
///
/// [`Declined::NotAnObject`] when `env` is not a JSON object.
pub fn canonical_bytes(env: &Json) -> Result<Vec<u8>, Declined> {
    let Json::Obj(fields) = env else {
        return Err(Declined::NotAnObject);
    };
    let mut out = String::new();
    write_object(fields, true, &mut out);
    Ok(out.into_bytes())
}

/// The same compact, key-sorted rendering of a whole value, a top-level `sig` included. A signed envelope printed this way
/// round-trips through Python's `json.loads`.
pub fn compact_json(value: &Json) -> Vec<u8> {
    let mut out = String::new();
    write_value(value, &mut out);
    out.into_bytes()
}

/// Parses envelope text the way Python's `json.loads` would, or declines.
///
/// `gsd_route::parse_json` does the parsing. It cannot represent an integer outside `i64`, rejects the `NaN` and `Infinity`
/// literals, and turns a lone surrogate escape into U+FFFD where Python keeps the surrogate; the first two surface as
/// [`Declined::InvalidJson`], and the lone surrogate is found by a scan before parsing and surfaces as [`Declined::LoneSurrogate`]
/// (the substitution would otherwise change the signed bytes silently).
///
/// # Errors
///
/// [`Declined::LoneSurrogate`] or [`Declined::InvalidJson`]; in both cases Python answers.
pub fn parse_envelope(text: &str) -> Result<Json, Declined> {
    if has_lone_surrogate_escape(text.as_bytes()) {
        return Err(Declined::LoneSurrogate);
    }
    parse_json(text).map_err(|_| Declined::InvalidJson)
}

fn write_value(value: &Json, out: &mut String) {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Int(number) => out.push_str(&number.to_string()),
        Json::Float(number) => out.push_str(&py_float(*number)),
        Json::Str(text) => write_string(text, out),
        Json::Arr(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Json::Obj(fields) => write_object(fields, false, out),
    }
}

fn write_object(fields: &[(String, Json)], skip_sig: bool, out: &mut String) {
    let mut sorted: Vec<&(String, Json)> = fields
        .iter()
        .filter(|(key, _)| !(skip_sig && key == "sig"))
        .collect();
    // `String` orders by UTF-8 bytes, which is Python's code point order for `str`.
    sorted.sort_by(|a, b| a.0.cmp(&b.0));
    out.push('{');
    for (index, (key, item)) in sorted.into_iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        write_string(key, out);
        out.push(':');
        write_value(item, out);
    }
    out.push('}');
}

fn write_string(text: &str, out: &mut String) {
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
            control if u32::from(control) < 0x20 => {
                let code = u32::from(control) as usize;
                out.push_str("\\u00");
                out.push(char::from(HEX[code >> 4]));
                out.push(char::from(HEX[code & 15]));
            }
            other => out.push(other),
        }
    }
    out.push('"');
}

/// Four hex digits at the start of `bytes` as a number, or `None`.
fn hex4(bytes: &[u8]) -> Option<u32> {
    let digits = bytes.get(..4)?;
    digits.iter().try_fold(0u32, |acc, &byte| {
        Some(acc * 16 + char::from(byte).to_digit(16)?)
    })
}

/// True when a string in `bytes` holds a `\uD800`..`\uDFFF` escape that is not a high surrogate directly followed by a `\u` low
/// surrogate. Malformed text is left for the parser to reject.
fn has_lone_surrogate_escape(bytes: &[u8]) -> bool {
    let mut in_string = false;
    let mut at = 0;
    while at < bytes.len() {
        let byte = bytes[at];
        if !in_string {
            in_string = byte == b'"';
            at += 1;
        } else if byte == b'"' {
            in_string = false;
            at += 1;
        } else if byte == b'\\' && bytes.get(at + 1) == Some(&b'u') {
            let Some(unit) = hex4(&bytes[at + 2..]) else {
                return false;
            };
            at += 6;
            if (0xD800..0xDC00).contains(&unit) {
                let low = (bytes.get(at) == Some(&b'\\') && bytes.get(at + 1) == Some(&b'u'))
                    .then(|| hex4(&bytes[at + 2..]))
                    .flatten();
                if !low.is_some_and(|second| (0xDC00..0xE000).contains(&second)) {
                    return true;
                }
                at += 6;
            } else if (0xDC00..0xE000).contains(&unit) {
                return true;
            }
        } else if byte == b'\\' {
            at += 2;
        } else {
            at += 1;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    type TestResult = Result<(), String>;

    fn canonical_of(text: &str) -> Result<String, String> {
        let env = parse_envelope(text).map_err(|e| e.to_string())?;
        let bytes = canonical_bytes(&env).map_err(|e| e.to_string())?;
        String::from_utf8(bytes).map_err(|e| e.to_string())
    }

    #[test]
    fn canonical_form_matches_python_json_dumps() -> TestResult {
        // Expected strings were produced by json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False) on CPython 3.12.
        let cases: [(&str, &str); 12] = [
            (
                r#"{"b":1,"a":[true,false,null]}"#,
                r#"{"a":[true,false,null],"b":1}"#,
            ),
            (
                r#"{ "a" : { "z" : 1 , "y" : [ ] , "x" : { } } }"#,
                r#"{"a":{"x":{},"y":[],"z":1}}"#,
            ),
            (
                r#"{"f":[0.1,1.0,-0.0,1e16,1e-5,1.5e-7,123456789.125,1e22,5e-324,1e400]}"#,
                r#"{"f":[0.1,1.0,-0.0,1e+16,1e-05,1.5e-07,123456789.125,1e+22,5e-324,Infinity]}"#,
            ),
            (
                r#"{"f":[1e15,9999999999999998.0,0.0001,0.00009999,100.0,-1e400]}"#,
                r#"{"f":[1000000000000000.0,9999999999999998.0,0.0001,9.999e-05,100.0,-Infinity]}"#,
            ),
            (
                r#"{"n":[9223372036854775807,-9223372036854775808,-0,0]}"#,
                r#"{"n":[9223372036854775807,-9223372036854775808,0,0]}"#,
            ),
            (
                "{\"s\":\"h\u{e9}llo \u{65e5}\u{672c} \u{1f600} \\u00e9\"}",
                "{\"s\":\"h\u{e9}llo \u{65e5}\u{672c} \u{1f600} \u{e9}\"}",
            ),
            (
                r#"{"s":"q\"b\\ \/ \n\r\t\b\f \u0001\u001f\u007f \u2028"}"#,
                "{\"s\":\"q\\\"b\\\\ / \\n\\r\\t\\b\\f \\u0001\\u001f\u{7f} \u{2028}\"}",
            ),
            (r#"{"s":"\ud83d\ude00"}"#, "{\"s\":\"\u{1f600}\"}"),
            (
                "{\"\u{ffff}\":1,\"\u{1f600}\":2,\"\u{e9}\":3,\"z\":4,\"Z\":5,\"a\":6}",
                "{\"Z\":5,\"a\":6,\"z\":4,\"\u{e9}\":3,\"\u{ffff}\":1,\"\u{1f600}\":2}",
            ),
            (r#"{"a":1,"sig":"hmac:abc","b":2}"#, r#"{"a":1,"b":2}"#),
            (
                r#"{"payload":{"sig":"inner","a":[{"sig":1}]},"sig":"x"}"#,
                r#"{"payload":{"a":[{"sig":1}],"sig":"inner"}}"#,
            ),
            (r#"{"a":1,"a":2}"#, r#"{"a":2}"#),
        ];
        for (text, expected) in cases {
            assert_eq!(canonical_of(text)?, expected, "input {text}");
        }
        Ok(())
    }

    #[test]
    fn compact_json_keeps_the_signature_member() -> TestResult {
        let env = parse_envelope(r#"{"b":1,"sig":"x","a":2}"#).map_err(|e| e.to_string())?;
        assert_eq!(compact_json(&env), br#"{"a":2,"b":1,"sig":"x"}"#);
        Ok(())
    }

    #[test]
    fn canonical_bytes_declines_a_non_object() {
        for value in [
            Json::Null,
            Json::Int(1),
            Json::Arr(Vec::new()),
            Json::Str("x".to_string()),
        ] {
            assert_eq!(canonical_bytes(&value), Err(Declined::NotAnObject));
        }
    }

    #[test]
    fn parse_envelope_declines_what_python_accepts_but_this_crate_cannot_represent() {
        let cases: [(&str, Declined); 9] = [
            (r#"{"a":9223372036854775808}"#, Declined::InvalidJson),
            (r#"{"a":-9223372036854775809}"#, Declined::InvalidJson),
            (r#"{"a":NaN}"#, Declined::InvalidJson),
            (r#"{"a":Infinity}"#, Declined::InvalidJson),
            (r#"{"a":"x"#, Declined::InvalidJson),
            (r#"{"a":"\ud800"}"#, Declined::LoneSurrogate),
            (r#"{"a":"\udc00x"}"#, Declined::LoneSurrogate),
            (r#"{"a":"\ud800\u0041"}"#, Declined::LoneSurrogate),
            (r#"{"a\ud800":1}"#, Declined::LoneSurrogate),
        ];
        for (text, expected) in cases {
            assert_eq!(parse_envelope(text), Err(expected), "input {text}");
        }
    }

    #[test]
    fn surrogate_scan_ignores_escaped_backslashes_and_text_outside_strings() {
        for text in [
            r#"{"a":"\\ud800"}"#,
            r#"{"a":"\\\\ud800 \ud83d\ude00"}"#,
            r#"{"a":"\ud83d\ude00\ud83d\ude00"}"#,
        ] {
            assert!(parse_envelope(text).is_ok(), "input {text}");
        }
    }
}
