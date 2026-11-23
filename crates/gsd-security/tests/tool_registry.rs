//! Tool-registry acceptance tests (SPE-37336).
//!
//! Verifies the Rust registry in `crates/gsd-security/src/registry.rs`
//! (plus `registry_dispatch.rs`) against the Python contract in
//! `swarm/tool_registry.py`: per-agent unique + shared resolution for all
//! 30 subagents, stable (sorted) discovery order, read-only discovery,
//! duplicate/shadowing rejection at registration, pre-exec
//! (`Tool::allows_caller`) and Rust-exec (`Dispatcher::dispatch`) scope
//! enforcement — a cross-subagent call to a unique tool is denied — and
//! shared tools registered exactly once.

use gsd_security::registry::{
    is_known_owner, prefix_for_owner, OwnerScope, Registry, RegistryError, Tool,
};
use gsd_security::registry_dispatch::{AuditKind, DispatchError, Dispatcher, Route};
use serde_json::{json, Value};

/// All 30 agent ids with their expected tool-name prefix, mirroring
/// `PREFIX` in `swarm/tool_registry.py` (plus `shared`).
const AGENTS: [(&str, &str); 30] = [
    ("A01", "orch_"),
    ("A02", "req_"),
    ("A03", "arch_"),
    ("A04", "ux_"),
    ("A05", "be_"),
    ("A06", "fe_"),
    ("A07", "data_"),
    ("A08", "qa_"),
    ("A09", "rev_"),
    ("A10", "sec_"),
    ("A11", "devops_"),
    ("A12", "rel_"),
    ("A13", "obs_"),
    ("A14", "maint_"),
    ("A15", "docs_"),
    ("A16", "cloud_"),
    ("A17", "plat_"),
    ("A18", "supply_"),
    ("A19", "event_"),
    ("A20", "finops_"),
    ("A21", "k8s_"),
    ("A22", "gitops_"),
    ("A23", "mesh_"),
    ("A24", "kpol_"),
    ("A25", "resil_"),
    ("A26", "sol_"),
    ("A27", "audit_"),
    ("A28", "chain_"),
    ("A29", "dapp_"),
    ("A30", "custody_"),
];

const SHARED_NAME: &str = "shared_probe_tool";

fn unique_tool(agent: &str) -> Tool {
    let prefix = prefix_for_owner(agent).expect("test uses known agents");
    Tool::new(format!("{prefix}probe_tool"), agent, OwnerScope::Unique)
}

fn shared_tool() -> Tool {
    Tool::new(SHARED_NAME, "shared", OwnerScope::Shared)
}

/// Registry with one unique tool per agent (A01..A30) plus one shared
/// tool, registered shared-first and uniques in reverse agent order so
/// discovery results cannot accidentally depend on insertion order.
fn registry_with_all() -> Registry {
    let mut reg = Registry::new();
    reg.register(shared_tool()).unwrap();
    for (agent, _) in AGENTS.iter().rev() {
        reg.register(unique_tool(agent)).unwrap();
    }
    reg
}

fn ok_handler(payload: Value) -> Box<dyn Fn(&Value) -> Result<Value, DispatchError> + Send + Sync> {
    Box::new(move |_p: &Value| Ok(payload.clone()))
}

#[test]
fn all_thirty_agents_resolve_own_unique_plus_shared() {
    let reg = registry_with_all();
    assert_eq!(reg.len(), 31, "30 unique + 1 shared");

    for (agent, prefix) in AGENTS {
        assert!(is_known_owner(agent), "{agent} must be a known owner");
        assert_eq!(prefix_for_owner(agent), Some(prefix));

        let own_name = format!("{prefix}probe_tool");
        // Direct lookup resolves the unique tool.
        let tool = reg
            .get(&own_name)
            .unwrap_or_else(|| panic!("{own_name} missing"));
        assert_eq!(tool.owner, agent);
        assert_eq!(tool.scope, OwnerScope::Unique);

        // Per-agent resolution: own unique + the shared tool, and never
        // another agent's unique tool.
        let resolved: Vec<String> = reg
            .tools_for_agent(agent)
            .iter()
            .map(|t| t.name.clone())
            .collect();
        let mut expected = vec![own_name.clone(), SHARED_NAME.to_string()];
        expected.sort();
        assert_eq!(resolved, expected, "resolution for {agent}");
        for (other, other_prefix) in AGENTS {
            if other != agent {
                assert!(
                    !resolved.contains(&format!("{other_prefix}probe_tool")),
                    "{agent} must not resolve {other}'s unique tool"
                );
            }
        }
    }

    // Unknown agents resolve to the shared pool only.
    assert!(!is_known_owner("A31"));
    assert!(!is_known_owner("nobody"));
    let names: Vec<String> = reg
        .tools_for_agent("A31")
        .iter()
        .map(|t| t.name.clone())
        .collect();
    assert_eq!(names, vec![SHARED_NAME.to_string()]);
}

