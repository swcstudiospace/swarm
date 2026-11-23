//! A01 unique-tool batch: native Rust handlers + Python-path replicas,
//! per-tool legacy fallback flags, namespaced `orch_*` registry names.
//!
//! The five tools migrated here are A01's unique lane tools from
//! `swarm/tools/a01_tools.py` (`orch_dag_blocked`, `orch_gate_coverage`,
//! `orch_rework_bound`, `orch_dispatch_batch`, `orch_budget_guard`): the
//! first batch discovered from INVENTORY and the smallest meaningful
//! one (5 tools, all pure payload functions). Every other family is
//! untouched; the shared family lives in [`crate::registry_dispatch`].
//!
//! **Parity.** Every handler reproduces the Python function's return dict
//! byte-for-byte (including the `"state"` envelope and the contract-level
//! `{"state": "INVALID_INPUT", "field", "message"}` error dicts, which
//! the Python tools RETURN as values, so contract errors are carried as
//! `Ok` values — never as handler errors). Structural payload defects —
//! a missing key, a wrong JSON type — raise in Python (`KeyError` /
//! `TypeError`; the registry surfaces those as `DEPENDENCY_UNAVAILABLE`)
//! and fail here with the typed [`DispatchError::InvalidInput`], the
//! crate's equivalent of a handler raise (same tear as the graph
//! family's payload parser).
//!
//! Each tool therefore has two handlers with identical observable
//! behavior: the **native** one ([`native_orch_dag_blocked`]...) and
//! the **legacy** one ([`legacy_orch_dag_blocked`]...), a Rust replica
//! of the Python path behind the `swarm.tool_registry.call_tool`
//! contract. Unlike the graph family, there is no behavioral split to
//! reproduce: the Python algorithms are pure functions of the payload,
//! so the native algorithm and the legacy replica coincide in output;
//! what the per-tool [`crate::registry::LegacyFallback`] flag controls
//! is which route serves the call (see below).
//!
//! **Rollback** (no schema or data change; nothing else moves; per tool):
//! 1. flip the tool's flag off (`orch_dag_blocked_tool(false)` or set
//!    `tool.legacy_fallback.enabled = false`) → native-only serving; a
//!    structural native failure is then the reported error and the
//!    legacy handler is never consulted;
//! 2. remove routing: stop registering the native handler for a tool
//!    (skip [`register_orch_dag_blocked`] or omit it when composing a
//!    [`Dispatcher`]) while its flag is on → every call takes the
//!    `LegacyPython` route with the identical value;
//! 3. remove routing entirely: stop registering the tool contract →
//!    `dispatch` returns `NotFound`, and Python callers keep using
//!    `swarm.tools.a01_tools` directly (the Python code is untouched).
//!
//! **Validation boundary.** Inputs the Python registry's
//! `swarm.tool_registry` schema layer rejects (extra keys,
//! out-of-contract field types) go through the Rust handlers'
//! structural parsing instead — same rejection class (typed
//! `InvalidInput`), messages in the crate's dialect. In-contract
//! payloads — the entire parity surface below — are value-identical to
//! the Python dicts, including number types: Python computes with
//! arbitrary-precision ints, so ints are tracked as [`Num::I`] and
//! only true float inputs go down the [`Num::F`] path (a float result
//! prints as `2.0`; an int result as `2`).
//!
//! **Not ported:** the harness `"probe"` inputs (`partial`/`sleep`/
//! `dependency` responses emitted by `swarm.tools.support.respond`
//! before the lane function runs). Probes are a Python contract-test
//! feature; the `graph` family this module follows never ported them
//! either.
//!
//! **`_shown_ratio` rounding.** Python's `round(x, 4)` is
//! round-half-even on the double's *exact* decimal value
//! (`round(0.12345, 4) == 0.1235`, since the stored double is slightly
//! above the tie). [`round4`] reproduces that through exact integer
//! arithmetic (decompose the double into its mantissa/exponent, round
//! the scaled rational half-even, divide) rather than a float
//! multiply — a float multiply can round *across* a tie boundary and
//! disagree with Python (see
//! `tests::rounding_matches_python_round`). Boundary: when the scaled
//! integer exceeds `2^53`, the final `k as f64 / 10000` conversion can
//! double-round (reachable only with ratios ≥ ~9e11, far outside
//! budget ratios).

use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet, HashMap};

use serde_json::{json, Value};

use crate::registry::{OwnerScope, Registry, Tool};
use crate::registry_dispatch::{DispatchError, Dispatcher};

// ---------------------------------------------------------------------------
// Registration names, families, python paths
// ---------------------------------------------------------------------------

/// Tool family of the A01 unique batch (used for the
/// `Registry::tools_for_family` discovery query).
pub const A01_FAMILY: &str = "orch";

/// Registry name of the A01 DAG-blocked classifier.
pub const ORCH_DAG_BLOCKED_TOOL: &str = "orch_dag_blocked";
/// Registry name of the A01 gate-coverage checker.
pub const ORCH_GATE_COVERAGE_TOOL: &str = "orch_gate_coverage";
/// Registry name of the A01 rework-bound decider.
pub const ORCH_REWORK_BOUND_TOOL: &str = "orch_rework_bound";
/// Registry name of the A01 round-dispatch picker.
pub const ORCH_DISPATCH_BATCH_TOOL: &str = "orch_dispatch_batch";
/// Registry name of the A01 budget-guard decider.
pub const ORCH_BUDGET_GUARD_TOOL: &str = "orch_budget_guard";

/// Python handler entry point for each tool, mirroring the `handler`
/// field in `agents.json` (used as the legacy fallback `python_path`).
const ORCH_DAG_BLOCKED_PYTHON: &str = "swarm.tools.a01_tools:dag_blocked";
const ORCH_GATE_COVERAGE_PYTHON: &str = "swarm.tools.a01_tools:gate_coverage";
const ORCH_REWORK_BOUND_PYTHON: &str = "swarm.tools.a01_tools:rework_bound";
const ORCH_DISPATCH_BATCH_PYTHON: &str = "swarm.tools.a01_tools:dispatch_batch";
const ORCH_BUDGET_GUARD_PYTHON: &str = "swarm.tools.a01_tools:budget_guard";

// ---------------------------------------------------------------------------
// Tool constructors
// ---------------------------------------------------------------------------
//
// `legacy_fallback_enabled` is the tool's per-tool rollback flag:
// `false` routes to the Rust native handler only, `true` (the default,
// keeping the Python decision "python default") enables the legacy
// Python-path fallback. Retry stays unclassified (no retry — the
// conservative D5 decision, matching the graph family). Scope is unique
// per subagent, so callers other than `A01` are rejected at dispatch.
// Tool input/output schemas stay at the `Tool::new` defaults, exactly
// as the graph family does — dispatch validates structure in the
// handlers, and the detailed schemas remain canonical in `agents.json`.

fn a01_tool(
    name: &'static str,
    capability: &'static str,
    python_path: &'static str,
    legacy_fallback_enabled: bool,
) -> Tool {
    let mut tool = Tool::new(name, "A01", OwnerScope::Unique);
    tool.capabilities = vec![capability.to_string()];
    tool.family = A01_FAMILY.to_string();
    tool.legacy_fallback.enabled = legacy_fallback_enabled;
    tool.legacy_fallback.python_path = python_path.to_string();
    tool
}

/// The registered contract for `orch_dag_blocked`.
pub fn orch_dag_blocked_tool(legacy_fallback_enabled: bool) -> Tool {
    a01_tool(
        ORCH_DAG_BLOCKED_TOOL,
        "orch.dispatch",
        ORCH_DAG_BLOCKED_PYTHON,
        legacy_fallback_enabled,
    )
}

/// The registered contract for `orch_gate_coverage`.
pub fn orch_gate_coverage_tool(legacy_fallback_enabled: bool) -> Tool {
    a01_tool(
        ORCH_GATE_COVERAGE_TOOL,
        "orch.gates",
        ORCH_GATE_COVERAGE_PYTHON,
        legacy_fallback_enabled,
    )
}

/// The registered contract for `orch_rework_bound`.
pub fn orch_rework_bound_tool(legacy_fallback_enabled: bool) -> Tool {
    a01_tool(
        ORCH_REWORK_BOUND_TOOL,
        "orch.rework",
        ORCH_REWORK_BOUND_PYTHON,
        legacy_fallback_enabled,
    )
}

/// The registered contract for `orch_dispatch_batch`.
pub fn orch_dispatch_batch_tool(legacy_fallback_enabled: bool) -> Tool {
    a01_tool(
        ORCH_DISPATCH_BATCH_TOOL,
        "orch.dispatch",
        ORCH_DISPATCH_BATCH_PYTHON,
        legacy_fallback_enabled,
    )
}

/// The registered contract for `orch_budget_guard`.
pub fn orch_budget_guard_tool(legacy_fallback_enabled: bool) -> Tool {
    a01_tool(
        ORCH_BUDGET_GUARD_TOOL,
        "orch.budget",
        ORCH_BUDGET_GUARD_PYTHON,
        legacy_fallback_enabled,
    )
}

/// All five tool names of the A01 unique batch, sorted (the discovery
/// order `Registry::names` returns).
pub const A01_BATCH_TOOLS: [&str; 5] = [
    ORCH_BUDGET_GUARD_TOOL,
    ORCH_DAG_BLOCKED_TOOL,
    ORCH_DISPATCH_BATCH_TOOL,
    ORCH_GATE_COVERAGE_TOOL,
    ORCH_REWORK_BOUND_TOOL,
];

