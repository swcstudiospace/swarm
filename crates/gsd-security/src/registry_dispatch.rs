//! Registry dispatch: server-side authorization, shared-tool limits,
//! idempotent-only retry with backoff, and per-family legacy fallback.
//!
//! Every failure is a typed [`DispatchError`] variant; callers never need
//! to parse error strings. Cross-agent calls to unique tools are rejected
//! **and** written to the in-memory audit log ([`Dispatcher::audit_log`]).
//!
//! Graph family (graph validation, `graph_validate` for Python, served
//! through the registry as [`GRAPH_VALIDATE_TOOL`]): the graph family is
//! the one family migrated through this dispatcher; every other family is
//! untouched. ONE flag governs its routing — the per-tool
//! [`crate::registry::LegacyFallback::enabled`] flag on the registered
//! [`graph_validate_tool`]:
//! * flag **off** → Rust native only. The native handler
//!   ([`native_graph_validate`]) emits the deterministic topological order;
//!   a native failure is the reported error and the legacy handler is never
//!   consulted.
//! * flag **on** (the default, keeping the Python decision "python default")
//!   → Rust native first; a failing or missing native handler falls back to
//!   the legacy Python path ([`legacy_graph_validate`]), which reproduces
//!   the legacy behaviour (insertion order, as in `swarm/graph_planner.py`'s
//!   ordering, validated exactly like native) behind the
//!   `swarm.tool_registry.call_tool` contract.
//!
//! Both graph handlers validate identically (duplicate/unknown/cycle are
//! typed [`DispatchError::InvalidInput`]); only the emitted order differs,
//! so a payload the native route rejects can never be served as valid by
//! the legacy route — fallback never launders an invalid graph.
//!
//! **Rollback** (no schema or data change; nothing else moves):
//! 1. flip the family flag off (`graph_validate_tool(false)` or set
//!    `tool.legacy_fallback.enabled = false`) → native-only serving, the
//!    pre-migration behaviour, live on the next dispatch;
//! 2. remove routing: stop registering [`graph_validate_tool`] in the
//!    [`Registry`] and stop calling [`register_graph_validate`] →
//!    `dispatch` returns `NotFound` for [`GRAPH_VALIDATE_TOOL`] and Python
//!    callers keep using `swarm._native.graph_validate` directly (its
//!    `legacy_fallback=True` default is unchanged; the 18 legacy exports
//!    are untouched).

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use serde_json::Value;

use crate::registry::{OwnerScope, Registry, Tool};

/// Handler for one tool: payload in, JSON value out (or a typed error).
pub type Handler = Box<dyn Fn(&Value) -> Result<Value, DispatchError> + Send + Sync>;

/// Which engine served a call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Route {
    /// Native Rust handler.
    Native,
    /// Python legacy path (`LegacyFallback::python_path`).
    LegacyPython,
}

/// Typed dispatch failures.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum DispatchError {
    #[error("tool {name:?} is not registered")]
    NotFound { name: String },
    #[error("caller {caller:?} may not call unique tool {name:?} owned by {owner:?}")]
    Unauthorized {
        caller: String,
        name: String,
        owner: String,
    },
    #[error("shared tool {name:?} rate limited: {reason}")]
    RateLimited { name: String, reason: String },
    #[error("tool {name:?} exceeded its timeout of {timeout_ms} ms")]
    Timeout { name: String, timeout_ms: u64 },
    #[error("tool {name:?} handler failed: {message}")]
    HandlerFailed { name: String, message: String },
    #[error("invalid payload for tool {name:?}: {message}")]
    InvalidInput { name: String, message: String },
}

/// Kind of a recorded audit-log entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuditKind {
    /// A cross-agent call to a unique tool was rejected.
    CrossAgentRejected,
    /// A shared-tool call was rate limited.
    RateLimited,
    /// A call was dispatched natively.
    DispatchedNative,
    /// A call fell back to the Python legacy path.
    LegacyFallback,
}

/// One audit-log row. Names only, never payload values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuditEntry {
    pub kind: AuditKind,
    pub tool: String,
    pub caller: String,
    pub owner: String,
}

/// Successful dispatch result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DispatchOutcome {
    pub tool: String,
    pub caller: String,
    pub route: Route,
    pub attempts: u32,
    pub value: Value,
}

/// Conservative caps for shared tools: at most `max_concurrent`
/// in-flight calls and `max_calls_per_window` calls per `window`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SharedLimits {
    pub max_concurrent: usize,
    pub max_calls_per_window: usize,
    pub window: Duration,
}

