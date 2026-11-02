//! Mirror of the omp constants and tool schema fragments in `omp/src/tools.ts` and
//! `omp/src/bridge.ts`.
//!
//! Values are copied from `tests/omp_contract/freeze.md` §5 and §6 and replayed against the frozen
//! records in `tests/omp_contract/vectors.json` (see `VECTOR_IDS`). Schema pieces are built as
//! `serde_json::Value`, so the schemas sent to the model and the argv validators here share one
//! source of truth.

use serde_json::{json, Value};

/// Schema pattern for ids that become argv values and task-id file names (`tools.ts:18`).
///
/// The runtime value of omp's `ID_PATTERN` (`tools.ts:18`). The TypeScript literal `\\.` unescapes
/// to one backslash, so this is the pattern omp sends to the model.
pub const ID_PATTERN: &str = r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,127}$";
/// Registry tool-name pattern (`tools.ts:65`).
pub const TOOL_NAME_PATTERN_SOURCE: &str = r"^[a-z][a-z0-9_]{0,63}$";
/// Script-name pattern (`bridge.ts:58`).
pub const SCRIPT_NAME_PATTERN_SOURCE: &str = r"^[a-z][a-z0-9_]*$";

/// The 14 Task Store states (`tools.ts:22-25`, `swarm/taskstore.py` TaskState).
pub const STATES: &[&str] = &[
    "CREATED",
    "VALIDATED",
    "PLANNED",
    "CLAIMED",
    "IN_PROGRESS",
    "IN_REVIEW",
    "CHANGES_REQUESTED",
    "APPROVED",
    "DONE",
    "BLOCKED",
    "FAILED",
    "RETRY",
    "ESCALATED",
    "CANCELLED",
];

/// `orch_plan --pattern` choices (`tools.ts:29`).
pub const PATTERNS: &[&str] = &[
    "feature",
    "hotfix",
    "dependency",
    "cloud-native",
    "cluster",
    "web3",
    "parallel",
];

/// `orch_plan --risk-class` choices (`tools.ts:30`).
pub const RISK_CLASSES: &[&str] = &["low", "medium", "high"];

/// `orch_plan --priority` choices (`tools.ts:31`).
pub const PRIORITIES: &[&str] = &["P0", "P1", "P2", "P3"];

/// Gate name to the script that derives and signs its verdict (`tools.ts:33-35`).
pub const GATE_SCRIPTS: &[(&str, &str)] = &[
    ("quality", "qa_gate"),
    ("review", "rev_gate"),
    ("security", "sec_gate"),
    ("release", "rel_plan"),
];

/// Finding severities, lowest to highest (`tools.ts:41`).
pub const SEVERITIES: &[&str] = &["info", "minor", "major", "critical", "blocker"];

/// The severity that blocks a target. Documented in prose only (`tools.ts:40`); no code path
/// reads it.
pub const BLOCKING_SEVERITY: &str = "major";

/// `swarm_tool.py --kind` choices (`tools.ts:63`).
pub const CATALOG_KINDS: &[&str] = &["tools", "skills", "all"];

/// Every script a bridge call may run (`bridge.ts:15`).
pub const SCRIPT_UNION: &[&str] = &[
    "orch_plan",
    "orch_status",
    "orch_assign",
    "qa_gate",
    "rev_gate",
    "sec_gate",
    "rel_plan",
    "swarm_tool",
];

/// The seven wire error codes (`bridge.ts:16`, `ERROR_CODES` at `bridge.ts:18-20`), in source
/// order.
pub const ERROR_CODE_UNION: &[&str] = &[
    "E-INPUT",
    "E-TIMEOUT",
    "E-DEP",
    "E-CAPACITY",
    "E-CONTRACT",
    "E-POLICY",
    "E-INTERNAL",
];

/// Environment variables removed from the child before spawn (`bridge.ts:59`).
pub const STRIPPED_ENV: &[&str] = &["SWARM_TASK_ID", "SWARM_CORRELATION_ID"];

