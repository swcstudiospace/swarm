//! ASCII character classes shared by the scanners.
//!
//! The Python module spells its classes out or compiles the pattern with `re.ASCII`, so every
//! class here is ASCII-only. A non-ASCII code point is never alphanumeric, a word character or
//! whitespace; the negated classes in the Python patterns (`[^ \t\r\n]` and the like) still
//! accept it, and the scanners express that by negating these predicates, not by widening them.

/// `[A-Za-z]`.
pub fn is_alpha(c: char) -> bool {
    c.is_ascii_alphabetic()
}

/// `[A-Za-z0-9]`, the Python `_ALNUM` set.
pub fn is_alnum(c: char) -> bool {
    c.is_ascii_alphanumeric()
}

/// `[A-Za-z0-9_]`, ASCII `\w`.
pub fn is_word(c: char) -> bool {
    c == '_' || c.is_ascii_alphanumeric()
}

/// `[0-9]`.
pub fn is_digit(c: char) -> bool {
    c.is_ascii_digit()
}

/// `[A-Z]`.
pub fn is_upper(c: char) -> bool {
    c.is_ascii_uppercase()
}

/// `[a-z]`.
pub fn is_lower(c: char) -> bool {
    c.is_ascii_lowercase()
}

/// `[ \t]`, a blank.
pub fn is_blank(c: char) -> bool {
    c == ' ' || c == '\t'
}

/// ASCII `\s`, that is `[ \t\n\r\f\v]`.
pub fn is_space(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\r' | '\x0c' | '\x0b')
}

/// `[\r\n]`, a line terminator for the `_EOL_RE` pattern.
pub fn is_eol(c: char) -> bool {
    c == '\r' || c == '\n'
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classes_are_ascii_only() {
        for c in [
            'é', 'ß', 'Ⅷ', '\u{a0}', '\u{85}', '\u{2028}', '\u{212a}', '\u{17f}',
        ] {
            assert!(
                !is_alpha(c) && !is_alnum(c) && !is_word(c) && !is_digit(c),
                "{c:?}"
            );
            assert!(
                !is_upper(c) && !is_lower(c) && !is_space(c) && !is_blank(c),
                "{c:?}"
            );
        }
    }

    #[test]
    fn space_is_python_ascii_whitespace() {
        for c in [' ', '\t', '\n', '\r', '\x0c', '\x0b'] {
            assert!(is_space(c), "{c:?}");
        }
        assert!(
            !is_space('\x1c') && !is_space('\x1f'),
            "the separators Python's str.isspace accepts are not ASCII \\s"
        );
    }

    #[test]
    fn blank_and_eol_are_narrow() {
        assert!(is_blank(' ') && is_blank('\t') && !is_blank('\n'));
        assert!(is_eol('\r') && is_eol('\n') && !is_eol('\x0c'));
    }
}