impl Default for SharedLimits {
    fn default() -> Self {
        Self {
            max_concurrent: 8,
            max_calls_per_window: 60,
            window: Duration::from_secs(60),
        }
    }
}

struct InFlightGuard<'a> {
    counter: &'a AtomicUsize,
}

impl Drop for InFlightGuard<'_> {
    fn drop(&mut self) {
        self.counter.fetch_sub(1, Ordering::SeqCst);
    }
}

/// Sync dispatcher over a [`Registry`]. Handlers are plain closures;
/// the dispatcher owns authorization, limits, retry and fallback.
pub struct Dispatcher {
    registry: Registry,
    native: HashMap<String, Handler>,
    legacy: HashMap<String, Handler>,
    shared_limits: SharedLimits,
    in_flight: AtomicUsize,
    call_times: Mutex<Vec<Instant>>,
    audit: Mutex<Vec<AuditEntry>>,
}

impl Dispatcher {
    /// A dispatcher over `registry` with default shared-tool limits.
    pub fn new(registry: Registry) -> Self {
        Self::with_shared_limits(registry, SharedLimits::default())
    }

    /// A dispatcher over `registry` with explicit shared-tool limits.
    pub fn with_shared_limits(registry: Registry, shared_limits: SharedLimits) -> Self {
        Self {
            registry,
            native: HashMap::new(),
            legacy: HashMap::new(),
            shared_limits,
            in_flight: AtomicUsize::new(0),
            call_times: Mutex::new(Vec::new()),
            audit: Mutex::new(Vec::new()),
        }
    }

    /// Register the native handler for a tool name.
    pub fn register_native(&mut self, name: impl Into<String>, handler: Handler) {
        self.native.insert(name.into(), handler);
    }

    /// Register the Python-legacy handler for a tool name.
    pub fn register_legacy(&mut self, name: impl Into<String>, handler: Handler) {
        self.legacy.insert(name.into(), handler);
    }

    /// The underlying registry (for discovery queries).
    pub fn registry(&self) -> &Registry {
        &self.registry
    }

    /// Snapshot of the audit log, oldest first.
    pub fn audit_log(&self) -> Vec<AuditEntry> {
        self.audit.lock().clone()
    }

    fn log(&self, kind: AuditKind, tool: &str, caller: &str, owner: &str) {
        self.audit.lock().push(AuditEntry {
            kind,
            tool: tool.to_string(),
            caller: caller.to_string(),
            owner: owner.to_string(),
        });
    }