/// Bridge timeout when a caller gives none (`bridge.ts:55`).
pub const DEFAULT_TIMEOUT_MS: u64 = 600_000;
/// Grace between SIGTERM and SIGKILL for a timed-out process group (`bridge.ts:56`).
pub const KILL_GRACE_MS: u64 = 3_000;
/// Characters of stderr or stdout kept in error text (`bridge.ts:57`).
pub const TAIL_CHARS: usize = 400;
/// `qa_gate`'s own 1800 s `--timeout` plus a 60 s margin (`tools.ts:42-43`).
pub const QA_GATE_TIMEOUT_MS: u64 = (1800 + 60) * 1000;
/// Largest UTF-8 byte length of a `swarm_tool_call` input (`tools.ts:67`).
pub const MAX_TOOL_INPUT_BYTES: usize = 100_000;

/// Frozen vector records replayed by this module.
pub const VECTOR_IDS: &[&str] = &[
    "C-ID-PATTERN",
    "C-ID-SCHEMA-01",
    "C-ID-SCHEMA-02",
    "C-ID-SCHEMA-03",
    "C-STATES",
    "C-PATTERNS",
    "C-RISK-CLASSES",
    "C-PRIORITIES",
    "C-GATE-SCRIPTS-01",
    "C-GATE-SCRIPTS-02",
    "C-GATE-SCRIPTS-03",
    "C-GATE-SCRIPTS-04",
    "C-SEVERITIES",
    "C-BLOCKING-SEVERITY",
    "C-QA-GATE-TIMEOUT-MS-01",
    "C-QA-GATE-TIMEOUT-MS-02",
    "C-FINDING-SCHEMA-01",
    "C-FINDING-SCHEMA-02",
    "C-FINDING-SCHEMA-03",
    "C-FINDING-SCHEMA-04",
    "C-FINDING-SCHEMA-05",
    "C-FINDING-SCHEMA-06",
    "C-FINDING-SCHEMA-07",
    "C-FINDING-SCHEMA-08",
    "C-FINDING-SCHEMA-09",
    "C-FINDING-SCHEMA-10",
    "C-FINDING-SCHEMA-11",
    "C-CATALOG-KINDS",
    "C-TOOL-NAME-PATTERN",
    "C-MAX-TOOL-INPUT-BYTES",
    "C-SCRIPT-UNION",
    "C-ERROR-CODE-UNION",
    "C-ERROR-CODES-01",
    "C-ERROR-CODES-02",
    "C-ERROR-CODES-03",
    "C-ERROR-CODES-04",
    "C-ERROR-CODES-05",
    "C-ERROR-CODES-06",
    "C-ERROR-CODES-07",
    "C-DEFAULT-TIMEOUT-MS",
    "C-KILL-GRACE-MS",
    "C-TAIL",
    "C-SCRIPT-NAME",
    "C-STRIPPED-ENV",
];

/// Whether `id` is a valid id: 1 to 128 ASCII characters, first alphanumeric, rest
/// `[A-Za-z0-9._-]`, and no `..`.
///
/// Mirrors `ID_PATTERN` plus the `..` lookahead; the first character rule keeps ids from
/// starting with a dash.
pub fn is_valid_id(id: &str) -> bool {
    let bytes = id.as_bytes();
    match bytes.split_first() {
        Some((first, rest)) => {
            bytes.len() <= 128
                && first.is_ascii_alphanumeric()
                && rest
                    .iter()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
                && !id.contains("..")
        }
        None => false,
    }
}

/// Whether `name` is a registry tool name: 1 to 64 characters, lowercase letter first, then
/// lowercase, digit or `_`.
pub fn is_valid_tool_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    match bytes.split_first() {
        Some((first, rest)) => {
            bytes.len() <= 64
                && first.is_ascii_lowercase()
                && rest
                    .iter()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
        }
        None => false,
    }
}

/// Whether `script` is a valid script name: non-empty, lowercase letter first, then lowercase,
/// digit or `_`.
pub fn is_valid_script_name(script: &str) -> bool {
    let bytes = script.as_bytes();
    match bytes.split_first() {
        Some((first, rest)) => {
            first.is_ascii_lowercase()
                && rest
                    .iter()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
        }
        None => false,
    }
}

/// The script that runs `gate`, or `None` when the gate is unknown (`GATE_SCRIPTS`).
pub fn gate_script(gate: &str) -> Option<&'static str> {
    GATE_SCRIPTS
        .iter()
        .find(|(name, _)| *name == gate)
        .map(|(_, script)| *script)
}

