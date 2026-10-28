//! Signing-key sources with the precedence of `swarm/envelope.py`, resolved once from an environment snapshot.
//!
//! Python decides on every call from `os.environ`; [`KeyMaterial`] evaluates the same decisions up front (`_ed25519_material`,
//! `insecure_dev_key`, `_signing_hmac`, `signing_config_error`) and keeps the outcome. Secret bytes live in [`Zeroizing`]
//! buffers that are wiped when dropped. That is a partial measure and is meant as one: the process environment still holds the
//! original value for as long as the process runs, and the Ed25519 key is wiped by `ed25519-dalek`'s own drop.
//!
//! No `Debug`, error text or log line here ever holds a key or a key-derived value.

use std::ffi::OsString;
use std::fmt;
use std::path::{Path, PathBuf};

use ed25519_dalek::SigningKey;
use zeroize::Zeroizing;

use crate::Declined;

/// The public development HMAC key, the literal of `insecure_dev_key()` in `swarm/envelope.py`. It signs only when
/// `SWARM_ALLOW_INSECURE_DEV_KEY=1`, no Ed25519 signer is configured and `SWARM_REQUIRE_KEY` is not `1`.
const DEV_KEY: &str = "dev-insecure-key";

const ED25519_UNLOADABLE: &str =
    "fail-closed: SWARM_ED25519_KEY is set but cannot be loaded (cryptography missing or seed is not 32-byte hex)";
const AGENT_SESSION_REFERENCE: &str =
    "fail-closed: private signing references are unavailable in agent sessions";
const REQUIRE_WITHOUT_KEY: &str = "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured";

/// The message of the refusal to sign when no key may sign (Python `sign_envelope`, `E-POLICY`).
pub(crate) const NO_SIGNING_KEY: &str = "fail-closed: no signing key configured";

/// The HMAC secret that signs and verifies `hmac:` signatures.
pub(crate) struct HmacKey {
    /// Secret bytes (`SWARM_SIGNING_KEY` as UTF-8, or the public dev key).
    pub(crate) secret: Zeroizing<Vec<u8>>,
    /// True for the unset-or-empty `SWARM_SIGNING_KEY` dev-key opt-in, which Python audits as `security.dev_key`.
    pub(crate) implicit_dev: bool,
}

/// The signing keys the process environment configures, with the outcome of Python's fail-closed checks.
pub struct KeyMaterial {
    pub(crate) ed25519: Option<SigningKey>,
    pub(crate) hmac: Option<HmacKey>,
    pub(crate) config_error: Option<&'static str>,
}

/// The signing-related environment, read once. Empty strings count as unset except for the key file reference.
struct Environment {
    ed25519_key: Option<Zeroizing<String>>,
    signing_key: Option<Zeroizing<String>>,
    key_file: Option<String>,
    agent_session: bool,
    allow_dev_key: bool,
    require_key: bool,
    config_home: Option<String>,
    home: Option<String>,
}

impl Environment {
    fn read(get: &impl Fn(&str) -> Option<OsString>) -> Result<Self, Declined> {
        let text = |name: &str| -> Result<Option<String>, Declined> {
            get(name)
                .map(|value| {
                    value
                        .into_string()
                        .map_err(|_| Declined::NonUnicodeEnvironment)
                })
                .transpose()
        };
        let non_empty = |value: Option<String>| value.filter(|v| !v.is_empty());
        let secret = |name: &str| -> Result<Option<Zeroizing<String>>, Declined> {
            Ok(non_empty(text(name)?).map(Zeroizing::new))
        };
        Ok(Self {
            ed25519_key: secret("SWARM_ED25519_KEY")?,
            signing_key: secret("SWARM_SIGNING_KEY")?,
            key_file: text("SWARM_ED25519_KEY_FILE")?,
            agent_session: text("SWARM_AGENT_SESSION")?.as_deref() == Some("1"),
            allow_dev_key: text("SWARM_ALLOW_INSECURE_DEV_KEY")?.as_deref() == Some("1"),
            require_key: text("SWARM_REQUIRE_KEY")?.as_deref() == Some("1"),
            config_home: non_empty(text("XDG_CONFIG_HOME")?),
            home: non_empty(text("HOME")?),
        })
    }