// ---------------------------------------------------------------------------
// Handler registration
// ---------------------------------------------------------------------------
//
// Each register fn wires the native + legacy handlers for one tool. The
// caller must have registered the corresponding tool contract in the
// [`Registry`] behind the [`Dispatcher`] first (see
// [`a01_batch_dispatcher`] for the composed wiring). Rollback: stop
// registering the native handler for a tool → its calls take the legacy
// route while the tool's flag is on (see module docs).

/// Registers the native and legacy handlers for [`ORCH_DAG_BLOCKED_TOOL`].
pub fn register_orch_dag_blocked(dispatcher: &mut Dispatcher) {
    dispatcher.register_native(ORCH_DAG_BLOCKED_TOOL, Box::new(native_orch_dag_blocked));
    dispatcher.register_legacy(ORCH_DAG_BLOCKED_TOOL, Box::new(legacy_orch_dag_blocked));
}

/// Registers the native and legacy handlers for [`ORCH_GATE_COVERAGE_TOOL`].
pub fn register_orch_gate_coverage(dispatcher: &mut Dispatcher) {
    dispatcher.register_native(ORCH_GATE_COVERAGE_TOOL, Box::new(native_orch_gate_coverage));
    dispatcher.register_legacy(ORCH_GATE_COVERAGE_TOOL, Box::new(legacy_orch_gate_coverage));
}

/// Registers the native and legacy handlers for [`ORCH_REWORK_BOUND_TOOL`].
pub fn register_orch_rework_bound(dispatcher: &mut Dispatcher) {
    dispatcher.register_native(ORCH_REWORK_BOUND_TOOL, Box::new(native_orch_rework_bound));
    dispatcher.register_legacy(ORCH_REWORK_BOUND_TOOL, Box::new(legacy_orch_rework_bound));
}

/// Registers the native and legacy handlers for [`ORCH_DISPATCH_BATCH_TOOL`].
pub fn register_orch_dispatch_batch(dispatcher: &mut Dispatcher) {
    dispatcher.register_native(
        ORCH_DISPATCH_BATCH_TOOL,
        Box::new(native_orch_dispatch_batch),
    );
    dispatcher.register_legacy(
        ORCH_DISPATCH_BATCH_TOOL,
        Box::new(legacy_orch_dispatch_batch),
    );
}

/// Registers the native and legacy handlers for [`ORCH_BUDGET_GUARD_TOOL`].
pub fn register_orch_budget_guard(dispatcher: &mut Dispatcher) {
    dispatcher.register_native(ORCH_BUDGET_GUARD_TOOL, Box::new(native_orch_budget_guard));
    dispatcher.register_legacy(ORCH_BUDGET_GUARD_TOOL, Box::new(legacy_orch_budget_guard));
}

/// Registers native and legacy handlers for every tool of the batch.
pub fn register_a01_batch(dispatcher: &mut Dispatcher) {
    register_orch_dag_blocked(dispatcher);
    register_orch_gate_coverage(dispatcher);
    register_orch_rework_bound(dispatcher);
    register_orch_dispatch_batch(dispatcher);
    register_orch_budget_guard(dispatcher);
}

/// A [`Dispatcher`] over a fresh [`Registry`] with all five A01 tool
/// contracts and both handlers registered — the composed migration
/// wiring. `legacy_fallback_enabled` is the per-tool rollback flag
/// (`true` = python default); every tool keeps its own [`Tool`] so the
/// caller can flip one tool's flag independently afterwards.
pub fn a01_batch_dispatcher(legacy_fallback_enabled: bool) -> Dispatcher {
    let mut registry = Registry::new();
    for tool in [
        orch_dag_blocked_tool(legacy_fallback_enabled),
        orch_gate_coverage_tool(legacy_fallback_enabled),
        orch_rework_bound_tool(legacy_fallback_enabled),
        orch_dispatch_batch_tool(legacy_fallback_enabled),
        orch_budget_guard_tool(legacy_fallback_enabled),
    ] {
        registry
            .register(tool)
            .expect("A01 batch tools are well-formed");
    }
    let mut dispatcher = Dispatcher::new(registry);
    register_a01_batch(&mut dispatcher);
    dispatcher
}

// ---------------------------------------------------------------------------
// Numbers: Python's int/float propagation
// ---------------------------------------------------------------------------
//
// Python ints are arbitrary precision. JSON integers that arrive as
// `i64` stay exact, and every operation a tool performs preserves the
// result type (int op int -> int, anything float -> float). [`Num`]
// carries that tag so emitted JSON has the same int/float shape the
// Python dicts have (`2` vs `2.0`). Comparisons go through `f64`,
// which is exact for integers below 2^53 in magnitude — counts and
// budgets are far below that in practice. `i64` arithmetic saturates
// instead of overflowing (astronomical payloads are outside the
// contract).

#[derive(Debug, Clone, Copy)]
enum Num {
    I(i64),
    F(f64),
}

impl Num {
    /// Parses a JSON value as a number; rejects bools and non-numbers.
    fn of(value: &Value) -> Option<Num> {
        value
            .as_i64()
            .map(Num::I)
            .or_else(|| value.as_f64().map(Num::F))
    }

    fn f(self) -> f64 {
        match self {
            Num::I(i) => i as f64,
            Num::F(f) => f,
        }
    }

    fn add(self, other: Num) -> Num {
        match (self, other) {
            (Num::I(a), Num::I(b)) => Num::I(a.saturating_add(b)),
            (a, b) => Num::F(a.f() + b.f()),
        }
    }

    fn sub(self, other: Num) -> Num {
        match (self, other) {
            (Num::I(a), Num::I(b)) => Num::I(a.saturating_sub(b)),
            (a, b) => Num::F(a.f() - b.f()),
        }
    }

    /// Python `max(0, x)`: the int literal `0` wins whenever `x <= 0`
    /// (including `x == 0.0`), else `x` keeps its own type.
    fn max_zero(self) -> Num {
        if self.f() > 0.0 {
            self
        } else {
            Num::I(0)
        }
    }

    fn to_json(self) -> Value {
        match self {
            Num::I(i) => Value::from(i),
            Num::F(f) => serde_json::Number::from_f64(f)
                .map(Value::Number)
                .unwrap_or(Value::Null),
        }
    }
}

/// Whether `value` is a JSON number (int or float), mirroring Python
/// `_is_num` (`isinstance(x, (int, float)) and not isinstance(x,
/// bool)`; JSON has no NaN/Infinity literals to exclude).
fn is_number(value: &Value) -> bool {
    !value.is_boolean() && (value.is_i64() || value.is_u64() || value.is_f64())
}

// ---------------------------------------------------------------------------
// Structural payload helpers
// ---------------------------------------------------------------------------
//
// Structural defects (missing keys, wrong JSON types) raise in Python
// (`KeyError`/`TypeError`); they are typed [`DispatchError::InvalidInput`]
// errors here, in the graph family's dialect — never value dicts.
// Contract-level rejections (the "field"/"message" dicts the Python
// tools return) are [`Ok`] values, byte-identical to the Python dicts.

/// Builds a structural payload error for `tool`.
fn invalid(tool: &'static str, message: String) -> DispatchError {
    DispatchError::InvalidInput {
        name: tool.to_string(),
        message,
    }
}

/// A contract-level invalid-input *value*: the exact dict the Python
/// tool returns. Carried as `Ok` so dispatch observes it unchanged.
fn invalid_dict(field: &'static str, message: String) -> Value {
    json!({"state": "INVALID_INPUT", "field": field, "message": message})
}

/// The payload object (the dispatcher guarantees an object already; the
/// check keeps the handler total when called directly).
fn as_object<'a>(
    tool: &'static str,
    payload: &'a Value,
) -> Result<&'a serde_json::Map<String, Value>, DispatchError> {
    payload
        .as_object()
        .ok_or_else(|| invalid(tool, "payload must be a JSON object".to_string()))
}

/// A required top-level array field, structurally.
fn field_array<'a>(
    tool: &'static str,
    obj: &'a serde_json::Map<String, Value>,
    key: &str,
) -> Result<&'a Vec<Value>, DispatchError> {
    match obj.get(key) {
        Some(Value::Array(items)) => Ok(items),
        Some(_) => Err(invalid(tool, format!("payload \"{key}\" must be an array"))),
        None => Err(invalid(
            tool,
            format!("payload must carry a \"{key}\" array"),
        )),
    }
}

/// A required top-level object field, structurally.
fn field_object<'a>(
    tool: &'static str,
    obj: &'a serde_json::Map<String, Value>,
    key: &str,
) -> Result<&'a serde_json::Map<String, Value>, DispatchError> {
    match obj.get(key) {
        Some(Value::Object(map)) => Ok(map),
        Some(_) => Err(invalid(
            tool,
            format!("payload \"{key}\" must be an object"),
        )),
        None => Err(invalid(
            tool,
            format!("payload must carry a \"{key}\" object"),
        )),
    }
}

/// A required top-level string field, structurally.
fn field_string<'a>(
    tool: &'static str,
    obj: &'a serde_json::Map<String, Value>,
    key: &str,
) -> Result<&'a str, DispatchError> {
    match obj.get(key) {
        Some(Value::String(s)) => Ok(s),
        Some(_) => Err(invalid(tool, format!("payload \"{key}\" must be a string"))),
        None => Err(invalid(
            tool,
            format!("payload must carry a \"{key}\" string"),
        )),
    }
}

