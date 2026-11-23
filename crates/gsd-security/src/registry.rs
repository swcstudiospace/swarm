//! Tool registry: typed tool contracts, registration and discovery.
//!
//! Mirrors the Python contract in `swarm/tool_registry.py`
//! (`REQUIRED_FIELDS`, `PREFIX`, `TIERS`, `call_tool`) without parsing
//! strings at dispatch time: names, owners, tiers and retry classes are
//! typed values, and every failure is a typed [`RegistryError`] variant.

use std::collections::HashMap;
use std::time::Duration;

/// Stable owner prefix for a tool name, mirroring `PREFIX` in
/// `swarm/tool_registry.py`. `None` for an unknown owner (never guessed).
pub fn prefix_for_owner(owner: &str) -> Option<&'static str> {
    Some(match owner {
        "A01" => "orch_",
        "A02" => "req_",
        "A03" => "arch_",
        "A04" => "ux_",
        "A05" => "be_",
        "A06" => "fe_",
        "A07" => "data_",
        "A08" => "qa_",
        "A09" => "rev_",
        "A10" => "sec_",
        "A11" => "devops_",
        "A12" => "rel_",
        "A13" => "obs_",
        "A14" => "maint_",
        "A15" => "docs_",
        "A16" => "cloud_",
        "A17" => "plat_",
        "A18" => "supply_",
        "A19" => "event_",
        "A20" => "finops_",
        "A21" => "k8s_",
        "A22" => "gitops_",
        "A23" => "mesh_",
        "A24" => "kpol_",
        "A25" => "resil_",
        "A26" => "sol_",
        "A27" => "audit_",
        "A28" => "chain_",
        "A29" => "dapp_",
        "A30" => "custody_",
        "shared" => "shared_",
        _ => return None,
    })
}

/// Whether `owner` is a known agent id or the `shared` pool.
pub fn is_known_owner(owner: &str) -> bool {
    prefix_for_owner(owner).is_some()
}

/// Whether `name` is a conventional tool name: first char lowercase,
/// all chars lowercase/digit/underscore (mirrors `_conventional_name`).
pub fn is_conventional_name(name: &str) -> bool {
    !name.is_empty()
        && name.chars().next().is_some_and(|c| c.is_ascii_lowercase())
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// Owner scope of a tool: a unique tool belongs to one agent, a shared
/// tool belongs to the shared pool and may be called by any agent.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum OwnerScope {
    /// Owned by exactly one agent; cross-agent calls are rejected.
    Unique,
    /// Shared pool tool; any agent may call it (rate-limited at dispatch).
    Shared,
}

/// Side-effect class of a tool, mirroring `TIERS`/`ALLOWED_TIERS`.
/// Only `READ` and `WRITE` may be registered (v2.2 rule).
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum SideEffectClass {
    Read,
    Write,
}

impl SideEffectClass {
    /// Wire name used in schemas/logs.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Read => "READ",
            Self::Write => "WRITE",
        }
    }
}

/// Idempotency classification of a tool. Retry is allowed **only** for
/// [`Idempotency::Idempotent`]; `Unclassified` (the default) never retries.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum Idempotency {
    /// Safe to retry: the same call repeated has the same effect.
    Idempotent,
    /// May be delivered more than once; not auto-retried.
    AtLeastOnce,
    /// Unsafe to retry (non-idempotent mutation); never auto-retried.
    Unsafe,
    /// No classification supplied; never auto-retried.
    Unclassified,
}

/// Retry policy for one tool. Exponential backoff from `base_delay`,
/// capped at `max_delay`, no jitter (conservative caps).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct RetryPolicy {
    /// Maximum attempts including the first.
    pub max_attempts: u32,
    /// Delay before the first retry.
    pub base_delay: Duration,
    /// Upper bound on any single retry delay.
    pub max_delay: Duration,
    /// Idempotency classification gating every retry.
    pub idempotency: Idempotency,
}

impl RetryPolicy {
    /// Conservative default: unclassified, so no retry ever happens.
    pub fn unclassified() -> Self {
        Self {
            max_attempts: 1,
            base_delay: Duration::from_millis(100),
            max_delay: Duration::from_secs(5),
            idempotency: Idempotency::Unclassified,
        }
    }

    /// An idempotent policy that may retry up to `max_attempts` times.
    pub fn idempotent(max_attempts: u32, base_delay: Duration, max_delay: Duration) -> Self {
        Self {
            max_attempts: max_attempts.max(1),
            base_delay,
            max_delay,
            idempotency: Idempotency::Idempotent,
        }
    }

