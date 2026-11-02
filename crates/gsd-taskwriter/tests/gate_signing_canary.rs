//! Signing canaries for the gate-issue tail (design item 13: U3.6a carries its own canary cases).
//!
//! A random 64-digit canary sits in `SWARM_SIGNING_KEY` and another in `SWARM_ED25519_KEY` (and, in the malformed-key cases, in the
//! wrong shapes of them). `TaskStoreWriter::issue_gate` runs every branch: HMAC, Ed25519, both, a keyless advisory run, an agent
//! session, the dev key, a signing configuration error, a malformed Ed25519 seed, a refusal by Python's own rules, a fail verdict with
//! feedback, and a request the writer declines. None of the canaries, in any of the forms the scanner below knows (raw, upper-case,
//! hex of the text, `0x`, spaced hex, `\x..`, decimal list, Python bytes repr, base64 at every alignment in both alphabets, UTF-16 in
//! both byte orders, the decoded key bytes and a 16-digit prefix and suffix window), may appear in: the `Debug` text of the key material,
//! the request and the plan; the plan's JSON; the verdict file; an envelope; an event; a refusal or error text; the rows of `tasks`,
//! `transitions` and `verdicts`; or the store file.
//!
//! The scanner is a copy of the one in `crates/gsd-adapter/tests/keys_never_in_guest.rs` (that file is another unit's), cut to what is
//! needed here. Canaries are made at run time, never written to a file and never printed: a failed assertion names the variable and the
//! form that matched.

use std::collections::hash_map::RandomState;
use std::ffi::OsString;
use std::hash::{BuildHasher, Hasher};
use std::path::Path;
use std::process::Command;
use std::sync::LazyLock;

use gsd_envelope::{Declined, KeyFilePresence, KeyMaterial};
use gsd_route::pydumps::to_python_dumps;
use gsd_route::{jstr, Json};
use gsd_taskwriter::{
    open_for_write, GateIssueRequest, SystemIds, TaskStoreWriter, WriteState, WriterConfig,
    WriterError,
};

// ---- the scanner (a copy; see the module documentation) ---------------------------------------------------------------------

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

fn python_bytes_repr(data: &[u8]) -> Vec<u8> {
    let quote = if data.contains(&b'\'') && !data.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    let mut out = Vec::new();
    for &byte in data {
        match byte {
            b'\\' => out.extend_from_slice(b"\\\\"),
            b'\t' => out.extend_from_slice(b"\\t"),
            b'\n' => out.extend_from_slice(b"\\n"),
            b'\r' => out.extend_from_slice(b"\\r"),
            other if other == quote => out.extend_from_slice(&[b'\\', other]),
            0x20..=0x7e => out.push(byte),
            other => out.extend_from_slice(format!("\\x{other:02x}").as_bytes()),
        }
    }
    out
}

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
        (format!("{prefix}bytes-repr"), python_bytes_repr(data)),
    ]
}

const KEY_WINDOW_MIN_DIGITS: usize = 32;

fn forms(value: &str) -> Forms {
    let raw = value.as_bytes();
    let hex = |bytes: &[u8]| -> String { bytes.iter().map(|byte| format!("{byte:02x}")).collect() };
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
        ("hex-of-text".to_string(), hex(raw).into_bytes()),
        (
            "HEX-of-text".to_string(),
            hex(raw).to_uppercase().into_bytes(),
        ),
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
    }
    if value.len() >= KEY_WINDOW_MIN_DIGITS {
        let (head, tail) = (&value[..16], &value[value.len() - 16..]);
        found.extend([
            ("prefix16".to_string(), head.as_bytes().to_vec()),
            ("suffix16".to_string(), tail.as_bytes().to_vec()),
            ("PREFIX16".to_string(), head.to_uppercase().into_bytes()),
            ("SUFFIX16".to_string(), tail.to_uppercase().into_bytes()),
        ]);
    }
    found
}

/// Per-run canary values and the forms to search for. `Debug` is redacted: a failing assertion must not print a value.
struct Canaries {
    needles: Vec<(String, Forms)>,
}

impl std::fmt::Debug for Canaries {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Canaries(<redacted>)")
    }
}

impl Canaries {
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

    fn require_clean(&self, data: &[u8], channel: &str) {
        let found = self.leaks(data);
        assert!(found.is_empty(), "canary in {channel}: {found:?}");
    }
}

// ---- the scenarios --------------------------------------------------------------------------------------------------------