    /// Enforce shared-tool concurrency/rate limits. Returns a guard
    /// holding the in-flight slot until the call finishes.
    fn acquire_shared_slot(
        &self,
        name: &str,
        caller: &str,
        owner: &str,
    ) -> Result<InFlightGuard<'_>, DispatchError> {
        let now = Instant::now();
        {
            let mut times = self.call_times.lock();
            let window = self.shared_limits.window;
            times.retain(|t| now.duration_since(*t) < window);
            if times.len() >= self.shared_limits.max_calls_per_window {
                self.log(AuditKind::RateLimited, name, caller, owner);
                return Err(DispatchError::RateLimited {
                    name: name.to_string(),
                    reason: format!(
                        "{} calls already in the {:?} window (limit {})",
                        times.len(),
                        window,
                        self.shared_limits.max_calls_per_window
                    ),
                });
            }
            times.push(now);
        }
        let prev = self.in_flight.fetch_add(1, Ordering::SeqCst);
        if prev >= self.shared_limits.max_concurrent {
            self.in_flight.fetch_sub(1, Ordering::SeqCst);
            self.log(AuditKind::RateLimited, name, caller, owner);
            return Err(DispatchError::RateLimited {
                name: name.to_string(),
                reason: format!(
                    "{} calls already in flight (limit {})",
                    prev, self.shared_limits.max_concurrent
                ),
            });
        }
        Ok(InFlightGuard {
            counter: &self.in_flight,
        })
    }

    fn call_handler(
        handler: &Handler,
        name: &str,
        payload: &Value,
        timeout: Duration,
    ) -> Result<Value, DispatchError> {
        let started = Instant::now();
        let value = handler(payload)?;
        if started.elapsed() > timeout {
            return Err(DispatchError::Timeout {
                name: name.to_string(),
                timeout_ms: u64::try_from(timeout.as_millis()).unwrap_or(u64::MAX),
            });
        }
        Ok(value)
    }

    /// Dispatch one call. Authorization is checked server-side against the
    /// registry contract; unique tools reject cross-agent callers (and log
    /// the rejection) before any handler runs.
    pub fn dispatch(
        &self,
        caller: &str,
        name: &str,
        payload: Value,
    ) -> Result<DispatchOutcome, DispatchError> {
        let tool = self
            .registry
            .get(name)
            .ok_or_else(|| DispatchError::NotFound {
                name: name.to_string(),
            })?
            .clone();
        if !payload.is_object() {
            return Err(DispatchError::InvalidInput {
                name: name.to_string(),
                message: "payload must be a JSON object".to_string(),
            });
        }
        if !tool.allows_caller(caller) {
            self.log(AuditKind::CrossAgentRejected, name, caller, &tool.owner);
            return Err(DispatchError::Unauthorized {
                caller: caller.to_string(),
                name: name.to_string(),
                owner: tool.owner.clone(),
            });
        }
        // Shared tools are rate-limited; unique tools are not.
        let _guard = match tool.scope {
            OwnerScope::Shared => Some(self.acquire_shared_slot(name, caller, &tool.owner)?),
            OwnerScope::Unique => None,
        };

        let max_attempts = tool.retry_policy.effective_max_attempts();
        let mut last_error: Option<DispatchError> = None;
        for attempt in 1..=max_attempts {
            if attempt > 1 {
                std::thread::sleep(tool.retry_policy.backoff(attempt - 1));
            }
            // Native first; a missing or failing native handler falls back
            // to the Python legacy path when the flag is on (the default).
            let native_result = self
                .native
                .get(name)
                .map(|h| Self::call_handler(h, name, &payload, tool.timeout));
            match native_result {
                Some(Ok(value)) => {
                    self.log(AuditKind::DispatchedNative, name, caller, &tool.owner);
                    return Ok(DispatchOutcome {
                        tool: name.to_string(),
                        caller: caller.to_string(),
                        route: Route::Native,
                        attempts: attempt,
                        value,
                    });
                }
                native_result => {
                    if tool.legacy_fallback.enabled {
                        if let Some(legacy) = self.legacy.get(name) {
                            match Self::call_handler(legacy, name, &payload, tool.timeout) {
                                Ok(value) => {
                                    self.log(AuditKind::LegacyFallback, name, caller, &tool.owner);
                                    return Ok(DispatchOutcome {
                                        tool: name.to_string(),
                                        caller: caller.to_string(),
                                        route: Route::LegacyPython,
                                        attempts: attempt,
                                        value,
                                    });
                                }
                                Err(e) => last_error = Some(e),
                            }
                        }
                    }
                    if let Some(Err(e)) = native_result {
                        last_error = Some(e);
                    } else if last_error.is_none() {
                        last_error = Some(DispatchError::HandlerFailed {
                            name: name.to_string(),
                            message: "no native handler registered and legacy fallback unavailable"
                                .to_string(),
                        });
                    }
                }
            }
            // Only handler failures of idempotent tools reach a next attempt;
            // effective_max_attempts already collapses to 1 when unclassified.
            if attempt == max_attempts {
                break;
            }
        }
        Err(last_error.unwrap_or(DispatchError::HandlerFailed {
            name: name.to_string(),
            message: "handler failed".to_string(),
        }))
    }
}
/// Registry name of the graph-validation tool.
///
/// The registry enforces `<owner-prefix>_` names, so the Python-side
/// `graph_validate` is served through the registry as `shared_graph_validate`
/// (owner `shared`, shared scope: any agent may call it). The pyo3 export
/// keeps its `graph_validate` name.
pub const GRAPH_VALIDATE_TOOL: &str = "shared_graph_validate";

/// Tool family of [`GRAPH_VALIDATE_TOOL`] (used for the
/// `Registry::tools_for_family` discovery query).
pub const GRAPH_FAMILY: &str = "graph";

/// Versioned intent of the graph family (mirrors `GRAPH_API_VERSION`
/// in `gsd-python`; payloads carry it as `api_version`).
pub const GRAPH_API_VERSION: &str = "graph/1";