    /// Whether this policy may retry at all (idempotent-only rule).
    pub fn allows_retry(&self) -> bool {
        self.idempotency == Idempotency::Idempotent && self.max_attempts > 1
    }

    /// Attempts actually permitted: 1 unless idempotent.
    pub fn effective_max_attempts(&self) -> u32 {
        if self.allows_retry() {
            self.max_attempts
        } else {
            1
        }
    }

    /// Backoff before retry number `retry` (1-based): `base * 2^(retry-1)`,
    /// capped at `max_delay`. No jitter.
    pub fn backoff(&self, retry: u32) -> Duration {
        let shift = retry.saturating_sub(1).min(16);
        let scaled = self
            .base_delay
            .checked_mul(1u32 << shift)
            .unwrap_or(self.max_delay);
        scaled.min(self.max_delay)
    }
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self::unclassified()
    }
}

/// Per-family legacy fallback: when enabled, a failed or missing native
/// handler routes to the Python legacy path. The Python path defaults to
/// `swarm.tool_registry.call_tool` (the `call_tool` contract).
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct LegacyFallback {
    /// Whether fallback to the legacy path is enabled for this tool/family.
    pub enabled: bool,
    /// Python entry point for the legacy path.
    pub python_path: String,
}

impl Default for LegacyFallback {
    fn default() -> Self {
        Self {
            enabled: true,
            python_path: "swarm.tool_registry.call_tool".to_string(),
        }
    }
}

/// One registered tool contract.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Tool {
    /// Stable tool name, e.g. `kpol_pss_check`. Never changes once registered.
    pub name: String,
    /// Owning agent id (`A01`..`A30`) or `shared`.
    pub owner: String,
    /// Unique (single-agent) or shared-pool scope.
    pub scope: OwnerScope,
    /// Capabilities this tool serves, e.g. `kpol.admission`. Used by discovery.
    pub capabilities: Vec<String>,
    /// Tool family, e.g. `kpol`. Used for per-family legacy fallback flags.
    pub family: String,
    /// JSON-schema object for the input payload (`type: "object"`).
    pub input_schema: serde_json::Value,
    /// JSON-schema object for the output payload (`type: "object"`).
    pub output_schema: serde_json::Value,
    /// Side-effect class; only READ/WRITE are registerable.
    pub side_effect_class: SideEffectClass,
    /// Per-call timeout; must be in `(0, 3600]` seconds.
    pub timeout: Duration,
    /// Retry policy; unclassified means no retry.
    pub retry_policy: RetryPolicy,
    /// Per-family legacy fallback flag (Python legacy path by default).
    pub legacy_fallback: LegacyFallback,
    /// Extra callers allowed for a unique tool beyond its owner.
    pub permitted_callers: Vec<String>,
}

impl Tool {
    /// A tool with empty object schemas, READ tier, 5 s timeout,
    /// unclassified (no-retry) policy and legacy fallback on.
    pub fn new(name: impl Into<String>, owner: impl Into<String>, scope: OwnerScope) -> Self {
        Self {
            name: name.into(),
            owner: owner.into(),
            scope,
            capabilities: Vec::new(),
            family: String::new(),
            input_schema: serde_json::json!({"type": "object"}),
            output_schema: serde_json::json!({"type": "object"}),
            side_effect_class: SideEffectClass::Read,
            timeout: Duration::from_secs(5),
            retry_policy: RetryPolicy::default(),
            legacy_fallback: LegacyFallback::default(),
            permitted_callers: Vec::new(),
        }
    }

    /// Whether `caller` may call this tool (unique-vs-shared authorization).
    /// Shared tools accept any non-empty caller; unique tools accept the
    /// owner plus explicitly permitted callers.
    pub fn allows_caller(&self, caller: &str) -> bool {
        if caller.is_empty() {
            return false;
        }
        match self.scope {
            OwnerScope::Shared => true,
            OwnerScope::Unique => {
                caller == self.owner || self.permitted_callers.iter().any(|c| c == caller)
            }
        }
    }
}

/// Typed registration failures. No string parsing is required by callers.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum RegistryError {
    #[error("duplicate tool {name:?}: already registered by owner {existing_owner:?}")]
    DuplicateTool {
        name: String,
        existing_owner: String,
    },
    #[error("schema collision for tool {name:?}: input/output schema differs from the registered contract")]
    SchemaCollision { name: String },
    #[error("invalid tool name {name:?}: {reason}")]
    InvalidName { name: String, reason: String },
    #[error("invalid owner {owner:?} for tool {name:?}: owner must be A01..A30 or shared")]
    InvalidOwner { name: String, owner: String },
    #[error("invalid {field} schema for tool {name:?}: {reason}")]
    InvalidSchema {
        name: String,
        field: &'static str,
        reason: String,
    },
    #[error("invalid timeout for tool {name:?}: timeout_s must be in (0, 3600], got {timeout_s}s")]
    InvalidTimeout { name: String, timeout_s: f64 },
}

