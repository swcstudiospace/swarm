//! The error branches of key loading name no key material (phase 14.5, unit U3.4e, requirement R4).
//!
//! `debug_output_names_no_key_material` in `keys.rs` covers a valid key. Here the values are the ones that go wrong: a canary key in
//! malformed shapes (63 hex digits, the canary plus `zz`, whitespace inside a byte pair, bytes that are not UTF-8, a key file
//! reference, a conventional key file that exists or cannot be classified) and every [`Declined`] variant, produced from input that
//! carries a canary where the variant allows it. For each outcome the `Display`, `Debug` and pretty `Debug` text of the error (and of
//! the [`SignError`] and boxed error made from it), or, when keys were loaded, the `Debug` text of the [`KeyMaterial`], its
//! `config_error` text and what signing says, is scanned for the canary in the forms the other key tests use: raw, upper case, hex,
//! `0x` and space-separated hex, `\x..` escapes, decimal lists, Python bytes repr, base64 at every alignment in both alphabets,
//! UTF-16 in both byte orders, the decoded key bytes and a 16-digit prefix and suffix window of a key.
//!
//! Canaries are made at run time from the standard library's randomly keyed hasher; they are never written to a file or printed, and
//! a failing assertion names the shape, the channel and the form, never a value.

use std::collections::hash_map::RandomState;
use std::collections::{BTreeSet, HashMap};
use std::error::Error;
use std::ffi::OsString;
use std::fmt;
use std::fs;
use std::hash::{BuildHasher, Hasher};
use std::os::unix::ffi::OsStringExt;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use std::sync::Mutex;

use gsd_envelope::{
    compact_json, parse_envelope, sign_envelope, verify_envelope, Declined, KeyFilePresence,
    KeyMaterial, SignError,
};

/// `bytes` random bytes as lowercase hex.
fn random_hex(bytes: usize) -> String {
    let mut out = String::new();
    while out.len() < bytes * 2 {
        let word = RandomState::new().build_hasher().finish();
        out.push_str(&format!("{word:016x}"));
    }
    out.truncate(bytes * 2);
    out
}

type Forms = Vec<(String, Vec<u8>)>;

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Base64 of `data` in the standard alphabet, without padding.
fn base64(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for chunk in data.chunks(3) {
        let word = chunk.iter().enumerate().fold(0u32, |word, (index, byte)| {
            word | u32::from(*byte) << (16 - 8 * index)
        });
        for position in 0..=chunk.len() {
            out.push(B64[(word >> (18 - 6 * position) & 63) as usize]);
        }
    }
    out
}

/// The base64 text `data` makes wherever it sits in a larger blob: one fragment per alignment and alphabet.
fn base64_fragments(data: &[u8]) -> Forms {
    let mut found = Vec::new();
    for offset in 0..3usize {
        let mut padded = vec![0u8; offset];
        padded.extend_from_slice(data);
        let encoded = base64(&padded);
        let head = [0, 2, 3][offset];
        let tail = [0, 1, 1][(offset + data.len()) % 3];
        let standard = encoded[head..encoded.len() - tail].to_vec();
        let url = standard
            .iter()
            .map(|byte| match byte {
                b'+' => b'-',
                b'/' => b'_',
                other => *other,
            })
            .collect();
        found.push((format!("b64.{offset}"), standard));
        found.push((format!("b64url.{offset}"), url));
    }
    found
}

/// The shapes a dump or list of `data` takes in code and logs, labelled `<prefix><shape>`.
fn byte_forms(prefix: &str, data: &[u8]) -> Forms {
    let listed = |separator: &str, show: fn(u8) -> String| -> Vec<u8> {
        data.iter()
            .map(|byte| show(*byte))
            .collect::<Vec<_>>()
            .join(separator)
            .into_bytes()
    };
    let hex: String = data.iter().map(|byte| format!("{byte:02x}")).collect();
    vec![
        (
            format!("{prefix}dec-list"),
            listed(", ", |byte| byte.to_string()),
        ),
        (
            format!("{prefix}dec-list-tight"),
            listed(",", |byte| byte.to_string()),
        ),
        (
            format!("{prefix}x-escapes"),
            listed("", |byte| format!("\\x{byte:02x}")),
        ),
        (format!("{prefix}0x-hex"), format!("0x{hex}").into_bytes()),
        (
            format!("{prefix}0x-list"),
            listed(", ", |byte| format!("0x{byte:02x}")),
        ),
        (
            format!("{prefix}hex-spaced"),
            listed(" ", |byte| format!("{byte:02x}")),
        ),
        (
            format!("{prefix}HEX-spaced"),
            listed(" ", |byte| format!("{byte:02X}")),
        ),
    ]
}