    /// `${XDG_CONFIG_HOME:-$HOME/.config}/agent-swarm/signing-key.hex` (Python `_conventional_reference`).
    fn conventional_reference(&self) -> Option<PathBuf> {
        let base = match (&self.config_home, &self.home) {
            (Some(config), _) => PathBuf::from(config),
            (None, Some(home)) => Path::new(home).join(".config"),
            (None, None) => return None,
        };
        Some(base.join("agent-swarm").join("signing-key.hex"))
    }
}

/// `bytes.fromhex(raw)` followed by the 32-byte length check of `Ed25519PrivateKey.from_private_bytes`. Python skips ASCII
/// whitespace between byte pairs and accepts either case; a space inside a pair, any other character or another length fails.
fn seed_from_hex(raw: &str) -> Option<Zeroizing<[u8; 32]>> {
    let bytes = raw.as_bytes();
    let mut seed = Zeroizing::new([0u8; 32]);
    let mut filled = 0;
    let mut at = 0;
    while at < bytes.len() {
        if matches!(bytes[at], b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c) {
            at += 1;
            continue;
        }
        let high = char::from(bytes[at]).to_digit(16)?;
        let low = char::from(*bytes.get(at + 1)?).to_digit(16)?;
        *seed.get_mut(filled)? = u8::try_from(high * 16 + low).ok()?;
        filled += 1;
        at += 2;
    }
    (filled == 32).then_some(seed)
}

impl KeyMaterial {
    /// Resolves the keys from the process environment and the real file system (existence of the conventional key file only).
    ///
    /// # Errors
    ///
    /// [`Declined`] when a key file reference would be consulted or an environment value is not UTF-8; Python answers then.
    pub fn from_env() -> Result<Self, Declined> {
        Self::from_lookup(
            |name| std::env::var_os(name),
            |path| path.symlink_metadata().is_ok(),
        )
    }

    /// Resolves the keys from an arbitrary environment. `get` looks a variable up; `conventional_exists` says whether a path
    /// exists (Python `os.path.lexists`). The file is never opened.
    ///
    /// The variables read are `SWARM_ED25519_KEY`, `SWARM_SIGNING_KEY`, `SWARM_ED25519_KEY_FILE`, `SWARM_AGENT_SESSION`,
    /// `SWARM_ALLOW_INSECURE_DEV_KEY`, `SWARM_REQUIRE_KEY`, `XDG_CONFIG_HOME` and `HOME`.
    ///
    /// # Errors
    ///
    /// [`Declined::NonUnicodeEnvironment`] for a non-UTF-8 value; [`Declined::KeyFileReference`] when `SWARM_ED25519_KEY_FILE` is
    /// set and Python would reach it (no raw key variable set, not an agent session); [`Declined::ConventionalKeyFile`] when the
    /// same holds for an existing conventional file.
    pub fn from_lookup(
        get: impl Fn(&str) -> Option<OsString>,
        conventional_exists: impl Fn(&Path) -> bool,
    ) -> Result<Self, Declined> {
        let env = Environment::read(&get)?;
        let conventional_present = env
            .conventional_reference()
            .is_some_and(|path| conventional_exists(&path));

        // `_ed25519_material`: (signer, error). The raw seed wins, then a raw HMAC key hides every file reference.
        let (ed25519, ed25519_error) = match (&env.ed25519_key, &env.signing_key) {
            (Some(raw), _) => match seed_from_hex(raw) {
                Some(seed) => (Some(SigningKey::from_bytes(&seed)), None),
                None => (None, Some(ED25519_UNLOADABLE)),
            },
            (None, Some(_)) => (None, None),
            (None, None) if env.agent_session => {
                (None, env.key_file.as_ref().map(|_| AGENT_SESSION_REFERENCE))
            }
            (None, None) if env.key_file.is_some() => return Err(Declined::KeyFileReference),
            (None, None) if conventional_present => return Err(Declined::ConventionalKeyFile),
            (None, None) => (None, None),
        };

        // `insecure_dev_key` then `_signing_hmac`: a configured secret that is not the public dev value always wins.
        let dev_key_allowed =
            env.allow_dev_key && ed25519.is_none() && ed25519_error.is_none() && !env.require_key;
        let hmac = match &env.signing_key {
            Some(raw) if raw.as_str() != DEV_KEY => Some(HmacKey {
                secret: Zeroizing::new(raw.as_bytes().to_vec()),
                implicit_dev: false,
            }),
            configured if dev_key_allowed => Some(HmacKey {
                secret: Zeroizing::new(DEV_KEY.as_bytes().to_vec()),
                implicit_dev: configured.is_none(),
            }),
            _ => None,
        };

        // `signing_config_error`.
        let key_file_named = env.key_file.as_deref().is_some_and(|file| !file.is_empty());
        let supplied = env.signing_key.is_some()
            || env.ed25519_key.is_some()
            || key_file_named
            || env.require_key
            || conventional_present;
        let real_key =
            ed25519.is_some() || env.signing_key.as_deref().is_some_and(|key| key != DEV_KEY);
        let both_raw_set = env.signing_key.is_some() && env.ed25519_key.is_some();
        let config_error = match ed25519_error {
            _ if !supplied => None,
            Some(error) if !both_raw_set => Some(error),
            _ if env.require_key && !real_key => Some(REQUIRE_WITHOUT_KEY),
            _ => None,
        };

        Ok(Self {
            ed25519,
            hmac,
            config_error,
        })
    }