/// A required top-level number field, structurally (any JSON number).
fn field_number(
    tool: &'static str,
    obj: &serde_json::Map<String, Value>,
    key: &str,
) -> Result<Num, DispatchError> {
    match obj.get(key) {
        Some(value) => Num::of(value)
            .ok_or_else(|| invalid(tool, format!("payload \"{key}\" must be a number"))),
        None => Err(invalid(
            tool,
            format!("payload must carry a \"{key}\" number"),
        )),
    }
}

// ---------------------------------------------------------------------------
// orch_dag_blocked
// ---------------------------------------------------------------------------

/// Kahn's algorithm over in-payload edges, mirroring
/// `swarm.tools.a01_tools._unresolved`: counts pending *known* deps per
/// task, then repeatedly pops zero-pending tasks off a stack. The
/// remainder — cycle members and everything depending on one — is
/// exact regardless of pop order; the Python version iterates the
/// payload's task order, which fixes the stack order the same way.
fn unresolved_of(deps: &[(&str, BTreeSet<String>)]) -> BTreeSet<String> {
    let known: BTreeSet<String> = deps.iter().map(|(id, _)| (*id).to_string()).collect();
    let mut pending: HashMap<&str, usize> = HashMap::new();
    let mut dependents: HashMap<&str, Vec<&str>> = HashMap::new();
    for (id, needs) in deps {
        pending.insert(id, needs.intersection(&known).count());
        for need in needs.intersection(&known) {
            dependents.entry(need.as_str()).or_default().push(id);
        }
    }
    let mut ready: Vec<&str> = deps
        .iter()
        .map(|(id, _)| *id)
        .filter(|id| pending[id] == 0)
        .collect();
    while let Some(done) = ready.pop() {
        if let Some(nexts) = dependents.get(done) {
            for next in nexts {
                let count = pending.get_mut(next).expect("dependent pending tracked");
                *count -= 1;
                if *count == 0 {
                    ready.push(next);
                }
            }
        }
    }
    deps.iter()
        .map(|(id, _)| *id)
        .filter(|id| pending[id] > 0)
        .map(str::to_string)
        .collect()
}

/// Shared core of both handlers for [`ORCH_DAG_BLOCKED_TOOL`].
fn dag_blocked_impl(payload: &Value) -> Result<Value, DispatchError> {
    let tool = ORCH_DAG_BLOCKED_TOOL;
    let obj = as_object(tool, payload)?;
    let tasks = field_array(tool, obj, "tasks")?;

    // Structural parse: each entry needs a string id and a depends_on
    // string array; the `state` rides as an arbitrary JSON value (only
    // string states classify — anything else is skipped, exactly as in
    // Python where the value only ever gets compared to string sets).
    let mut ids: Vec<String> = Vec::with_capacity(tasks.len());
    let mut state: Vec<(String, Value)> = Vec::with_capacity(tasks.len());
    let mut deps_owned: Vec<BTreeSet<String>> = Vec::with_capacity(tasks.len());
    for entry in tasks {
        let entry = entry.as_object().ok_or_else(|| {
            invalid(
                tool,
                "each task must be an object with a string \"id\", a \"state\", and a \"depends_on\" array of strings"
                    .to_string(),
            )
        })?;
        let id = entry
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid(tool, "each task must carry a string \"id\"".to_string()))?;
        let task_state = entry
            .get("state")
            .cloned()
            .ok_or_else(|| invalid(tool, "each task must carry a \"state\"".to_string()))?;
        let depends = entry.get("depends_on").ok_or_else(|| {
            invalid(
                tool,
                "each task must carry a \"depends_on\" array of strings".to_string(),
            )
        })?;
        let depends = depends.as_array().ok_or_else(|| {
            invalid(
                tool,
                "each task must carry a \"depends_on\" array of strings".to_string(),
            )
        })?;
        let mut set = BTreeSet::new();
        for dep in depends {
            match dep.as_str() {
                Some(dep) => {
                    set.insert(dep.to_string());
                }
                None => {
                    return Err(invalid(
                        tool,
                        "depends_on entries must be strings".to_string(),
                    ));
                }
            }
        }
        ids.push(id.to_string());
        state.push((id.to_string(), task_state));
        deps_owned.push(set);
    }
    let deps: Vec<(&str, BTreeSet<String>)> = ids
        .iter()
        .zip(deps_owned)
        .map(|(id, set)| (id.as_str(), set))
        .collect();

    // Contract check: duplicate task ids (sorted for the message).
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut dupes: BTreeSet<String> = BTreeSet::new();
    for id in &ids {
        if !seen.insert(id.clone()) {
            dupes.insert(id.clone());
        }
    }
    if !dupes.is_empty() {
        let dupes: Vec<String> = dupes.into_iter().collect();
        return Ok(invalid_dict(
            "tasks",
            format!("duplicate task ids: {}", dupes.join(", ")),
        ));
    }

    let state_map: BTreeMap<&str, &Value> = state
        .iter()
        .map(|(id, value)| (id.as_str(), value))
        .collect();
    let unresolved = unresolved_of(&deps);
    let deps_map: BTreeMap<&str, &BTreeSet<String>> =
        deps.iter().map(|(id, set)| (*id, set)).collect();

    let is_dispatchable = |value: &Value| {
        value
            .as_str()
            .is_some_and(|s| matches!(s, "PLANNED" | "RETRY"))
    };
    let state_in_ab =
        |value: &Value, choices: &[&str]| value.as_str().is_some_and(|s| choices.contains(&s));

    let mut ready: Vec<String> = Vec::new();
    let mut waiting: Vec<String> = Vec::new();
    let mut blocked: Vec<Value> = Vec::new();
    // Python iterates `sorted(state)` — ids in lexicographic order.
    let mut ordered_ids: Vec<&str> = state_map.keys().copied().collect();
    ordered_ids.sort_unstable();
    for tid in ordered_ids {
        let tid_state = state_map[tid];
        if !is_dispatchable(tid_state) {
            continue;
        }
        let needs = deps_map[tid];
        let missing: Vec<String> = needs
            .iter()
            .filter(|dep| !state_map.contains_key(dep.as_str()))
            .cloned()
            .collect();
        let dead: Vec<String> = needs
            .iter()
            .filter(|dep| {
                state_in_ab(
                    state_map.get(dep.as_str()).copied().unwrap_or(&Value::Null),
                    &["ESCALATED", "CANCELLED"],
                )
            })
            .cloned()
            .collect();
        if unresolved.contains(tid) {
            let cycle: Vec<String> = needs
                .iter()
                .filter(|dep| unresolved.contains(dep.as_str()))
                .cloned()
                .collect();
            blocked.push(json!({"id": tid, "reason": "cycle", "causes": cycle}));
        } else if !missing.is_empty() {
            blocked.push(json!({"id": tid, "reason": "missing_dependency", "causes": missing}));
        } else if !dead.is_empty() {
            blocked.push(json!({"id": tid, "reason": "dead_dependency", "causes": dead}));
        } else if needs
            .iter()
            .all(|dep| state_in_ab(state_map[dep.as_str()], &["IN_REVIEW", "APPROVED", "DONE"]))
        {
            ready.push(tid.to_string());
        } else {
            waiting.push(tid.to_string());
        }
    }

    Ok(json!({
        "state": "SUCCESS",
        "ready": ready,
        "waiting": waiting,
        "blocked": blocked,
        "unresolved": unresolved.iter().collect::<Vec<_>>(),
    }))
}

/// Rust native handler for [`ORCH_DAG_BLOCKED_TOOL`]: classifies
/// PLANNED/RETRY tasks as ready, waiting or blocked and reports
/// unorderable (cycle) tasks, exactly as `swarm.tools.a01_tools`
/// does over the payload.
pub fn native_orch_dag_blocked(payload: &Value) -> Result<Value, DispatchError> {
    dag_blocked_impl(payload)
}

/// Legacy Python handler for [`ORCH_DAG_BLOCKED_TOOL`]: reproduces the
/// `swarm.tools.a01_tools` behaviour behind the
/// `swarm.tool_registry.call_tool` contract.
pub fn legacy_orch_dag_blocked(payload: &Value) -> Result<Value, DispatchError> {
    dag_blocked_impl(payload)
}

// ---------------------------------------------------------------------------
// orch_gate_coverage
// ---------------------------------------------------------------------------