const SEED_SCRIPT: &str = r#"
import sys
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
s.create(task_id="G1", correlation_id="corr-g1", capability="cap", notes={"gate": "quality", "gate_for": ["T-a", "T-b"]})
for t in ("T-a", "T-b"):
    s.create(task_id=t, correlation_id="corr-" + t, capability="cap", notes={"gates": ["quality"]})
s.conn.execute("UPDATE tasks SET state='IN_PROGRESS' WHERE task_id='G1'")
s.conn.execute("UPDATE tasks SET state='IN_REVIEW' WHERE task_id IN ('T-a','T-b')")
s.conn.commit()
s.conn.close()
"#;

static SEEDED: LazyLock<Vec<u8>> = LazyLock::new(|| {
    let root = match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => std::path::PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    };
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("tasks.db");
    let output = Command::new("python3")
        .arg("-c")
        .arg(SEED_SCRIPT)
        .arg(&db)
        .current_dir(&root)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("SWARM_GSD_STATE_FILE", "")
        .env_remove("SWARM_TASKSTORE_WAL")
        .output()
        .expect("run python3");
    assert!(
        output.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    std::fs::read(&db).expect("read seeded store")
});

fn finding(severity: &str) -> Json {
    Json::Obj(vec![
        ("id".to_string(), jstr("F-1")),
        ("severity".to_string(), jstr(severity)),
        ("kind".to_string(), jstr("functional")),
        ("summary".to_string(), jstr("a finding")),
    ])
}

fn request(task_id: Option<&str>, per_target: Vec<(String, Vec<Json>)>) -> GateIssueRequest {
    GateIssueRequest {
        gate: "quality".to_string(),
        agent_id: "A08@local".to_string(),
        findings: vec![finding("minor")],
        runs: vec![(
            "pytest".to_string(),
            Json::Obj(vec![("passed".to_string(), Json::Int(3))]),
        )],
        expires_s: 86_400,
        extra: Vec::new(),
        simulate: false,
        per_target,
        task_id: task_id.map(str::to_string),
        correlation_id: None,
        session: false,
        dryrun_fail: String::new(),
        now: 1_700_000_123.456_789,
    }
}