#[test]
fn discovery_order_is_stable_and_read_only() {
    let reg = registry_with_all();

    // names() is sorted regardless of registration order.
    let mut sorted: Vec<String> = reg.names();
    sorted.sort();
    assert_eq!(reg.names(), sorted);
    assert_eq!(reg.names().len(), 31);

    // Repeated discovery returns identical results and never mutates
    // the registry: discovery borrows `&self` and the count is unchanged.
    let before = reg.len();
    let first: Vec<String> = reg
        .tools_for_agent("A24")
        .iter()
        .map(|t| t.name.clone())
        .collect();
    let second: Vec<String> = reg
        .tools_for_agent("A24")
        .iter()
        .map(|t| t.name.clone())
        .collect();
    assert_eq!(first, second);
    assert_eq!(first.len(), 2);
    let _ = reg.get(SHARED_NAME);
    let _ = reg.tools_for_capability("graph.validate");
    let _ = reg.tools_for_family("graph");
    let _ = reg.tools_for_owner("shared");
    assert_eq!(reg.len(), before, "discovery must not mutate the registry");
    assert!(!reg.is_empty());
}

#[test]
fn duplicate_and_shadowing_rejected_at_registration() {
    let mut reg = Registry::new();
    reg.register(unique_tool("A05")).unwrap();
    reg.register(shared_tool()).unwrap();
    assert_eq!(reg.len(), 2);

    // Exact duplicate (same contract) is rejected.
    assert!(matches!(
        reg.register(unique_tool("A05")),
        Err(RegistryError::DuplicateTool { .. })
    ));

    // Same name, different schema is a schema collision, not an overwrite.
    let mut colliding = unique_tool("A05");
    colliding.input_schema = json!({"type": "object", "required": ["x"]});
    assert!(matches!(
        reg.register(colliding),
        Err(RegistryError::SchemaCollision { .. })
    ));

    // Shadowing across owners: a `be_` name claimed by A24 fails the
    // per-owner prefix rule even though the name is already taken.
    assert!(matches!(
        reg.register(Tool::new("be_probe_tool", "A24", OwnerScope::Unique)),
        Err(RegistryError::InvalidName { .. })
    ));

    // Scope shadowing: an agent tool registered as Shared (or a shared
    // tool as Unique) would break cross-agent denial; reject both.
    assert!(matches!(
        reg.register(Tool::new("be_probe_tool", "A05", OwnerScope::Shared)),
        Err(RegistryError::ScopeMismatch { .. })
    ));
    assert!(matches!(
        reg.register(Tool::new("shared_probe_tool", "shared", OwnerScope::Unique)),
        Err(RegistryError::ScopeMismatch { .. })
    ));

    // The shared tool is registered once: a second registration fails.
    assert!(matches!(
        reg.register(shared_tool()),
        Err(RegistryError::DuplicateTool { .. })
    ));

    // Nothing above mutated the registry.
    assert_eq!(reg.len(), 2);
    assert_eq!(reg.get("be_probe_tool").unwrap().owner, "A05");
    assert_eq!(reg.get(SHARED_NAME).unwrap().owner, "shared");
}

#[test]
fn pre_exec_and_dispatch_scope_enforcement() {
    // Pre-exec: Tool::allows_caller is the caller check both layers use.
    let mut unique = unique_tool("A05");
    assert!(unique.allows_caller("A05"), "owner may call");
    assert!(!unique.allows_caller("A06"), "cross-subagent denied");
    assert!(!unique.allows_caller(""), "empty caller denied");
    unique.permitted_callers = vec!["A06".to_string()];
    assert!(unique.allows_caller("A06"), "permitted caller allowed");
    let shared = shared_tool();
    assert!(shared.allows_caller("A05"), "shared accepts any agent");
    assert!(shared.allows_caller("A30"), "shared accepts any agent");
    assert!(!shared.allows_caller(""), "shared rejects empty caller");

    // Rust-exec: the dispatcher enforces the same rule server-side.
    let mut reg = Registry::new();
    reg.register(unique_tool("A05")).unwrap();
    reg.register(shared_tool()).unwrap();
    let mut d = Dispatcher::new(reg);
    d.register_native("be_probe_tool", ok_handler(json!({"ok": true})));
    d.register_native(SHARED_NAME, ok_handler(json!({"ok": true})));

    // Cross-subagent call to a unique tool: denied and audit-logged.
    let err = d.dispatch("A06", "be_probe_tool", json!({})).unwrap_err();
    assert!(
        matches!(err, DispatchError::Unauthorized { .. }),
        "got {err:?}"
    );
    let log = d.audit_log();
    assert_eq!(log.len(), 1);
    assert_eq!(log[0].kind, AuditKind::CrossAgentRejected);
    assert_eq!(log[0].caller, "A06");
    assert_eq!(log[0].owner, "A05");

    // Owner dispatch succeeds natively; shared dispatch works for any agent.
    let out = d.dispatch("A05", "be_probe_tool", json!({})).unwrap();
    assert_eq!(out.route, Route::Native);
    assert_eq!(out.caller, "A05");
    for caller in ["A01", "A24", "A30"] {
        let out = d.dispatch(caller, SHARED_NAME, json!({})).unwrap();
        assert_eq!(out.route, Route::Native);
        assert_eq!(out.caller, caller);
    }

    // Unknown tool and empty caller are typed failures, not panics.
    assert!(matches!(
        d.dispatch("A05", "be_missing", json!({})),
        Err(DispatchError::NotFound { .. })
    ));
    assert!(matches!(
        d.dispatch("", "be_probe_tool", json!({})),
        Err(DispatchError::Unauthorized { .. })
    ));
}