    /// Why a signature made now could never verify (Python `signing_config_error`), or `None`. A message is a fixed string.
    pub fn config_error(&self) -> Option<&'static str> {
        self.config_error
    }

    /// The Ed25519 public key (never the seed) of the configured signer, if one loaded.
    pub fn ed25519_public_key(&self) -> Option<[u8; 32]> {
        self.ed25519
            .as_ref()
            .map(|key| key.verifying_key().to_bytes())
    }
}

impl fmt::Debug for KeyMaterial {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let presence = |present: bool| if present { "present" } else { "absent" };
        f.debug_struct("KeyMaterial")
            .field("ed25519", &presence(self.ed25519.is_some()))
            .field("hmac", &presence(self.hmac.is_some()))
            .field(
                "implicit_dev_key",
                &self.hmac.as_ref().is_some_and(|key| key.implicit_dev),
            )
            .field("config_error", &self.config_error)
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use sha2::{Digest, Sha256};

    use super::*;

    type TestResult = Result<(), String>;
    type Vars<'a> = &'a [(&'a str, &'a str)];

    /// Stands for the derived test seed in a variable table; `resolve` substitutes the real value.
    const SEED_HEX: &str = "<seed>";

    /// A 32-byte test seed derived at run time from a documented phrase, so no key-shaped literal lives in the source.
    fn seed_bytes() -> [u8; 32] {
        Sha256::digest(b"gsd-envelope keys.rs unit tests: derived seed phrase, not a secret").into()
    }

    fn seed_hex() -> String {
        hex::encode(seed_bytes())
    }

    fn resolve(vars: Vars, exists: bool) -> Result<KeyMaterial, Declined> {
        let map: HashMap<String, OsString> = vars
            .iter()
            .map(|(k, v)| {
                (
                    (*k).to_string(),
                    OsString::from(if *v == SEED_HEX {
                        seed_hex()
                    } else {
                        (*v).to_string()
                    }),
                )
            })
            .collect();
        KeyMaterial::from_lookup(|name| map.get(name).cloned(), |_| exists)
    }

    #[test]
    fn seed_parsing_follows_bytes_fromhex() {
        let seed = seed_hex();
        let spaced = seed
            .as_bytes()
            .chunks(2)
            .map(|pair| String::from_utf8_lossy(pair).into_owned())
            .collect::<Vec<_>>()
            .join(" ");
        let cases: Vec<(String, bool)> = vec![
            (seed.clone(), true),
            (seed.to_uppercase(), true),
            (spaced, true),
            (format!("{seed}\n"), true),
            (format!(" \t{seed}\r\x0b\x0c"), true),
            (seed[..62].to_string(), false),
            (format!("{seed}00"), false),
            (format!("{} {}", &seed[..3], &seed[3..]), false),
            (format!("{}zz", &seed[..62]), false),
            (format!("{}\u{e9}", &seed[..62]), false),
            (format!("0x{seed}"), false),
        ];
        for (raw, loads) in cases {
            assert_eq!(seed_from_hex(&raw).is_some(), loads, "input {raw:?}");
        }
    }

    #[test]
    fn seed_bytes_are_the_decoded_hex() {
        let decoded = seed_from_hex(&seed_hex());
        assert_eq!(decoded.as_deref().copied(), Some(seed_bytes()));
    }