/// The registered contract for the migrated graph-validation family.
///
/// `legacy_fallback_enabled` is the family flag: `false` routes to the Rust
/// native handler only, `true` (the default) enables the legacy Python
/// fallback. Retry stays unclassified (no retry — the graph family's
/// conservative decision); scope is shared, so [`SharedLimits`] apply.
pub fn graph_validate_tool(legacy_fallback_enabled: bool) -> Tool {
    let mut tool = Tool::new(GRAPH_VALIDATE_TOOL, "shared", OwnerScope::Shared);
    tool.capabilities = vec!["graph.validate".to_string()];
    tool.family = GRAPH_FAMILY.to_string();
    tool.legacy_fallback.enabled = legacy_fallback_enabled;
    tool
}

/// Registers the native and legacy handlers for [`GRAPH_VALIDATE_TOOL`].
/// The caller must have registered [`graph_validate_tool`] in the
/// [`Registry`] behind this [`Dispatcher`] first (see
/// [`graph_validate_dispatcher`] for the full wiring). Rollback: stop
/// calling this and the tool is unroutable (see module docs).
pub fn register_graph_validate(dispatcher: &mut Dispatcher) {
    dispatcher.register_native(GRAPH_VALIDATE_TOOL, Box::new(native_graph_validate));
    dispatcher.register_legacy(GRAPH_VALIDATE_TOOL, Box::new(legacy_graph_validate));
}

/// A [`Dispatcher`] over a fresh [`Registry`] with the graph-validation
/// tool and both handlers registered — the composed migration wiring.
/// `legacy_fallback_enabled` is the family flag (`true` = python default).
pub fn graph_validate_dispatcher(legacy_fallback_enabled: bool) -> Dispatcher {
    let mut registry = Registry::new();
    registry
        .register(graph_validate_tool(legacy_fallback_enabled))
        .expect("graph family tool is well-formed");
    let mut dispatcher = Dispatcher::new(registry);
    register_graph_validate(&mut dispatcher);
    dispatcher
}

/// Parsed graph payload: node ids and directed edges.
type GraphPayload = (Vec<String>, Vec<(String, String)>);

/// Parses the shared graph payload: `{ "nodes": [..], "edges": [[from, to], ..] }`.
/// Any shape deviation is a typed [`DispatchError::InvalidInput`].
fn parse_graph_payload(payload: &Value) -> Result<GraphPayload, DispatchError> {
    let invalid = |message: String| DispatchError::InvalidInput {
        name: GRAPH_VALIDATE_TOOL.to_string(),
        message,
    };
    let nodes = payload
        .get("nodes")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("payload must carry a \"nodes\" array".to_string()))?;
    let mut node_ids = Vec::with_capacity(nodes.len());
    for node in nodes {
        node_ids.push(
            node.as_str()
                .map(str::to_string)
                .ok_or_else(|| invalid("nodes must be strings".to_string()))?,
        );
    }
    let edges = payload
        .get("edges")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("payload must carry an \"edges\" array".to_string()))?;
    let mut pairs = Vec::with_capacity(edges.len());
    for edge in edges {
        let pair = edge
            .as_array()
            .ok_or_else(|| invalid("each edge must be a [from, to] pair".to_string()))?;
        match pair.as_slice() {
            [from, to] => pairs.push((
                from.as_str()
                    .map(str::to_string)
                    .ok_or_else(|| invalid("edge endpoints must be strings".to_string()))?,
                to.as_str()
                    .map(str::to_string)
                    .ok_or_else(|| invalid("edge endpoints must be strings".to_string()))?,
            )),
            _ => return Err(invalid("each edge must be a [from, to] pair".to_string())),
        }
    }
    Ok((node_ids, pairs))
}