/// Schema for one id field: string, `ID_PATTERN`, minimum length 1 (`tools.ts:19`).
pub fn id_schema() -> Value {
    json!({
        "type": "string",
        "pattern": ID_PATTERN,
        "minLength": 1,
    })
}

/// The finding object schema (`tools.ts:45-60`, freeze §5.7). Severities decide; there is no
/// verdict field.
pub fn finding_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "severity": {
                "type": "string",
                "enum": SEVERITIES,
                "description": "major, critical or blocker fails the target",
            },
            "summary": {"type": "string", "minLength": 1, "description": "What is wrong"},
            "id": {"type": "string"},
            "kind": {"type": "string", "description": "e.g. semantic, security, functional"},
            "evidence": {"type": "string"},
            "ac_ref": {
                "type": ["string", "null"],
                "description": "Acceptance criterion it violates",
            },
            "owner_suggestion": {
                "type": ["string", "null"],
                "description": "Agent that should fix it, e.g. A05",
            },
            "location": {"type": ["string", "null"], "description": "path:line"},
        },
        "required": ["severity", "summary"],
        "additionalProperties": false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract_vectors::{all, record, value};
    use gsd_codes::ErrorCode;

    /// The spelled `value` string of a fragment record.
    fn spelled(id: &str) -> String {
        record(id)["value"].as_str().unwrap().to_owned()
    }

    /// The numeric literal of a record, with `_` digit separators removed.
    fn numeric(id: &str) -> u64 {
        spelled(id).replace('_', "").parse().unwrap()
    }

    #[test]
    fn id_boundaries_follow_the_pattern_length_and_dotdot_rule() {
        assert!(is_valid_id("a"));
        assert!(is_valid_id(&"a".repeat(128)));
        assert!(!is_valid_id(&"a".repeat(129)));
        assert!(!is_valid_id(""));
        assert!(!is_valid_id("-leading"));
        assert!(!is_valid_id("a..b"));
        assert!(!is_valid_id("..a"));
        assert!(is_valid_id("a.b"));
        assert!(is_valid_id("a."));
        assert!(!is_valid_id("a b"));
        assert!(!is_valid_id("a/b"));
    }

    #[test]
    fn id_rejects_multibyte_characters_even_when_short() {
        assert!(!is_valid_id("é"));
        assert!(!is_valid_id("a€"));
    }

    #[test]
    fn tool_name_boundaries() {
        assert!(is_valid_tool_name(&format!("a{}", "b".repeat(63))));
        assert!(!is_valid_tool_name(&format!("a{}", "b".repeat(64))));
        assert!(!is_valid_tool_name("9lead"));
        assert!(!is_valid_tool_name("Upper"));
        assert!(!is_valid_tool_name("a-b"));
        assert!(!is_valid_tool_name(""));
        assert!(is_valid_tool_name("be_route_norm2"));
    }

    #[test]
    fn script_name_accepts_snake_case_and_rejects_others() {
        assert!(is_valid_script_name("orch_status"));
        assert!(!is_valid_script_name("Orch"));
        assert!(!is_valid_script_name("a-b"));
        assert!(!is_valid_script_name(""));
        assert!(!is_valid_script_name("_lead"));
    }

    #[test]
    fn gate_script_maps_each_gate_and_rejects_unknown() {
        assert_eq!(gate_script("quality"), Some("qa_gate"));
        assert_eq!(gate_script("release"), Some("rel_plan"));
        assert_eq!(gate_script("Quality"), None);
        assert_eq!(gate_script(""), None);
    }

    #[test]
    fn qa_gate_timeout_is_one_million_eight_hundred_sixty_thousand_ms() {
        assert_eq!(QA_GATE_TIMEOUT_MS, 1_860_000);
        assert_eq!(spelled("C-QA-GATE-TIMEOUT-MS-01"), "1800 + 60");
        assert_eq!(spelled("C-QA-GATE-TIMEOUT-MS-02"), "* 1000");
    }

    #[test]
    fn finding_schema_requires_severity_and_summary_and_forbids_extra_keys() {
        let schema = finding_schema();
        assert_eq!(schema["required"], json!(["severity", "summary"]));
        assert_eq!(schema["additionalProperties"], json!(false));
        assert_eq!(schema["properties"]["severity"]["enum"], json!(SEVERITIES));
        assert_eq!(schema["properties"]["summary"]["minLength"], json!(1));
    }

    #[test]
    fn finding_schema_nullable_fields_accept_null() {
        let schema = finding_schema();
        for field in ["ac_ref", "owner_suggestion", "location"] {
            assert_eq!(
                schema["properties"][field]["type"],
                json!(["string", "null"]),
                "{field}"
            );
        }
    }

    #[test]
    fn id_schema_carries_the_pattern_and_min_length() {
        let schema = id_schema();
        assert_eq!(schema["type"], json!("string"));
        assert_eq!(schema["pattern"], json!(ID_PATTERN));
        assert_eq!(schema["minLength"], json!(1));
    }

    #[test]
    fn error_code_union_matches_the_shared_error_codes() {
        let codes = [
            ErrorCode::Input,
            ErrorCode::Timeout,
            ErrorCode::Dep,
            ErrorCode::Capacity,
            ErrorCode::Contract,
            ErrorCode::Policy,
            ErrorCode::Internal,
        ];
        assert_eq!(codes.len(), ERROR_CODE_UNION.len());
        for code in codes {
            assert!(
                ERROR_CODE_UNION.contains(&code.as_str()),
                "{}",
                code.as_str()
            );
        }
    }

    #[test]
    fn timing_and_tail_constants_have_the_source_values() {
        assert_eq!(DEFAULT_TIMEOUT_MS, 600_000);
        assert_eq!(KILL_GRACE_MS, 3_000);
        assert_eq!(TAIL_CHARS, 400);
    }

    #[test]
    fn vector_ids_are_unique_and_present_in_the_frozen_records() {
        let mut seen = std::collections::BTreeSet::new();
        let present: std::collections::BTreeSet<String> = all()
            .iter()
            .filter_map(|r| r["id"].as_str().map(str::to_owned))
            .collect();
        for id in VECTOR_IDS {
            assert!(seen.insert(*id), "duplicate {id}");
            assert!(present.contains(*id), "{id} is not a frozen record");
        }
    }

    #[test]
    fn id_pattern_and_name_patterns_replay_their_records() {
        // The vector holds omp's TypeScript literal, so its `\\` pairs unescape to one backslash.
        let runtime = value("C-ID-PATTERN")
            .as_str()
            .unwrap()
            .replace("\\\\", "\\");
        assert_eq!(runtime, ID_PATTERN);
        assert_eq!(
            value("C-TOOL-NAME-PATTERN"),
            json!(TOOL_NAME_PATTERN_SOURCE)
        );
        assert_eq!(value("C-SCRIPT-NAME"), json!(SCRIPT_NAME_PATTERN_SOURCE));
    }

    #[test]
    fn id_schema_fragments_replay_against_the_mirror() {
        let schema = id_schema();
        assert_eq!(spelled("C-ID-SCHEMA-01"), r#"type: "string""#);
        assert_eq!(schema["type"], json!("string"));
        assert_eq!(spelled("C-ID-SCHEMA-02"), "pattern: ID_PATTERN");
        assert_eq!(schema["pattern"], json!(ID_PATTERN));
        assert_eq!(spelled("C-ID-SCHEMA-03"), "minLength: 1");
        assert_eq!(schema["minLength"], json!(1));
    }

    #[test]
    fn finding_schema_fragments_replay_against_the_mirror() {
        let schema = finding_schema();
        let props = &schema["properties"];
        assert_eq!(spelled("C-FINDING-SCHEMA-01"), r#"type: "object""#);
        assert_eq!(schema["type"], json!("object"));
        assert_eq!(
            spelled("C-FINDING-SCHEMA-02"),
            concat!(
                r#"severity: { type: "string", enum: SEVERITIES, "#,
                r#"description: "major, critical or blocker fails the target" }"#
            )
        );
        assert_eq!(props["severity"]["type"], json!("string"));
        assert_eq!(
            spelled("C-FINDING-SCHEMA-03"),
            r#"summary: { type: "string", minLength: 1, description: "What is wrong" }"#
        );
        assert_eq!(props["summary"]["minLength"], json!(1));
        assert_eq!(spelled("C-FINDING-SCHEMA-04"), r#"id: { type: "string" }"#);
        assert_eq!(props["id"]["type"], json!("string"));
        assert_eq!(
            spelled("C-FINDING-SCHEMA-05"),
            r#"kind: { type: "string", description: "e.g. semantic, security, functional" }"#
        );
        assert_eq!(
            props["kind"]["description"],
            json!("e.g. semantic, security, functional")
        );
        assert_eq!(
            spelled("C-FINDING-SCHEMA-06"),
            r#"evidence: { type: "string" }"#
        );
        assert_eq!(props["evidence"]["type"], json!("string"));
        assert_eq!(
            spelled("C-FINDING-SCHEMA-07"),
            concat!(
                r#"ac_ref: { type: ["string", "null"], "#,
                r#"description: "Acceptance criterion it violates" }"#
            )
        );
        assert_eq!(props["ac_ref"]["type"], json!(["string", "null"]));
        assert_eq!(
            spelled("C-FINDING-SCHEMA-08"),
            concat!(
                r#"owner_suggestion: { type: ["string", "null"], "#,
                r#"description: "Agent that should fix it, e.g. A05" }"#
            )
        );
        assert_eq!(
            props["owner_suggestion"]["description"],
            json!("Agent that should fix it, e.g. A05")
        );
        assert_eq!(
            spelled("C-FINDING-SCHEMA-09"),
            r#"location: { type: ["string", "null"], description: "path:line" }"#
        );
        assert_eq!(props["location"]["description"], json!("path:line"));
        assert_eq!(
            spelled("C-FINDING-SCHEMA-10"),
            r#"required: ["severity", "summary"]"#
        );
        assert_eq!(schema["required"], json!(["severity", "summary"]));
        assert_eq!(
            spelled("C-FINDING-SCHEMA-11"),
            "additionalProperties: false"
        );
        assert_eq!(schema["additionalProperties"], json!(false));
    }

    #[test]
    fn enumerated_constants_replay_their_records() {
        assert_eq!(value("C-STATES"), json!(STATES));
        assert_eq!(value("C-PATTERNS"), json!(PATTERNS));
        assert_eq!(value("C-RISK-CLASSES"), json!(RISK_CLASSES));
        assert_eq!(value("C-PRIORITIES"), json!(PRIORITIES));
        assert_eq!(value("C-SEVERITIES"), json!(SEVERITIES));
        assert_eq!(value("C-CATALOG-KINDS"), json!(CATALOG_KINDS));
        assert_eq!(value("C-SCRIPT-UNION"), json!(SCRIPT_UNION));
        assert_eq!(value("C-ERROR-CODE-UNION"), json!(ERROR_CODE_UNION));
        assert_eq!(value("C-STRIPPED-ENV"), json!(STRIPPED_ENV));
        assert_eq!(value("C-BLOCKING-SEVERITY"), json!(BLOCKING_SEVERITY));
    }

    #[test]
    fn gate_script_records_replay_each_pair_in_source_spelling() {
        for (index, id) in [
            "C-GATE-SCRIPTS-01",
            "C-GATE-SCRIPTS-02",
            "C-GATE-SCRIPTS-03",
            "C-GATE-SCRIPTS-04",
        ]
        .iter()
        .enumerate()
        {
            let (gate, script) = GATE_SCRIPTS[index];
            assert_eq!(spelled(id), format!("{gate}: \"{script}\""), "{id}");
        }
    }

    #[test]
    fn error_code_records_replay_the_union_in_order() {
        for (index, id) in [
            "C-ERROR-CODES-01",
            "C-ERROR-CODES-02",
            "C-ERROR-CODES-03",
            "C-ERROR-CODES-04",
            "C-ERROR-CODES-05",
            "C-ERROR-CODES-06",
            "C-ERROR-CODES-07",
        ]
        .iter()
        .enumerate()
        {
            let key = spelled(id).split('"').nth(1).unwrap().to_owned();
            assert_eq!(ERROR_CODE_UNION[index], key, "{id}");
        }
    }

    #[test]
    fn numeric_records_replay_the_timing_and_size_constants() {
        assert_eq!(numeric("C-DEFAULT-TIMEOUT-MS"), DEFAULT_TIMEOUT_MS);
        assert_eq!(numeric("C-KILL-GRACE-MS"), KILL_GRACE_MS);
        assert_eq!(numeric("C-TAIL"), TAIL_CHARS as u64);
        assert_eq!(
            numeric("C-MAX-TOOL-INPUT-BYTES"),
            MAX_TOOL_INPUT_BYTES as u64
        );
    }
}