    /// Table of (variables, signer present, HMAC secret present, implicit dev flag, config error), each row derived by reading
    /// `swarm/envelope.py` and cross-checked against the Python oracle by the committed vectors.
    #[test]
    fn resolution_follows_python_precedence() -> TestResult {
        type Row = (
            &'static [(&'static str, &'static str)],
            bool,
            bool,
            bool,
            Option<&'static str>,
        );
        let rows: [Row; 18] = [
            (&[], false, false, false, None),
            (&[("SWARM_SIGNING_KEY", "k")], false, true, false, None),
            (&[("SWARM_SIGNING_KEY", "")], false, false, false, None),
            (&[("SWARM_ED25519_KEY", SEED_HEX)], true, false, false, None),
            (
                &[("SWARM_ED25519_KEY", SEED_HEX), ("SWARM_SIGNING_KEY", "k")],
                true,
                true,
                false,
                None,
            ),
            (
                &[("SWARM_ED25519_KEY", "zz")],
                false,
                false,
                false,
                Some(ED25519_UNLOADABLE),
            ),
            (
                &[("SWARM_ED25519_KEY", "zz"), ("SWARM_SIGNING_KEY", "k")],
                false,
                true,
                false,
                None,
            ),
            (
                &[("SWARM_REQUIRE_KEY", "1")],
                false,
                false,
                false,
                Some(REQUIRE_WITHOUT_KEY),
            ),
            (
                &[("SWARM_REQUIRE_KEY", "1"), ("SWARM_SIGNING_KEY", "k")],
                false,
                true,
                false,
                None,
            ),
            (&[("SWARM_REQUIRE_KEY", "yes")], false, false, false, None),
            (
                &[("SWARM_ALLOW_INSECURE_DEV_KEY", "1")],
                false,
                true,
                true,
                None,
            ),
            (
                &[
                    ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                    ("SWARM_SIGNING_KEY", ""),
                ],
                false,
                true,
                true,
                None,
            ),
            (
                &[
                    ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                    ("SWARM_SIGNING_KEY", DEV_KEY),
                ],
                false,
                true,
                false,
                None,
            ),
            (
                &[("SWARM_ALLOW_INSECURE_DEV_KEY", "true")],
                false,
                false,
                false,
                None,
            ),
            (
                &[
                    ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                    ("SWARM_ED25519_KEY", SEED_HEX),
                ],
                true,
                false,
                false,
                None,
            ),
            (
                &[
                    ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                    ("SWARM_REQUIRE_KEY", "1"),
                ],
                false,
                false,
                false,
                Some(REQUIRE_WITHOUT_KEY),
            ),
            (
                &[
                    ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                    ("SWARM_ED25519_KEY", "zz"),
                ],
                false,
                false,
                false,
                Some(ED25519_UNLOADABLE),
            ),
            (&[("SWARM_SIGNING_KEY", DEV_KEY)], false, false, false, None),
        ];
        for (vars, signer, hmac, implicit, error) in rows {
            let keys = resolve(vars, false).map_err(|e| format!("{vars:?}: {e}"))?;
            assert_eq!(keys.ed25519.is_some(), signer, "signer for {vars:?}");
            assert_eq!(keys.hmac.is_some(), hmac, "hmac for {vars:?}");
            assert_eq!(
                keys.hmac.as_ref().is_some_and(|key| key.implicit_dev),
                implicit,
                "implicit dev for {vars:?}"
            );
            assert_eq!(keys.config_error(), error, "config error for {vars:?}");
        }
        Ok(())
    }

    #[test]
    fn dev_secret_is_the_public_literal_and_a_configured_secret_is_used_verbatim() -> TestResult {
        let dev =
            resolve(&[("SWARM_ALLOW_INSECURE_DEV_KEY", "1")], false).map_err(|e| e.to_string())?;
        assert_eq!(
            dev.hmac.as_ref().map(|key| key.secret.as_slice()),
            Some(DEV_KEY.as_bytes())
        );
        let custom =
            resolve(&[("SWARM_SIGNING_KEY", "k\u{e9}y")], false).map_err(|e| e.to_string())?;
        assert_eq!(
            custom.hmac.as_ref().map(|key| key.secret.as_slice()),
            Some("k\u{e9}y".as_bytes())
        );
        Ok(())
    }

