//! Surface pins: the omp identities, reason strings, flags, env names and tool names that
//! operators, the A01 dispatcher and the omp host read by spelling. Each constant mirrors one
//! `S-*` record of `tests/omp_contract/vectors.json`, documented in section 9 of
//! `tests/omp_contract/freeze.md`.
//!
//! The sources are `omp/src/guard.ts`, `hooks.ts`, `context.ts`, `commands.ts`, `index.ts`,
//! `tools.ts` and `swarm_bar.ts`. Nothing here runs omp code: the tests only replay the frozen
//! records against these constants.

/// The orchestrator slug (`guard.ts:14`), named in the task-depth-cap reason.
pub const ORCHESTRATOR: &str = "a01-orchestrator";

/// The 30 swarm agent slugs in `SWARM_SLUGS` order (`guard.ts:17-24`).
pub const SWARM_SLUGS: [&str; 30] = [
    "a01-orchestrator",
    "a02-requirements",
    "a03-architect",
    "a04-ux-designer",
    "a05-backend",
    "a06-frontend",
    "a07-data",
    "a08-qa",
    "a09-reviewer",
    "a10-security",
    "a11-devops",
    "a12-release",
    "a13-observability",
    "a14-maintenance",
    "a15-docs",
    "a16-cloud-architect",
    "a17-platform-engineer",
    "a18-supply-chain",
    "a19-event-driven",
    "a20-finops",
    "a21-cluster-ops",
    "a22-gitops",
    "a23-mesh-network",
    "a24-cluster-security",
    "a25-resilience",
    "a26-smart-contract",
    "a27-contract-audit",
    "a28-chain-infra",
    "a29-dapp-wallet",
    "a30-custody-compliance",
];

/// Refusal reason for swarm-state tools (`guard.ts:32`). Operators and the A01 dispatcher read it.
pub const SWARM_STATE_REASON: &str = "BLOCKED needs: human-approval (swarm-state)";

/// Refusal reason for swarm-assignment (`guard.ts:33`). Operators and the A01 dispatcher read it.
pub const SWARM_ASSIGNMENT_REASON: &str = "BLOCKED needs: human-approval (swarm-assignment)";

/// Prefix of the reason returned when the guard throws inside a swarm session (`guard.ts:35`).
/// index.ts appends the error message and `)`.
pub const GUARD_ERROR_PREFIX: &str = "BLOCKED needs: human-approval (guard error: ";

/// Rule id `assignment-script-shell` of the guard `RULES` table (`guard.ts:2930`).
pub const RULE_ASSIGNMENT_SCRIPT_SHELL: &str = "assignment-script-shell";

/// Rule id `orch-state-shell` of the guard `RULES` table (`guard.ts:2943`).
pub const RULE_ORCH_STATE_SHELL: &str = "orch-state-shell";

/// Rule id `signer-file-shell` of the guard `RULES` table (`guard.ts:2959`).
pub const RULE_SIGNER_FILE_SHELL: &str = "signer-file-shell";

/// Rule id `protected-path-shell` of the guard `RULES` table (`guard.ts:2981`).
pub const RULE_PROTECTED_PATH_SHELL: &str = "protected-path-shell";

/// Rule id `gate-script-shell` of the guard `RULES` table (`guard.ts:3012`).
pub const RULE_GATE_SCRIPT_SHELL: &str = "gate-script-shell";

/// Rule id `git-force-push` of the guard `RULES` table (`guard.ts:3062`).
pub const RULE_GIT_FORCE_PUSH: &str = "git-force-push";

/// Gate to the only agent allowed to run it, as `(gate, slug)` pairs (`context.ts:15`,
/// `GATE_AGENTS`).
pub const GATE_AGENTS: [(&str, &str); 4] = [
    ("quality", "a08-qa"),
    ("review", "a09-reviewer"),
    ("security", "a10-security"),
    ("release", "a12-release"),
];

/// Env var that names the calling agent in a headless `-p` session (`context.ts:33`).
pub const SWARM_AGENT_ENV: &str = "SWARM_AGENT";

/// Heading of the HOOK-01 context part (`hooks.ts:24`, the first line of `SWARM_CONTEXT`).
pub const SWARM_CONTEXT_HEADING: &str = "## AgentSwarm";

/// Heading of the runtime part (`hooks.ts:152`). A prior part that starts with this heading means
/// the runtime part is already present.
pub const RUNTIME_HEADING: &str = "## AgentSwarm runtime";

/// Label of the runtime part's root line (`hooks.ts:155`). The omp preamble reads that line.
pub const RUNTIME_ROOT_LABEL: &str = "Runtime root:";

