//! Reads one envelope body (a JSON object) from stdin, signs it with the keys of the process environment and prints the signed
//! envelope as one line of compact, key-sorted JSON.
//!
//! The Python test `tests/test_envelope_rust_signed.py` runs this binary to prove that Rust-signed envelopes verify with
//! `swarm.envelope.verify_envelope`. The keys come from the same variables Python reads (`SWARM_SIGNING_KEY`,
//! `SWARM_ED25519_KEY`, `SWARM_ALLOW_INSECURE_DEV_KEY`, `SWARM_REQUIRE_KEY`); nothing key-like is ever printed.
//!
//! Exit codes: 0 signed (stdout holds the envelope); 1 the signing refused fail-closed (stderr holds Python's `E-POLICY` text);
//! 3 the request is one this crate declines and Python must answer (stderr names the reason); 2 stdin could not be read.

use std::io::{Read, Write};
use std::process::ExitCode;

use gsd_envelope::{compact_json, parse_envelope, sign_envelope, KeyMaterial, SignError};

fn main() -> ExitCode {
    let mut text = String::new();
    if std::io::stdin().read_to_string(&mut text).is_err() {
        eprintln!("sign_stdin: stdin is not UTF-8 text");
        return ExitCode::from(2);
    }
    let keys = match KeyMaterial::from_env() {
        Ok(keys) => keys,
        Err(declined) => {
            eprintln!("sign_stdin: declined: {declined}");
            return ExitCode::from(3);
        }
    };
    let mut envelope = match parse_envelope(&text) {
        Ok(envelope) => envelope,
        Err(declined) => {
            eprintln!("sign_stdin: declined: {declined}");
            return ExitCode::from(3);
        }
    };
    match sign_envelope(&mut envelope, &keys) {
        Ok(_) => {
            let mut line = compact_json(envelope.as_json());
            line.push(b'\n');
            if std::io::stdout().write_all(&line).is_err() {
                return ExitCode::from(2);
            }
            ExitCode::SUCCESS
        }
        Err(SignError::Policy(error)) => {
            eprintln!("{}", error.message());
            ExitCode::from(1)
        }
        Err(SignError::Declined(declined)) => {
            eprintln!("sign_stdin: declined: {declined}");
            ExitCode::from(3)
        }
    }
}