/// Shared core of both handlers for [`ORCH_GATE_COVERAGE_TOOL`].
fn gate_coverage_impl(payload: &Value) -> Result<Value, DispatchError> {
    let tool = ORCH_GATE_COVERAGE_TOOL;
    let obj = as_object(tool, payload)?;
    let rules = field_array(tool, obj, "rules")?;
    let risk = field_string(tool, obj, "risk")?;
    let results = field_array(tool, obj, "results")?;

    // Structural parse: risk of every rule first, exactly as Python
    // collects `risks` before anything else.
    let mut rule_risks: Vec<String> = Vec::with_capacity(rules.len());
    for rule in rules {
        let rule = rule.as_object().ok_or_else(|| {
            invalid(
                tool,
                "each rule must be an object with a \"risk\" and a \"gates\" array of strings"
                    .to_string(),
            )
        })?;
        let rule_risk = rule
            .get("risk")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid(tool, "each rule must carry a string \"risk\"".to_string()))?;
        rule_risks.push(rule_risk.to_string());
    }
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut has_dups = false;
    for risk in &rule_risks {
        if !seen.insert(risk.clone()) {
            has_dups = true;
            break;
        }
    }
    if has_dups {
        return Ok(invalid_dict("rules", "duplicate risk class".to_string()));
    }

    let Some(matched_idx) = rule_risks.iter().position(|r| r == risk) else {
        return Ok(invalid_dict("risk", format!("no rule for risk '{risk}'")));
    };

    // Gates of the first matched rule only (Python reads the matched
    // rule's gates lazily); duplicates dropped keeping first occurrence.
    let matched = rules[matched_idx]
        .as_object()
        .expect("rule structurally parsed");
    let gates = matched
        .get("gates")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            invalid(
                tool,
                "each rule must carry a \"gates\" array of strings".to_string(),
            )
        })?;
    let mut required: Vec<String> = Vec::new();
    for gate in gates {
        match gate.as_str() {
            Some(gate) => {
                if !required.iter().any(|seen| seen == gate) {
                    required.push(gate.to_string());
                }
            }
            None => {
                return Err(invalid(tool, "gates entries must be strings".to_string()));
            }
        }
    }

    // Latest verdict per gate (results are chronological; last wins).
    let mut last: BTreeMap<String, String> = BTreeMap::new();
    for row in results {
        let row = row.as_object().ok_or_else(|| {
            invalid(
                tool,
                "each result must be an object with a string \"gate\" and a string \"verdict\""
                    .to_string(),
            )
        })?;
        let gate = row
            .get("gate")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid(tool, "each result must carry a string \"gate\"".to_string()))?;
        let verdict = row.get("verdict").and_then(Value::as_str).ok_or_else(|| {
            invalid(
                tool,
                "each result must carry a string \"verdict\"".to_string(),
            )
        })?;
        last.insert(gate.to_string(), verdict.to_string());
    }

    let covered = required.iter().all(|gate| {
        matches!(
            last.get(gate).map(String::as_str),
            Some("pass") | Some("waive")
        )
    });
    let pick = |verdict: &str| -> Vec<String> {
        required
            .iter()
            .filter(|gate| last.get(*gate).map(String::as_str) == Some(verdict))
            .cloned()
            .collect()
    };
    let missing: Vec<String> = required
        .iter()
        .filter(|gate| !last.contains_key(*gate))
        .cloned()
        .collect();
    let mut extra: Vec<String> = last
        .keys()
        .filter(|gate| !required.contains(gate))
        .cloned()
        .collect();
    extra.sort();

    Ok(json!({
        "state": "SUCCESS",
        "covered": covered,
        "passed": pick("pass"),
        "waived": pick("waive"),
        "failed": pick("fail"),
        "missing": missing,
        "extra": extra,
    }))
}

/// Rust native handler for [`ORCH_GATE_COVERAGE_TOOL`]: checks the latest
/// verdict per gate against the gates a risk class requires.
pub fn native_orch_gate_coverage(payload: &Value) -> Result<Value, DispatchError> {
    gate_coverage_impl(payload)
}

/// Legacy Python handler for [`ORCH_GATE_COVERAGE_TOOL`].
pub fn legacy_orch_gate_coverage(payload: &Value) -> Result<Value, DispatchError> {
    gate_coverage_impl(payload)
}

// ---------------------------------------------------------------------------
// orch_rework_bound
// ---------------------------------------------------------------------------

/// Shared core of both handlers for [`ORCH_REWORK_BOUND_TOOL`].
fn rework_bound_impl(payload: &Value) -> Result<Value, DispatchError> {
    let tool = ORCH_REWORK_BOUND_TOOL;
    let obj = as_object(tool, payload)?;
    let loops = field_number(tool, obj, "rework_loops")?;
    let blocking = field_number(tool, obj, "blocking_findings")?;
    let cap = match obj.get("max_loops") {
        None => Num::I(2),
        Some(value) => Num::of(value)
            .ok_or_else(|| invalid(tool, "payload \"max_loops\" must be a number".to_string()))?,
    };

    let (decision, after) = if blocking.f() == 0.0 {
        ("PASS", loops)
    } else if loops.f() < cap.f() {
        ("REWORK", loops.add(Num::I(1)))
    } else {
        ("ESCALATED", loops)
    };
    let remaining = cap.sub(after).max_zero();

    Ok(json!({
        "state": "SUCCESS",
        "decision": decision,
        "rework_loops": after.to_json(),
        "loops_remaining": remaining.to_json(),
    }))
}

/// Rust native handler for [`ORCH_REWORK_BOUND_TOOL`]: applies the
/// rework-loop cap (default 2, then ESCALATED) to a gate failure.
pub fn native_orch_rework_bound(payload: &Value) -> Result<Value, DispatchError> {
    rework_bound_impl(payload)
}

/// Legacy Python handler for [`ORCH_REWORK_BOUND_TOOL`].
pub fn legacy_orch_rework_bound(payload: &Value) -> Result<Value, DispatchError> {
    rework_bound_impl(payload)
}

// ---------------------------------------------------------------------------
// orch_dispatch_batch
// ---------------------------------------------------------------------------

/// Shared core of both handlers for [`ORCH_DISPATCH_BATCH_TOOL`].
/// Fetch/validation order mirrors the Python handler exactly: the
/// three top-level keys, the dict checks, the value checks (running,
/// then ceilings), the ready-entry checks, then `global_cap` (only
/// touched while computing `free`).
fn dispatch_batch_impl(payload: &Value) -> Result<Value, DispatchError> {
    let tool = ORCH_DISPATCH_BATCH_TOOL;
    let obj = as_object(tool, payload)?;
    let ready = field_array(tool, obj, "ready")?;
    // A non-dict running/ceilings value is a contract INVALID_INPUT
    // dict in Python; a missing key raises instead (structural).
    let running = match obj.get("running") {
        Some(Value::Object(map)) => map,
        Some(_) => {
            return Ok(invalid_dict(
                "running",
                "running must be an object of agent id to integer".to_string(),
            ));
        }
        None => {
            return Err(invalid(
                tool,
                "payload must carry a \"running\" object".to_string(),
            ));
        }
    };
    let ceilings = match obj.get("ceilings") {
        Some(Value::Object(map)) => map,
        Some(_) => {
            return Ok(invalid_dict(
                "ceilings",
                "ceilings must be an object of agent id to integer".to_string(),
            ));
        }
        None => {
            return Err(invalid(
                tool,
                "payload must carry a \"ceilings\" object".to_string(),
            ));
        }
    };

    for (agent, count) in running.iter() {
        match count.as_i64() {
            Some(count) if count >= 0 => {}
            _ => {
                return Ok(invalid_dict(
                    "running",
                    format!("running['{agent}'] must be an integer >= 0"),
                ));
            }
        }
    }
    for (agent, ceiling) in ceilings.iter() {
        match ceiling.as_i64() {
            Some(ceiling) if ceiling >= 1 => {}
            _ => {
                return Ok(invalid_dict(
                    "ceilings",
                    format!("ceilings['{agent}'] must be an integer >= 1"),
                ));
            }
        }
    }

    // Ready-entry checks first (Python returns the duplicate/ceiling
    // invalid-input values before `global_cap` is ever read).
    struct ReadyTask {
        id: String,
        agent: String,
        priority: f64,
        index: usize,
    }
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut tasks: Vec<ReadyTask> = Vec::with_capacity(ready.len());
    for (index, entry) in ready.iter().enumerate() {
        let entry = entry.as_object().ok_or_else(|| {
            invalid(
                tool,
                "each ready entry must be an object with a string \"id\" and a string \"agent_id\""
                    .to_string(),
            )
        })?;
        let id = entry.get("id").and_then(Value::as_str).ok_or_else(|| {
            invalid(
                tool,
                "each ready entry must carry a string \"id\"".to_string(),
            )
        })?;
        if !seen.insert(id.to_string()) {
            return Ok(invalid_dict("ready", format!("duplicate task id '{id}'")));
        }
        let agent = entry
            .get("agent_id")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                invalid(
                    tool,
                    "each ready entry must carry a string \"agent_id\"".to_string(),
                )
            })?;
        if !ceilings.contains_key(agent) {
            return Ok(invalid_dict(
                "ceilings",
                format!("no ceiling for agent '{agent}' (task '{id}'); pass its max_parallel"),
            ));
        }
        let priority = match entry.get("priority") {
            None => 0.0,
            Some(value) => value
                .as_f64()
                .ok_or_else(|| invalid(tool, "ready priorities must be numbers".to_string()))?,
        };
        tasks.push(ReadyTask {
            id: id.to_string(),
            agent: agent.to_string(),
            priority,
            index,
        });
    }

    let cap = field_number(tool, obj, "global_cap")?;
    let total: i64 = running.values().fold(0i64, |acc, value| {
        acc.saturating_add(value.as_i64().expect("validated above"))
    });
    let free = cap.sub(Num::I(total)).max_zero();

    let mut counts: HashMap<String, i64> = running
        .iter()
        .map(|(agent, count)| (agent.clone(), count.as_i64().expect("validated above")))
        .collect();
    // Order: priority (higher first), then the order supplied.
    tasks.sort_by(|a, b| {
        b.priority
            .partial_cmp(&a.priority)
            .unwrap_or(Ordering::Equal)
            .then(a.index.cmp(&b.index))
    });

    let mut start: Vec<String> = Vec::new();
    let mut deferred: Vec<Value> = Vec::new();
    for task in &tasks {
        let ceiling = ceilings[task.agent.as_str()]
            .as_i64()
            .expect("validated above");
        let slots_open = counts.get(task.agent.as_str()).copied().unwrap_or(0) < ceiling;
        if !slots_open {
            deferred.push(json!({"id": task.id, "reason": "agent_ceiling"}));
        } else if start.len() as f64 >= free.f() {
            deferred.push(json!({"id": task.id, "reason": "global_cap"}));
        } else {
            *counts.entry(task.agent.clone()).or_insert(0) += 1;
            start.push(task.id.clone());
        }
    }

    let slots: BTreeMap<&str, i64> = ceilings
        .iter()
        .map(|(agent, ceiling)| {
            let free_slots = ceiling
                .as_i64()
                .expect("validated above")
                .saturating_sub(counts.get(agent).copied().unwrap_or(0));
            (agent.as_str(), free_slots.max(0))
        })
        .collect();

    let global_free = free.sub(Num::I(start.len() as i64));
    Ok(json!({
        "state": "SUCCESS",
        "start": start,
        "deferred": deferred,
        "slots": slots,
        "global_free": global_free.to_json(),
    }))
}

