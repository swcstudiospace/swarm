//! `sign_envelope` and `verify_envelope` over the canonical form, with Python's signature formats and failure rules.
//!
//! A signature is `hmac:` or `ed25519:` followed by standard base64. Verification returns `false` for every malformed or
//! unmatched input, exactly where `swarm/envelope.py` does, and compares MACs in constant time.

use ed25519_dalek::{Signature, Signer, Verifier};
use gsd_codes::ErrorCode;
use gsd_route::{obj_get, Json, SeamError};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use subtle::ConstantTimeEq;

use crate::keys::{HmacKey, KeyMaterial, NO_SIGNING_KEY};
use crate::{base64, canonical_bytes, Declined, SignError};

/// What a successful [`sign_envelope`] reports besides the signed envelope.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SignOutcome {
    /// True when the unset-or-empty `SWARM_SIGNING_KEY` dev-key opt-in signed. Python then records a `security.dev_key` audit event
    /// (unless the caller is a key-less agent-session preview); this crate has no run log, so the caller does.
    pub dev_key_audit: bool,
}

/// HMAC-SHA256 of `data` under `key`, or `None` if the HMAC implementation rejects the key length (it never does).
fn hmac_sha256(key: &HmacKey, data: &[u8]) -> Option<Vec<u8>> {
    let mut mac = Hmac::<Sha256>::new_from_slice(&key.secret).ok()?;
    mac.update(data);
    Some(mac.finalize().into_bytes().to_vec())
}

/// Signs `env` in place: sets or replaces the top-level `sig` member (a new one is appended, as Python's `env["sig"] = ...`).
///
/// An Ed25519 signer wins over an HMAC secret. Python's checks run first and fail closed.
///
/// # Errors
///
/// [`SignError::Policy`] with Python's `E-POLICY` text for a key misconfiguration or when no key may sign;
/// [`SignError::Declined`] when `env` is not an object.
pub fn sign_envelope(env: &mut Json, keys: &KeyMaterial) -> Result<SignOutcome, SignError> {
    if let Some(message) = keys.config_error {
        return Err(SignError::Policy(SeamError::new(
            ErrorCode::Policy,
            message,
        )));
    }
    let (signature, dev_key_audit) = if let Some(signer) = &keys.ed25519 {
        let signed = signer.sign(&canonical_bytes(env)?);
        (
            format!("ed25519:{}", base64::encode(&signed.to_bytes())),
            false,
        )
    } else if let Some(hmac_key) = &keys.hmac {
        let mac = hmac_sha256(hmac_key, &canonical_bytes(env)?).ok_or(Declined::HmacKeyRejected)?;
        (
            format!("hmac:{}", base64::encode(&mac)),
            hmac_key.implicit_dev,
        )
    } else {
        return Err(SignError::Policy(SeamError::new(
            ErrorCode::Policy,
            NO_SIGNING_KEY,
        )));
    };
    if let Json::Obj(fields) = env {
        match fields.iter_mut().find(|(name, _)| name == "sig") {
            Some(slot) => slot.1 = Json::Str(signature),
            None => fields.push(("sig".to_string(), Json::Str(signature))),
        }
    }
    Ok(SignOutcome { dev_key_audit })
}

