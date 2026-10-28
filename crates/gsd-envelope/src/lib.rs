//! Host-side swarm envelope handling for the Rust engine: the canonical signing form, `verify_envelope`, `sign_envelope` and the
//! signing-key sources of `swarm/envelope.py`.
//!
//! This is the only GSD-Core crate that reads key material. It is a host crate by design: `gsd-route` and `gsd-guest` must never
//! depend on it, so a key can never reach the sandbox guest (decision D-P24, key custody). A route that needs a signature fact
//! verifies here and hands the guest a plain boolean.
//!
//! The Python module is the oracle. Every function answers only what it can prove byte-identical to Python, and refuses the rest
//! with a [`Declined`] so the caller lets Python answer:
//!
//! * a key *file* reference (`SWARM_ED25519_KEY_FILE`, or the conventional `signing-key.hex` when it exists) is never opened here;
//! * an envelope text Python's `json.loads` accepts but this crate's JSON layer cannot represent exactly (an integer outside `i64`,
//!   the `NaN` and `Infinity` literals, a lone surrogate escape) is declined by [`parse_envelope`];
//! * an environment value that is not valid Unicode is declined.
//!
//! Differences that are deliberate and documented: the Python `cryptography` package is optional and Python fails closed without
//! it, while Ed25519 here is always available; and a [`KeyMaterial`] is a snapshot of the environment, where Python re-reads
//! `os.environ` on every call.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

mod base64;
mod canonical;
mod envelope;
mod error;
mod keys;

pub use canonical::{canonical_bytes, compact_json, parse_envelope};
pub use envelope::{sign_envelope, verify_envelope, SignOutcome};
pub use error::{Declined, SignError};
pub use keys::KeyMaterial;
