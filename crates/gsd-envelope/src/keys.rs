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
use std::io;
use std::path::{Component, Path, PathBuf};

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

/// What the conventional key-file path holds, decided without opening the file. The rules are those of the path walk in
/// `swarm/envelope.py` (`_private_seed`): that walk opens every directory with `O_NOFOLLOW`, treats a missing component as "no
/// file" and every other failure as a configuration error.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyFilePresence {
    /// The first missing component is reached through real directories only: Python finds no file and stays keyless.
    Absent,
    /// Every ancestor is a real directory and the last component exists (whatever it is): Python would try to load it.
    Present,
    /// The path cannot be classified the way Python's walk would: a relative path, a symlinked or non-directory ancestor, an
    /// ancestor that cannot be opened, or any error other than "not found". Python reports a configuration error here, so a
    /// caller must not answer for it.
    Unclear,
}

/// What one path component is, by `lstat` (no link is followed).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum EntryKind {
    Directory,
    Symlink,
    Other,
}

/// `lstat` of `path`; a directory must also be openable for reading, as Python opens each ancestor with `O_RDONLY`.
fn lstat_entry(path: &Path) -> io::Result<EntryKind> {
    let file_type = std::fs::symlink_metadata(path)?.file_type();
    if file_type.is_symlink() {
        Ok(EntryKind::Symlink)
    } else if file_type.is_dir() {
        std::fs::read_dir(path).map(|_| EntryKind::Directory)
    } else {
        Ok(EntryKind::Other)
    }
}

/// Classifies the conventional key-file `path` on the real file system, without opening the file.
///
/// A relative path is [`KeyFilePresence::Unclear`] (Python refuses it as a configuration error). Otherwise each component is
/// inspected with `lstat`, from the root down: the result is [`KeyFilePresence::Absent`] only when the first missing component
/// fails with "not found" and every component before it is a real directory (not a symlink) that can be read;
/// [`KeyFilePresence::Present`] when all ancestors are such directories and the last component exists; anything else is
/// [`KeyFilePresence::Unclear`].
pub fn conventional_key_file_presence(path: &Path) -> KeyFilePresence {
    classify_path(path, lstat_entry)
}

/// The walk of [`conventional_key_file_presence`] over an injectable `lstat`.
fn classify_path(path: &Path, lstat: impl Fn(&Path) -> io::Result<EntryKind>) -> KeyFilePresence {
    if !path.is_absolute() {
        return KeyFilePresence::Unclear;
    }
    let mut seen = PathBuf::new();
    let mut components = path.components().peekable();
    while let Some(component) = components.next() {
        if !matches!(
            component,
            Component::RootDir | Component::Normal(_) | Component::ParentDir
        ) {
            return KeyFilePresence::Unclear;
        }
        seen.push(component);
        match lstat(&seen) {
            Ok(_) if components.peek().is_none() => return KeyFilePresence::Present,
            Ok(EntryKind::Directory) => {}
            Ok(EntryKind::Symlink | EntryKind::Other) => return KeyFilePresence::Unclear,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                return KeyFilePresence::Absent
            }
            Err(_) => return KeyFilePresence::Unclear,
        }
    }
    KeyFilePresence::Unclear
}

/// The HMAC secret that signs and verifies `hmac:` signatures.
pub(crate) struct HmacKey {
    /// Secret bytes (`SWARM_SIGNING_KEY` as UTF-8, or the public dev key).
    pub(crate) secret: Zeroizing<Vec<u8>>,
    /// True for the unset-or-empty `SWARM_SIGNING_KEY` dev-key opt-in, which Python audits as `security.dev_key`.
    pub(crate) implicit_dev: bool,
}