fn validate_schema(
    name: &str,
    field: &'static str,
    schema: &serde_json::Value,
) -> Result<(), RegistryError> {
    let obj = schema
        .as_object()
        .ok_or_else(|| RegistryError::InvalidSchema {
            name: name.to_string(),
            field,
            reason: "schema must be a JSON object".to_string(),
        })?;
    match obj.get("type").and_then(|t| t.as_str()) {
        Some("object") => Ok(()),
        other => Err(RegistryError::InvalidSchema {
            name: name.to_string(),
            field,
            reason: format!("schema type must be \"object\", got {other:?}"),
        }),
    }
}

/// The tool registry. Registration is fail-fast: duplicates and schema
/// collisions are rejected instead of overwriting an existing contract.
#[derive(Debug, Default)]
pub struct Registry {
    tools: HashMap<String, Tool>,
}

impl Registry {
    /// An empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Register one tool. Fails fast on duplicate names, schema
    /// collisions, bad names/owners/schemas/timeouts.
    pub fn register(&mut self, tool: Tool) -> Result<(), RegistryError> {
        if !is_known_owner(&tool.owner) {
            return Err(RegistryError::InvalidOwner {
                name: tool.name.clone(),
                owner: tool.owner.clone(),
            });
        }
        if !is_conventional_name(&tool.name) {
            return Err(RegistryError::InvalidName {
                name: tool.name.clone(),
                reason: "name must start lowercase and contain only [a-z0-9_]".to_string(),
            });
        }
        let prefix = prefix_for_owner(&tool.owner).expect("owner checked above");
        if !tool.name.starts_with(prefix) {
            return Err(RegistryError::InvalidName {
                name: tool.name.clone(),
                reason: format!("name for owner {} must start with {prefix:?}", tool.owner),
            });
        }
        validate_schema(&tool.name, "input", &tool.input_schema)?;
        validate_schema(&tool.name, "output", &tool.output_schema)?;
        let timeout_s = tool.timeout.as_secs_f64();
        if !(timeout_s > 0.0 && timeout_s <= 3600.0) {
            return Err(RegistryError::InvalidTimeout {
                name: tool.name.clone(),
                timeout_s,
            });
        }
        if let Some(existing) = self.tools.get(&tool.name) {
            let same_schema = existing.input_schema == tool.input_schema
                && existing.output_schema == tool.output_schema;
            return Err(if same_schema {
                RegistryError::DuplicateTool {
                    name: tool.name.clone(),
                    existing_owner: existing.owner.clone(),
                }
            } else {
                RegistryError::SchemaCollision {
                    name: tool.name.clone(),
                }
            });
        }
        self.tools.insert(tool.name.clone(), tool);
        Ok(())
    }

    /// The tool registered under `name`, if any.
    pub fn get(&self, name: &str) -> Option<&Tool> {
        self.tools.get(name)
    }

    /// Number of registered tools.
    pub fn len(&self) -> usize {
        self.tools.len()
    }

    /// Whether no tools are registered.
    pub fn is_empty(&self) -> bool {
        self.tools.is_empty()
    }

    /// All registered tool names, sorted.
    pub fn names(&self) -> Vec<String> {
        let mut names: Vec<String> = self.tools.keys().cloned().collect();
        names.sort();
        names
    }