    #[test]
    fn agent_sessions_never_reach_a_key_file() -> TestResult {
        let cases: [(Vars, Option<&str>); 4] = [
            (
                &[
                    ("SWARM_AGENT_SESSION", "1"),
                    ("SWARM_ED25519_KEY_FILE", "/k"),
                ],
                Some(AGENT_SESSION_REFERENCE),
            ),
            (
                &[("SWARM_AGENT_SESSION", "1"), ("SWARM_ED25519_KEY_FILE", "")],
                None,
            ),
            (
                &[
                    ("SWARM_AGENT_SESSION", "1"),
                    ("SWARM_ED25519_KEY_FILE", ""),
                    ("SWARM_REQUIRE_KEY", "1"),
                ],
                Some(AGENT_SESSION_REFERENCE),
            ),
            (&[("SWARM_AGENT_SESSION", "1")], None),
        ];
        for (vars, error) in cases {
            let keys = resolve(vars, false).map_err(|e| format!("{vars:?}: {e}"))?;
            assert_eq!(keys.config_error(), error, "{vars:?}");
        }
        // The conventional file only makes a key "supplied" there: it surfaces the agent-session error for an empty reference.
        let keys = resolve(
            &[
                ("SWARM_AGENT_SESSION", "1"),
                ("SWARM_ED25519_KEY_FILE", ""),
                ("HOME", "/h"),
            ],
            true,
        )
        .map_err(|e| e.to_string())?;
        assert_eq!(keys.config_error(), Some(AGENT_SESSION_REFERENCE));
        Ok(())
    }

    #[test]
    fn key_file_references_are_declined_when_reached() {
        let declined: [(Vars, bool, Declined); 4] = [
            (
                &[("SWARM_ED25519_KEY_FILE", "/k")],
                false,
                Declined::KeyFileReference,
            ),
            (
                &[("SWARM_ED25519_KEY_FILE", "")],
                false,
                Declined::KeyFileReference,
            ),
            (
                &[
                    ("SWARM_ED25519_KEY_FILE", "/k"),
                    ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                ],
                false,
                Declined::KeyFileReference,
            ),
            (&[("HOME", "/home/u")], true, Declined::ConventionalKeyFile),
        ];
        for (vars, exists, expected) in declined {
            assert_eq!(resolve(vars, exists).err(), Some(expected), "{vars:?}");
        }
    }

    #[test]
    fn key_file_references_are_ignored_when_a_raw_key_hides_them() -> TestResult {
        for vars in [
            &[("SWARM_ED25519_KEY_FILE", "/k"), ("SWARM_SIGNING_KEY", "k")][..],
            &[
                ("SWARM_ED25519_KEY_FILE", "/k"),
                ("SWARM_ED25519_KEY", SEED_HEX),
            ][..],
            &[
                ("SWARM_ED25519_KEY_FILE", "/k"),
                ("SWARM_ED25519_KEY", "zz"),
            ][..],
        ] {
            resolve(vars, true).map_err(|e| format!("{vars:?}: {e}"))?;
        }
        Ok(())
    }

    #[test]
    fn conventional_file_path_follows_xdg_then_home() -> TestResult {
        let seen = std::cell::RefCell::new(Vec::<PathBuf>::new());
        let cases: [(Vars, Option<&str>); 4] = [
            (
                &[("XDG_CONFIG_HOME", "/x"), ("HOME", "/h")],
                Some("/x/agent-swarm/signing-key.hex"),
            ),
            (
                &[("HOME", "/h")],
                Some("/h/.config/agent-swarm/signing-key.hex"),
            ),
            (
                &[("XDG_CONFIG_HOME", ""), ("HOME", "/h")],
                Some("/h/.config/agent-swarm/signing-key.hex"),
            ),
            (&[], None),
        ];
        for (vars, expected) in cases {
            seen.borrow_mut().clear();
            let map: HashMap<String, OsString> = vars
                .iter()
                .map(|(k, v)| ((*k).to_string(), OsString::from(*v)))
                .collect();
            KeyMaterial::from_lookup(
                |name| map.get(name).cloned(),
                |path| {
                    seen.borrow_mut().push(path.to_path_buf());
                    false
                },
            )
            .map_err(|e| e.to_string())?;
            let first = seen.borrow().first().cloned();
            assert_eq!(first, expected.map(PathBuf::from), "{vars:?}");
        }
        Ok(())
    }

