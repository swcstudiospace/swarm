//! Module identity for the sandboxed GSD-Core guest artifact.
//!
//! Checks the SHA-256 of the module bytes and its semver against a manifest.
//! A mismatch is a dependency failure (`E-DEP`, fault S-06 in `plan/wasmer-spike.md`).

use gsd_failure::ErrorCode;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fmt::Write as _;

/// Module name the manifest must carry.
pub const MODULE_NAME: &str = "gsd-core";
/// Component target the manifest must carry.
pub const MODULE_TARGET: &str = "wasm32-wasip2";

/// Manifest describing one guest module artifact.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModuleManifest {
    /// Module name; must equal [`MODULE_NAME`].
    pub name: String,
    /// Dotted semver `MAJOR.MINOR.PATCH`.
    pub semver: String,
    /// Build target; must equal [`MODULE_TARGET`].
    pub target: String,
    /// Lowercase hex SHA-256 of the module bytes.
    pub sha256: String,
}

/// Identity of a module whose bytes matched its manifest.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModuleIdentity {
    /// Verified semver.
    pub semver: String,
    /// Verified lowercase hex SHA-256.
    pub sha256: String,
    /// Length of the verified module in bytes.
    pub byte_len: usize,
}

/// Refusals raised while verifying or parsing a guest module identity.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum SandboxError {
    /// The module byte slice is empty.
    #[error("guest module is empty")]
    EmptyModule,
    /// The module bytes do not hash to the digest in the manifest.
    #[error("guest module bytes do not match the manifest SHA-256 digest")]
    DigestMismatch,
    /// The manifest semver differs from the semver the host expects.
    #[error("guest module semver mismatch: expected {expected}, manifest has {found}")]
    VersionMismatch {
        /// Semver the host expects.
        expected: String,
        /// Semver recorded in the manifest.
        found: String,
    },
    /// The manifest is malformed; the payload is a short reason.
    #[error("guest module manifest is invalid: {0}")]
    InvalidManifest(String),
}

impl SandboxError {
    /// Maps every refusal to the shared `E-DEP` code: an unverifiable artifact is a dependency failure.
    pub fn error_code(&self) -> ErrorCode {
        match self {
            Self::EmptyModule
            | Self::DigestMismatch
            | Self::VersionMismatch { .. }
            | Self::InvalidManifest(_) => ErrorCode::Dep,
        }
    }
}

/// Builds a manifest for `bytes` with the given name and semver (build tooling).
pub fn describe(bytes: &[u8], name: &str, semver: &str) -> ModuleManifest {
    ModuleManifest {
        name: name.to_owned(),
        semver: semver.to_owned(),
        target: MODULE_TARGET.to_owned(),
        sha256: hex_lower(&Sha256::digest(bytes)),
    }
}

/// Parses and validates a manifest from JSON bytes.
pub fn parse_manifest(json: &[u8]) -> Result<ModuleManifest, SandboxError> {
    let text = std::str::from_utf8(json)
        .map_err(|_| SandboxError::InvalidManifest("manifest is not valid UTF-8".to_owned()))?;
    let manifest: ModuleManifest = serde_json::from_str(text).map_err(|_| {
        SandboxError::InvalidManifest("manifest is not valid JSON for the schema".to_owned())
    })?;

    if manifest.name != MODULE_NAME {
        return Err(SandboxError::InvalidManifest(format!(
            "name must be {MODULE_NAME}"
        )));
    }
    if manifest.target != MODULE_TARGET {
        return Err(SandboxError::InvalidManifest(format!(
            "target must be {MODULE_TARGET}"
        )));
    }
    if !is_semver(&manifest.semver) {
        return Err(SandboxError::InvalidManifest(
            "semver must be MAJOR.MINOR.PATCH with decimal parts".to_owned(),
        ));
    }
    if !is_lower_hex_digest(&manifest.sha256) {
        return Err(SandboxError::InvalidManifest(
            "sha256 must be 64 lowercase hex characters".to_owned(),
        ));
    }
    Ok(manifest)
}

/// Verifies module bytes against a manifest and the host's expected semver.
///
/// Checks run in order: empty bytes, semver, then digest.
pub fn verify_module(
    bytes: &[u8],
    manifest: &ModuleManifest,
    expected_semver: &str,
) -> Result<ModuleIdentity, SandboxError> {
    if bytes.is_empty() {
        return Err(SandboxError::EmptyModule);
    }
    if manifest.semver != expected_semver {
        return Err(SandboxError::VersionMismatch {
            expected: expected_semver.to_owned(),
            found: manifest.semver.clone(),
        });
    }
    let digest = hex_lower(&Sha256::digest(bytes));
    if digest != manifest.sha256 {
        return Err(SandboxError::DigestMismatch);
    }
    Ok(ModuleIdentity {
        semver: manifest.semver.clone(),
        sha256: digest,
        byte_len: bytes.len(),
    })
}

/// Encodes bytes as lowercase hex.
fn hex_lower(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        // Writing into a String cannot fail.
        let _ = write!(out, "{byte:02x}");
    }
    out
}

