//! Standard-alphabet base64 with the exact acceptance rule of Python's `base64.b64decode(data, validate=True)`, which is
//! `binascii.a2b_base64(data, strict_mode=True)`.
//!
//! In strict mode: leading `=` fails; every other byte must be in the alphabet (no whitespace, no other alphabet); once a `=` has
//! been seen, any data fails; the data characters must fill whole groups of four once padding completes them, and a `=` that
//! completes a group must be the last byte. A `=` after a complete group (or after one data character of a group) is ignored, so
//! `AAAA=====` decodes. The unused low bits of the last data character are not checked, so several spellings decode to the same
//! bytes, and this decoder accepts all of them.

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Encodes with the standard alphabet and `=` padding (Python `base64.b64encode`).
pub(crate) fn encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk.first().copied().unwrap_or(0);
        let b1 = chunk.get(1).copied().unwrap_or(0);
        let b2 = chunk.get(2).copied().unwrap_or(0);
        let group = (u32::from(b0) << 16) | (u32::from(b1) << 8) | u32::from(b2);
        let sextets = [
            (group >> 18) & 63,
            (group >> 12) & 63,
            (group >> 6) & 63,
            group & 63,
        ];
        for (index, sextet) in sextets.iter().enumerate() {
            if index <= chunk.len() {
                out.push(char::from(ALPHABET[*sextet as usize]));
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// Decodes like `base64.b64decode(text, validate=True)`, returning `None` where Python raises.
pub(crate) fn decode(text: &str) -> Option<Vec<u8>> {
    let bytes = text.as_bytes();
    if bytes.first() == Some(&b'=') {
        return None;
    }
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3 + 2);
    let mut acc: u32 = 0;
    let mut bits = 0u32;
    let mut quad = 0usize;
    let mut pads = 0usize;
    let mut padding_started = false;
    // Set when the padding completed a group: Python then skips the final whole-group check.
    let mut padded = false;
    for (at, &byte) in bytes.iter().enumerate() {
        if byte == b'=' {
            padding_started = true;
            if quad >= 2 {
                pads += 1;
                if quad + pads >= 4 {
                    if at + 1 < bytes.len() {
                        return None;
                    }
                    padded = true;
                    break;
                }
            }
            continue;
        }
        if padding_started {
            return None;
        }
        let value = ALPHABET.iter().position(|&a| a == byte)? as u32;
        acc = ((acc << 6) | value) & 0x1fff;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((acc >> bits) & 0xff) as u8);
        }
        quad = (quad + 1) % 4;
    }
    (padded || quad == 0).then_some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_pads_each_remainder() {
        let cases: [(&[u8], &str); 5] = [
            (b"", ""),
            (b"f", "Zg=="),
            (b"fo", "Zm8="),
            (b"foo", "Zm9v"),
            (b"foob", "Zm9vYg=="),
        ];
        for (input, expected) in cases {
            assert_eq!(encode(input), expected);
        }
    }

    #[test]
    fn decode_round_trips_every_length_up_to_forty() {
        for length in 0..40u8 {
            let data: Vec<u8> = (0..length)
                .map(|i| i.wrapping_mul(37).wrapping_add(11))
                .collect();
            assert_eq!(decode(&encode(&data)), Some(data));
        }
    }

    /// The oracle's own answers (`swarm.envelope._b64`, generated into the vector file) for every string over `ABQ=` up to length five
    /// plus hand-picked padding, whitespace and alphabet cases.
    #[test]
    fn decode_agrees_with_the_python_oracle_on_every_vector() -> Result<(), String> {
        let vectors = gsd_route::parse_json(include_str!("../tests/vectors/envelope.json"))
            .map_err(|e| e.message().to_string())?;
        let gsd_route::Json::Obj(fields) = &vectors else {
            return Err("vectors are not an object".to_string());
        };
        let Some(gsd_route::Json::Arr(cases)) = gsd_route::obj_get(fields, "base64") else {
            return Err("no base64 section".to_string());
        };
        let mut accepted = 0;
        for case in cases {
            let gsd_route::Json::Obj(case) = case else {
                return Err("case is not an object".to_string());
            };
            let Some(gsd_route::Json::Str(input)) = gsd_route::obj_get(case, "input") else {
                return Err("case without input".to_string());
            };
            let expected = match gsd_route::obj_get(case, "decoded_hex") {
                Some(gsd_route::Json::Str(hex_text)) => Some(hex_text.clone()),
                Some(gsd_route::Json::Null) => None,
                _ => return Err("case without decoded_hex".to_string()),
            };
            accepted += usize::from(expected.is_some());
            assert_eq!(decode(input).map(hex::encode), expected, "input {input:?}");
        }
        assert!(
            cases.len() > 1000 && accepted > 50,
            "the vector set must hold both kinds: {} cases, {accepted} accepted",
            cases.len()
        );
        Ok(())
    }

    #[test]
    fn decode_follows_pythons_strict_rule() {
        let cases: [(&str, Option<&[u8]>); 24] = [
            ("", Some(b"")),
            ("Zg==", Some(b"f")),
            ("Zh==", Some(b"f")),
            ("Zm8=", Some(b"fo")),
            ("Zm9=", Some(b"fo")),
            ("Zm9v=", Some(b"foo")),
            ("Zm9v====", Some(b"foo")),
            ("Zg=", None),
            ("Zg", None),
            ("Z", None),
            ("Zm9vY", None),
            ("Zm8==", None),
            ("Zg===", None),
            ("Zg==Zg==", None),
            ("Zm9v=A", None),
            ("Zm9v=Zm9v", None),
            ("Z===", None),
            ("=", None),
            ("==", None),
            ("=Zg==", None),
            ("Zg==\n", None),
            ("Zm 8=", None),
            (" Zm8=", None),
            ("Zm8=\u{0}", None),
        ];
        for (input, expected) in cases {
            assert_eq!(decode(input).as_deref(), expected, "input {input:?}");
        }
    }

    #[test]
    fn decode_rejects_other_alphabets_and_non_ascii() {
        for input in ["Zm8-", "Zm8_", "Zm8é", "Zm8\u{0}"] {
            assert_eq!(decode(input), None, "input {input:?}");
        }
    }
}