/// The signing keys the process environment configures, with the outcome of Python's fail-closed checks.
///
/// Callers decide "is a real key configured" and "is the dev key in play" only through [`KeyMaterial::real_key_configured`] and
/// [`KeyMaterial::dev_key_active`], never from whether an HMAC secret is present: that secret can be the public dev key, and a
/// configured real secret can coexist with an active dev-key opt-in.
pub struct KeyMaterial {
    pub(crate) ed25519: Option<SigningKey>,
    pub(crate) hmac: Option<HmacKey>,
    pub(crate) config_error: Option<&'static str>,
    real_key: bool,
    dev_key_allowed: bool,
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
    /// Resolves the keys from the process environment and the real file system (the conventional key file is classified with
    /// `lstat` and never opened).
    ///
    /// # Errors
    ///
    /// [`Declined`] when a key file reference would be consulted, the conventional key-file path cannot be classified, or an
    /// environment value is not UTF-8; Python answers then.
    pub fn from_env() -> Result<Self, Declined> {
        Self::from_lookup(
            |name| std::env::var_os(name),
            conventional_key_file_presence,
        )
    }

    /// Resolves the keys from an arbitrary environment. `get` looks a variable up; `presence` classifies the conventional
    /// key-file path (see [`conventional_key_file_presence`]) and is called only when Python's answer depends on the file. The
    /// file is never opened.
    ///
    /// The variables read are `SWARM_ED25519_KEY`, `SWARM_SIGNING_KEY`, `SWARM_ED25519_KEY_FILE`, `SWARM_AGENT_SESSION`,
    /// `SWARM_ALLOW_INSECURE_DEV_KEY`, `SWARM_REQUIRE_KEY`, `XDG_CONFIG_HOME` and `HOME`.
    ///
    /// # Errors
    ///
    /// [`Declined::NonUnicodeEnvironment`] for a non-UTF-8 value; [`Declined::KeyFileReference`] when `SWARM_ED25519_KEY_FILE` is
    /// set and Python would reach it (no raw key variable set, not an agent session); [`Declined::ConventionalKeyFile`] when the
    /// same holds for an existing conventional file; [`Declined::ConventionalKeyPath`] when the same holds for a conventional
    /// path that is [`KeyFilePresence::Unclear`] (Python then reports a configuration error and refuses even the dev key), or
    /// when an agent session with an empty `SWARM_ED25519_KEY_FILE` would have its `supplied` decision made by such a path.
    pub fn from_lookup(
        get: impl Fn(&str) -> Option<OsString>,
        presence: impl Fn(&Path) -> KeyFilePresence,
    ) -> Result<Self, Declined> {
        let env = Environment::read(&get)?;
        let conventional = || {
            env.conventional_reference()
                .map_or(KeyFilePresence::Absent, |path| presence(&path))
        };

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
            (None, None) => match conventional() {
                KeyFilePresence::Absent => (None, None),
                KeyFilePresence::Present => return Err(Declined::ConventionalKeyFile),
                KeyFilePresence::Unclear => return Err(Declined::ConventionalKeyPath),
            },
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

        // `signing_config_error`. Python's `_signing_key_supplied` also counts an existing conventional file; that only decides
        // anything when no variable supplies a key and the material carries an error, which is the agent-session case above.
        let key_file_named = env.key_file.as_deref().is_some_and(|file| !file.is_empty());
        let supplied = env.signing_key.is_some()
            || env.ed25519_key.is_some()
            || key_file_named
            || env.require_key
            || (ed25519_error.is_some()
                && match conventional() {
                    KeyFilePresence::Absent => false,
                    KeyFilePresence::Present => true,
                    KeyFilePresence::Unclear => return Err(Declined::ConventionalKeyPath),
                });
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
            real_key,
            dev_key_allowed,
        })
    }

    /// Why a signature made now could never verify (Python `signing_config_error`), or `None`. A message is a fixed string.
    pub fn config_error(&self) -> Option<&'static str> {
        self.config_error
    }

    /// Python's `real_key_configured()`: true when a loadable Ed25519 seed or a non-dev `SWARM_SIGNING_KEY` is configured.
    ///
    /// This is the only way to ask. An HMAC secret being present proves nothing: it can be the public dev key.
    pub fn real_key_configured(&self) -> bool {
        self.real_key
    }

    /// Whether Python's `insecure_dev_key()` returns the dev key: `SWARM_ALLOW_INSECURE_DEV_KEY=1`, no Ed25519 signer, no
    /// Ed25519 loading error and `SWARM_REQUIRE_KEY` not `1`.
    ///
    /// This is the only way to ask. It stays true beside a real `SWARM_SIGNING_KEY` (Python then still returns the dev key from
    /// `insecure_dev_key()`, though the real secret signs), and an HMAC secret being present does not imply it. A caller that
    /// refuses unless "a real key is configured or the dev key is active" must test both accessors.
    pub fn dev_key_active(&self) -> bool {
        self.dev_key_allowed
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
            .field("real_key_configured", &self.real_key)
            .field("dev_key_active", &self.dev_key_allowed)
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
        let presence = if exists {
            KeyFilePresence::Present
        } else {
            KeyFilePresence::Absent
        };
        resolve_with(vars, presence).0
    }

    /// Resolves under a fixed classification of the conventional path, and counts how often the classification was asked for.
    fn resolve_with(
        vars: Vars,
        presence: KeyFilePresence,
    ) -> (Result<KeyMaterial, Declined>, usize) {
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
        let asked = std::cell::Cell::new(0);
        let keys = KeyMaterial::from_lookup(
            |name| map.get(name).cloned(),
            |_| {
                asked.set(asked.get() + 1);
                presence
            },
        );
        (keys, asked.get())
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
                    KeyFilePresence::Absent
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
            |_| KeyFilePresence::Absent,
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

    type Entries<'a> = &'a [(&'a str, Result<EntryKind, io::ErrorKind>)];

    /// Runs `classify_path` over a fake file system: a listed path answers as given and every other path is "not found".
    /// Returns the verdict and every path that was looked at, in order.
    fn classify_fake(path: &str, entries: Entries) -> (KeyFilePresence, Vec<String>) {
        let seen = std::cell::RefCell::new(Vec::<String>::new());
        let verdict = classify_path(Path::new(path), |probed| {
            seen.borrow_mut().push(probed.display().to_string());
            entries
                .iter()
                .find(|(listed, _)| Path::new(listed) == probed)
                .map_or(Err(io::ErrorKind::NotFound), |(_, answer)| *answer)
                .map_err(io::Error::from)
        });
        (verdict, seen.into_inner())
    }

    /// The conventional path walk of Python's `_private_seed`, row by row. EACCES cannot be produced by a file system test as
    /// root (an unreadable ancestor does not block `lstat` or `open` there), so permission failures are injected.
    #[test]
    fn classification_follows_pythons_path_walk() {
        const KEY: &str = "/h/.config/agent-swarm/signing-key.hex";
        const DIR: Result<EntryKind, io::ErrorKind> = Ok(EntryKind::Directory);
        const DENIED: Result<EntryKind, io::ErrorKind> = Err(io::ErrorKind::PermissionDenied);
        type Row<'a> = (
            &'a str,
            &'a str,
            Entries<'a>,
            KeyFilePresence,
            &'a [&'a str],
        );
        let rows: [Row; 17] = [
            (
                "clean absence: every ancestor a real directory, the file missing",
                KEY,
                &[
                    ("/", DIR),
                    ("/h", DIR),
                    ("/h/.config", DIR),
                    ("/h/.config/agent-swarm", DIR),
                ],
                KeyFilePresence::Absent,
                &["/", "/h", "/h/.config", "/h/.config/agent-swarm", KEY],
            ),
            (
                "first missing component ends the walk (.config)",
                KEY,
                &[("/", DIR), ("/h", DIR)],
                KeyFilePresence::Absent,
                &["/", "/h", "/h/.config"],
            ),
            (
                "first missing component ends the walk (agent-swarm)",
                KEY,
                &[("/", DIR), ("/h", DIR), ("/h/.config", DIR)],
                KeyFilePresence::Absent,
                &["/", "/h", "/h/.config", "/h/.config/agent-swarm"],
            ),
            (
                "a regular file at the end is present",
                KEY,
                &[
                    ("/", DIR),
                    ("/h", DIR),
                    ("/h/.config", DIR),
                    ("/h/.config/agent-swarm", DIR),
                    (KEY, Ok(EntryKind::Other)),
                ],
                KeyFilePresence::Present,
                &["/", "/h", "/h/.config", "/h/.config/agent-swarm", KEY],
            ),
            (
                "a symlink at the end is present (Python then fails to open it)",
                KEY,
                &[
                    ("/", DIR),
                    ("/h", DIR),
                    ("/h/.config", DIR),
                    ("/h/.config/agent-swarm", DIR),
                    (KEY, Ok(EntryKind::Symlink)),
                ],
                KeyFilePresence::Present,
                &["/", "/h", "/h/.config", "/h/.config/agent-swarm", KEY],
            ),
            (
                "a directory at the end is present",
                KEY,
                &[
                    ("/", DIR),
                    ("/h", DIR),
                    ("/h/.config", DIR),
                    ("/h/.config/agent-swarm", DIR),
                    (KEY, DIR),
                ],
                KeyFilePresence::Present,
                &["/", "/h", "/h/.config", "/h/.config/agent-swarm", KEY],
            ),
            (
                "a symlinked .config is unclear and the walk stops there",
                KEY,
                &[
                    ("/", DIR),
                    ("/h", DIR),
                    ("/h/.config", Ok(EntryKind::Symlink)),
                ],
                KeyFilePresence::Unclear,
                &["/", "/h", "/h/.config"],
            ),
            (
                "a symlinked HOME is unclear",
                KEY,
                &[("/", DIR), ("/h", Ok(EntryKind::Symlink))],
                KeyFilePresence::Unclear,
                &["/", "/h"],
            ),
            (
                "a regular file where a directory is expected is unclear",
                KEY,
                &[
                    ("/", DIR),
                    ("/h", DIR),
                    ("/h/.config", DIR),
                    ("/h/.config/agent-swarm", Ok(EntryKind::Other)),
                ],
                KeyFilePresence::Unclear,
                &["/", "/h", "/h/.config", "/h/.config/agent-swarm"],
            ),
            (
                "an ancestor that cannot be opened (EACCES injected) is unclear",
                KEY,
                &[("/", DIR), ("/h", DENIED)],
                KeyFilePresence::Unclear,
                &["/", "/h"],
            ),
            (
                "an unreadable root is unclear",
                KEY,
                &[("/", DENIED)],
                KeyFilePresence::Unclear,
                &["/"],
            ),
            (
                "any other error on an ancestor is unclear",
                KEY,
                &[
                    ("/", DIR),
                    ("/h", DIR),
                    ("/h/.config", Err(io::ErrorKind::Other)),
                ],
                KeyFilePresence::Unclear,
                &["/", "/h", "/h/.config"],
            ),
            (
                "an error other than not-found on the last component is unclear",
                KEY,
                &[
                    ("/", DIR),
                    ("/h", DIR),
                    ("/h/.config", DIR),
                    ("/h/.config/agent-swarm", DIR),
                    (KEY, DENIED),
                ],
                KeyFilePresence::Unclear,
                &["/", "/h", "/h/.config", "/h/.config/agent-swarm", KEY],
            ),
            (
                "a relative path is unclear and is never looked at",
                "h/.config/agent-swarm/signing-key.hex",
                &[("h", DIR)],
                KeyFilePresence::Unclear,
                &[],
            ),
            (
                "a leading dot is relative too",
                "./h/signing-key.hex",
                &[],
                KeyFilePresence::Unclear,
                &[],
            ),
            (
                "a parent-directory component is walked like any directory",
                "/h/../x/signing-key.hex",
                &[("/", DIR), ("/h", DIR), ("/h/..", DIR), ("/h/../x", DIR)],
                KeyFilePresence::Absent,
                &["/", "/h", "/h/..", "/h/../x", "/h/../x/signing-key.hex"],
            ),
            (
                "a symlink above a missing file hides the absence",
                KEY,
                &[("/", Ok(EntryKind::Symlink))],
                KeyFilePresence::Unclear,
                &["/"],
            ),
        ];
        for (label, path, entries, verdict, looked_at) in rows {
            let (got, seen) = classify_fake(path, entries);
            assert_eq!(got, verdict, "{label}");
            assert_eq!(seen, looked_at, "{label}: paths looked at");
        }
    }

    /// Where Python reaches the conventional path, an unclear one is declined, whatever the dev-key opt-in says; where Python
    /// never reaches it (a raw key, an agent session without a file reference, no base directory) nothing is asked and nothing
    /// is declined.
    #[test]
    fn an_unclear_conventional_path_is_declined_exactly_where_python_consults_it() {
        type Row = (Vars<'static>, Result<Option<&'static str>, Declined>, usize);
        let rows: [Row; 12] = [
            (&[("HOME", "/h")], Err(Declined::ConventionalKeyPath), 1),
            (
                &[("HOME", "/h"), ("SWARM_ALLOW_INSECURE_DEV_KEY", "1")],
                Err(Declined::ConventionalKeyPath),
                1,
            ),
            (
                &[("HOME", "/h"), ("SWARM_REQUIRE_KEY", "1")],
                Err(Declined::ConventionalKeyPath),
                1,
            ),
            (&[], Ok(None), 0),
            (&[("HOME", "/h"), ("SWARM_SIGNING_KEY", "k")], Ok(None), 0),
            (
                &[("HOME", "/h"), ("SWARM_ED25519_KEY", "zz")],
                Ok(Some(ED25519_UNLOADABLE)),
                0,
            ),
            (
                &[("HOME", "/h"), ("SWARM_ED25519_KEY_FILE", "/k")],
                Err(Declined::KeyFileReference),
                0,
            ),
            (&[("HOME", "/h"), ("SWARM_AGENT_SESSION", "1")], Ok(None), 0),
            (
                &[
                    ("HOME", "/h"),
                    ("SWARM_AGENT_SESSION", "1"),
                    ("SWARM_ED25519_KEY_FILE", "/k"),
                ],
                Ok(Some(AGENT_SESSION_REFERENCE)),
                0,
            ),
            (
                &[
                    ("HOME", "/h"),
                    ("SWARM_AGENT_SESSION", "1"),
                    ("SWARM_ED25519_KEY_FILE", ""),
                ],
                Err(Declined::ConventionalKeyPath),
                1,
            ),
            (
                &[
                    ("HOME", "/h"),
                    ("SWARM_AGENT_SESSION", "1"),
                    ("SWARM_ED25519_KEY_FILE", ""),
                    ("SWARM_REQUIRE_KEY", "1"),
                ],
                Ok(Some(AGENT_SESSION_REFERENCE)),
                0,
            ),
            (
                &[("XDG_CONFIG_HOME", "relative"), ("SWARM_SIGNING_KEY", "k")],
                Ok(None),
                0,
            ),
        ];
        for (vars, expected, asked) in rows {
            let (keys, calls) = resolve_with(vars, KeyFilePresence::Unclear);
            assert_eq!(keys.map(|keys| keys.config_error()), expected, "{vars:?}");
            assert_eq!(calls, asked, "{vars:?}: times the path was classified");
        }
    }

    /// `real_key_configured` and `dev_key_active` for every combination of Ed25519 seed, HMAC secret, opt-in and
    /// `SWARM_REQUIRE_KEY`. The expected values restate Python's `real_key_configured()` and `insecure_dev_key() is not None`;
    /// the committed vectors compare them with the real module.
    #[test]
    fn real_key_and_dev_key_answers_cover_every_combination() -> TestResult {
        let seeds: [(&str, Option<&str>); 3] = [
            ("none", None),
            ("valid", Some(SEED_HEX)),
            ("invalid", Some("zz")),
        ];
        let secrets: [(&str, Option<&str>); 4] = [
            ("none", None),
            ("real", Some("k")),
            ("the public dev value", Some(DEV_KEY)),
            ("empty", Some("")),
        ];
        let mut checked = 0;
        for (seed_name, seed) in seeds {
            for (secret_name, secret) in secrets {
                for opt_in in [false, true] {
                    for require in [false, true] {
                        let mut vars: Vec<(&str, &str)> = Vec::new();
                        vars.extend(seed.map(|value| ("SWARM_ED25519_KEY", value)));
                        vars.extend(secret.map(|value| ("SWARM_SIGNING_KEY", value)));
                        vars.extend(opt_in.then_some(("SWARM_ALLOW_INSECURE_DEV_KEY", "1")));
                        vars.extend(require.then_some(("SWARM_REQUIRE_KEY", "1")));
                        let keys = resolve(&vars, false).map_err(|e| format!("{vars:?}: {e}"))?;
                        let real = seed == Some(SEED_HEX)
                            || secret.is_some_and(|value| !value.is_empty() && value != DEV_KEY);
                        let dev = opt_in && seed.is_none() && !require;
                        let label = format!(
                            "seed {seed_name}, secret {secret_name}, opt-in {opt_in}, require {require}"
                        );
                        assert_eq!(keys.real_key_configured(), real, "{label}: real key");
                        assert_eq!(keys.dev_key_active(), dev, "{label}: dev key");
                        checked += 1;
                    }
                }
            }
        }
        assert_eq!(checked, 48);
        Ok(())
    }

    /// The two answers are independent of whether an HMAC secret is present, which is why callers must not infer them from it.
    #[test]
    fn an_hmac_secret_does_not_imply_either_answer() -> TestResult {
        let cases: [(Vars, bool, bool, bool); 4] = [
            // (variables, HMAC secret present, real key configured, dev key active)
            (&[("SWARM_ALLOW_INSECURE_DEV_KEY", "1")], true, false, true),
            (
                &[
                    ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                    ("SWARM_SIGNING_KEY", DEV_KEY),
                ],
                true,
                false,
                true,
            ),
            (
                &[
                    ("SWARM_ALLOW_INSECURE_DEV_KEY", "1"),
                    ("SWARM_SIGNING_KEY", "k"),
                ],
                true,
                true,
                true,
            ),
            (&[("SWARM_SIGNING_KEY", DEV_KEY)], false, false, false),
        ];
        for (vars, hmac, real, dev) in cases {
            let keys = resolve(vars, false).map_err(|e| format!("{vars:?}: {e}"))?;
            assert_eq!(keys.hmac.is_some(), hmac, "{vars:?}: HMAC secret");
            assert_eq!(keys.real_key_configured(), real, "{vars:?}: real key");
            assert_eq!(keys.dev_key_active(), dev, "{vars:?}: dev key");
        }
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
        let created =
            std::env::temp_dir().join(format!("gsd-envelope-from-env-{}", std::process::id()));
        fs::create_dir_all(created.join("agent-swarm")).map_err(|e| e.to_string())?;
        // A temp dir that itself sits behind a symlink would make every ancestor walk unclear; walk from the real path.
        let root = fs::canonicalize(&created).map_err(|e| e.to_string())?;
        let key_dir = root.join("agent-swarm");
        let key_path = key_dir.join("signing-key.hex");

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
        fs::remove_file(&key_path).map_err(|e| e.to_string())?;
        let linked_base = root.join("linked-base");
        symlink(&root, &linked_base).map_err(|e| e.to_string())?;
        std::env::set_var("XDG_CONFIG_HOME", &linked_base);
        observed.push(format!(
            "symlinked base: {:?}",
            KeyMaterial::from_env().err()
        ));
        std::env::set_var("XDG_CONFIG_HOME", "relative/base");
        observed.push(format!(
            "relative base: {:?}",
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
                "symlinked base: Some(ConventionalKeyPath)",
                "relative base: Some(ConventionalKeyPath)",
            ]
        );
        Ok(())
    }
}