/// True when `value` is three dot-separated decimal parts without leading zeros.
fn is_semver(value: &str) -> bool {
    let parts: Vec<&str> = value.split('.').collect();
    parts.len() == 3 && parts.iter().all(|part| is_plain_decimal(part))
}

/// True when `part` is non-empty ASCII digits with no leading zero unless it is `0`.
fn is_plain_decimal(part: &str) -> bool {
    !part.is_empty()
        && part.bytes().all(|b| b.is_ascii_digit())
        && (part == "0" || !part.starts_with('0'))
}

/// True when `value` is exactly 64 lowercase hex characters.
fn is_lower_hex_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

#[cfg(test)]
mod tests {
    use super::*;

    const WASM: &[u8] = b"\0asm-guest-module-bytes";

    #[test]
    fn verify_accepts_matching_digest_and_semver() {
        let manifest = describe(WASM, MODULE_NAME, "0.1.0");
        let identity = verify_module(WASM, &manifest, "0.1.0").expect("matching module verifies");
        assert_eq!(identity.byte_len, WASM.len());
        assert_eq!(identity.semver, "0.1.0");
        assert_eq!(identity.sha256, manifest.sha256);
    }

    #[test]
    fn verify_refuses_one_flipped_byte() {
        let manifest = describe(WASM, MODULE_NAME, "0.1.0");
        let mut tampered = WASM.to_vec();
        tampered[3] ^= 0x01;
        assert_eq!(
            verify_module(&tampered, &manifest, "0.1.0"),
            Err(SandboxError::DigestMismatch)
        );
    }

    #[test]
    fn verify_refuses_a_wrong_semver() {
        let manifest = describe(WASM, MODULE_NAME, "0.1.0");
        assert_eq!(
            verify_module(WASM, &manifest, "0.2.0"),
            Err(SandboxError::VersionMismatch {
                expected: "0.2.0".to_owned(),
                found: "0.1.0".to_owned(),
            })
        );
    }

    #[test]
    fn verify_refuses_an_empty_module() {
        let manifest = describe(WASM, MODULE_NAME, "0.1.0");
        assert_eq!(
            verify_module(&[], &manifest, "0.1.0"),
            Err(SandboxError::EmptyModule)
        );
    }

    #[test]
    fn parse_manifest_rejects_unknown_fields() {
        let json = br#"{"name":"gsd-core","semver":"0.1.0","target":"wasm32-wasip2",
            "sha256":"0000000000000000000000000000000000000000000000000000000000000000",
            "extra":true}"#;
        assert!(matches!(
            parse_manifest(json),
            Err(SandboxError::InvalidManifest(_))
        ));
    }

    #[test]
    fn parse_manifest_rejects_a_malformed_digest() {
        let short = format!(
            r#"{{"name":"gsd-core","semver":"0.1.0","target":"wasm32-wasip2","sha256":"{}"}}"#,
            "a".repeat(63)
        );
        let upper = format!(
            r#"{{"name":"gsd-core","semver":"0.1.0","target":"wasm32-wasip2","sha256":"{}"}}"#,
            "A".repeat(64)
        );
        assert!(matches!(
            parse_manifest(short.as_bytes()),
            Err(SandboxError::InvalidManifest(_))
        ));
        assert!(matches!(
            parse_manifest(upper.as_bytes()),
            Err(SandboxError::InvalidManifest(_))
        ));
    }

    #[test]
    fn parse_manifest_rejects_a_foreign_name_or_target() {
        let digest = "a".repeat(64);
        let foreign_name = format!(
            r#"{{"name":"other","semver":"0.1.0","target":"wasm32-wasip2","sha256":"{digest}"}}"#
        );
        let foreign_target = format!(
            r#"{{"name":"gsd-core","semver":"0.1.0","target":"wasm32-wasip1","sha256":"{digest}"}}"#
        );
        assert!(matches!(
            parse_manifest(foreign_name.as_bytes()),
            Err(SandboxError::InvalidManifest(_))
        ));
        assert!(matches!(
            parse_manifest(foreign_target.as_bytes()),
            Err(SandboxError::InvalidManifest(_))
        ));
    }

    #[test]
    fn parse_manifest_round_trips_a_described_manifest() {
        let manifest = describe(WASM, MODULE_NAME, "1.2.3");
        let json = serde_json::to_vec(&manifest).expect("manifest serializes");
        let parsed = parse_manifest(&json).expect("described manifest parses");
        assert_eq!(parsed, manifest);
    }

    #[test]
    fn every_refusal_maps_to_the_dep_code() {
        let variants = [
            SandboxError::EmptyModule,
            SandboxError::DigestMismatch,
            SandboxError::VersionMismatch {
                expected: "0.2.0".to_owned(),
                found: "0.1.0".to_owned(),
            },
            SandboxError::InvalidManifest("reason".to_owned()),
        ];
        for variant in &variants {
            assert_eq!(variant.error_code(), ErrorCode::Dep);
        }
    }

    #[test]
    fn error_messages_hold_no_module_bytes() {
        let secret = b"\0asm SECRET-MARKER payload";
        let manifest = describe(secret, MODULE_NAME, "0.1.0");
        let err = verify_module(secret, &manifest, "0.2.0").expect_err("wrong semver refuses");
        assert!(!err.to_string().contains("SECRET-MARKER"));
    }
}