/// Marker that tags the `/swarm` dispatch prompt so the HOOK-01 hook never re-steers it
/// (`hooks.ts:17`).
pub const DISPATCH_MARKER: &str = "[agent-swarm:dispatch]";

/// The `--pattern=` flag of `/swarm` (`commands.ts:16`, the usage line).
pub const SWARM_FLAG_PATTERN: &str = "--pattern=";

/// The `--risk=` flag of `/swarm` (`commands.ts:16`, the usage line).
pub const SWARM_FLAG_RISK: &str = "--risk=";

/// Name the extension registers as a command (`index.ts:39`).
pub const SWARM_COMMAND: &str = "swarm";

/// Registered tool `swarm_status` (`tools.ts:197`, built by `buildTools`, registered at
/// `index.ts:37`).
pub const TOOL_SWARM_STATUS: &str = "swarm_status";

/// Registered tool `swarm_assignment` (`tools.ts:220`).
pub const TOOL_SWARM_ASSIGNMENT: &str = "swarm_assignment";

/// Registered tool `swarm_plan` (`tools.ts:269`).
pub const TOOL_SWARM_PLAN: &str = "swarm_plan";

/// Registered tool `swarm_ingest` (`tools.ts:321`).
pub const TOOL_SWARM_INGEST: &str = "swarm_ingest";

/// Registered tool `swarm_transition` (`tools.ts:347`).
pub const TOOL_SWARM_TRANSITION: &str = "swarm_transition";

/// Registered tool `swarm_gate` (`tools.ts:374`).
pub const TOOL_SWARM_GATE: &str = "swarm_gate";

/// Registered tool `swarm_catalog` (`tools.ts:444`).
pub const TOOL_SWARM_CATALOG: &str = "swarm_catalog";

/// Registered tool `swarm_tool_call` (`tools.ts:473`).
pub const TOOL_SWARM_TOOL_CALL: &str = "swarm_tool_call";

/// Widget key the status bar mounts under (`swarm_bar.ts:31`).
pub const SWARM_BAR_KEY: &str = "agent-swarm";

/// Env var of the bar switch (`swarm_bar.ts:2567`).
pub const SWARM_BAR_ENV: &str = "SWARM_BAR";

/// The `SWARM_BAR` value that turns the bar off (`swarm_bar.ts:2567`).
pub const SWARM_BAR_OFF: &str = "off";

/// Env var of the linger time after a settled row, in seconds (`swarm_bar.ts:2839`).
pub const SWARM_BAR_LINGER_S_ENV: &str = "SWARM_BAR_LINGER_S";

/// Default linger time in seconds, used when `SWARM_BAR_LINGER_S` is not a positive number
/// (`swarm_bar.ts:2839`).
pub const SWARM_BAR_LINGER_S_DEFAULT: u64 = 20;

/// Env var of the linger time after a failed row, in seconds (`swarm_bar.ts:2840`).
pub const SWARM_BAR_FAIL_LINGER_S_ENV: &str = "SWARM_BAR_FAIL_LINGER_S";

/// Default failed-row linger time in seconds (`swarm_bar.ts:2840`).
pub const SWARM_BAR_FAIL_LINGER_S_DEFAULT: u64 = 600;

/// Env var that turns reduced motion on (`swarm_bar.ts:2842`).
pub const SWARM_BAR_REDUCED_MOTION_ENV: &str = "SWARM_BAR_REDUCED_MOTION";

/// The `SWARM_BAR_REDUCED_MOTION` value that turns reduced motion on (`swarm_bar.ts:2842`).
pub const SWARM_BAR_REDUCED_MOTION_ON: &str = "1";

/// Env var of the idle poll interval in ms (`swarm_bar.ts:2845`).
pub const SWARM_BAR_IDLE_POLL_MS_ENV: &str = "SWARM_BAR_IDLE_POLL_MS";

/// Default idle poll interval in ms (`swarm_bar.ts:2508`).
pub const IDLE_POLL_DEFAULT_MS: u64 = 2_000;

/// Lower clamp of the idle poll interval in ms (`swarm_bar.ts:2509`).
pub const IDLE_POLL_MIN_MS: u64 = 500;

/// Upper clamp of the idle poll interval in ms (`swarm_bar.ts:2510`).
pub const IDLE_POLL_MAX_MS: u64 = 30_000;

