//! A small std-only JSON reader for the replay tests of this crate (compiled for tests only).
//!
//! The crate has no dependencies, dev-dependencies included, so the Python-oracle vectors under
//! `tests/parity/fixtures/scheduler/` are read with this recursive-descent parser instead of a JSON crate. Two properties matter for
//! the replays and differ from a stock JSON crate:
//!
//! * **Object key order is kept as written**, and [`Value`] equality is order-sensitive (it is derived on the `Vec` of members), so a
//!   comparison also checks the order in which, for example, the claim table lists its holders.
//! * **Numbers are exact.** A literal without fraction or exponent is [`Value::Int`], an `i128` limited to the span `i64::MIN..=u64::MAX`
//!   (the only integers the tests read back, through [`Value::as_i64`] and [`Value::as_u64`]); an integer outside that span, and
//!   every literal with a fraction or an exponent, is [`Value::Float`] parsed by `str::parse::<f64>`, which is correctly rounded and
//!   so yields the very double Python's `float()` yields. `1` and `1.0` are therefore different values, as in most JSON crates.
//!
//! The grammar is RFC 8259 JSON and nothing more: no comments, no trailing commas, no leading zeros, no lone surrogates, no raw
//! control characters in strings, no duplicate object keys (a repeated key is an error here, where Python would silently keep the
//! last one: a vector file with one is a corpus bug), no non-finite numbers, and nesting no deeper than [`MAX_DEPTH`].

use std::fmt;
use std::ops::Index;

/// The deepest nesting of arrays and objects [`parse`] accepts; a deeper document is an error, never a stack overflow.
pub(crate) const MAX_DEPTH: usize = 256;

/// What an index into a missing key, a missing position or a non-container yields, as the common JSON crates do.
static NULL: Value = Value::Null;

/// One JSON value. Object members keep the order of the text.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Value {
    /// `null`.
    Null,
    /// `true` or `false`.
    Bool(bool),
    /// An integer literal within `i64::MIN..=u64::MAX`.
    Int(i128),
    /// Any other number: a fraction, an exponent, or an integer beyond the `Int` span.
    Float(f64),
    /// A string, with its escapes decoded.
    String(String),
    /// An array.
    Array(Vec<Value>),
    /// An object as its members in the order written; keys are unique.
    Object(Vec<(String, Value)>),
}

impl Value {
    /// An object from `members`, in the given order.
    pub(crate) fn object<const N: usize>(members: [(&str, Value); N]) -> Self {
        Self::Object(
            members
                .into_iter()
                .map(|(key, value)| (key.to_string(), value))
                .collect(),
        )
    }

    /// The member `key` of an object; `None` for a missing key and for every other kind of value.
    pub(crate) fn get(&self, key: &str) -> Option<&Self> {
        match self {
            Self::Object(members) => members
                .iter()
                .find(|(name, _)| name == key)
                .map(|(_, value)| value),
            _ => None,
        }
    }

    /// Whether this is `null`.
    pub(crate) fn is_null(&self) -> bool {
        matches!(self, Self::Null)
    }

    /// The boolean, if this is one.
    pub(crate) fn as_bool(&self) -> Option<bool> {
        match self {
            Self::Bool(flag) => Some(*flag),
            _ => None,
        }
    }

    /// The integer, if this is an `Int` that fits `i64` (a `Float` never converts, even `1.0`).
    pub(crate) fn as_i64(&self) -> Option<i64> {
        match self {
            Self::Int(number) => i64::try_from(*number).ok(),
            _ => None,
        }
    }

    /// The integer, if this is an `Int` that fits `u64` (a `Float` never converts, even `1.0`).
    pub(crate) fn as_u64(&self) -> Option<u64> {
        match self {
            Self::Int(number) => u64::try_from(*number).ok(),
            _ => None,
        }
    }

    /// The number as a double, for an `Int` (rounded to nearest) and a `Float`.
    pub(crate) fn as_f64(&self) -> Option<f64> {
        match self {
            Self::Int(number) => Some(*number as f64),
            Self::Float(number) => Some(*number),
            _ => None,
        }
    }