/// The bytes the hex digits of `value` spell, or `None` for any other text or an odd number of digits.
fn hex_bytes(value: &str) -> Option<Vec<u8>> {
    let digits: Option<Vec<u8>> = value
        .chars()
        .map(|digit| digit.to_digit(16).map(|number| number as u8))
        .collect();
    let digits = digits?;
    (digits.len() % 2 == 0).then(|| {
        digits
            .chunks(2)
            .map(|pair| pair[0] << 4 | pair[1])
            .collect()
    })
}

/// Every byte form of the secret `value` a leak could take, with a label that is safe to print.
fn forms(value: &str) -> Forms {
    let raw = value.as_bytes();
    let hex: String = raw.iter().map(|byte| format!("{byte:02x}")).collect();
    let mut found = vec![
        ("utf8".to_string(), raw.to_vec()),
        ("utf8-upper".to_string(), value.to_uppercase().into_bytes()),
        (
            "utf16le".to_string(),
            value.encode_utf16().flat_map(u16::to_le_bytes).collect(),
        ),
        (
            "utf16be".to_string(),
            value.encode_utf16().flat_map(u16::to_be_bytes).collect(),
        ),
        ("hex-of-text".to_string(), hex.clone().into_bytes()),
        ("HEX-of-text".to_string(), hex.to_uppercase().into_bytes()),
    ];
    found.extend(base64_fragments(raw));
    found.extend(byte_forms("text-", raw));
    if let Some(key) = hex_bytes(value) {
        found.push(("key-bytes".to_string(), key.clone()));
        found.extend(
            base64_fragments(&key)
                .into_iter()
                .map(|(label, fragment)| (format!("key-{label}"), fragment)),
        );
        found.extend(byte_forms("key-", &key));
        if value.len() >= 32 {
            let (head, tail) = (&value[..16], &value[value.len() - 16..]);
            found.extend([
                ("key-prefix16".to_string(), head.as_bytes().to_vec()),
                ("key-suffix16".to_string(), tail.as_bytes().to_vec()),
                ("key-PREFIX16".to_string(), head.to_uppercase().into_bytes()),
                ("key-SUFFIX16".to_string(), tail.to_uppercase().into_bytes()),
                ("key-first8-bytes".to_string(), key[..8].to_vec()),
                ("key-last8-bytes".to_string(), key[key.len() - 8..].to_vec()),
            ]);
        }
    }
    found
}

/// The forms of a set of secret texts, named, and the scan over them. `Debug` is redacted.
struct Scan {
    needles: Vec<(String, Forms)>,
}

impl fmt::Debug for Scan {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Scan(<redacted>)")
    }
}

impl Scan {
    fn new(secrets: &[(String, String)]) -> Self {
        Self {
            needles: secrets
                .iter()
                .map(|(name, value)| (name.clone(), forms(value)))
                .collect(),
        }
    }

    /// `NAME/form` of every form of every secret found in `data`; a value is never part of the answer.
    fn leaks(&self, data: &[u8]) -> Vec<String> {
        let mut found = Vec::new();
        for (name, table) in &self.needles {
            for (label, needle) in table {
                if !needle.is_empty() && data.windows(needle.len()).any(|window| window == needle) {
                    found.push(format!("{name}/{label}"));
                }
            }
        }
        found
    }

    /// Fails with the labels of the forms found in `text`, naming the shape and the channel.
    fn require_clean(&self, text: &str, shape: &str, channel: &str) {
        let found = self.leaks(text.as_bytes());
        assert!(
            found.is_empty(),
            "canary in {channel} of the shape {shape}: {found:?}"
        );
    }
}

/// The `Display`, `Debug` and pretty `Debug` text of `error`, named.
fn texts_of<E: fmt::Display + fmt::Debug>(label: &str, error: &E) -> Vec<(String, String)> {
    vec![
        (format!("{label} Display"), error.to_string()),
        (format!("{label} Debug"), format!("{error:?}")),
        (format!("{label} pretty Debug"), format!("{error:#?}")),
    ]
}

/// What one malformed environment is expected to come to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Expect {
    /// `from_lookup` refuses: the caller lets Python answer.
    Declined(Declined),
    /// Keys loaded; `error` is whether `config_error` names a problem, `real_key` what `real_key_configured` says.
    Loaded { error: bool, real_key: bool },
}