/// Validates the graph and returns its node order.
/// `topological == true` (Rust native): deterministic Kahn order with ties
/// broken by insertion order. Otherwise (legacy Python behaviour): the
/// insertion order. Duplicate/unknown/cycle are always rejected — the two
/// routes differ only in the emitted order, never in what is accepted.
fn ordered_graph(
    nodes: &[String],
    edges: &[(String, String)],
    topological: bool,
) -> Result<Vec<String>, DispatchError> {
    let invalid = |message: String| DispatchError::InvalidInput {
        name: GRAPH_VALIDATE_TOOL.to_string(),
        message,
    };
    let mut seen: std::collections::HashSet<&str> =
        std::collections::HashSet::with_capacity(nodes.len());
    for node in nodes {
        if !seen.insert(node.as_str()) {
            return Err(invalid(format!("duplicate node {node:?}")));
        }
    }
    for (from, to) in edges {
        for endpoint in [from, to] {
            if !seen.contains(endpoint.as_str()) {
                return Err(invalid(format!(
                    "edge references unknown node {endpoint:?}"
                )));
            }
        }
    }
    // Cycle check (Kahn) runs for both routes even though the legacy route
    // emits insertion order: a cyclic graph is never a valid graph.
    let index: HashMap<&str, usize> = nodes
        .iter()
        .enumerate()
        .map(|(i, node)| (node.as_str(), i))
        .collect();
    let mut indegree: HashMap<&str, usize> = nodes.iter().map(|n| (n.as_str(), 0)).collect();
    let mut outgoing: HashMap<&str, Vec<&str>> = HashMap::new();
    for (from, to) in edges {
        outgoing.entry(from.as_str()).or_default().push(to.as_str());
        *indegree.get_mut(to.as_str()).expect("validated above") += 1;
    }
    if !topological {
        let mut ready: Vec<&str> = nodes
            .iter()
            .map(String::as_str)
            .filter(|node| indegree[node] == 0)
            .collect();
        let mut emitted = 0usize;
        while let Some(node) = ready.pop() {
            emitted += 1;
            if let Some(nexts) = outgoing.get(node) {
                for next in nexts {
                    let degree = indegree.get_mut(next).expect("validated above");
                    *degree -= 1;
                    if *degree == 0 {
                        ready.push(next);
                    }
                }
            }
        }
        if emitted != nodes.len() {
            return Err(invalid("graph contains a cycle".to_string()));
        }
        return Ok(nodes.to_vec());
    }
    let mut ready: Vec<&str> = nodes
        .iter()
        .map(String::as_str)
        .filter(|node| indegree[node] == 0)
        .collect();
    let mut order: Vec<String> = Vec::with_capacity(nodes.len());
    while let Some(pos) = ready
        .iter()
        .enumerate()
        .min_by_key(|(_, n)| index[*n])
        .map(|(i, _)| i)
    {
        let node = ready.remove(pos);
        order.push(node.to_owned());
        if let Some(nexts) = outgoing.get(node) {
            for next in nexts {
                let degree = indegree.get_mut(next).expect("validated above");
                *degree -= 1;
                if *degree == 0 {
                    ready.push(next);
                }
            }
        }
    }
    if order.len() != nodes.len() {
        return Err(invalid("graph contains a cycle".to_string()));
    }
    Ok(order)
}

/// Builds the graph payload plus the [`GRAPH_API_VERSION`] version stamp.
fn graph_report(order: Vec<String>, node_count: usize, edge_count: usize) -> Value {
    serde_json::json!({
        "order": order,
        "node_count": node_count,
        "edge_count": edge_count,
        "api_version": GRAPH_API_VERSION,
    })
}

/// Rust native handler for [`GRAPH_VALIDATE_TOOL`]: deterministic
/// topological order via [`ordered_graph`].
pub fn native_graph_validate(payload: &Value) -> Result<Value, DispatchError> {
    let (nodes, edges) = parse_graph_payload(payload)?;
    let order = ordered_graph(&nodes, &edges, true)?;
    Ok(graph_report(order, nodes.len(), edges.len()))
}