    /// The text, if this is a string.
    pub(crate) fn as_str(&self) -> Option<&str> {
        match self {
            Self::String(text) => Some(text),
            _ => None,
        }
    }

    /// The elements, if this is an array.
    pub(crate) fn as_array(&self) -> Option<&[Self]> {
        match self {
            Self::Array(items) => Some(items),
            _ => None,
        }
    }

    /// The members in written order, if this is an object.
    pub(crate) fn as_object(&self) -> Option<&[(String, Self)]> {
        match self {
            Self::Object(members) => Some(members),
            _ => None,
        }
    }
}

impl Index<&str> for Value {
    type Output = Value;

    /// The member `key`, or `null` when there is none (also on a value that is not an object).
    fn index(&self, key: &str) -> &Value {
        self.get(key).unwrap_or(&NULL)
    }
}

impl Index<usize> for Value {
    type Output = Value;

    /// The element at `position`, or `null` when there is none (also on a value that is not an array).
    fn index(&self, position: usize) -> &Value {
        self.as_array()
            .and_then(|items| items.get(position))
            .unwrap_or(&NULL)
    }
}

impl From<bool> for Value {
    fn from(flag: bool) -> Self {
        Self::Bool(flag)
    }
}

impl From<usize> for Value {
    fn from(number: usize) -> Self {
        Self::Int(number as i128)
    }
}

impl From<&str> for Value {
    fn from(text: &str) -> Self {
        Self::String(text.to_string())
    }
}

impl From<String> for Value {
    fn from(text: String) -> Self {
        Self::String(text)
    }
}

impl<T: Into<Value>> From<Vec<T>> for Value {
    fn from(items: Vec<T>) -> Self {
        Self::Array(items.into_iter().map(Into::into).collect())
    }
}

impl PartialEq<str> for Value {
    fn eq(&self, other: &str) -> bool {
        self.as_str() == Some(other)
    }
}

impl PartialEq<&str> for Value {
    fn eq(&self, other: &&str) -> bool {
        self.as_str() == Some(*other)
    }
}

impl PartialEq<i64> for Value {
    fn eq(&self, other: &i64) -> bool {
        self.as_i64() == Some(*other)
    }
}

/// The value as JSON text (members in their order, floats in Rust's shortest round-trip form), for assertion messages.
impl fmt::Display for Value {
    fn fmt(&self, out: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Null => out.write_str("null"),
            Self::Bool(flag) => write!(out, "{flag}"),
            Self::Int(number) => write!(out, "{number}"),
            Self::Float(number) => write!(out, "{number:?}"),
            Self::String(text) => write_quoted(out, text),
            Self::Array(items) => {
                out.write_str("[")?;
                for (position, item) in items.iter().enumerate() {
                    if position > 0 {
                        out.write_str(",")?;
                    }
                    write!(out, "{item}")?;
                }
                out.write_str("]")
            }
            Self::Object(members) => {
                out.write_str("{")?;
                for (position, (key, value)) in members.iter().enumerate() {
                    if position > 0 {
                        out.write_str(",")?;
                    }
                    write_quoted(out, key)?;
                    write!(out, ":{value}")?;
                }
                out.write_str("}")
            }
        }
    }
}

fn write_quoted(out: &mut fmt::Formatter<'_>, text: &str) -> fmt::Result {
    out.write_str("\"")?;
    for ch in text.chars() {
        match ch {
            '"' => out.write_str("\\\"")?,
            '\\' => out.write_str("\\\\")?,
            '\n' => out.write_str("\\n")?,
            '\r' => out.write_str("\\r")?,
            '\t' => out.write_str("\\t")?,
            control if u32::from(control) < 0x20 => write!(out, "\\u{:04x}", u32::from(control))?,
            other => write!(out, "{other}")?,
        }
    }
    out.write_str("\"")
}

