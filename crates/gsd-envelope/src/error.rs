//! Error types: what this crate declines to answer, and the fail-closed signing errors.

use std::fmt;

use gsd_route::SeamError;

/// A request this crate will not answer, because it cannot prove its answer byte-identical to Python's. The caller falls back to
/// the Python implementation. No variant carries any value taken from the environment or from the envelope.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Declined {
    /// `SWARM_ED25519_KEY_FILE` is set and would be consulted. Key files are opened only by Python (`O_NOFOLLOW`, owner and mode checks).
    KeyFileReference,
    /// No raw key variable is set and the conventional `signing-key.hex` file exists, so Python would load it.
    ConventionalKeyFile,
    /// A signing-related environment variable holds bytes that are not valid UTF-8; Python's handling of those is not reproduced.
    NonUnicodeEnvironment,
    /// The envelope is not a JSON object; Python's `_canonical` raises on it.
    NotAnObject,
    /// The envelope text is invalid JSON, or valid JSON this crate cannot represent exactly: an integer outside `i64`, a `NaN` or
    /// `Infinity` literal, or nesting deeper than the parser allows. Python's `json.loads` may accept it.
    InvalidJson,
    /// The envelope text holds a `\u` escape of a surrogate that is not half of a valid pair. Python keeps it in the string and
    /// then fails to encode the canonical form; this crate cannot represent it.
    LoneSurrogate,
    /// The HMAC implementation rejected the key length. HMAC accepts every length, so this is unreachable; it exists so that no
    /// path panics.
    HmacKeyRejected,
}

impl fmt::Display for Declined {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let text = match self {
            Declined::KeyFileReference => {
                "a signing key file reference is set; only the Python engine opens key files"
            }
            Declined::ConventionalKeyFile => {
                "the conventional signing key file exists; only the Python engine opens key files"
            }
            Declined::NonUnicodeEnvironment => "a signing environment variable is not valid UTF-8",
            Declined::NotAnObject => "the envelope is not a JSON object",
            Declined::InvalidJson => {
                "the envelope text is not JSON this crate can represent exactly"
            }
            Declined::LoneSurrogate => "the envelope text holds an unpaired surrogate escape",
            Declined::HmacKeyRejected => "the HMAC implementation rejected the key length",
        };
        f.write_str(text)
    }
}

impl std::error::Error for Declined {}

/// Why [`crate::sign_envelope`] did not sign.
#[derive(Debug, Clone, PartialEq)]
pub enum SignError {
    /// Python raises `SwarmError(E_POLICY, ...)`: fail-closed key misconfiguration or no key. The message equals Python's `str(error)`.
    Policy(SeamError),
    /// The request is outside what this crate answers.
    Declined(Declined),
}

impl fmt::Display for SignError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SignError::Policy(error) => f.write_str(error.message()),
            SignError::Declined(declined) => declined.fmt(f),
        }
    }
}

impl std::error::Error for SignError {}

impl From<Declined> for SignError {
    fn from(declined: Declined) -> Self {
        SignError::Declined(declined)
    }
}