/// Legacy Python handler for [`GRAPH_VALIDATE_TOOL`]: insertion order via
/// [`ordered_graph`], behind the `swarm.tool_registry.call_tool` contract.
/// Validation is identical to the native handler (see [`ordered_graph`]).
pub fn legacy_graph_validate(payload: &Value) -> Result<Value, DispatchError> {
    let (nodes, edges) = parse_graph_payload(payload)?;
    let order = ordered_graph(&nodes, &edges, false)?;
    Ok(graph_report(order, nodes.len(), edges.len()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::registry::{Idempotency, OwnerScope, RetryPolicy, Tool};
    use std::sync::atomic::AtomicUsize;
    use std::sync::Arc;

    fn registry_with(tool: Tool) -> Registry {
        let mut reg = Registry::new();
        reg.register(tool).unwrap();
        reg
    }

    fn ok_handler(value: Value) -> Handler {
        Box::new(move |_payload: &Value| Ok(value.clone()))
    }

    #[test]
    fn cross_agent_unique_rejected_and_logged() {
        let reg = registry_with(Tool::new("kpol_pss_check", "A24", OwnerScope::Unique));
        let d = Dispatcher::new(reg);
        let err = d
            .dispatch("A05", "kpol_pss_check", serde_json::json!({}))
            .unwrap_err();
        assert!(
            matches!(err, DispatchError::Unauthorized { .. }),
            "got {err:?}"
        );
        let log = d.audit_log();
        assert_eq!(log.len(), 1);
        assert_eq!(log[0].kind, AuditKind::CrossAgentRejected);
        assert_eq!(log[0].caller, "A05");
        assert_eq!(log[0].tool, "kpol_pss_check");
        assert_eq!(log[0].owner, "A24");
    }

    #[test]
    fn unique_owner_dispatch_ok() {
        let reg = registry_with(Tool::new("kpol_pss_check", "A24", OwnerScope::Unique));
        let mut d = Dispatcher::new(reg);
        d.register_native(
            "kpol_pss_check",
            ok_handler(serde_json::json!({"ok": true})),
        );
        let out = d
            .dispatch("A24", "kpol_pss_check", serde_json::json!({}))
            .unwrap();
        assert_eq!(out.route, Route::Native);
        assert_eq!(out.attempts, 1);
        assert_eq!(out.value, serde_json::json!({"ok": true}));
    }

    #[test]
    fn shared_dispatch_ok_for_any_agent() {
        let reg = registry_with(Tool::new("shared_clock", "shared", OwnerScope::Shared));
        let mut d = Dispatcher::new(reg);
        d.register_native("shared_clock", ok_handler(serde_json::json!({"now": 1})));
        for caller in ["A01", "A24", "A30"] {
            let out = d
                .dispatch(caller, "shared_clock", serde_json::json!({}))
                .unwrap();
            assert_eq!(out.route, Route::Native);
            assert_eq!(out.caller, caller);
        }
    }

    #[test]
    fn fallback_routes_to_legacy() {
        let reg = registry_with(Tool::new("kpol_pss_check", "A24", OwnerScope::Unique));
        let mut d = Dispatcher::new(reg);
        d.register_native(
            "kpol_pss_check",
            Box::new(|_p: &Value| {
                Err(DispatchError::HandlerFailed {
                    name: "kpol_pss_check".to_string(),
                    message: "native boom".to_string(),
                })
            }),
        );
        d.register_legacy(
            "kpol_pss_check",
            ok_handler(serde_json::json!({"via": "legacy"})),
        );
        let out = d
            .dispatch("A24", "kpol_pss_check", serde_json::json!({}))
            .unwrap();
        assert_eq!(out.route, Route::LegacyPython);
        assert_eq!(out.value, serde_json::json!({"via": "legacy"}));
        assert!(d
            .audit_log()
            .iter()
            .any(|e| e.kind == AuditKind::LegacyFallback));
    }

    #[test]
    fn fallback_when_no_native_handler() {
        let reg = registry_with(Tool::new("kpol_pss_check", "A24", OwnerScope::Unique));
        let mut d = Dispatcher::new(reg);
        d.register_legacy(
            "kpol_pss_check",
            ok_handler(serde_json::json!({"via": "legacy"})),
        );
        let out = d
            .dispatch("A24", "kpol_pss_check", serde_json::json!({}))
            .unwrap();
        assert_eq!(out.route, Route::LegacyPython);
    }

    #[test]
    fn no_fallback_when_disabled() {
        let mut tool = Tool::new("kpol_pss_check", "A24", OwnerScope::Unique);
        tool.legacy_fallback.enabled = false;
        let reg = registry_with(tool);
        let mut d = Dispatcher::new(reg);
        d.register_native(
            "kpol_pss_check",
            Box::new(|_p: &Value| {
                Err(DispatchError::HandlerFailed {
                    name: "kpol_pss_check".to_string(),
                    message: "native boom".to_string(),
                })
            }),
        );
        d.register_legacy(
            "kpol_pss_check",
            ok_handler(serde_json::json!({"via": "legacy"})),
        );
        assert!(matches!(
            d.dispatch("A24", "kpol_pss_check", serde_json::json!({})),
            Err(DispatchError::HandlerFailed { .. })
        ));
    }

    #[test]
    fn unclassified_tool_is_not_retried() {
        let mut tool = Tool::new("kpol_pss_check", "A24", OwnerScope::Unique);
        tool.legacy_fallback.enabled = false;
        tool.retry_policy = RetryPolicy {
            max_attempts: 5,
            base_delay: Duration::from_millis(1),
            max_delay: Duration::from_millis(2),
            idempotency: Idempotency::Unclassified,
        };
        let calls = Arc::new(AtomicUsize::new(0));
        let seen = Arc::clone(&calls);
        let reg = registry_with(tool);
        let mut d = Dispatcher::new(reg);
        d.register_native(
            "kpol_pss_check",
            Box::new(move |_p: &Value| {
                seen.fetch_add(1, Ordering::SeqCst);
                Err(DispatchError::HandlerFailed {
                    name: "kpol_pss_check".to_string(),
                    message: "always".to_string(),
                })
            }),
        );
        assert!(d
            .dispatch("A24", "kpol_pss_check", serde_json::json!({}))
            .is_err());
        assert_eq!(
            calls.load(Ordering::SeqCst),
            1,
            "unclassified must not retry"
        );
    }

    #[test]
    fn idempotent_tool_is_retried() {
        let mut tool = Tool::new("kpol_pss_check", "A24", OwnerScope::Unique);
        tool.legacy_fallback.enabled = false;
        tool.retry_policy =
            RetryPolicy::idempotent(3, Duration::from_millis(1), Duration::from_millis(2));
        let calls = Arc::new(AtomicUsize::new(0));
        let seen = Arc::clone(&calls);
        let reg = registry_with(tool);
        let mut d = Dispatcher::new(reg);
        d.register_native(
            "kpol_pss_check",
            Box::new(move |_p: &Value| {
                let n = seen.fetch_add(1, Ordering::SeqCst);
                if n < 2 {
                    Err(DispatchError::HandlerFailed {
                        name: "kpol_pss_check".to_string(),
                        message: "flaky".to_string(),
                    })
                } else {
                    Ok(serde_json::json!({"ok": true}))
                }
            }),
        );
        let out = d
            .dispatch("A24", "kpol_pss_check", serde_json::json!({}))
            .unwrap();
        assert_eq!(out.attempts, 3);
        assert_eq!(calls.load(Ordering::SeqCst), 3);
    }

    #[test]
    fn shared_rate_limit_rejects() {
        let reg = registry_with(Tool::new("shared_clock", "shared", OwnerScope::Shared));
        let mut d = Dispatcher::with_shared_limits(
            reg,
            SharedLimits {
                max_concurrent: 8,
                max_calls_per_window: 2,
                window: Duration::from_secs(60),
            },
        );
        d.register_native("shared_clock", ok_handler(serde_json::json!({})));
        d.dispatch("A01", "shared_clock", serde_json::json!({}))
            .unwrap();
        d.dispatch("A02", "shared_clock", serde_json::json!({}))
            .unwrap();
        let err = d
            .dispatch("A03", "shared_clock", serde_json::json!({}))
            .unwrap_err();
        assert!(
            matches!(err, DispatchError::RateLimited { .. }),
            "got {err:?}"
        );
        assert!(d
            .audit_log()
            .iter()
            .any(|e| e.kind == AuditKind::RateLimited));
    }

    #[test]
    fn unknown_tool_is_typed_not_found() {
        let d = Dispatcher::new(Registry::new());
        let err = d
            .dispatch("A24", "kpol_missing", serde_json::json!({}))
            .unwrap_err();
        assert!(matches!(err, DispatchError::NotFound { .. }), "got {err:?}");
    }

    // --- graph validation family (graph_validate), SPE-36785 ---

    fn graph_payload(nodes: &[&str], edges: &[(&str, &str)]) -> Value {
        serde_json::json!({
            "nodes": nodes,
            "edges": edges.iter().map(|(from, to)| serde_json::json!([from, to])).collect::<Vec<_>>(),
        })
    }

    /// Round-trip through the composed wiring with the family flag off:
    /// the Rust native handler serves the deterministic topo order.
    #[test]
    fn graph_validate_round_trip_native_when_flag_off() {
        let d = graph_validate_dispatcher(false);
        let out = d
            .dispatch(
                "A05",
                GRAPH_VALIDATE_TOOL,
                graph_payload(&["c", "a", "b"], &[("a", "b"), ("b", "c")]),
            )
            .unwrap();
        assert_eq!(out.route, Route::Native);
        assert_eq!(out.attempts, 1);
        assert_eq!(out.value["order"], serde_json::json!(["a", "b", "c"]));
        assert_eq!(out.value["api_version"], serde_json::json!("graph/1"));
        assert_eq!(out.value["node_count"], serde_json::json!(3));
        assert_eq!(out.value["edge_count"], serde_json::json!(2));
        // The tool registered itself in the graph family, shared scope.
        let tool = d.registry().get(GRAPH_VALIDATE_TOOL).unwrap();
        assert_eq!(tool.family, GRAPH_FAMILY);
        assert_eq!(tool.owner, "shared");
        assert_eq!(tool.scope, OwnerScope::Shared);
        assert!(!tool.legacy_fallback.enabled);
        assert!(
            !tool.retry_policy.allows_retry(),
            "graph family keeps the no-retry (unclassified) decision"
        );
        assert_eq!(d.registry().tools_for_family(GRAPH_FAMILY).len(), 1);
        assert_eq!(d.registry().names(), vec![GRAPH_VALIDATE_TOOL.to_string()]);
    }

    /// Family flag on (python default): a native failure falls back to the
    /// legacy Python path, which reproduces the legacy insertion order.
    #[test]
    fn graph_validate_flag_on_falls_back_to_legacy_on_native_failure() {
        let mut d = graph_validate_dispatcher(true);
        d.register_native(
            GRAPH_VALIDATE_TOOL,
            Box::new(|_p: &Value| {
                Err(DispatchError::HandlerFailed {
                    name: GRAPH_VALIDATE_TOOL.to_string(),
                    message: "native boom".to_string(),
                })
            }),
        );
        let out = d
            .dispatch(
                "A05",
                GRAPH_VALIDATE_TOOL,
                graph_payload(&["c", "a", "b"], &[("a", "b"), ("b", "c")]),
            )
            .unwrap();
        assert_eq!(out.route, Route::LegacyPython);
        assert_eq!(out.value["order"], serde_json::json!(["c", "a", "b"]));
        assert_eq!(out.value["api_version"], serde_json::json!("graph/1"));
        assert!(d
            .audit_log()
            .iter()
            .any(|e| e.kind == AuditKind::LegacyFallback && e.tool == GRAPH_VALIDATE_TOOL));
    }

    /// Family flag off: a native failure is the reported error; the legacy
    /// handler is never consulted even though one is registered.
    #[test]
    fn graph_validate_flag_off_native_failure_is_not_legacy_served() {
        let mut d = graph_validate_dispatcher(false);
        d.register_native(
            GRAPH_VALIDATE_TOOL,
            Box::new(|_p: &Value| {
                Err(DispatchError::HandlerFailed {
                    name: GRAPH_VALIDATE_TOOL.to_string(),
                    message: "native boom".to_string(),
                })
            }),
        );
        assert!(matches!(
            d.dispatch("A05", GRAPH_VALIDATE_TOOL, graph_payload(&["a"], &[])),
            Err(DispatchError::HandlerFailed { .. })
        ));
        assert!(!d
            .audit_log()
            .iter()
            .any(|e| e.kind == AuditKind::LegacyFallback));
    }

    /// Duplicate/unknown/cycle payloads are typed errors on BOTH routes:
    /// flag on must not launder an invalid graph through legacy.
    #[test]
    fn graph_validate_invalid_graphs_are_typed_errors_on_both_routes() {
        let d = graph_validate_dispatcher(true);
        for payload in [
            graph_payload(&["a", "a"], &[]),
            graph_payload(&["a"], &[("a", "b")]),
            graph_payload(&["a", "b"], &[("a", "b"), ("b", "a")]),
            serde_json::json!({"nodes": ["a"]}),
            serde_json::json!({"nodes": [1], "edges": []}),
        ] {
            assert!(
                matches!(
                    d.dispatch("A05", GRAPH_VALIDATE_TOOL, payload.clone()),
                    Err(DispatchError::InvalidInput { .. })
                ),
                "payload {payload} must fail with InvalidInput"
            );
        }
        // Handlers directly: both routes reject the same invalid inputs.
        let cycle = graph_payload(&["a", "b"], &[("a", "b"), ("b", "a")]);
        assert!(native_graph_validate(&cycle).is_err());
        assert!(legacy_graph_validate(&cycle).is_err());
        // And agree on the happy path shape (same report, different order).
        let valid = graph_payload(&["c", "a", "b"], &[("a", "b"), ("b", "c")]);
        assert_eq!(
            native_graph_validate(&valid).unwrap()["order"],
            serde_json::json!(["a", "b", "c"])
        );
        assert_eq!(
            legacy_graph_validate(&valid).unwrap()["order"],
            serde_json::json!(["c", "a", "b"])
        );
    }
}