    fn sorted<'a>(&self, mut tools: Vec<&'a Tool>) -> Vec<&'a Tool> {
        tools.sort_by(|a, b| a.name.cmp(&b.name));
        tools
    }

    /// Discovery: tools serving `capability`, sorted by name.
    pub fn tools_for_capability(&self, capability: &str) -> Vec<&Tool> {
        self.sorted(
            self.tools
                .values()
                .filter(|t| t.capabilities.iter().any(|c| c == capability))
                .collect(),
        )
    }

    /// Discovery: tools owned by `agent` plus every shared tool, sorted.
    pub fn tools_for_agent(&self, agent: &str) -> Vec<&Tool> {
        self.sorted(
            self.tools
                .values()
                .filter(|t| t.owner == agent || t.scope == OwnerScope::Shared)
                .collect(),
        )
    }

    /// Discovery: tools in `family`, sorted by name.
    pub fn tools_for_family(&self, family: &str) -> Vec<&Tool> {
        self.sorted(self.tools.values().filter(|t| t.family == family).collect())
    }

    /// Discovery: tools owned by `owner`, sorted by name.
    pub fn tools_for_owner(&self, owner: &str) -> Vec<&Tool> {
        self.sorted(self.tools.values().filter(|t| t.owner == owner).collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tool(name: &str, owner: &str, scope: OwnerScope) -> Tool {
        Tool::new(name, owner, scope)
    }

    #[test]
    fn duplicate_rejected() {
        let mut reg = Registry::new();
        reg.register(tool("kpol_pss_check", "A24", OwnerScope::Unique))
            .unwrap();
        let err = reg
            .register(tool("kpol_pss_check", "A24", OwnerScope::Unique))
            .unwrap_err();
        assert!(
            matches!(err, RegistryError::DuplicateTool { .. }),
            "got {err:?}"
        );
        assert_eq!(reg.len(), 1);
    }

    #[test]
    fn schema_collision_rejected() {
        let mut reg = Registry::new();
        reg.register(tool("kpol_pss_check", "A24", OwnerScope::Unique))
            .unwrap();
        let mut other = tool("kpol_pss_check", "A24", OwnerScope::Unique);
        other.input_schema = serde_json::json!({"type": "object", "required": ["x"]});
        let err = reg.register(other).unwrap_err();
        assert!(
            matches!(err, RegistryError::SchemaCollision { .. }),
            "got {err:?}"
        );
    }

    #[test]
    fn bad_prefix_and_schema_rejected() {
        let mut reg = Registry::new();
        assert!(matches!(
            reg.register(tool("be_wrong", "A24", OwnerScope::Unique)),
            Err(RegistryError::InvalidName { .. })
        ));
        let mut bad = tool("kpol_x", "A24", OwnerScope::Unique);
        bad.input_schema = serde_json::json!({"type": "array"});
        assert!(matches!(
            reg.register(bad),
            Err(RegistryError::InvalidSchema { field: "input", .. })
        ));
        assert!(matches!(
            reg.register(tool("kpol_y", "nobody", OwnerScope::Unique)),
            Err(RegistryError::InvalidOwner { .. })
        ));
    }

    #[test]
    fn discovery_per_capability() {
        let mut reg = Registry::new();
        let mut a = tool("kpol_pss_check", "A24", OwnerScope::Unique);
        a.capabilities = vec!["kpol.admission".to_string()];
        a.family = "kpol".to_string();
        let mut b = tool("kpol_rbac_risk_scan", "A24", OwnerScope::Unique);
        b.capabilities = vec!["kpol.rbac".to_string()];
        b.family = "kpol".to_string();
        let mut s = tool("shared_clock", "shared", OwnerScope::Shared);
        s.capabilities = vec!["kpol.admission".to_string()];
        reg.register(a).unwrap();
        reg.register(b).unwrap();
        reg.register(s).unwrap();
        let names: Vec<&str> = reg
            .tools_for_capability("kpol.admission")
            .iter()
            .map(|t| t.name.as_str())
            .collect();
        assert_eq!(names, ["kpol_pss_check", "shared_clock"]);
        assert_eq!(reg.tools_for_family("kpol").len(), 2);
        // Agent discovery: owner tools + shared, never another agent's unique tools.
        let agent_names: Vec<&str> = reg
            .tools_for_agent("A24")
            .iter()
            .map(|t| t.name.as_str())
            .collect();
        assert_eq!(
            agent_names,
            ["kpol_pss_check", "kpol_rbac_risk_scan", "shared_clock"]
        );
        assert_eq!(reg.tools_for_agent("A05").len(), 1);
    }

    #[test]
    fn unclassified_never_retries_and_backoff_caps() {
        let unclassified = RetryPolicy::default();
        assert!(!unclassified.allows_retry());
        assert_eq!(unclassified.effective_max_attempts(), 1);
        let idem =
            RetryPolicy::idempotent(4, Duration::from_millis(100), Duration::from_millis(250));
        assert!(idem.allows_retry());
        assert_eq!(idem.backoff(1), Duration::from_millis(100));
        assert_eq!(idem.backoff(2), Duration::from_millis(200));
        assert_eq!(idem.backoff(3), Duration::from_millis(250));
        assert_eq!(idem.backoff(99), Duration::from_millis(250));
    }

    #[test]
    fn legacy_default_is_python_path() {
        let t = tool("kpol_pss_check", "A24", OwnerScope::Unique);
        assert!(t.legacy_fallback.enabled);
        assert_eq!(
            t.legacy_fallback.python_path,
            "swarm.tool_registry.call_tool"
        );
    }
}