/// One environment that goes wrong: its variables, what the conventional key-file path is classified as, and the texts a
/// leak of it would contain.
struct Shape {
    name: &'static str,
    vars: Vec<(&'static str, OsString)>,
    presence: KeyFilePresence,
    expect: Expect,
}

impl Shape {
    fn lookup(&self) -> HashMap<String, OsString> {
        self.vars
            .iter()
            .map(|(name, value)| ((*name).to_string(), value.clone()))
            .collect()
    }

    /// The canary values of this shape, named: the key, and every variable's value as text (lossily for bytes that are not UTF-8).
    fn secrets(&self, canary: &str) -> Vec<(String, String)> {
        let mut secrets = vec![("CANARY".to_string(), canary.to_string())];
        for (name, value) in &self.vars {
            let text = value.to_string_lossy().into_owned();
            if text.len() > 1 && text != "1" {
                secrets.push(((*name).to_string(), text));
            }
        }
        secrets
    }
}

/// The environments of the requirement, built around `canary` (64 hex digits).
fn shapes(canary: &str) -> Vec<Shape> {
    let text = |value: &str| OsString::from(value);
    let loaded = |error, real_key| Expect::Loaded { error, real_key };
    let mut non_utf8 = canary.as_bytes().to_vec();
    non_utf8.extend_from_slice(&[0xff, 0xfe]);
    vec![
        Shape {
            name: "ed25519-63-digits",
            vars: vec![("SWARM_ED25519_KEY", text(&canary[..63]))],
            presence: KeyFilePresence::Absent,
            expect: loaded(true, false),
        },
        Shape {
            name: "ed25519-plus-zz",
            vars: vec![("SWARM_ED25519_KEY", text(&format!("{canary}zz")))],
            presence: KeyFilePresence::Absent,
            expect: loaded(true, false),
        },
        Shape {
            name: "ed25519-space-inside-a-pair",
            vars: vec![(
                "SWARM_ED25519_KEY",
                text(&format!("{} {}", &canary[..31], &canary[31..])),
            )],
            presence: KeyFilePresence::Absent,
            expect: loaded(true, false),
        },
        Shape {
            name: "ed25519-space-between-pairs",
            vars: vec![(
                "SWARM_ED25519_KEY",
                text(&format!("{} {}", &canary[..32], &canary[32..])),
            )],
            presence: KeyFilePresence::Absent,
            expect: loaded(false, true),
        },
        Shape {
            name: "ed25519-trailing-newline",
            vars: vec![("SWARM_ED25519_KEY", text(&format!("{canary}\n")))],
            presence: KeyFilePresence::Absent,
            expect: loaded(false, true),
        },
        Shape {
            name: "ed25519-valid",
            vars: vec![("SWARM_ED25519_KEY", text(canary))],
            presence: KeyFilePresence::Absent,
            expect: loaded(false, true),
        },
        Shape {
            name: "signing-key-valid",
            vars: vec![("SWARM_SIGNING_KEY", text(canary))],
            presence: KeyFilePresence::Absent,
            expect: loaded(false, true),
        },
        Shape {
            name: "both-raw-keys-one-malformed",
            vars: vec![
                ("SWARM_SIGNING_KEY", text(canary)),
                ("SWARM_ED25519_KEY", text(&canary[..63])),
            ],
            presence: KeyFilePresence::Absent,
            expect: loaded(false, true),
        },
        Shape {
            name: "require-key-with-a-malformed-seed",
            vars: vec![
                ("SWARM_ED25519_KEY", text(&format!("{canary}zz"))),
                ("SWARM_REQUIRE_KEY", text("1")),
            ],
            presence: KeyFilePresence::Absent,
            expect: loaded(true, false),
        },
        Shape {
            name: "signing-key-not-utf8",
            vars: vec![("SWARM_SIGNING_KEY", OsString::from_vec(non_utf8.clone()))],
            presence: KeyFilePresence::Absent,
            expect: Expect::Declined(Declined::NonUnicodeEnvironment),
        },
        Shape {
            name: "ed25519-key-not-utf8",
            vars: vec![("SWARM_ED25519_KEY", OsString::from_vec(non_utf8))],
            presence: KeyFilePresence::Absent,
            expect: Expect::Declined(Declined::NonUnicodeEnvironment),
        },
        Shape {
            name: "key-file-reference",
            vars: vec![(
                "SWARM_ED25519_KEY_FILE",
                text(&format!("/keys/{canary}.pem")),
            )],
            presence: KeyFilePresence::Absent,
            expect: Expect::Declined(Declined::KeyFileReference),
        },
        Shape {
            name: "key-file-reference-in-an-agent-session",
            vars: vec![
                (
                    "SWARM_ED25519_KEY_FILE",
                    text(&format!("/keys/{canary}.pem")),
                ),
                ("SWARM_AGENT_SESSION", text("1")),
            ],
            presence: KeyFilePresence::Absent,
            expect: loaded(true, false),
        },
        Shape {
            name: "conventional-key-file-exists",
            vars: vec![("HOME", text(&format!("/home/{canary}")))],
            presence: KeyFilePresence::Present,
            expect: Expect::Declined(Declined::ConventionalKeyFile),
        },
        Shape {
            name: "conventional-key-path-unclear",
            vars: vec![("HOME", text(&format!("relative/{canary}")))],
            presence: KeyFilePresence::Unclear,
            expect: Expect::Declined(Declined::ConventionalKeyPath),
        },
    ]
}

/// Every text an outcome of key loading shows, named: the error in its three renderings and as a [`SignError`] and a boxed error,
/// or the loaded keys' `Debug`, `config_error`, and what signing an envelope with them says.
fn outputs(result: &Result<KeyMaterial, Declined>) -> Vec<(String, String)> {
    match result {
        Err(declined) => {
            let mut texts = texts_of("Declined", declined);
            texts.extend(texts_of("SignError of it", &SignError::from(*declined)));
            let boxed: Box<dyn Error> = Box::new(*declined);
            texts.push(("boxed error".to_string(), boxed.to_string()));
            texts
        }
        Ok(keys) => {
            let mut texts = vec![
                ("KeyMaterial Debug".to_string(), format!("{keys:?}")),
                ("KeyMaterial pretty Debug".to_string(), format!("{keys:#?}")),
                (
                    "config_error".to_string(),
                    keys.config_error().unwrap_or_default().to_string(),
                ),
            ];
            let mut envelope = parse_envelope(r#"{"task_id": "T-1", "gate": "review"}"#)
                .expect("a plain envelope parses");
            match sign_envelope(&mut envelope, keys) {
                Ok(outcome) => {
                    texts.push(("SignOutcome Debug".to_string(), format!("{outcome:?}")));
                    let signed = compact_json(envelope.as_json());
                    texts.push((
                        "the signed envelope".to_string(),
                        String::from_utf8_lossy(&signed).into_owned(),
                    ));
                    texts.push((
                        "verify".to_string(),
                        verify_envelope(&envelope, keys).to_string(),
                    ));
                }
                Err(error) => texts.extend(texts_of("sign refusal", &error)),
            }
            texts
        }
    }
}

/// Checks `result` against `expect` and panics, naming the shape and never a value, when they differ.
fn settle(shape: &str, result: &Result<KeyMaterial, Declined>, expect: Expect) {
    match (result, expect) {
        (Err(got), Expect::Declined(want)) => {
            assert_eq!(*got, want, "{shape}: the declined variant")
        }
        (Ok(keys), Expect::Loaded { error, real_key }) => {
            assert_eq!(
                keys.config_error().is_some(),
                error,
                "{shape}: config_error"
            );
            assert_eq!(keys.real_key_configured(), real_key, "{shape}: real_key");
        }
        (Err(_), Expect::Loaded { .. }) => panic!("{shape}: declined, keys were expected"),
        (Ok(_), Expect::Declined(_)) => panic!("{shape}: loaded, a refusal was expected"),
    }
}

#[test]
fn a_malformed_canary_key_shows_up_in_no_error_text() {
    let canary = random_hex(32);
    let all = shapes(&canary);
    assert!(all.len() >= 15, "the shape table lost entries");
    for shape in &all {
        let lookup = shape.lookup();
        let presence = shape.presence;
        let result =
            KeyMaterial::from_lookup(|name| lookup.get(name).cloned(), |_: &Path| presence);
        settle(shape.name, &result, shape.expect);
        let scan = Scan::new(&shape.secrets(&canary));
        let texts = outputs(&result);
        assert!(!texts.is_empty(), "{}: nothing was scanned", shape.name);
        for (channel, text) in &texts {
            assert!(
                !text.is_empty() || channel == "config_error",
                "{}: {channel} is empty",
                shape.name
            );
            scan.require_clean(text, shape.name, channel);
        }
    }
}

/// Builds the error texts of a failing parse of an envelope that carries `canary`, in the way the host would see them.
fn envelope_errors(canary: &str) -> Vec<(&'static str, Declined, String)> {
    let keys = KeyMaterial::from_lookup(
        |name| (name == "SWARM_SIGNING_KEY").then(|| OsString::from(canary)),
        |_: &Path| KeyFilePresence::Absent,
    )
    .expect("a signing key loads");
    let mut found = Vec::new();
    let unterminated = format!(r#"{{"note": "{canary}" "#);
    found.push((
        "invalid-json",
        parse_envelope(&unterminated).expect_err("invalid JSON is declined"),
        unterminated,
    ));
    let lone = format!(r#"{{"note": "{canary}\ud800"}}"#);
    found.push((
        "lone-surrogate",
        parse_envelope(&lone).expect_err("a lone surrogate is declined"),
        lone,
    ));
    let array = format!(r#"["{canary}"]"#);
    let mut parsed = parse_envelope(&array).expect("an array is JSON");
    let refusal = sign_envelope(&mut parsed, &keys).expect_err("an array is not an envelope");
    let SignError::Declined(declined) = refusal else {
        panic!("signing an array declines");
    };
    found.push(("not-an-object", declined, array));
    found
}

#[test]
fn every_declined_variant_is_covered_and_none_echoes_its_input() {
    let canary = random_hex(32);
    let mut produced: Vec<(String, Declined, Vec<String>)> = Vec::new();
    // Variants an environment makes.
    for shape in shapes(&canary) {
        let lookup = shape.lookup();
        let presence = shape.presence;
        if let Err(declined) =
            KeyMaterial::from_lookup(|name| lookup.get(name).cloned(), |_: &Path| presence)
        {
            produced.push((shape.name.to_string(), declined, vec![canary.clone()]));
        }
    }
    // Variants an envelope makes: the text carries the canary, so an error that echoed its input would show it.
    for (name, declined, input) in envelope_errors(&canary) {
        produced.push((name.to_string(), declined, vec![canary.clone(), input]));
    }
    // The last variant cannot be produced (HMAC takes every key length); it is named here so that a new variant breaks the build.
    produced.push((
        "hmac-key-rejected".to_string(),
        Declined::HmacKeyRejected,
        vec![canary.clone()],
    ));

    let mut seen = BTreeSet::new();
    for (name, declined, secrets) in &produced {
        // An exhaustive match: a new variant is a compile error here until the test covers it.
        let index = match declined {
            Declined::KeyFileReference => 0,
            Declined::ConventionalKeyFile => 1,
            Declined::ConventionalKeyPath => 2,
            Declined::NonUnicodeEnvironment => 3,
            Declined::NotAnObject => 4,
            Declined::InvalidJson => 5,
            Declined::LoneSurrogate => 6,
            Declined::HmacKeyRejected => 7,
        };
        seen.insert(index);
        let secrets: Vec<(String, String)> = secrets
            .iter()
            .enumerate()
            .map(|(position, value)| (format!("SECRET{position}"), value.clone()))
            .collect();
        let scan = Scan::new(&secrets);
        for (channel, text) in outputs(&Err(*declined)) {
            scan.require_clean(&text, name, &channel);
        }
    }
    assert_eq!(
        seen,
        (0..8).collect::<BTreeSet<_>>(),
        "a Declined variant has no test"
    );
}

/// Serializes the one test that changes the process environment.
static ENV_LOCK: Mutex<()> = Mutex::new(());

const ENV_NAMES: [&str; 8] = [
    "SWARM_SIGNING_KEY",
    "SWARM_ED25519_KEY",
    "SWARM_ED25519_KEY_FILE",
    "SWARM_ALLOW_INSECURE_DEV_KEY",
    "SWARM_REQUIRE_KEY",
    "SWARM_AGENT_SESSION",
    "XDG_CONFIG_HOME",
    "HOME",
];

/// A process environment to set (`name`, variables) and the refusal `KeyMaterial::from_env` must answer with.
type EnvCase = (&'static str, Vec<(&'static str, OsString)>, Declined);

#[test]
fn the_real_environment_and_a_real_key_file_leave_no_canary_in_an_error() {
    let _env = ENV_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let canary = random_hex(32);
    let content = random_hex(32);
    let home = std::env::temp_dir().join(format!("key-error-canary-{}", random_hex(8)));
    let key_dir = home.join(".config").join("agent-swarm");
    fs::create_dir_all(&key_dir).expect("key directory");
    fs::write(key_dir.join("signing-key.hex"), format!("{content}\n")).expect("key file");
    let clear = || ENV_NAMES.iter().for_each(|name| std::env::remove_var(name));

    let mut not_utf8 = canary.as_bytes().to_vec();
    not_utf8.push(0xff);
    let cases: Vec<EnvCase> = vec![
        (
            "conventional file that exists",
            vec![("HOME", home.clone().into_os_string())],
            Declined::ConventionalKeyFile,
        ),
        (
            "key file reference",
            vec![(
                "SWARM_ED25519_KEY_FILE",
                OsString::from(format!("/keys/{canary}.pem")),
            )],
            Declined::KeyFileReference,
        ),
        (
            "a signing key that is not UTF-8",
            vec![("SWARM_SIGNING_KEY", OsString::from_vec(not_utf8))],
            Declined::NonUnicodeEnvironment,
        ),
        (
            "a relative home",
            vec![("HOME", OsString::from(format!("relative/{canary}")))],
            Declined::ConventionalKeyPath,
        ),
    ];
    let scan = Scan::new(&[
        ("CANARY".to_string(), canary.clone()),
        ("FILE_CONTENT".to_string(), content),
    ]);
    for (name, vars, expected) in &cases {
        clear();
        for (variable, value) in vars {
            std::env::set_var(variable, value);
        }
        let result = KeyMaterial::from_env();
        clear();
        settle(name, &result, Expect::Declined(*expected));
        for (channel, text) in outputs(&result) {
            scan.require_clean(&text, name, &channel);
        }
    }
    // The key file the conventional path names was classified, not read: it is as it was written.
    assert_eq!(
        fs::read_to_string(key_dir.join("signing-key.hex"))
            .expect("key file")
            .trim()
            .len(),
        64
    );
    fs::remove_dir_all(&home).expect("remove the scratch home");
}

// ---- teeth ----------------------------------------------------------------------------------------------------------------

/// An error type whose text echoes what it was given, as a careless message would.
struct Echo(String);

impl fmt::Display for Echo {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "cannot load the key {}", self.0)
    }
}

impl fmt::Debug for Echo {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_tuple("Echo").field(&self.0).finish()
    }
}

#[test]
fn the_scan_flags_a_message_that_echoes_any_shape_and_every_form() {
    let canary = random_hex(32);
    for shape in shapes(&canary) {
        let scan = Scan::new(&shape.secrets(&canary));
        for (_, value) in shape.secrets(&canary).iter().take(2) {
            let echoed = texts_of("echo", &Echo(value.clone()));
            let flagged = echoed
                .iter()
                .filter(|(channel, text)| {
                    catch_unwind(AssertUnwindSafe(|| {
                        scan.require_clean(text, shape.name, channel)
                    }))
                    .is_err()
                })
                .count();
            assert_eq!(
                flagged,
                echoed.len(),
                "{}: an echo of a value is found",
                shape.name
            );
        }
    }
    let scan = Scan::new(&[("CANARY".to_string(), canary.clone())]);
    for (label, needle) in forms(&canary) {
        let mut text = b"before ".to_vec();
        text.extend_from_slice(&needle);
        text.extend_from_slice(b" after");
        assert!(
            scan.leaks(&text).contains(&format!("CANARY/{label}")),
            "the form {label} is not found"
        );
    }
    assert!(scan
        .leaks(br#"{"error": "fail-closed: SWARM_ED25519_KEY is set"}"#)
        .is_empty());
}

#[test]
fn a_failing_scan_names_the_shape_the_channel_and_the_form_and_no_value() {
    let canary = random_hex(32);
    let scan = Scan::new(&[("CANARY".to_string(), canary.clone())]);
    let payload = catch_unwind(AssertUnwindSafe(|| {
        scan.require_clean(&format!("bad key {canary}"), "a-shape", "a-channel");
    }))
    .expect_err("an echoed canary fails the scan");
    let message = payload
        .downcast_ref::<String>()
        .cloned()
        .unwrap_or_default();
    assert!(
        message.contains("a-shape") && message.contains("a-channel"),
        "{message}"
    );
    assert!(message.contains("CANARY/utf8"), "{message}");
    assert!(!message.contains(&canary), "the message holds the value");
    assert!(format!("{scan:?}") == "Scan(<redacted>)");
}