/// Rust native handler for [`ORCH_DISPATCH_BATCH_TOOL`]: picks which
/// ready tasks start this round under each agent's ceiling, then the
/// global cap.
pub fn native_orch_dispatch_batch(payload: &Value) -> Result<Value, DispatchError> {
    dispatch_batch_impl(payload)
}

/// Legacy Python handler for [`ORCH_DISPATCH_BATCH_TOOL`].
pub fn legacy_orch_dispatch_batch(payload: &Value) -> Result<Value, DispatchError> {
    dispatch_batch_impl(payload)
}

// ---------------------------------------------------------------------------
// orch_budget_guard
// ---------------------------------------------------------------------------

/// Python `round(x, 4)`: round-half-even on the double's *exact*
/// decimal value, reproduced through exact integer arithmetic:
/// decompose `x = m * 2^e` (m below 2^53), form the scaled rational
/// `m * 10^4 * 2^e`, round it half-even to an integer `k`, and emit
/// `k / 10000`.
///
/// A float multiply (`(x * 10000).round_ties_even()`) can round across
/// a tie boundary and disagree with Python — e.g. for `0.12345`, where
/// the stored double sits just above the tie (`Python round` gives
/// `0.1235`) but the f64 product lands exactly on `1234.5` (yielding
/// `0.1234`).
fn round4(x: f64) -> f64 {
    if !x.is_finite() || x == 0.0 {
        return x;
    }
    let bits = x.to_bits();
    let sign = if bits >> 63 != 0 { -1.0f64 } else { 1.0f64 };
    let raw_exp = ((bits >> 52) & 0x7ff) as i32;
    let frac = bits & ((1u64 << 52) - 1);
    let (m, e): (u64, i32) = if raw_exp == 0 {
        (frac, -1074) // subnormal
    } else {
        (frac + (1u64 << 52), raw_exp - 1075)
    };
    if e >= 0 {
        // |x| ≥ 2^52: x is already an integer multiple of 1, so a
        // 4-decimal rounding is the identity (Python's `round` agrees).
        return x;
    }
    let shift = (-e) as u32;
    // scaled = m * 10^4 (below 9.01e19, fits u128); the value to round
    // is scaled / 2^shift.
    let scaled: u128 = m as u128 * 10_000;
    let k: u128 = if shift >= 128 {
        // scaled < 2^67 << 2^(shift-1): strictly below the half point.
        0
    } else {
        let q = scaled >> shift;
        let r = scaled & ((1u128 << shift) - 1);
        let half = 1u128 << (shift - 1);
        if r > half || (r == half && q % 2 == 1) {
            q + 1
        } else {
            q
        }
    };
    // `k` is exact in f64 whenever |x| * 10^4 < 2^53 (|x| below ~9e11,
    // far beyond any budget ratio); beyond that the conversion rounds
    // once before the division rounds again and can differ from
    // Python's once-converted result by 1 ulp.
    sign * ((k as f64) / 10_000.0)
}

/// Python `_shown_ratio`: four decimals, but never `1.0` for a dimension
/// that did not breach (299999.99 of 300000 tokens shows `0.9999`).
fn shown_ratio(ratio: f64, breached: bool) -> f64 {
    let shown = round4(ratio);
    if breached || shown < 1.0 {
        shown
    } else {
        0.9999
    }
}

/// Shared core of both handlers for [`ORCH_BUDGET_GUARD_TOOL`].
fn budget_guard_impl(payload: &Value) -> Result<Value, DispatchError> {
    let tool = ORCH_BUDGET_GUARD_TOOL;
    let obj = as_object(tool, payload)?;
    let budget = field_object(tool, obj, "budget")?;
    let usage = field_object(tool, obj, "usage")?;

    // warn_at validation comes before any budget math, exactly as in
    // the Python handler.
    let warn_at = match obj.get("warn_at") {
        None => 0.8,
        Some(value) if is_number(value) => {
            let w = value.as_f64().expect("numbers convert");
            if !(0.0 < w && w < 1.0) {
                return Ok(invalid_dict(
                    "warn_at",
                    "warn_at must be a number above 0 and below 1".to_string(),
                ));
            }
            w
        }
        Some(_) => {
            return Ok(invalid_dict(
                "warn_at",
                "warn_at must be a number above 0 and below 1".to_string(),
            ));
        }
    };

    const DIMS: [&str; 3] = ["tokens", "wall_s", "cost_usd"];
    const EPSILON: f64 = 1e-9;
    let mut ratios: Vec<(String, f64)> = Vec::with_capacity(3);
    for dim in DIMS {
        let limit_key = format!("max_{dim}");
        let limit = match budget.get(limit_key.as_str()) {
            Some(value) if is_number(value) => value.as_f64().expect("numbers convert"),
            _ => {
                return Ok(invalid_dict(
                    "budget",
                    format!("{limit_key} must be a number above 0"),
                ));
            }
        };
        if limit <= 0.0 {
            return Ok(invalid_dict(
                "budget",
                format!("{limit_key} must be a number above 0"),
            ));
        }
        let used = match usage.get(dim) {
            Some(value) if is_number(value) => value.as_f64().expect("numbers convert"),
            _ => {
                return Ok(invalid_dict(
                    "usage",
                    format!("{dim} must be a number >= 0"),
                ));
            }
        };
        if used < 0.0 {
            return Ok(invalid_dict(
                "usage",
                format!("{dim} must be a number >= 0"),
            ));
        }
        let ratio = used / limit;
        if !ratio.is_finite() {
            return Ok(invalid_dict(
                "usage",
                format!("{dim} is too large relative to {limit_key} to compare"),
            ));
        }
        ratios.push((dim.to_string(), ratio));
    }

    let breached: Vec<String> = ratios
        .iter()
        .filter(|(_, ratio)| *ratio >= 1.0 - EPSILON)
        .map(|(dim, _)| dim.clone())
        .collect();

    let prior = field_number(tool, obj, "prior_breaches")?;
    let (decision, next_action) = if !breached.is_empty() && prior.f() >= 1.0 {
        ("ESCALATED", "escalate")
    } else if !breached.is_empty() {
        ("HARD_STOP", "requeue_once_annotated")
    } else if ratios
        .iter()
        .map(|(_, ratio)| *ratio)
        .fold(f64::NEG_INFINITY, f64::max)
        >= warn_at - EPSILON
    {
        ("WARN", "note_usage")
    } else {
        ("OK", "continue")
    };

    let ratio_map: BTreeMap<&str, f64> = ratios
        .iter()
        .map(|(dim, ratio)| (dim.as_str(), shown_ratio(*ratio, breached.contains(dim))))
        .collect();
    let breaches_after = if breached.is_empty() {
        prior
    } else {
        prior.add(Num::I(1))
    };

    Ok(json!({
        "state": "SUCCESS",
        "decision": decision,
        "ratios": ratio_map,
        "breached": breached,
        "next_action": next_action,
        "breaches_after": breaches_after.to_json(),
    }))
}

/// Rust native handler for [`ORCH_BUDGET_GUARD_TOOL`]: decides OK, WARN,
/// HARD_STOP, or ESCALATED from usage against budget and earlier
/// breaches.
pub fn native_orch_budget_guard(payload: &Value) -> Result<Value, DispatchError> {
    budget_guard_impl(payload)
}