    #[test]
    fn a_non_unicode_value_is_declined() {
        use std::os::unix::ffi::OsStringExt;
        let value = OsString::from_vec(vec![0x66, 0xff, 0x6f]);
        let outcome = KeyMaterial::from_lookup(
            |name| (name == "SWARM_SIGNING_KEY").then(|| value.clone()),
            |_| false,
        );
        assert_eq!(outcome.err(), Some(Declined::NonUnicodeEnvironment));
    }

    #[test]
    fn debug_output_names_no_key_material() -> TestResult {
        let canary_secret = "canary-hmac-secret-7c1e";
        let canary_seed = seed_hex();
        let keys = resolve(
            &[
                ("SWARM_SIGNING_KEY", canary_secret),
                ("SWARM_ED25519_KEY", SEED_HEX),
            ],
            false,
        )
        .map_err(|e| e.to_string())?;
        assert!(
            keys.ed25519.is_some(),
            "the canary seed must load for the check to mean anything"
        );
        let public = hex::encode(keys.ed25519_public_key().unwrap_or_default());
        let shown = format!("{keys:?} {keys:#?}");
        assert!(
            !shown.contains(canary_secret)
                && !shown.contains(&canary_seed)
                && !shown.contains(&public),
            "{shown}"
        );
        assert!(shown.contains("present"));
        Ok(())
    }
}

#[cfg(test)]
mod env_tests {
    use std::fs;
    use std::os::unix::fs::symlink;

    use super::*;

    const NAMES: [&str; 8] = [
        "SWARM_ED25519_KEY",
        "SWARM_SIGNING_KEY",
        "SWARM_ED25519_KEY_FILE",
        "SWARM_AGENT_SESSION",
        "SWARM_ALLOW_INSECURE_DEV_KEY",
        "SWARM_REQUIRE_KEY",
        "XDG_CONFIG_HOME",
        "HOME",
    ];

    /// The only test that reads the real process environment (through `from_env`), so it owns every variable it touches and
    /// restores them; the file system part uses a private directory under the temp dir.
    #[test]
    fn from_env_sees_the_conventional_file_only_through_the_real_file_system() -> Result<(), String>
    {
        let saved: Vec<(&str, Option<OsString>)> = NAMES
            .iter()
            .map(|name| (*name, std::env::var_os(name)))
            .collect();
        let root =
            std::env::temp_dir().join(format!("gsd-envelope-from-env-{}", std::process::id()));
        let key_dir = root.join("agent-swarm");
        let key_path = key_dir.join("signing-key.hex");
        fs::create_dir_all(&key_dir).map_err(|e| e.to_string())?;

        let mut observed: Vec<String> = Vec::new();
        for name in NAMES {
            std::env::remove_var(name);
        }
        std::env::set_var("XDG_CONFIG_HOME", &root);
        observed.push(format!(
            "absent: {:?}",
            KeyMaterial::from_env().map(|keys| keys.config_error())
        ));
        fs::write(&key_path, b"placeholder, never read").map_err(|e| e.to_string())?;
        observed.push(format!("regular file: {:?}", KeyMaterial::from_env().err()));
        std::env::set_var("SWARM_SIGNING_KEY", "k");
        observed.push(format!(
            "hidden by a raw key: {:?}",
            KeyMaterial::from_env().map(|keys| keys.hmac.is_some())
        ));
        std::env::remove_var("SWARM_SIGNING_KEY");
        fs::remove_file(&key_path).map_err(|e| e.to_string())?;
        symlink(root.join("missing-target"), &key_path).map_err(|e| e.to_string())?;
        observed.push(format!(
            "dangling link: {:?}",
            KeyMaterial::from_env().err()
        ));

        for (name, value) in saved {
            match value {
                Some(value) => std::env::set_var(name, value),
                None => std::env::remove_var(name),
            }
        }
        let _ = fs::remove_dir_all(&root);
        assert_eq!(
            observed,
            [
                "absent: Ok(None)",
                "regular file: Some(ConventionalKeyFile)",
                "hidden by a raw key: Ok(true)",
                "dangling link: Some(ConventionalKeyFile)",
            ]
        );
        Ok(())
    }
}