/// Every `S-*` record of `vectors.json`, in file order. The replay tests check that this list
/// matches the file.
pub const VECTOR_IDS: &[&str] = &[
    "S-ORCHESTRATOR",
    "S-SWARM-STATE-REASON",
    "S-SWARM-ASSIGNMENT-REASON",
    "S-GUARD-ERROR-PREFIX",
    "S-SWARM-SLUGS-01",
    "S-SWARM-SLUGS-02",
    "S-RULE-ID-01",
    "S-RULE-ID-02",
    "S-RULE-ID-03",
    "S-RULE-ID-04",
    "S-RULE-ID-05",
    "S-RULE-ID-06",
    "S-GATE-AGENTS-01",
    "S-GATE-AGENTS-02",
    "S-GATE-AGENTS-03",
    "S-GATE-AGENTS-04",
    "S-SWARM-AGENT-ENV",
    "S-RUNTIME-HEADING",
    "S-RUNTIME-ROOT-LABEL",
    "S-HOOK01-HEADING",
    "S-DISPATCH-MARKER",
    "S-SWARM-FLAG-PATTERN",
    "S-SWARM-FLAG-RISK",
    "S-COMMAND-SWARM",
    "S-TOOL-NAME-01",
    "S-TOOL-NAME-02",
    "S-TOOL-NAME-03",
    "S-TOOL-NAME-04",
    "S-TOOL-NAME-05",
    "S-TOOL-NAME-06",
    "S-TOOL-NAME-07",
    "S-TOOL-NAME-08",
    "S-BAR-WIDGET-KEY",
    "S-BAR-ENV-SWITCH",
    "S-BAR-OFF-VALUE",
    "S-BAR-LINGER-S-ENV",
    "S-BAR-LINGER-S-DEFAULT",
    "S-BAR-FAIL-LINGER-S-ENV",
    "S-BAR-FAIL-LINGER-S-DEFAULT",
    "S-BAR-REDUCED-MOTION-ENV",
    "S-BAR-REDUCED-MOTION-ON",
    "S-BAR-IDLE-POLL-ENV",
    "S-BAR-IDLE-POLL-DEFAULT",
    "S-BAR-IDLE-POLL-MIN",
    "S-BAR-IDLE-POLL-MAX",
];

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contract_vectors;
    use std::collections::BTreeSet;

    /// The string value of a record. A missing or non-string value is a wiring error.
    fn text(id: &str) -> String {
        contract_vectors::value(id)
            .as_str()
            .unwrap_or_else(|| panic!("{id} is not a string record"))
            .to_owned()
    }

    /// The list value of a record, each item a string.
    fn items(id: &str) -> Vec<String> {
        contract_vectors::value(id)
            .as_array()
            .unwrap_or_else(|| panic!("{id} is not a list record"))
            .iter()
            .map(|item| {
                item.as_str()
                    .unwrap_or_else(|| panic!("{id} has a non-string item"))
                    .to_owned()
            })
            .collect()
    }

    /// The numeric value of a record. The source spelling keeps underscores (`30_000`), so they
    /// are removed first.
    fn number(id: &str) -> u64 {
        text(id)
            .replace('_', "")
            .parse()
            .unwrap_or_else(|err| panic!("{id} is not a number: {err}"))
    }

    /// String records and the constants they pin.
    fn text_pins() -> Vec<(&'static str, &'static str)> {
        vec![
            ("S-ORCHESTRATOR", ORCHESTRATOR),
            ("S-SWARM-STATE-REASON", SWARM_STATE_REASON),
            ("S-SWARM-ASSIGNMENT-REASON", SWARM_ASSIGNMENT_REASON),
            ("S-GUARD-ERROR-PREFIX", GUARD_ERROR_PREFIX),
            ("S-RULE-ID-01", RULE_ASSIGNMENT_SCRIPT_SHELL),
            ("S-RULE-ID-02", RULE_ORCH_STATE_SHELL),
            ("S-RULE-ID-03", RULE_SIGNER_FILE_SHELL),
            ("S-RULE-ID-04", RULE_PROTECTED_PATH_SHELL),
            ("S-RULE-ID-05", RULE_GATE_SCRIPT_SHELL),
            ("S-RULE-ID-06", RULE_GIT_FORCE_PUSH),
            ("S-SWARM-AGENT-ENV", SWARM_AGENT_ENV),
            ("S-RUNTIME-HEADING", RUNTIME_HEADING),
            ("S-RUNTIME-ROOT-LABEL", RUNTIME_ROOT_LABEL),
            ("S-HOOK01-HEADING", SWARM_CONTEXT_HEADING),
            ("S-DISPATCH-MARKER", DISPATCH_MARKER),
            ("S-SWARM-FLAG-PATTERN", SWARM_FLAG_PATTERN),
            ("S-SWARM-FLAG-RISK", SWARM_FLAG_RISK),
            ("S-COMMAND-SWARM", SWARM_COMMAND),
            ("S-TOOL-NAME-01", TOOL_SWARM_STATUS),
            ("S-TOOL-NAME-02", TOOL_SWARM_ASSIGNMENT),
            ("S-TOOL-NAME-03", TOOL_SWARM_PLAN),
            ("S-TOOL-NAME-04", TOOL_SWARM_INGEST),
            ("S-TOOL-NAME-05", TOOL_SWARM_TRANSITION),
            ("S-TOOL-NAME-06", TOOL_SWARM_GATE),
            ("S-TOOL-NAME-07", TOOL_SWARM_CATALOG),
            ("S-TOOL-NAME-08", TOOL_SWARM_TOOL_CALL),
            ("S-BAR-WIDGET-KEY", SWARM_BAR_KEY),
            ("S-BAR-ENV-SWITCH", SWARM_BAR_ENV),
            ("S-BAR-OFF-VALUE", SWARM_BAR_OFF),
            ("S-BAR-LINGER-S-ENV", SWARM_BAR_LINGER_S_ENV),
            ("S-BAR-FAIL-LINGER-S-ENV", SWARM_BAR_FAIL_LINGER_S_ENV),
            ("S-BAR-REDUCED-MOTION-ENV", SWARM_BAR_REDUCED_MOTION_ENV),
            ("S-BAR-REDUCED-MOTION-ON", SWARM_BAR_REDUCED_MOTION_ON),
            ("S-BAR-IDLE-POLL-ENV", SWARM_BAR_IDLE_POLL_MS_ENV),
        ]
    }

    /// List records and the constant slices or pairs they pin. The slug split matches the two
    /// records.
    fn list_pins() -> Vec<(&'static str, Vec<&'static str>)> {
        vec![
            ("S-SWARM-SLUGS-01", SWARM_SLUGS[..20].to_vec()),
            ("S-SWARM-SLUGS-02", SWARM_SLUGS[20..].to_vec()),
            ("S-GATE-AGENTS-01", vec![GATE_AGENTS[0].0, GATE_AGENTS[0].1]),
            ("S-GATE-AGENTS-02", vec![GATE_AGENTS[1].0, GATE_AGENTS[1].1]),
            ("S-GATE-AGENTS-03", vec![GATE_AGENTS[2].0, GATE_AGENTS[2].1]),
            ("S-GATE-AGENTS-04", vec![GATE_AGENTS[3].0, GATE_AGENTS[3].1]),
        ]
    }

    /// Numeric records and the constants they pin.
    fn number_pins() -> Vec<(&'static str, u64)> {
        vec![
            ("S-BAR-LINGER-S-DEFAULT", SWARM_BAR_LINGER_S_DEFAULT),
            (
                "S-BAR-FAIL-LINGER-S-DEFAULT",
                SWARM_BAR_FAIL_LINGER_S_DEFAULT,
            ),
            ("S-BAR-IDLE-POLL-DEFAULT", IDLE_POLL_DEFAULT_MS),
            ("S-BAR-IDLE-POLL-MIN", IDLE_POLL_MIN_MS),
            ("S-BAR-IDLE-POLL-MAX", IDLE_POLL_MAX_MS),
        ]
    }

    #[test]
    fn string_pins_match_their_vector_records() {
        for (id, pinned) in text_pins() {
            assert_eq!(text(id), pinned, "{id}");
        }
    }

    #[test]
    fn list_pins_match_their_vector_records() {
        for (id, pinned) in list_pins() {
            assert_eq!(items(id), pinned, "{id}");
        }
    }

    #[test]
    fn number_pins_match_their_vector_records() {
        for (id, pinned) in number_pins() {
            assert_eq!(number(id), pinned, "{id}");
        }
    }

    #[test]
    fn every_surface_record_is_replayed_by_exactly_one_pin() {
        let mut replayed: Vec<&str> = text_pins()
            .iter()
            .map(|(id, _)| *id)
            .chain(list_pins().iter().map(|(id, _)| *id))
            .chain(number_pins().iter().map(|(id, _)| *id))
            .collect();
        replayed.sort_unstable();
        let mut listed = VECTOR_IDS.to_vec();
        listed.sort_unstable();
        assert_eq!(replayed, listed);
    }

    #[test]
    fn vector_ids_follow_the_file_order_and_are_unique() {
        let file: Vec<String> = contract_vectors::all()
            .iter()
            .filter_map(|record| record["id"].as_str())
            .filter(|id| id.starts_with("S-"))
            .map(str::to_owned)
            .collect();
        let listed: Vec<String> = VECTOR_IDS.iter().map(|id| (*id).to_owned()).collect();
        assert_eq!(listed, file);
        let unique: BTreeSet<&str> = VECTOR_IDS.iter().copied().collect();
        assert_eq!(unique.len(), VECTOR_IDS.len());
    }
}