/// Legacy Python handler for [`ORCH_BUDGET_GUARD_TOOL`].
pub fn legacy_orch_budget_guard(payload: &Value) -> Result<Value, DispatchError> {
    budget_guard_impl(payload)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::registry_dispatch::{AuditKind, Route};
    use serde_json::json;

    // -----------------------------------------------------------------
    // Parity helpers. Expected values below are the actual outputs of
    // `swarm.tools.a01_tools.<fn>(payload)` on branch `swarm/T74c2-be`
    // (captured with json.dumps(..., sort_keys=True) and re-serialized
    // as Rust literals). The tool dicts — including `state` and the
    // INVALID_INPUT dicts — must be value-identical.
    // -----------------------------------------------------------------

    fn native(payload: &Value, name: &str) -> Value {
        match name {
            ORCH_DAG_BLOCKED_TOOL => native_orch_dag_blocked(payload).unwrap(),
            ORCH_GATE_COVERAGE_TOOL => native_orch_gate_coverage(payload).unwrap(),
            ORCH_REWORK_BOUND_TOOL => native_orch_rework_bound(payload).unwrap(),
            ORCH_DISPATCH_BATCH_TOOL => native_orch_dispatch_batch(payload).unwrap(),
            ORCH_BUDGET_GUARD_TOOL => native_orch_budget_guard(payload).unwrap(),
            _ => panic!("unknown tool {name}"),
        }
    }

    fn legacy(payload: &Value, name: &str) -> Value {
        match name {
            ORCH_DAG_BLOCKED_TOOL => legacy_orch_dag_blocked(payload).unwrap(),
            ORCH_GATE_COVERAGE_TOOL => legacy_orch_gate_coverage(payload).unwrap(),
            ORCH_REWORK_BOUND_TOOL => legacy_orch_rework_bound(payload).unwrap(),
            ORCH_DISPATCH_BATCH_TOOL => legacy_orch_dispatch_batch(payload).unwrap(),
            ORCH_BUDGET_GUARD_TOOL => legacy_orch_budget_guard(payload).unwrap(),
            _ => panic!("unknown tool {name}"),
        }
    }

    fn assert_parity(name: &str, payload: &Value, expected: Value) {
        assert_eq!(
            native(payload, name),
            expected,
            "native mismatch for {name}"
        );
        assert_eq!(
            legacy(payload, name),
            expected,
            "legacy mismatch for {name}"
        );
        let outcome = a01_batch_dispatcher(true)
            .dispatch("A01", name, payload.clone())
            .unwrap();
        assert_eq!(outcome.route, Route::Native, "route for {name}");
        assert_eq!(outcome.value, expected, "dispatch mismatch for {name}");
    }

    // ---- orch_dag_blocked -------------------------------------------

    #[test]
    fn dag_blocked_parity() {
        let payload = json!({"tasks": [
            {"id":"a","state":"PLANNED","depends_on":[]},
            {"id":"b","state":"PLANNED","depends_on":["a"]},
            {"id":"c","state":"DONE","depends_on":[]},
            {"id":"d","state":"PLANNED","depends_on":["c"]},
            {"id":"e","state":"PLANNED","depends_on":["missing"]},
            {"id":"f","state":"PLANNED","depends_on":["g"]},
            {"id":"g","state":"PLANNED","depends_on":["f"]},
            {"id":"h","state":"PLANNED","depends_on":["c","a"]},
            {"id":"i","state":"IN_PROGRESS","depends_on":[]},
        ]});
        assert_parity(
            ORCH_DAG_BLOCKED_TOOL,
            &payload,
            json!({"state": "SUCCESS", "ready": ["a", "d"], "waiting": ["b", "h"],
                "blocked": [
                    {"causes": ["missing"], "id": "e", "reason": "missing_dependency"},
                    {"causes": ["g"], "id": "f", "reason": "cycle"},
                    {"causes": ["f"], "id": "g", "reason": "cycle"},
                ],
                "unresolved": ["f", "g"]}),
        );
    }

    #[test]
    fn dag_blocked_empty_and_done_parity() {
        assert_parity(
            ORCH_DAG_BLOCKED_TOOL,
            &json!({"tasks": []}),
            json!({"state": "SUCCESS", "ready": [], "waiting": [], "blocked": [], "unresolved": []}),
        );
        assert_parity(
            ORCH_DAG_BLOCKED_TOOL,
            &json!({"tasks": [
                {"id":"x","state":"DONE","depends_on":[]},
                {"id":"y","state":"PLANNED","depends_on":["x"]},
                {"id":"z","state":"DEAD","depends_on":[]},
                {"id":"w","state":"PLANNED","depends_on":["z"]},
            ]}),
            json!({"state": "SUCCESS", "ready": ["y"], "waiting": ["w"], "blocked": [], "unresolved": []}),
        );
    }

    #[test]
    fn dag_blocked_duplicate_error_dict_parity() {
        assert_parity(
            ORCH_DAG_BLOCKED_TOOL,
            &json!({"tasks": [
                {"id":"x","state":"PLANNED","depends_on":[]},
                {"id":"x","state":"PLANNED","depends_on":[]},
            ]}),
            json!({"state": "INVALID_INPUT", "field": "tasks", "message": "duplicate task ids: x"}),
        );
    }

    #[test]
    fn dag_blocked_structural_error() {
        let err = native_orch_dag_blocked(&json!({})).unwrap_err();
        assert!(
            matches!(err, DispatchError::InvalidInput { ref name, .. } if name == ORCH_DAG_BLOCKED_TOOL),
            "got {err:?}"
        );
    }

    // ---- orch_gate_coverage ------------------------------------------

    #[test]
    fn gate_coverage_parity() {
        assert_parity(
            ORCH_GATE_COVERAGE_TOOL,
            &json!({
                "rules": [
                    {"risk": "high", "gates": ["q", "s", "sec"]},
                    {"risk": "low", "gates": ["q"]},
                ],
                "risk": "high",
                "results": [
                    {"gate": "q", "verdict": "pass"},
                    {"gate": "s", "verdict": "waive"},
                    {"gate": "q", "verdict": "fail"},
                    {"gate": "x", "verdict": "pass"},
                ],
            }),
            json!({"state": "SUCCESS", "covered": false, "passed": [], "waived": ["s"],
                "failed": ["q"], "missing": ["sec"], "extra": ["x"]}),
        );
    }

    #[test]
    fn gate_coverage_covered_parity() {
        assert_parity(
            ORCH_GATE_COVERAGE_TOOL,
            &json!({
                "rules": [{"risk": "high", "gates": ["q", "s", "q"]}],
                "risk": "high",
                "results": [
                    {"gate": "q", "verdict": "pass"},
                    {"gate": "s", "verdict": "waive"},
                ],
            }),
            json!({"state": "SUCCESS", "covered": true, "passed": ["q"], "waived": ["s"],
                "failed": [], "missing": [], "extra": []}),
        );
    }

    #[test]
    fn gate_coverage_error_dicts_parity() {
        assert_parity(
            ORCH_GATE_COVERAGE_TOOL,
            &json!({
                "rules": [
                    {"risk": "high", "gates": ["q"]},
                    {"risk": "high", "gates": ["s"]},
                ],
                "risk": "high",
                "results": [],
            }),
            json!({"state": "INVALID_INPUT", "field": "rules", "message": "duplicate risk class"}),
        );
        assert_parity(
            ORCH_GATE_COVERAGE_TOOL,
            &json!({
                "rules": [{"risk": "low", "gates": ["q"]}],
                "risk": "high",
                "results": [],
            }),
            json!({"state": "INVALID_INPUT", "field": "risk", "message": "no rule for risk 'high'"}),
        );
    }

    // ---- orch_rework_bound -------------------------------------------

    #[test]
    fn rework_bound_parity() {
        assert_parity(
            ORCH_REWORK_BOUND_TOOL,
            &json!({"rework_loops": 1, "blocking_findings": 0}),
            json!({"state": "SUCCESS", "decision": "PASS", "rework_loops": 1, "loops_remaining": 1}),
        );
        assert_parity(
            ORCH_REWORK_BOUND_TOOL,
            &json!({"rework_loops": 0, "blocking_findings": 2}),
            json!({"state": "SUCCESS", "decision": "REWORK", "rework_loops": 1, "loops_remaining": 1}),
        );
        assert_parity(
            ORCH_REWORK_BOUND_TOOL,
            &json!({"rework_loops": 2, "blocking_findings": 1, "max_loops": 2}),
            json!({"state": "SUCCESS", "decision": "ESCALATED", "rework_loops": 2, "loops_remaining": 0}),
        );
        assert_parity(
            ORCH_REWORK_BOUND_TOOL,
            &json!({"rework_loops": 5, "blocking_findings": 3}),
            json!({"state": "SUCCESS", "decision": "ESCALATED", "rework_loops": 5, "loops_remaining": 0}),
        );
    }

    #[test]
    fn rework_bound_float_parity() {
        // Python passes the float through (input_schema says integer,
        // so this is handler-level parity only): 1.5 loops, cap 2.
        assert_parity(
            ORCH_REWORK_BOUND_TOOL,
            &json!({"rework_loops": 1.5, "blocking_findings": 1}),
            json!({"state": "SUCCESS", "decision": "REWORK", "rework_loops": 2.5, "loops_remaining": 0}),
        );
    }

    // ---- orch_dispatch_batch -----------------------------------------

    #[test]
    fn dispatch_batch_parity() {
        assert_parity(
            ORCH_DISPATCH_BATCH_TOOL,
            &json!({
                "ready": [
                    {"id": "t1", "agent_id": "A01"},
                    {"id": "t2", "agent_id": "A01", "priority": 5},
                    {"id": "t3", "agent_id": "A02"},
                ],
                "running": {"A01": 1, "A02": 0},
                "ceilings": {"A01": 2, "A02": 1},
                "global_cap": 2,
            }),
            json!({"state": "SUCCESS", "start": ["t2"],
                "deferred": [
                    {"id": "t1", "reason": "agent_ceiling"},
                    {"id": "t3", "reason": "global_cap"},
                ],
                "slots": {"A01": 0, "A02": 1}, "global_free": 0}),
        );
    }

    #[test]
    fn dispatch_batch_error_dicts_parity() {
        assert_parity(
            ORCH_DISPATCH_BATCH_TOOL,
            &json!({
                "ready": [
                    {"id": "t1", "agent_id": "A01"},
                    {"id": "t1", "agent_id": "A01"},
                ],
                "running": {},
                "ceilings": {"A01": 2},
                "global_cap": 2,
            }),
            json!({"state": "INVALID_INPUT", "field": "ready", "message": "duplicate task id 't1'"}),
        );
        assert_parity(
            ORCH_DISPATCH_BATCH_TOOL,
            &json!({
                "ready": [{"id": "t1", "agent_id": "A09"}],
                "running": {},
                "ceilings": {"A01": 2},
                "global_cap": 2,
            }),
            json!({"state": "INVALID_INPUT", "field": "ceilings",
                "message": "no ceiling for agent 'A09' (task 't1'); pass its max_parallel"}),
        );
        assert_parity(
            ORCH_DISPATCH_BATCH_TOOL,
            &json!({
                "ready": [],
                "running": {"A01": true},
                "ceilings": {"A01": 2},
                "global_cap": 2,
            }),
            json!({"state": "INVALID_INPUT", "field": "running",
                "message": "running['A01'] must be an integer >= 0"}),
        );
        assert_parity(
            ORCH_DISPATCH_BATCH_TOOL,
            &json!({
                "ready": [],
                "running": {},
                "ceilings": {"A01": 0},
                "global_cap": 2,
            }),
            json!({"state": "INVALID_INPUT", "field": "ceilings",
                "message": "ceilings['A01'] must be an integer >= 1"}),
        );
    }

    #[test]
    fn dispatch_batch_float_cap_parity() {
        // global_cap 0.5: free = max(0, 0.5 - 0) = 0.5; t1 starts
        // (0 < 0.5), t2 defers global_cap (1 >= 0.5); global_free =
        // 0.5 - 1 = -0.5.
        assert_parity(
            ORCH_DISPATCH_BATCH_TOOL,
            &json!({
                "ready": [
                    {"id": "t1", "agent_id": "A01"},
                    {"id": "t2", "agent_id": "A01"},
                ],
                "running": {},
                "ceilings": {"A01": 4},
                "global_cap": 0.5,
            }),
            json!({"state": "SUCCESS", "start": ["t1"],
                "deferred": [{"id": "t2", "reason": "global_cap"}],
                "slots": {"A01": 3}, "global_free": -0.5}),
        );
    }

    // ---- orch_budget_guard -------------------------------------------

    fn budget_payload(usage: Value, max_tokens: Value, prior: Value) -> Value {
        json!({
            "budget": {"max_tokens": max_tokens, "max_wall_s": 10, "max_cost_usd": 50},
            "usage": usage,
            "prior_breaches": prior,
        })
    }

    #[test]
    fn budget_guard_parity() {
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": 10, "wall_s": 1, "cost_usd": 5}),
                json!(100),
                json!(0),
            ),
            json!({"state": "SUCCESS", "decision": "OK",
                "ratios": {"tokens": 0.1, "wall_s": 0.1, "cost_usd": 0.1},
                "breached": [], "next_action": "continue", "breaches_after": 0}),
        );
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": 85, "wall_s": 1, "cost_usd": 5}),
                json!(100),
                json!(0),
            ),
            json!({"state": "SUCCESS", "decision": "WARN",
                "ratios": {"tokens": 0.85, "wall_s": 0.1, "cost_usd": 0.1},
                "breached": [], "next_action": "note_usage", "breaches_after": 0}),
        );
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": 100, "wall_s": 1, "cost_usd": 5}),
                json!(100),
                json!(0),
            ),
            json!({"state": "SUCCESS", "decision": "HARD_STOP",
                "ratios": {"tokens": 1.0, "wall_s": 0.1, "cost_usd": 0.1},
                "breached": ["tokens"], "next_action": "requeue_once_annotated", "breaches_after": 1}),
        );
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": 100, "wall_s": 1, "cost_usd": 5}),
                json!(100),
                json!(1),
            ),
            json!({"state": "SUCCESS", "decision": "ESCALATED",
                "ratios": {"tokens": 1.0, "wall_s": 0.1, "cost_usd": 0.1},
                "breached": ["tokens"], "next_action": "escalate", "breaches_after": 2}),
        );
        // The 0.9999 clamp: 299999.99/300000 rounds to 1.0, but the
        // dimension did not breach → shows 0.9999.
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": 299999.99, "wall_s": 1, "cost_usd": 5}),
                json!(300000),
                json!(0),
            ),
            json!({"state": "SUCCESS", "decision": "WARN",
                "ratios": {"tokens": 0.9999, "wall_s": 0.1, "cost_usd": 0.1},
                "breached": [], "next_action": "note_usage", "breaches_after": 0}),
        );
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": 150, "wall_s": 1, "cost_usd": 5}),
                json!(100),
                json!(0),
            ),
            json!({"state": "SUCCESS", "decision": "HARD_STOP",
                "ratios": {"tokens": 1.5, "wall_s": 0.1, "cost_usd": 0.1},
                "breached": ["tokens"], "next_action": "requeue_once_annotated", "breaches_after": 1}),
        );
    }

    #[test]
    fn budget_guard_multi_dim_parity() {
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &json!({
                "budget": {"max_tokens": 100, "max_wall_s": 200, "max_cost_usd": 50},
                "usage": {"tokens": 10, "wall_s": 200, "cost_usd": 200},
                "prior_breaches": 0,
            }),
            json!({"state": "SUCCESS", "decision": "HARD_STOP",
                "ratios": {"tokens": 0.1, "wall_s": 1.0, "cost_usd": 4.0},
                "breached": ["wall_s", "cost_usd"], "next_action": "requeue_once_annotated",
                "breaches_after": 1}),
        );
    }

    #[test]
    fn budget_guard_tie_parity() {
        // Exact-tie ratios: Python round() breaks ties to even; the
        // Rust round4 must agree.
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": 15625, "wall_s": 1, "cost_usd": 5}),
                json!(100000),
                json!(0),
            ),
            json!({"state": "SUCCESS", "decision": "OK",
                "ratios": {"tokens": 0.1562, "wall_s": 0.1, "cost_usd": 0.1},
                "breached": [], "next_action": "continue", "breaches_after": 0}),
        );
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": 46875, "wall_s": 1, "cost_usd": 5}),
                json!(100000),
                json!(0),
            ),
            json!({"state": "SUCCESS", "decision": "OK",
                "ratios": {"tokens": 0.4688, "wall_s": 0.1, "cost_usd": 0.1},
                "breached": [], "next_action": "continue", "breaches_after": 0}),
        );
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": 53125, "wall_s": 1, "cost_usd": 5}),
                json!(100000),
                json!(0),
            ),
            json!({"state": "SUCCESS", "decision": "OK",
                "ratios": {"tokens": 0.5312, "wall_s": 0.1, "cost_usd": 0.1},
                "breached": [], "next_action": "continue", "breaches_after": 0}),
        );
    }

    #[test]
    fn budget_guard_error_dicts_parity() {
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &json!({
                "budget": {"max_tokens": 100, "max_wall_s": 10, "max_cost_usd": 50},
                "usage": {"tokens": 10, "wall_s": 1, "cost_usd": 5},
                "prior_breaches": 0,
                "warn_at": 1.0,
            }),
            json!({"state": "INVALID_INPUT", "field": "warn_at",
                "message": "warn_at must be a number above 0 and below 1"}),
        );
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": 10, "wall_s": 1, "cost_usd": 5}),
                json!(0),
                json!(0),
            ),
            json!({"state": "INVALID_INPUT", "field": "budget",
                "message": "max_tokens must be a number above 0"}),
        );
        assert_parity(
            ORCH_BUDGET_GUARD_TOOL,
            &budget_payload(
                json!({"tokens": -1, "wall_s": 1, "cost_usd": 5}),
                json!(100),
                json!(0),
            ),
            json!({"state": "INVALID_INPUT", "field": "usage",
                "message": "tokens must be a number >= 0"}),
        );
    }

    // ---- rounding parity ---------------------------------------------

    #[test]
    fn rounding_matches_python_round() {
        // Population of (value, python round(v, 4)) pairs, captured from
        // CPython 3.12's round() — the exact-decimal half-even
        // reference the Rust implementation must match. The ties
        // (x.xxxx5 exactly representable) and the above-tie doubles
        // (0.12345 etc.) are the cases where a naive
        // (x*10000).round_ties_even() diverges.
        let battery: [(f64, f64); 40] = [
            (0.15625, 0.1562),
            (0.46875, 0.4688),
            (0.53125, 0.5312),
            (0.12345, 0.1235),
            (0.49995, 0.5),
            (2.675, 2.675),
            (0.99995, 1.0),
            (5e-05, 0.0001),
            (1.00005, 1.0001),
            (0.85, 0.85),
            (0.1, 0.1),
            (1.5, 1.5),
            (0.03125, 0.0312),
            (0.0625, 0.0625),
            (0.2, 0.2),
            (0.3333, 0.3333),
            (2.6755, 2.6755),
            (0.00015, 0.0001),
            (0.12335, 0.1234),
            (0.98765, 0.9877),
            (3.285, 3.285),
            (0.07145, 0.0714),
            (0.61995, 0.62),
            (12.34005, 12.34),
            (1.23455, 1.2346),
            (8.875, 8.875),
            (5.55, 5.55),
            (0.7, 0.7),
            (0.75, 0.75),
            (0.125, 0.125),
            (0.375, 0.375),
            (0.625, 0.625),
            (0.875, 0.875),
            (1.2345, 1.2345),
            (1.2346, 1.2346),
            (0.999999, 1.0),
            (0.50005, 0.5),
            (0.50015, 0.5001),
            (999999.95 / 1000000.0, 1.0),
            (0.12345, 0.1235),
        ];
        for (value, expected) in battery {
            let got = round4(value);
            assert_eq!(
                got, expected,
                "round4({value}) = {got}, Python round gives {expected}"
            );
        }
    }

    // ---- isolation ----------------------------------------------------

    #[test]
    fn batch_isolated_to_a01() {
        let dispatcher = a01_batch_dispatcher(true);
        for name in A01_BATCH_TOOLS {
            let err = dispatcher.dispatch("A02", name, json!({})).unwrap_err();
            assert!(
                matches!(err, DispatchError::Unauthorized { ref caller, ref owner, .. }
                    if caller == "A02" && owner == "A01"),
                "expected Unauthorized for {name}, got {err:?}"
            );
        }
        let log = dispatcher.audit_log();
        assert_eq!(log.len(), 5);
        assert!(
            log.iter()
                .all(|entry| entry.kind == AuditKind::CrossAgentRejected),
            "all five rejections audited: {log:?}"
        );
        // Discovery: A02 sees none of the batch (no shared tools are
        // registered in this dispatcher); A01 sees all five.
        assert!(
            dispatcher.registry().tools_for_agent("A02").is_empty(),
            "A02 must not see A01's unique tools"
        );
        assert_eq!(
            dispatcher
                .registry()
                .tools_for_agent("A01")
                .iter()
                .map(|tool| tool.name.as_str())
                .collect::<Vec<_>>(),
            A01_BATCH_TOOLS.to_vec()
        );
        assert_eq!(
            dispatcher
                .registry()
                .tools_for_family(A01_FAMILY)
                .iter()
                .map(|tool| tool.name.as_str())
                .collect::<Vec<_>>(),
            A01_BATCH_TOOLS.to_vec()
        );
    }

    #[test]
    fn owner_can_call_every_batch_tool() {
        let dispatcher = a01_batch_dispatcher(true);
        let payloads = [
            (ORCH_DAG_BLOCKED_TOOL, json!({"tasks": []})),
            (
                ORCH_GATE_COVERAGE_TOOL,
                json!({"rules": [], "risk": "low", "results": []}),
            ),
            (
                ORCH_REWORK_BOUND_TOOL,
                json!({"rework_loops": 0, "blocking_findings": 0}),
            ),
            (
                ORCH_DISPATCH_BATCH_TOOL,
                json!({"ready": [], "running": {}, "ceilings": {}, "global_cap": 0}),
            ),
            (
                ORCH_BUDGET_GUARD_TOOL,
                json!({"budget": {"max_tokens": 1, "max_wall_s": 1, "max_cost_usd": 1},
                    "usage": {"tokens": 0, "wall_s": 0, "cost_usd": 0},
                    "prior_breaches": 0}),
            ),
        ];
        for (name, payload) in payloads {
            let outcome = dispatcher.dispatch("A01", name, payload).unwrap();
            assert_eq!(outcome.caller, "A01");
            assert_eq!(outcome.route, Route::Native);
            assert_eq!(outcome.attempts, 1);
        }
    }

    // ---- per-tool rollback ---------------------------------------------
    //
    // Rollback step 1 (flag off) is covered per tool by the
    // `*_tool(false)` constructor; step 2 (native removed) is covered
    // by building dispatchers that register only the legacy handler
    // for a tool.

    fn dispatcher_with_native_missing(name: &str, legacy_fallback_enabled: bool) -> Dispatcher {
        let mut registry = Registry::new();
        let tool = match name {
            ORCH_DAG_BLOCKED_TOOL => orch_dag_blocked_tool(legacy_fallback_enabled),
            ORCH_GATE_COVERAGE_TOOL => orch_gate_coverage_tool(legacy_fallback_enabled),
            ORCH_REWORK_BOUND_TOOL => orch_rework_bound_tool(legacy_fallback_enabled),
            ORCH_DISPATCH_BATCH_TOOL => orch_dispatch_batch_tool(legacy_fallback_enabled),
            ORCH_BUDGET_GUARD_TOOL => orch_budget_guard_tool(legacy_fallback_enabled),
            _ => unreachable!(),
        };
        registry.register(tool).unwrap();
        let mut dispatcher = Dispatcher::new(registry);
        // Register ONLY the legacy handler — the native registration is
        // left out, as rollback step 2 prescribes.
        match name {
            ORCH_DAG_BLOCKED_TOOL => {
                dispatcher.register_legacy(name, Box::new(legacy_orch_dag_blocked))
            }
            ORCH_GATE_COVERAGE_TOOL => {
                dispatcher.register_legacy(name, Box::new(legacy_orch_gate_coverage))
            }
            ORCH_REWORK_BOUND_TOOL => {
                dispatcher.register_legacy(name, Box::new(legacy_orch_rework_bound))
            }
            ORCH_DISPATCH_BATCH_TOOL => {
                dispatcher.register_legacy(name, Box::new(legacy_orch_dispatch_batch))
            }
            ORCH_BUDGET_GUARD_TOOL => {
                dispatcher.register_legacy(name, Box::new(legacy_orch_budget_guard))
            }
            _ => unreachable!(),
        }
        dispatcher
    }

    fn tool_payload(name: &str) -> Value {
        match name {
            ORCH_DAG_BLOCKED_TOOL => json!({"tasks": []}),
            ORCH_GATE_COVERAGE_TOOL => json!({"rules": [], "risk": "low", "results": []}),
            ORCH_REWORK_BOUND_TOOL => json!({"rework_loops": 0, "blocking_findings": 0}),
            ORCH_DISPATCH_BATCH_TOOL => {
                json!({"ready": [], "running": {}, "ceilings": {}, "global_cap": 0})
            }
            ORCH_BUDGET_GUARD_TOOL => {
                json!({"budget": {"max_tokens": 1, "max_wall_s": 1, "max_cost_usd": 1},
                "usage": {"tokens": 0, "wall_s": 0, "cost_usd": 0}, "prior_breaches": 0})
            }
            _ => unreachable!(),
        }
    }

    #[test]
    fn rollback_native_removed_flag_on_uses_legacy_route() {
        for name in A01_BATCH_TOOLS {
            let dispatcher = dispatcher_with_native_missing(name, true);
            let native = a01_batch_dispatcher(true)
                .dispatch("A01", name, tool_payload(name))
                .unwrap();
            let legacy = dispatcher
                .dispatch("A01", name, tool_payload(name))
                .unwrap();
            assert_eq!(native.route, Route::Native, "route for {name}");
            assert_eq!(legacy.route, Route::LegacyPython, "route for {name}");
            assert_eq!(native.value, legacy.value, "value parity for {name}");
            assert!(
                dispatcher
                    .audit_log()
                    .iter()
                    .any(|entry| entry.kind == AuditKind::LegacyFallback),
                "legacy fallback audited for {name}"
            );
        }
    }

    #[test]
    fn rollback_flag_off_without_native_fails_closed() {
        for name in A01_BATCH_TOOLS {
            let dispatcher = dispatcher_with_native_missing(name, false);
            let err = dispatcher
                .dispatch("A01", name, tool_payload(name))
                .unwrap_err();
            assert!(
                matches!(err, DispatchError::HandlerFailed { .. }),
                "expected HandlerFailed for {name}, got {err:?}"
            );
            assert!(
                !dispatcher
                    .audit_log()
                    .iter()
                    .any(|entry| entry.kind == AuditKind::LegacyFallback),
                "legacy handler never consulted for {name}"
            );
        }
    }

    #[test]
    fn rollback_flag_off_native_failure_never_calls_legacy() {
        // A structural payload error fails the native handler; with the
        // flag off the typed error surfaces and the legacy handler is
        // never consulted.
        let dispatcher = a01_batch_dispatcher(false);
        let err = dispatcher
            .dispatch("A01", ORCH_DAG_BLOCKED_TOOL, json!({}))
            .unwrap_err();
        assert!(
            matches!(err, DispatchError::InvalidInput { .. }),
            "native structural error surfaces, got {err:?}"
        );
        assert!(
            !dispatcher
                .audit_log()
                .iter()
                .any(|entry| entry.kind == AuditKind::LegacyFallback),
            "legacy handler must not be consulted when the flag is off"
        );
    }

    #[test]
    fn rollback_is_per_tool() {
        // One dispatcher where orch_rework_bound's flag is off and its
        // native handler is absent while every other tool keeps the
        // flag on and the native handler: the rolled-back tool fails
        // closed while its siblings still fall back natively.
        let mut registry = Registry::new();
        registry.register(orch_rework_bound_tool(false)).unwrap();
        registry.register(orch_budget_guard_tool(true)).unwrap();
        let mut dispatcher = Dispatcher::new(registry);
        register_orch_budget_guard(&mut dispatcher);
        // Note: no register_orch_rework_bound — native removed, flag off.

        let budget = dispatcher
            .dispatch(
                "A01",
                ORCH_BUDGET_GUARD_TOOL,
                tool_payload(ORCH_BUDGET_GUARD_TOOL),
            )
            .unwrap();
        assert_eq!(budget.route, Route::Native);

        let err = dispatcher
            .dispatch(
                "A01",
                ORCH_REWORK_BOUND_TOOL,
                tool_payload(ORCH_REWORK_BOUND_TOOL),
            )
            .unwrap_err();
        assert!(
            matches!(err, DispatchError::HandlerFailed { .. }),
            "rolled-back tool fails closed, got {err:?}"
        );
    }

    #[test]
    fn tool_contracts_are_namespaced_and_pinned() {
        let dispatcher = a01_batch_dispatcher(true);
        for tool in dispatcher.registry().tools_for_family(A01_FAMILY) {
            assert_eq!(tool.owner, "A01", "owner of {}", tool.name);
            assert_eq!(tool.scope, OwnerScope::Unique, "scope of {}", tool.name);
            assert!(tool.name.starts_with("orch_"), "namespace of {}", tool.name);
            assert_eq!(tool.side_effect_class.as_str(), "READ");
        }
        let dag = dispatcher
            .registry()
            .get(ORCH_DAG_BLOCKED_TOOL)
            .expect("tool registered");
        assert_eq!(
            dag.legacy_fallback.python_path,
            "swarm.tools.a01_tools:dag_blocked"
        );
        assert_eq!(dag.timeout.as_secs(), 5);
    }
}
