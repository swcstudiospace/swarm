//! Python `str` rules that a Rust reader must reproduce to cut and blank-test lines as Python does.
//!
//! `str.splitlines()` ends a line at `\n`, `\r`, `\r\n`, `\v`, `\f`, U+001C, U+001D, U+001E, U+0085, U+2028 and
//! U+2029, and yields no empty piece after a final break. `str.strip()` tests whitespace with `str.isspace()`, which
//! is the Unicode `White_Space` set plus U+001C to U+001F. Rust's `str::lines` ends a line only at `\n` and
//! `char::is_whitespace` omits U+001C to U+001F, so neither matches Python.

/// Python's `str.isspace()` for one character: the Unicode `White_Space` set plus U+001C to U+001F.
pub fn py_isspace(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\u{1c}'..='\u{1f}')
}

/// Python's `not line.strip()`: whether every character of `line` is `isspace`.
pub fn py_is_blank(line: &str) -> bool {
    line.chars().all(py_isspace)
}

/// Python's `str.splitlines()` without `keepends`.
///
/// A `Path.read_text` universal-newline translation of `\r\n` and `\r` to `\n` changes no boundary here, because
/// both characters already end a line.
pub fn py_splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((index, ch)) = chars.next() {
        if !is_line_break(ch) {
            continue;
        }
        lines.push(&text[start..index]);
        start = index + ch.len_utf8();
        if ch == '\r' && matches!(chars.peek(), Some((_, '\n'))) {
            chars.next();
            start += 1;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

fn is_line_break(ch: char) -> bool {
    matches!(
        ch,
        '\n' | '\r' | '\u{0b}' | '\u{0c}' | '\u{1c}'
            ..='\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}'
    )
}

#[cfg(test)]
mod tests {
    use super::{py_is_blank, py_splitlines};

    #[test]
    fn splitlines_breaks_at_every_python_boundary() {
        for separator in [
            "\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\u{85}", "\u{2028}",
            "\u{2029}",
        ] {
            let text = format!("a{separator}b");
            assert_eq!(
                py_splitlines(&text),
                vec!["a", "b"],
                "separator {separator:?}"
            );
        }
    }

    #[test]
    fn splitlines_matches_python_on_edge_inputs() {
        assert_eq!(py_splitlines(""), Vec::<&str>::new());
        assert_eq!(py_splitlines("\n"), vec![""]);
        assert_eq!(py_splitlines("\r"), vec![""]);
        assert_eq!(py_splitlines("a\n"), vec!["a"]);
        assert_eq!(py_splitlines("a\n\n"), vec!["a", ""]);
        assert_eq!(py_splitlines("a\r\n"), vec!["a"]);
        assert_eq!(py_splitlines("a\r"), vec!["a"]);
        assert_eq!(py_splitlines("\r\r\n"), vec!["", ""]);
        assert_eq!(py_splitlines("\r\n\r\n"), vec!["", ""]);
        assert_eq!(py_splitlines("a\x1cb\x1db\x1ec"), vec!["a", "b", "b", "c"]);
        assert_eq!(py_splitlines("a\u{2028}\u{2028}b"), vec!["a", "", "b"]);
        assert_eq!(
            py_splitlines("\u{e9}\u{2028}\u{fc}\n"),
            vec!["\u{e9}", "\u{fc}"]
        );
    }

    #[test]
    fn splitlines_keeps_characters_python_does_not_break_on() {
        assert_eq!(py_splitlines("a\x1fb"), vec!["a\x1fb"]);
        assert_eq!(py_splitlines("a\u{a0}b"), vec!["a\u{a0}b"]);
        assert_eq!(
            py_splitlines("a\u{200b}b\u{feff}"),
            vec!["a\u{200b}b\u{feff}"]
        );
    }

    #[test]
    fn blank_follows_python_strip() {
        for blank in [
            "",
            " \t\x0b\x0c\r\n",
            "\x1c",
            "\x1d",
            "\x1e",
            "\x1f",
            "\u{85}",
            "\u{a0}",
            "\u{2028}",
            "\u{2029}",
            "\u{3000}",
            " \x1f\u{a0} ",
        ] {
            assert!(py_is_blank(blank), "{blank:?} is blank for Python");
        }
        for text in ["x", " x ", "\u{200b}", "\u{feff}", "\x00", "{}"] {
            assert!(!py_is_blank(text), "{text:?} is not blank for Python");
        }
    }
}