/// Verifies the top-level `sig` of `env` against the configured keys. `false` for anything that is not a valid signature,
/// including a missing or non-string `sig`, an unknown prefix, bad base64, a missing key and a non-object `env`.
pub fn verify_envelope(env: &Json, keys: &KeyMaterial) -> bool {
    let Json::Obj(fields) = env else {
        return false;
    };
    let Some(Json::Str(sig)) = obj_get(fields, "sig") else {
        return false;
    };
    if let Some(encoded) = sig.strip_prefix("hmac:") {
        let Some(hmac_key) = &keys.hmac else {
            return false;
        };
        let Some(given) = base64::decode(encoded) else {
            return false;
        };
        let Ok(canonical) = canonical_bytes(env) else {
            return false;
        };
        return match hmac_sha256(hmac_key, &canonical) {
            Some(expected) => bool::from(expected.ct_eq(&given)),
            None => false,
        };
    }
    if let Some(encoded) = sig.strip_prefix("ed25519:") {
        let (Some(signer), Some(given)) = (&keys.ed25519, base64::decode(encoded)) else {
            return false;
        };
        let (Ok(signature), Ok(canonical)) = (Signature::from_slice(&given), canonical_bytes(env))
        else {
            return false;
        };
        return signer
            .verifying_key()
            .verify(&canonical, &signature)
            .is_ok();
    }
    false
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::ffi::OsString;

    use sha2::Digest;

    use super::*;
    use crate::parse_envelope;

    type TestResult = Result<(), String>;

    /// Test keys come from this documented phrase: `sha256(PHRASE + label)`, hex-encoded, is the secret or the Ed25519 seed.
    const PHRASE: &str = "gsd-envelope unit tests: derived key phrase, not a secret: ";

    fn derived(label: &str) -> String {
        hex::encode(Sha256::digest(format!("{PHRASE}{label}").as_bytes()))
    }

    fn keys(vars: &[(&str, &str)]) -> Result<KeyMaterial, String> {
        let map: HashMap<String, OsString> = vars
            .iter()
            .map(|(k, v)| {
                (
                    (*k).to_string(),
                    OsString::from(
                        v.strip_prefix('@')
                            .map_or_else(|| (*v).to_string(), derived),
                    ),
                )
            })
            .collect();
        KeyMaterial::from_lookup(|name| map.get(name).cloned(), |_| false)
            .map_err(|e| e.to_string())
    }

    fn body() -> Result<Json, String> {
        parse_envelope(r#"{"msg_id":"m1","type":"gate.verdict","ttl_s":300,"score":0.5,"payload":{"verdict":"APPROVED","n":[1,2.5,"\u00e9"]}}"#)
            .map_err(|e| e.to_string())
    }

    fn sig_of(env: &Json) -> Option<String> {
        match env {
            Json::Obj(fields) => match obj_get(fields, "sig") {
                Some(Json::Str(sig)) => Some(sig.clone()),
                _ => None,
            },
            _ => None,
        }
    }

    #[test]
    fn hmac_matches_a_reference_value_computed_independently() -> TestResult {
        // The reference tag is HMAC-SHA256 of the literal canonical text, computed with the `hmac` crate directly; so a wrong
        // canonical form (key order, separators, float text) or a wrong key choice changes the expected string.
        let secret = derived("hmac");
        let mut env = parse_envelope(r#"{"type":"gate.verdict","payload":{"n":[1,2.5,1e22],"verdict":"APPROVED"},"msg_id":"m1","ttl_s":300}"#)
            .map_err(|e| e.to_string())?;
        let signed = sign_envelope(&mut env, &keys(&[("SWARM_SIGNING_KEY", "@hmac")])?)
            .map_err(|e| e.to_string())?;
        assert!(!signed.dev_key_audit);
        let canonical = br#"{"msg_id":"m1","payload":{"n":[1,2.5,1e+22],"verdict":"APPROVED"},"ttl_s":300,"type":"gate.verdict"}"#;
        let mut reference =
            Hmac::<Sha256>::new_from_slice(secret.as_bytes()).map_err(|e| e.to_string())?;
        reference.update(canonical);
        let expected = format!(
            "hmac:{}",
            base64::encode(&reference.finalize().into_bytes())
        );
        assert_eq!(sig_of(&env), Some(expected));
        Ok(())
    }

    #[test]
    fn signed_envelopes_verify_and_changes_break_them() -> TestResult {
        let hmac_keys = keys(&[("SWARM_SIGNING_KEY", "@hmac")])?;
        let ed_keys = keys(&[("SWARM_ED25519_KEY", "@ed")])?;
        let other_hmac = keys(&[("SWARM_SIGNING_KEY", "@other")])?;
        let other_ed = keys(&[("SWARM_ED25519_KEY", "@other")])?;
        for (signer, wrong, prefix) in [
            (&hmac_keys, &other_hmac, "hmac:"),
            (&ed_keys, &other_ed, "ed25519:"),
        ] {
            let mut env = body()?;
            sign_envelope(&mut env, signer).map_err(|e| e.to_string())?;
            assert!(sig_of(&env).is_some_and(|sig| sig.starts_with(prefix)));
            assert!(
                verify_envelope(&env, signer),
                "{prefix} verifies with its own key"
            );
            assert!(
                !verify_envelope(&env, wrong),
                "{prefix} fails under another key"
            );
            assert!(
                !verify_envelope(&env, &keys(&[])?),
                "{prefix} fails without a key"
            );
            let Json::Obj(fields) = &mut env else {
                return Err("not an object".to_string());
            };
            fields[0].1 = Json::Str("m2".to_string());
            assert!(
                !verify_envelope(&env, signer),
                "{prefix} fails after a body change"
            );
        }
        Ok(())
    }

    #[test]
    fn ed25519_wins_over_hmac_and_each_prefix_needs_its_own_material() -> TestResult {
        let both = keys(&[("SWARM_SIGNING_KEY", "@hmac"), ("SWARM_ED25519_KEY", "@ed")])?;
        let mut env = body()?;
        sign_envelope(&mut env, &both).map_err(|e| e.to_string())?;
        assert!(sig_of(&env).is_some_and(|sig| sig.starts_with("ed25519:")));
        assert!(verify_envelope(&env, &both));
        assert!(
            !verify_envelope(&env, &keys(&[("SWARM_SIGNING_KEY", "@hmac")])?),
            "an ed25519 signature needs the ed25519 key"
        );
        let mut hmac_env = body()?;
        sign_envelope(&mut hmac_env, &keys(&[("SWARM_SIGNING_KEY", "@hmac")])?)
            .map_err(|e| e.to_string())?;
        assert!(
            verify_envelope(&hmac_env, &both),
            "an hmac signature verifies with the HMAC secret even beside an ed25519 key"
        );
        Ok(())
    }

    #[test]
    fn signing_replaces_an_existing_sig_in_place_and_ignores_it_in_the_canonical_form() -> TestResult
    {
        let keys = keys(&[("SWARM_SIGNING_KEY", "@hmac")])?;
        let mut env =
            parse_envelope(r#"{"a":1,"sig":"stale","b":2}"#).map_err(|e| e.to_string())?;
        let mut plain = parse_envelope(r#"{"a":1,"b":2}"#).map_err(|e| e.to_string())?;
        sign_envelope(&mut env, &keys).map_err(|e| e.to_string())?;
        sign_envelope(&mut plain, &keys).map_err(|e| e.to_string())?;
        assert_eq!(sig_of(&env), sig_of(&plain));
        let Json::Obj(fields) = &env else {
            return Err("not an object".to_string());
        };
        assert_eq!(
            fields.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>(),
            ["a", "sig", "b"]
        );
        Ok(())
    }

    #[test]
    fn verify_is_false_for_every_malformed_signature() -> TestResult {
        let keys = keys(&[("SWARM_SIGNING_KEY", "@hmac"), ("SWARM_ED25519_KEY", "@ed")])?;
        let mut env = body()?;
        sign_envelope(&mut env, &keys).map_err(|e| e.to_string())?;
        let good = sig_of(&env).ok_or("no sig")?;
        let good_hmac = {
            let mut e = body()?;
            sign_envelope(
                &mut e,
                &super::tests::keys(&[("SWARM_SIGNING_KEY", "@hmac")])?,
            )
            .map_err(|e| e.to_string())?;
            sig_of(&e).ok_or("no sig")?
        };
        let truncated = good[..good.len() - 4].to_string();
        let bad_sigs: Vec<Json> = vec![
            Json::Null,
            Json::Int(1),
            Json::Str(String::new()),
            Json::Str("hmac:".to_string()),
            Json::Str("ed25519:".to_string()),
            Json::Str("rsa:AAAA".to_string()),
            Json::Str(good.replacen("ed25519:", "ED25519:", 1)),
            Json::Str(format!(" {good}")),
            Json::Str(format!("{good}\n")),
            Json::Str("hmac:@@@@".to_string()),
            Json::Str(truncated),
            Json::Str(good_hmac.replacen("hmac:", "ed25519:", 1)),
            Json::Str(good.replacen("ed25519:", "hmac:", 1)),
        ];
        for sig in bad_sigs {
            let mut candidate = body()?;
            if let Json::Obj(fields) = &mut candidate {
                fields.push(("sig".to_string(), sig.clone()));
            }
            assert!(!verify_envelope(&candidate, &keys), "{sig:?}");
        }
        for not_an_object in [Json::Null, Json::Arr(Vec::new()), Json::Str(good.clone())] {
            assert!(!verify_envelope(&not_an_object, &keys));
        }
        let mut without_sig = body()?;
        if let Json::Obj(fields) = &mut without_sig {
            fields.retain(|(name, _)| name != "sig");
        }
        assert!(!verify_envelope(&without_sig, &keys));
        Ok(())
    }

    #[test]
    fn a_non_canonical_base64_spelling_of_the_real_tag_still_verifies() -> TestResult {
        // 32 bytes encode to 43 data characters plus `=`; the last data character carries two unused bits Python does not check.
        let keys = keys(&[("SWARM_SIGNING_KEY", "@hmac")])?;
        let mut env = body()?;
        sign_envelope(&mut env, &keys).map_err(|e| e.to_string())?;
        let sig = sig_of(&env).ok_or("no sig")?;
        let (head, tail) = sig.split_at(sig.len() - 2);
        let last = tail.as_bytes()[0];
        let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let index = alphabet
            .iter()
            .position(|&b| b == last)
            .ok_or("not base64")?;
        let variant = alphabet[(index & !3) | ((index + 1) & 3)];
        let respelled = format!("{head}{}=", char::from(variant));
        assert_ne!(respelled, sig);
        let Json::Obj(fields) = &mut env else {
            return Err("not an object".to_string());
        };
        fields.retain(|(name, _)| name != "sig");
        fields.push(("sig".to_string(), Json::Str(respelled)));
        assert!(verify_envelope(&env, &keys));
        Ok(())
    }

    #[test]
    fn ed25519_malleated_s_is_rejected() -> TestResult {
        // S + L (the group order) is the same scalar mod L but a non-canonical encoding; RFC 8032 verifiers reject it.
        const L: [u8; 32] = [
            0xed, 0xd3, 0xf5, 0x5c, 0x1a, 0x63, 0x12, 0x58, 0xd6, 0x9c, 0xf7, 0xa2, 0xde, 0xf9,
            0xde, 0x14, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x10,
        ];
        let keys = keys(&[("SWARM_ED25519_KEY", "@ed")])?;
        let mut env = body()?;
        sign_envelope(&mut env, &keys).map_err(|e| e.to_string())?;
        assert!(
            verify_envelope(&env, &keys),
            "the unmodified signature is the control"
        );
        let sig = sig_of(&env).ok_or("no sig")?;
        let mut raw = base64::decode(sig.strip_prefix("ed25519:").ok_or("prefix")?).ok_or("b64")?;
        let mut carry = 0u16;
        for (byte, order) in raw[32..].iter_mut().zip(L) {
            let sum = u16::from(*byte) + u16::from(order) + carry;
            *byte = (sum & 0xff) as u8;
            carry = sum >> 8;
        }
        assert_eq!(carry, 0, "S + L must still fit in 32 bytes");
        let Json::Obj(fields) = &mut env else {
            return Err("not an object".to_string());
        };
        fields.retain(|(name, _)| name != "sig");
        fields.push((
            "sig".to_string(),
            Json::Str(format!("ed25519:{}", base64::encode(&raw))),
        ));
        assert!(
            !verify_envelope(&env, &keys),
            "the malleated signature must not verify"
        );
        Ok(())
    }

    #[test]
    fn signing_fails_closed_with_pythons_messages() -> TestResult {
        let cases: [(&[(&str, &str)], &str); 4] = [
            (&[], "E-POLICY: fail-closed: no signing key configured"),
            (&[("SWARM_REQUIRE_KEY", "1")], "E-POLICY: fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured"),
            (
                &[("SWARM_ED25519_KEY", "not-hex")],
                "E-POLICY: fail-closed: SWARM_ED25519_KEY is set but cannot be loaded (cryptography missing or seed is not 32-byte hex)",
            ),
            (&[("SWARM_SIGNING_KEY", "dev-insecure-key")], "E-POLICY: fail-closed: no signing key configured"),
        ];
        for (vars, message) in cases {
            let mut env = body()?;
            let error = sign_envelope(&mut env, &keys(vars)?)
                .err()
                .ok_or_else(|| format!("{vars:?} signed"))?;
            assert_eq!(error.to_string(), message, "{vars:?}");
            assert_eq!(
                sig_of(&env),
                None,
                "a refused signing must leave the envelope untouched"
            );
        }
        Ok(())
    }

    #[test]
    fn sign_declines_a_non_object_only_after_the_key_checks() -> TestResult {
        let mut not_object = Json::Arr(Vec::new());
        assert_eq!(
            sign_envelope(&mut not_object, &keys(&[("SWARM_SIGNING_KEY", "@hmac")])?),
            Err(SignError::Declined(Declined::NotAnObject))
        );
        let refused = sign_envelope(&mut not_object, &keys(&[])?)
            .err()
            .map(|e| e.to_string());
        assert_eq!(
            refused.as_deref(),
            Some("E-POLICY: fail-closed: no signing key configured")
        );
        Ok(())
    }

    #[test]
    fn only_the_implicit_dev_key_asks_for_an_audit_event() -> TestResult {
        let cases: [(&[(&str, &str)], bool); 4] = [
            (&[("SWARM_ALLOW_INSECURE_DEV_KEY", "1")], true),
            (
                &[
                    ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                    ("SWARM_SIGNING_KEY", "dev-insecure-key"),
                ],
                false,
            ),
            (&[("SWARM_SIGNING_KEY", "@hmac")], false),
            (&[("SWARM_ED25519_KEY", "@ed")], false),
        ];
        for (vars, audit) in cases {
            let mut env = body()?;
            let outcome = sign_envelope(&mut env, &keys(vars)?).map_err(|e| e.to_string())?;
            assert_eq!(outcome.dev_key_audit, audit, "{vars:?}");
        }
        Ok(())
    }

    #[test]
    fn a_dev_key_signature_verifies_only_under_the_opt_in() -> TestResult {
        let opted_in = keys(&[("SWARM_ALLOW_INSECURE_DEV_KEY", "1")])?;
        let mut env = body()?;
        sign_envelope(&mut env, &opted_in).map_err(|e| e.to_string())?;
        assert!(verify_envelope(&env, &opted_in));
        for vars in [
            &[][..],
            &[
                ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                ("SWARM_REQUIRE_KEY", "1"),
            ][..],
            &[
                ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                ("SWARM_ED25519_KEY", "@ed"),
            ][..],
        ] {
            assert!(!verify_envelope(&env, &keys(vars)?), "{vars:?}");
        }
        Ok(())
    }

    #[test]
    fn errors_and_outputs_never_carry_key_text() -> TestResult {
        let secret = "canary-hmac-secret-9d42";
        let bad_seed = "canary-not-hex-seed-9d42";
        let mut texts = Vec::new();
        for vars in [
            vec![("SWARM_SIGNING_KEY", secret)],
            vec![("SWARM_ED25519_KEY", bad_seed)],
            vec![
                ("SWARM_SIGNING_KEY", "dev-insecure-key"),
                ("SWARM_REQUIRE_KEY", "1"),
            ],
        ] {
            let material = keys(&vars)?;
            texts.push(format!("{material:?}"));
            let mut env = body()?;
            match sign_envelope(&mut env, &material) {
                Ok(outcome) => texts.push(format!(
                    "{outcome:?} {}",
                    String::from_utf8_lossy(&crate::compact_json(&env))
                )),
                Err(error) => texts.push(format!("{error} {error:?}")),
            }
        }
        for text in texts {
            assert!(!text.contains(secret) && !text.contains(bad_seed), "{text}");
        }
        Ok(())
    }
}