/// Parses one JSON document. The error names the reason and the position: `"<reason> at byte <n> (line <l>, column <c>)"`, where the
/// byte offset counts from 0 and the column counts bytes from 1 on its line.
pub(crate) fn parse(text: &str) -> Result<Value, String> {
    let mut parser = Parser { text, position: 0 };
    parser.skip_whitespace();
    let value = parser.value(0)?;
    parser.skip_whitespace();
    if parser.position != text.len() {
        return parser.fail("trailing characters after the value");
    }
    Ok(value)
}

struct Parser<'a> {
    text: &'a str,
    /// A byte offset, always on a character boundary.
    position: usize,
}

impl Parser<'_> {
    fn fail<T>(&self, reason: &str) -> Result<T, String> {
        let before = &self.text.as_bytes()[..self.position];
        let line = before.iter().filter(|byte| **byte == b'\n').count() + 1;
        let line_start = before
            .iter()
            .rposition(|byte| *byte == b'\n')
            .map_or(0, |newline| newline + 1);
        Err(format!(
            "{reason} at byte {} (line {line}, column {})",
            self.position,
            self.position - line_start + 1
        ))
    }

    fn peek(&self) -> Option<u8> {
        self.text.as_bytes().get(self.position).copied()
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.position += 1;
        }
    }

    /// Consumes `expected` or fails with `reason`.
    fn expect(&mut self, expected: u8, reason: &str) -> Result<(), String> {
        if self.peek() == Some(expected) {
            self.position += 1;
            Ok(())
        } else {
            self.fail(reason)
        }
    }

    fn value(&mut self, depth: usize) -> Result<Value, String> {
        if depth > MAX_DEPTH {
            return self.fail("nesting too deep");
        }
        match self.peek() {
            None => self.fail("unexpected end of input"),
            Some(b'n') => self.literal("null", Value::Null),
            Some(b't') => self.literal("true", Value::Bool(true)),
            Some(b'f') => self.literal("false", Value::Bool(false)),
            Some(b'"') => self.string().map(Value::String),
            Some(b'[') => self.array(depth),
            Some(b'{') => self.object(depth),
            Some(b'-' | b'0'..=b'9') => self.number(),
            Some(_) => {
                let found = self.text[self.position..].chars().next().unwrap_or('?');
                self.fail(&format!("unexpected character {found:?}"))
            }
        }
    }

    fn literal(&mut self, word: &str, value: Value) -> Result<Value, String> {
        if self.text[self.position..].starts_with(word) {
            self.position += word.len();
            Ok(value)
        } else {
            self.fail(&format!("expected `{word}`"))
        }
    }

    /// Consumes a run of ASCII digits and returns how many there were.
    fn digits(&mut self) -> usize {
        let start = self.position;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.position += 1;
        }
        self.position - start
    }

    fn number(&mut self) -> Result<Value, String> {
        let start = self.position;
        if self.peek() == Some(b'-') {
            self.position += 1;
        }
        if self.peek() == Some(b'0') {
            self.position += 1;
            if matches!(self.peek(), Some(b'0'..=b'9')) {
                return self.fail("a number must not have a leading zero");
            }
        } else if self.digits() == 0 {
            return self.fail("expected a digit");
        }
        let mut integral = true;
        if self.peek() == Some(b'.') {
            integral = false;
            self.position += 1;
            if self.digits() == 0 {
                return self.fail("expected a digit after the decimal point");
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            integral = false;
            self.position += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.position += 1;
            }
            if self.digits() == 0 {
                return self.fail("expected a digit in the exponent");
            }
        }
        let literal = &self.text[start..self.position];
        if integral {
            if let Ok(number) = literal.parse::<i128>() {
                if (i128::from(i64::MIN)..=i128::from(u64::MAX)).contains(&number) {
                    return Ok(Value::Int(number));
                }
            }
        }
        match literal.parse::<f64>() {
            Ok(number) if number.is_finite() => Ok(Value::Float(number)),
            _ => {
                self.position = start;
                self.fail("number out of range")
            }
        }
    }

    fn string(&mut self) -> Result<String, String> {
        self.position += 1; // the opening quote
        let mut text = String::new();
        loop {
            match self.peek() {
                None => return self.fail("unterminated string"),
                Some(b'"') => {
                    self.position += 1;
                    return Ok(text);
                }
                Some(b'\\') => {
                    self.position += 1;
                    text.push(self.escape()?);
                }
                Some(byte) if byte < 0x20 => {
                    return self.fail("raw control character in a string");
                }
                Some(_) => {
                    let Some(ch) = self.text[self.position..].chars().next() else {
                        return self.fail("unexpected end of input");
                    };
                    text.push(ch);
                    self.position += ch.len_utf8();
                }
            }
        }
    }

    /// The character of the escape after a backslash; a `\uXXXX` high surrogate must be followed by a `\uXXXX` low one.
    fn escape(&mut self) -> Result<char, String> {
        let Some(byte) = self.peek() else {
            return self.fail("unterminated escape");
        };
        self.position += 1;
        let simple = match byte {
            b'"' => '"',
            b'\\' => '\\',
            b'/' => '/',
            b'b' => '\u{8}',
            b'f' => '\u{c}',
            b'n' => '\n',
            b'r' => '\r',
            b't' => '\t',
            b'u' => return self.unicode_escape(),
            _ => {
                self.position -= 1;
                return self.fail("invalid escape");
            }
        };
        Ok(simple)
    }

    fn unicode_escape(&mut self) -> Result<char, String> {
        let first = self.hex4()?;
        let code = match first {
            0xD800..=0xDBFF => {
                if !self.text[self.position..].starts_with("\\u") {
                    return self.fail("a high surrogate must be followed by a \\u low surrogate");
                }
                self.position += 2;
                let second = self.hex4()?;
                if !(0xDC00..=0xDFFF).contains(&second) {
                    self.position -= 4;
                    return self.fail("expected a low surrogate after a high surrogate");
                }
                0x10000 + ((u32::from(first) - 0xD800) << 10) + (u32::from(second) - 0xDC00)
            }
            0xDC00..=0xDFFF => {
                self.position -= 4;
                return self.fail("a low surrogate without a high surrogate");
            }
            other => u32::from(other),
        };
        match char::from_u32(code) {
            Some(ch) => Ok(ch),
            None => self.fail("not a Unicode scalar value"),
        }
    }

    fn hex4(&mut self) -> Result<u16, String> {
        let digits = self.text.get(self.position..self.position + 4);
        match digits {
            Some(hex) if hex.bytes().all(|byte| byte.is_ascii_hexdigit()) => {
                self.position += 4;
                u16::from_str_radix(hex, 16).or_else(|_| self.fail("invalid \\u escape"))
            }
            _ => self.fail("expected four hexadecimal digits after \\u"),
        }
    }

    fn array(&mut self, depth: usize) -> Result<Value, String> {
        self.position += 1; // `[`
        let mut items = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b']') {
            self.position += 1;
            return Ok(Value::Array(items));
        }
        loop {
            self.skip_whitespace();
            items.push(self.value(depth + 1)?);
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.position += 1,
                Some(b']') => {
                    self.position += 1;
                    return Ok(Value::Array(items));
                }
                _ => return self.fail("expected `,` or `]`"),
            }
        }
    }

    fn object(&mut self, depth: usize) -> Result<Value, String> {
        self.position += 1; // `{`
        let mut members: Vec<(String, Value)> = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b'}') {
            self.position += 1;
            return Ok(Value::Object(members));
        }
        loop {
            self.skip_whitespace();
            if self.peek() != Some(b'"') {
                return self.fail("expected a string key");
            }
            let key_start = self.position;
            let key = self.string()?;
            if members.iter().any(|(name, _)| *name == key) {
                self.position = key_start;
                return self.fail(&format!("duplicate key {key:?}"));
            }
            self.skip_whitespace();
            self.expect(b':', "expected `:` after the key")?;
            self.skip_whitespace();
            members.push((key, self.value(depth + 1)?));
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.position += 1,
                Some(b'}') => {
                    self.position += 1;
                    return Ok(Value::Object(members));
                }
                _ => return self.fail("expected `,` or `}`"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(text: &str) -> Value {
        parse(text).unwrap_or_else(|error| panic!("{text:?}: {error}"))
    }

    fn float_bits(text: &str) -> u64 {
        match ok(text) {
            Value::Float(number) => number.to_bits(),
            other => panic!("{text}: not a float: {other:?}"),
        }
    }

    #[test]
    fn scalars_and_whitespace_parse() {
        assert_eq!(ok(" \t\r\n null "), Value::Null);
        assert_eq!(ok("true"), Value::Bool(true));
        assert_eq!(ok("false").as_bool(), Some(false));
        assert_eq!(ok("\"\""), Value::String(String::new()));
        assert_eq!(ok("[ ]"), Value::Array(vec![]));
        assert_eq!(ok("{ }"), Value::Object(vec![]));
        assert!(ok("null").is_null());
        assert!(!ok("0").is_null());
    }

    #[test]
    fn string_escapes_and_surrogate_pairs_decode() {
        let got = ok(r#""a\"\\\/\b\f\n\r\t\u00e9\u0041 é""#);
        assert_eq!(got, "a\"\\/\u{8}\u{c}\n\r\t\u{e9}A é");
        // U+1F600 as the pair D83D DE00, upper and lower case hex, and U+10FFFF as DBFF DFFF
        assert_eq!(ok(r#""\ud83d\ude00""#), "\u{1F600}");
        assert_eq!(ok(r#""\uD83D\uDE00""#), "\u{1F600}");
        assert_eq!(ok(r#""\uDBFF\uDFFF""#), "\u{10FFFF}");
        assert_eq!(ok(r#""\u0000""#), "\u{0}");
        for bad in [
            r#""\ud83d""#,       // high surrogate alone
            r#""\ud83dx""#,      // high surrogate followed by a plain character
            r#""\ud83d\u0041""#, // high surrogate followed by a non-surrogate
            r#""\ud83d\ud83d""#, // high surrogate followed by a high one
            r#""\ude00""#,       // low surrogate alone
            r#""\u12""#,         // short escape
            r#""\u12g4""#,       // not hex
            r#""\u+123""#,       // a sign is not a hex digit
            r#""\x41""#,         // unknown escape
            "\"\\",              // truncated escape
            "\"a\nb\"",          // raw newline
            "\"a\u{1}b\"",       // raw control character
            "\"unterminated",    // no closing quote
        ] {
            assert!(parse(bad).is_err(), "{bad:?} must be rejected");
        }
    }

    #[test]
    fn object_key_order_is_kept_and_compared() {
        let got = ok(r#"{"b": 1, "a": 2, "c": {"z": 0, "y": [1, {"q": 1, "p": 2}]}}"#);
        let keys: Vec<&str> = got
            .as_object()
            .expect("object")
            .iter()
            .map(|(key, _)| key.as_str())
            .collect();
        assert_eq!(keys, ["b", "a", "c"]);
        assert_eq!(got["c"]["z"], 0);
        assert_eq!(got["c"]["y"][1]["p"], 2);
        // equal text is equal; the same members in another order are a different value, at any depth
        assert_eq!(
            got,
            ok(r#"{"b":1,"a":2,"c":{"z":0,"y":[1,{"q":1,"p":2}]}}"#)
        );
        assert_ne!(
            got,
            ok(r#"{"a":2,"b":1,"c":{"z":0,"y":[1,{"q":1,"p":2}]}}"#)
        );
        assert_ne!(
            got,
            ok(r#"{"b":1,"a":2,"c":{"z":0,"y":[1,{"p":2,"q":1}]}}"#)
        );
        let error = parse(r#"{"a": 1, "a": 2}"#).expect_err("duplicate key");
        assert!(error.contains("duplicate key \"a\" at byte 9"), "{error}");
    }

    #[test]
    fn floats_parse_to_the_correctly_rounded_double() {
        assert_eq!(float_bits("0.1"), 0x3FB9_9999_9999_999A);
        assert_eq!(float_bits("1e-7"), 0x3E7A_D7F2_9ABC_AF48);
        assert_eq!(float_bits("1E-7"), float_bits("1e-7"));
        assert_eq!(float_bits("1.7976931348623157e308"), f64::MAX.to_bits());
        assert_eq!(float_bits("5e-324"), 1);
        assert_eq!(float_bits("-0.0"), (-0.0_f64).to_bits());
        // 17 significant digits: the sum of 0.1 and 0.2 prints this way and is that very double
        assert_eq!(float_bits("0.30000000000000004"), (0.1_f64 + 0.2).to_bits());
        // a tie between two doubles rounds to even (2^53), a hair above it rounds up
        assert_eq!(
            float_bits("9007199254740993.0"),
            9_007_199_254_740_992_f64.to_bits()
        );
        assert_eq!(
            float_bits("9007199254740993.000001"),
            9_007_199_254_740_994_f64.to_bits()
        );
        assert_eq!(ok("1e2"), Value::Float(100.0));
        assert_eq!(ok("-2.5E+1").as_f64(), Some(-25.0));
        assert_ne!(ok("1"), ok("1.0"));
        for bad in [
            "1e999", "-1e999", "1e", "1e+", "1.", ".5", "-", "+1", "01", "-01", "0x10", "1.5.2",
            "NaN", "Infinity",
        ] {
            assert!(parse(bad).is_err(), "{bad:?} must be rejected");
        }
    }

    #[test]
    fn integers_are_exact_at_the_i64_and_u64_edges() {
        let min = ok("-9223372036854775808");
        assert_eq!(min.as_i64(), Some(i64::MIN));
        assert_eq!(min.as_u64(), None);
        let max = ok("9223372036854775807");
        assert_eq!(max.as_i64(), Some(i64::MAX));
        assert_eq!(max.as_u64(), Some(i64::MAX as u64));
        let above = ok("9223372036854775808");
        assert_eq!(above.as_i64(), None);
        assert_eq!(above.as_u64(), Some(1 << 63));
        let top = ok("18446744073709551615");
        assert_eq!(top.as_u64(), Some(u64::MAX));
        assert_eq!(top.as_i64(), None);
        assert_eq!(top.as_f64(), Some(u64::MAX as f64));
        // beyond the span an integer literal degrades to a float, as the common JSON crates do
        assert_eq!(
            ok("18446744073709551616"),
            Value::Float(18_446_744_073_709_551_616.0)
        );
        assert_eq!(
            ok("-9223372036854775809"),
            Value::Float(-9_223_372_036_854_775_809.0)
        );
        assert_eq!(ok("-9223372036854775809").as_i64(), None);
        assert_eq!(ok("-0"), Value::Int(0));
        // a float never reads back as an integer, even when it is whole
        assert_eq!(ok("1.0").as_i64(), None);
        assert_eq!(ok("1e2").as_u64(), None);
        assert_eq!(ok("7"), 7);
        assert_ne!(ok("7.0"), 7);
    }

    #[test]
    fn nesting_is_bounded() {
        let nested = |depth: usize| format!("{}1{}", "[".repeat(depth), "]".repeat(depth));
        // the outermost array is at depth 0 and the `1` at depth MAX_DEPTH
        let mut value = ok(&nested(MAX_DEPTH));
        for _ in 0..MAX_DEPTH {
            value = match value {
                Value::Array(mut items) => items.pop().expect("one element"),
                other => panic!("not an array: {other:?}"),
            };
        }
        assert_eq!(value, 1);
        let error = parse(&nested(MAX_DEPTH + 1)).expect_err("too deep");
        assert!(error.starts_with("nesting too deep at byte"), "{error}");
        let objects = format!(
            "{}1{}",
            r#"{"k":"#.repeat(MAX_DEPTH + 1),
            "}".repeat(MAX_DEPTH + 1)
        );
        assert!(parse(&objects).is_err());
        assert!(parse(&"[".repeat(100_000)).is_err());
    }

    #[test]
    fn trailing_garbage_and_truncated_input_are_errors_with_a_position() {
        let error = parse("[1] x").expect_err("trailing");
        assert_eq!(
            error,
            "trailing characters after the value at byte 4 (line 1, column 5)"
        );
        let error = parse("{\n  \"a\": tru\n}").expect_err("bad literal");
        assert_eq!(error, "expected `true` at byte 9 (line 2, column 8)");
        for bad in [
            "1 2",
            "{} {}",
            "[1,2]]",
            "null null",
            "truefalse",
            "\"a\" \"b\"",
        ] {
            let error = parse(bad).expect_err(bad);
            assert!(error.contains("trailing characters"), "{bad:?}: {error}");
        }
        for bad in [
            "",
            "   ",
            "[",
            "[1",
            "[1,",
            "[1,]",
            "[,1]",
            "[1 2]",
            "{",
            "{\"a\"",
            "{\"a\":",
            "{\"a\":1",
            "{\"a\":1,",
            "{\"a\":1,}",
            "{,}",
            "{\"a\" 1}",
            "{1:2}",
            "{'a':1}",
            "tru",
            "nul",
            "fals",
            "-",
            "\"abc",
            "[1,2",
        ] {
            assert!(parse(bad).is_err(), "{bad:?} must be rejected");
        }
        assert_eq!(
            parse("").expect_err("empty"),
            "unexpected end of input at byte 0 (line 1, column 1)"
        );
        assert!(parse("@")
            .expect_err("junk")
            .starts_with("unexpected character '@'"));
    }

    #[test]
    fn indexing_a_missing_member_or_the_wrong_kind_yields_null() {
        let got = ok(r#"{"a": [10, 20], "s": "text", "n": null}"#);
        assert_eq!(got["a"][1], 20);
        assert!(got["missing"].is_null());
        assert!(got["a"][2].is_null());
        assert!(got["a"]["a"].is_null());
        assert!(got["s"][0].is_null());
        assert!(got["n"]["x"]["y"][3].is_null());
        assert_eq!(got.get("n"), Some(&Value::Null));
        assert_eq!(got.get("missing"), None);
        assert_eq!(ok("[1]").get("a"), None);
        assert_eq!(got["s"].as_str(), Some("text"));
        assert_eq!(got["s"], "text");
        assert_eq!(&got["s"], "text");
        assert_ne!(got["s"], "other");
        assert_eq!(got["a"].as_array().map(<[Value]>::len), Some(2));
        assert_eq!(got["a"].as_object(), None);
        assert_eq!(got["s"].as_f64(), None);
        assert_eq!(got["s"].as_bool(), None);
        assert_eq!(got["a"].as_str(), None);
    }

    #[test]
    fn constructors_and_display_round_trip() {
        let built = Value::object([
            ("ok", true.into()),
            ("names", vec!["x", "y\"z"].into()),
            ("owner", String::from("A").into()),
            ("none", Value::Null),
            ("n", Value::Int(-3)),
            ("count", 2_usize.into()),
            ("f", Value::Float(1e-7)),
        ]);
        let text = built.to_string();
        assert_eq!(
            text,
            r#"{"ok":true,"names":["x","y\"z"],"owner":"A","none":null,"n":-3,"count":2,"f":1e-7}"#
        );
        assert_eq!(ok(&text), built);
        let tricky =
            ok(r#"{"b":["\u0001\n\\",0.1,1.7976931348623157e308,18446744073709551615,{}],"a":[]}"#);
        assert_eq!(ok(&tricky.to_string()), tricky);
    }
}