/// One scenario: the environment variables set, and what to ask.
struct Scenario {
    name: &'static str,
    env: Vec<(&'static str, String)>,
    request: GateIssueRequest,
}

fn lookup<'a>(env: &'a [(&'static str, String)]) -> impl Fn(&str) -> Option<OsString> + 'a {
    move |name| {
        env.iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| OsString::from(value))
    }
}

/// Runs the scenario against a copy of the seeded store and checks every channel. Returns the plan's JSON text for the caller.
async fn run_clean(canaries: &Canaries, scenario: &Scenario) -> String {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("tasks.db");
    std::fs::write(&path, &*SEEDED).expect("copy store");
    let name = scenario.name;
    let mut channels: Vec<(String, Vec<u8>)> = Vec::new();

    let mut request = scenario.request.clone();
    request.session = scenario
        .env
        .iter()
        .any(|(key, value)| *key == "SWARM_AGENT_SESSION" && value == "1");
    channels.push((
        "request Debug".to_string(),
        format!("{request:?}").into_bytes(),
    ));

    // The keys are also in this process's environment while the scenario runs, as they are in the real engine, so code that reads
    // the environment on its own (rather than through `KeyMaterial`) has a canary to leak. Only this test touches them.
    for (key, value) in &scenario.env {
        std::env::set_var(key, value);
    }
    let keys = KeyMaterial::from_lookup(lookup(&scenario.env), |_| KeyFilePresence::Absent);
    let mut shown = String::new();
    match keys {
        Err(declined) => {
            channels.push((
                "declined Display".to_string(),
                declined.to_string().into_bytes(),
            ));
            channels.push((
                "declined Debug".to_string(),
                format!("{declined:?}").into_bytes(),
            ));
            assert!(matches!(
                declined,
                Declined::KeyFileReference
                    | Declined::ConventionalKeyFile
                    | Declined::ConventionalKeyPath
            ));
            let refusal = WriterError::from(declined);
            assert_eq!(refusal.write_state(), WriteState::NotStarted, "{name}");
            channels.push((
                "declined WriterError".to_string(),
                format!("{refusal} {refusal:?}").into_bytes(),
            ));
        }
        Ok(keys) => {
            channels.push((
                "key material Debug".to_string(),
                format!("{keys:?}").into_bytes(),
            ));
            let store = open_for_write(&WriterConfig::new(&path))
                .await
                .expect("open store");
            let writer = TaskStoreWriter::new(store);
            let mut ids = SystemIds::open().expect("ids");
            let outcome = writer.issue_gate(&request, &keys, &mut ids).await;
            writer.close().await;
            match outcome {
                Ok(plan) => {
                    shown = to_python_dumps(&plan.to_json());
                    channels.push(("plan JSON".to_string(), shown.clone().into_bytes()));
                    channels.push(("plan Debug".to_string(), format!("{plan:?}").into_bytes()));
                    if let Some(file) = &plan.file {
                        channels
                            .push(("verdict file".to_string(), file.bytes.clone().into_bytes()));
                    }
                    if let Some(error) = &plan.error {
                        channels.push((
                            "refusal text".to_string(),
                            error.message().as_bytes().to_vec(),
                        ));
                        channels.push((
                            "refusal Debug".to_string(),
                            format!("{error:?}").into_bytes(),
                        ));
                    }
                }
                Err(error) => {
                    channels.push((
                        "writer error Display".to_string(),
                        error.to_string().into_bytes(),
                    ));
                    channels.push((
                        "writer error Debug".to_string(),
                        format!("{error:?}").into_bytes(),
                    ));
                }
            }
        }
    }
    for (key, _) in &scenario.env {
        std::env::remove_var(key);
    }
    channels.push((
        "store file".to_string(),
        std::fs::read(&path).expect("read store"),
    ));
    for (channel, data) in channels {
        canaries.require_clean(&data, &format!("{name}: {channel}"));
    }
    shown
}

fn canaries_for(values: &[&str]) -> Canaries {
    Canaries {
        needles: values
            .iter()
            .enumerate()
            .map(|(index, value)| (format!("value{index}"), forms(value)))
            .collect(),
    }
}

fn fresh_pair() -> (String, String) {
    let signing = random_hex(32);
    let mut seed = random_hex(32);
    while seed == signing {
        seed = random_hex(32);
    }
    (signing, seed)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_scanner_finds_every_form_of_a_canary() {
    // Teeth of the scanner: each form is detected where it sits in a larger blob, so a clean result below means something.
    let (signing, seed) = fresh_pair();
    let canaries = canaries_for(&[&signing, &seed]);
    for value in [&signing, &seed] {
        for (label, needle) in forms(value) {
            let mut blob = b"prefix ".to_vec();
            blob.extend_from_slice(&needle);
            blob.extend_from_slice(b" suffix");
            assert!(
                !canaries.leaks(&blob).is_empty(),
                "form {label} is not detected"
            );
        }
    }
    assert!(canaries.leaks(b"nothing to see").is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn no_canary_reaches_any_output_of_any_branch() {
    let (signing, seed) = fresh_pair();
    let (other_signing, other_seed) = fresh_pair();
    let shapes: Vec<(&'static str, String)> = vec![
        ("ed25519-nonhex", format!("{seed}zz")),
        ("ed25519-odd-length", seed[..seed.len() - 2].to_string()),
        ("ed25519-31-bytes", seed[..62].to_string()),
        ("ed25519-doubled", format!("{seed}{other_seed}")),
        ("ed25519-0x-prefix", format!("0x{seed}")),
        (
            "ed25519-spaced",
            seed.as_bytes()
                .chunks(2)
                .map(|c| std::str::from_utf8(c).expect("ascii"))
                .collect::<Vec<_>>()
                .join(" "),
        ),
        ("ed25519-quoted", format!("\"{seed}\"")),
    ];
    let mut all_values = vec![
        signing.clone(),
        seed.clone(),
        other_signing.clone(),
        other_seed.clone(),
    ];
    all_values.extend(shapes.iter().map(|(_, value)| value.clone()));
    let refs: Vec<&str> = all_values.iter().map(String::as_str).collect();
    let canaries = canaries_for(&refs);

    let blocking = vec![
        ("T-a".to_string(), vec![finding("blocker")]),
        ("T-b".to_string(), Vec::new()),
    ];
    let mut scenarios = vec![
        Scenario {
            name: "hmac only",
            env: vec![("SWARM_SIGNING_KEY", signing.clone())],
            request: request(Some("G1"), blocking.clone()),
        },
        Scenario {
            name: "ed25519 only",
            env: vec![("SWARM_ED25519_KEY", seed.clone())],
            request: request(Some("G1"), blocking.clone()),
        },
        Scenario {
            name: "both keys",
            env: vec![
                ("SWARM_SIGNING_KEY", signing.clone()),
                ("SWARM_ED25519_KEY", seed.clone()),
            ],
            request: request(Some("G1"), blocking.clone()),
        },
        Scenario {
            name: "no store",
            env: vec![("SWARM_SIGNING_KEY", signing.clone())],
            request: request(None, Vec::new()),
        },
        Scenario {
            name: "agent session with keys",
            env: vec![
                ("SWARM_SIGNING_KEY", signing.clone()),
                ("SWARM_AGENT_SESSION", "1".to_string()),
            ],
            request: request(Some("G1"), blocking.clone()),
        },
        Scenario {
            name: "dev key opt-in beside a real key",
            env: vec![
                ("SWARM_SIGNING_KEY", signing.clone()),
                ("SWARM_ALLOW_INSECURE_DEV_KEY", "1".to_string()),
            ],
            request: request(Some("G1"), blocking.clone()),
        },
        Scenario {
            name: "require key with a key",
            env: vec![
                ("SWARM_REQUIRE_KEY", "1".to_string()),
                ("SWARM_SIGNING_KEY", signing.clone()),
            ],
            request: request(Some("G1"), blocking.clone()),
        },
        Scenario {
            name: "foreign correlation refusal with keys",
            env: vec![
                ("SWARM_SIGNING_KEY", signing.clone()),
                ("SWARM_ED25519_KEY", seed.clone()),
            ],
            request: GateIssueRequest {
                correlation_id: Some("corr-other".to_string()),
                ..request(Some("G1"), Vec::new())
            },
        },
        Scenario {
            name: "a request the writer declines",
            env: vec![("SWARM_SIGNING_KEY", signing.clone())],
            request: GateIssueRequest {
                extra: vec![("verdict".to_string(), jstr("pass"))],
                ..request(Some("G1"), Vec::new())
            },
        },
        Scenario {
            name: "signing key beside an unloadable seed",
            env: vec![
                ("SWARM_SIGNING_KEY", signing.clone()),
                ("SWARM_ED25519_KEY", format!("{other_seed}zz")),
            ],
            request: request(Some("G1"), blocking.clone()),
        },
        Scenario {
            name: "key file reference",
            env: vec![(
                "SWARM_ED25519_KEY_FILE",
                format!("/nonexistent/{other_signing}/key.pem"),
            )],
            request: request(Some("G1"), blocking.clone()),
        },
        Scenario {
            name: "key file reference in an agent session",
            env: vec![
                (
                    "SWARM_ED25519_KEY_FILE",
                    format!("/nonexistent/{other_signing}/key.pem"),
                ),
                ("SWARM_AGENT_SESSION", "1".to_string()),
            ],
            request: request(Some("G1"), blocking.clone()),
        },
        Scenario {
            name: "keyless advisory",
            env: vec![],
            request: request(Some("G1"), blocking.clone()),
        },
        Scenario {
            name: "require key without a key",
            env: vec![("SWARM_REQUIRE_KEY", "1".to_string())],
            request: request(Some("G1"), blocking.clone()),
        },
    ];
    for (name, value) in &shapes {
        scenarios.push(Scenario {
            name,
            env: vec![("SWARM_ED25519_KEY", value.clone())],
            request: request(Some("G1"), blocking.clone()),
        });
        scenarios.push(Scenario {
            name,
            env: vec![
                ("SWARM_ED25519_KEY", value.clone()),
                ("SWARM_REQUIRE_KEY", "1".to_string()),
            ],
            request: request(None, Vec::new()),
        });
    }
    let mut signed = 0;
    for scenario in &scenarios {
        let shown = run_clean(&canaries, scenario).await;
        if shown.contains("\"sig\": \"hmac:") || shown.contains("\"sig\": \"ed25519:") {
            signed += 1;
        }
    }
    assert!(
        signed >= 4,
        "only {signed} scenarios signed: the branches that sign were not reached"
    );
}

#[test]
fn the_key_types_print_no_value() {
    let (signing, seed) = fresh_pair();
    let canaries = canaries_for(&[&signing, &seed]);
    let env = vec![
        ("SWARM_SIGNING_KEY", signing.clone()),
        ("SWARM_ED25519_KEY", seed.clone()),
    ];
    let keys = KeyMaterial::from_lookup(lookup(&env), |_| KeyFilePresence::Absent).expect("keys");
    canaries.require_clean(format!("{keys:?}").as_bytes(), "KeyMaterial Debug");
    canaries.require_clean(format!("{keys:#?}").as_bytes(), "KeyMaterial pretty Debug");
    let request = request(Some("G1"), Vec::new());
    canaries.require_clean(format!("{request:?}").as_bytes(), "request Debug");
}
