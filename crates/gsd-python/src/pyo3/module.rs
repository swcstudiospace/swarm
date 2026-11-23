//! Additive graph-family exports for `swarm._native` (SPE-36781 graph).
//!
//! Versioned intent: the graph family is `graph/1` ([`GRAPH_API_VERSION`]).
//! Additive only: the 18 legacy exports are untouched and keep working.
//! Each item below documents its boundary contract (Python sign, Rust sign,
//! types, error map, ownership, GIL) in its doc comment.
//!
//! Legacy fallback: every graph entry point takes a keyword-only
//! `legacy_fallback` flag, defaulting to `True` (the Python default):
//! when true the result follows legacy insertion order; when false the
//! native deterministic topological order is returned. One flag per family,
//! so a caller can fall back per family without changing other families.
//!
//! Registry migration (SPE-36785): `graph_validate` is also served through
//! the Rust registry dispatcher (`gsd-security`, `shared_graph_validate`,
//! family `graph`); this module's boundary is unchanged so rollback is
//! "stop registering / flip the family flag off" — the 18 legacy exports
//! and this `legacy_fallback=True` default keep serving either way.
use std::collections::{HashMap, HashSet};

use ::pyo3::prelude::*;
use ::pyo3::types::{PyAnyMethods, PyTypeMethods};

use crate::pyo3_conversion::{CheckedBool, CheckedEdgeList, CheckedId, CheckedIdList};
use crate::pyo3_errors::{guard, BridgeError};

/// Versioned intent for the graph family.
pub const GRAPH_API_VERSION: &str = "graph/1";

/// Pure deterministic validation + ordering. No PyO3 types, so `cargo test`
/// covers it without an interpreter.
///
/// - Python sign per caller; Rust sign: this function.
/// - Types: owned `Vec<String>` nodes/edges, returns owned `Vec<String>`.
/// - Error map: duplicate/unknown -> `InvalidInput` (`E-INPUT`), cycle ->
///   `Cycle` (`E-CONTRACT`). Ownership: caller keeps its inputs; the result
///   is a fresh vector. GIL: the caller releases it around this call.
pub fn topo_order(
    op: &'static str,
    nodes: &[String],
    edges: &[(String, String)],
    legacy_fallback: bool,
) -> Result<Vec<String>, BridgeError> {
    // Validate: duplicates first, then unknown endpoints.
    let mut seen: HashSet<&str> = HashSet::with_capacity(nodes.len());
    for node in nodes {
        if !seen.insert(node.as_str()) {
            return Err(BridgeError::InvalidInput {
                op,
                detail: format!("duplicate node {node:?}"),
            });
        }
    }
    for (from, to) in edges {
        for endpoint in [from, to] {
            if !seen.contains(endpoint.as_str()) {
                return Err(BridgeError::InvalidInput {
                    op,
                    detail: format!("edge references unknown node {endpoint:?}"),
                });
            }
        }
    }
    if legacy_fallback {
        // Legacy Python behaviour: insertion order, still fully validated.
        return Ok(nodes.to_vec());
    }
    // Deterministic Kahn topological sort: ties broken by insertion order,
    // so the same input always yields the same output.
    let index: HashMap<&str, usize> = nodes
        .iter()
        .enumerate()
        .map(|(i, node)| (node.as_str(), i))
        .collect();
    let mut indegree: HashMap<&str, usize> = nodes.iter().map(|n| (n.as_str(), 0)).collect();
    let mut outgoing: HashMap<&str, Vec<&str>> = HashMap::new();
    for (from, to) in edges {
        outgoing.entry(from.as_str()).or_default().push(to.as_str());
        *indegree.get_mut(to.as_str()).expect("validated") += 1;
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
                let degree = indegree.get_mut(next).expect("validated");
                *degree -= 1;
                if *degree == 0 {
                    ready.push(next);
                }
            }
        }
    }
    if order.len() != nodes.len() {
        return Err(BridgeError::Cycle { op });
    }
    Ok(order)
}

/// Deterministic FNV-1a receipt for a validated graph. Pure, no I/O.
pub fn submission_receipt(nodes: &[String], edges: &[(String, String)]) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    let mut mix = |bytes: &[u8]| {
        for byte in bytes {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
        }
    };
    mix(GRAPH_API_VERSION.as_bytes());
    for node in nodes {
        mix(node.as_bytes());
        mix(b"\n");
    }
    for (from, to) in edges {
        mix(from.as_bytes());
        mix(b"->");
        mix(to.as_bytes());
        mix(b"\n");
    }
    format!("{GRAPH_API_VERSION}:{hash:016x}")
}

/// Placeholder registry: known families only. Pure, no I/O.
pub fn registry_version(name: &str) -> Option<&'static str> {
    match name {
        "graph" => Some(GRAPH_API_VERSION),
        "scheduler" => Some("sched/1"),
        "chat" => Some("chat/1"),
        _ => None,
    }
}

/// Validated graph report returned to Python. Frozen; fields are read-only.
/// Ownership: every field is an owned copy created for this call.
#[pyclass(name = "GraphReport", frozen, module = "swarm._native")]
pub struct GraphReport {
    #[pyo3(get)]
    node_count: usize,
    #[pyo3(get)]
    edge_count: usize,
    #[pyo3(get)]
    order: Vec<String>,
    #[pyo3(get)]
    legacy_fallback: bool,
    #[pyo3(get)]
    api_version: &'static str,
    #[pyo3(get)]
    submission_id: String,
}

fn edge_pairs(edges: &CheckedEdgeList) -> Vec<(String, String)> {
    edges
        .0
        .iter()
        .map(|edge| (edge.0.clone(), edge.1.clone()))
        .collect()
}

fn build_report(
    op: &'static str,
    nodes: &CheckedIdList,
    edges: &CheckedEdgeList,
    legacy_fallback: bool,
) -> Result<GraphReport, BridgeError> {
    let pairs = edge_pairs(edges);
    let order = topo_order(op, &nodes.0, &pairs, legacy_fallback)?;
    let submission_id = submission_receipt(&nodes.0, &pairs);
    Ok(GraphReport {
        node_count: nodes.0.len(),
        edge_count: pairs.len(),
        order,
        legacy_fallback,
        api_version: GRAPH_API_VERSION,
        submission_id,
    })
}

/// Python: `graph_api_version() -> str`
/// Rust: `fn graph_api_version() -> &'static str`
/// Types: no arguments, returns `"graph/1"`. Error map: none (infallible).
/// Ownership: a fresh `&'static str` copy. GIL: held, trivial work.
#[pyfunction(name = "graph_api_version")]
fn graph_api_version() -> &'static str {
    GRAPH_API_VERSION
}

/// Python: `graph_validate(nodes: list[str], edges: list[tuple[str, str]], *, legacy_fallback: bool = True) -> GraphReport`
/// Rust: `fn graph_validate(nodes: CheckedIdList, edges: CheckedEdgeList, legacy_fallback: CheckedBool) -> PyResult<GraphReport>`
/// Types: ids are checked `str` (adjacent); edges are 2-`tuple`s only.
/// Error map: duplicate/unknown -> `NativeInputError` (`E-INPUT`), cycle ->
/// `NativeContractError` (`E-CONTRACT`); wrong type -> `TypeError` (`E-INPUT`).
/// Ownership: inputs copied into owned Rust values; report owns its copies.
/// GIL: released (`detach`) around validation/ordering.
#[pyfunction(name = "graph_validate")]
#[pyo3(signature = (nodes, edges, *, legacy_fallback=CheckedBool(true)))]
fn graph_validate(
    py: Python<'_>,
    nodes: CheckedIdList,
    edges: CheckedEdgeList,
    legacy_fallback: CheckedBool,
) -> PyResult<GraphReport> {
    let legacy = legacy_fallback.0;
    py.detach(|| {
        guard("graph_validate", || {
            build_report("graph_validate", &nodes, &edges, legacy)
        })
    })
    .map_err(|error| error.to_pyerr())
}

/// Python: `graph_schedule(nodes: list[str], edges: list[tuple[str, str]], *, legacy_fallback: bool = True) -> list[str]`
/// Rust: `fn graph_schedule(nodes: CheckedIdList, edges: CheckedEdgeList, legacy_fallback: CheckedBool) -> PyResult<Vec<String>>`
/// Types: as `graph_validate`. Error map: as `graph_validate`.
/// Ownership: a fresh `list[str]` copy. GIL: released (`detach`) around compute.
#[pyfunction(name = "graph_schedule")]
#[pyo3(signature = (nodes, edges, *, legacy_fallback=CheckedBool(true)))]
fn graph_schedule(
    py: Python<'_>,
    nodes: CheckedIdList,
    edges: CheckedEdgeList,
    legacy_fallback: CheckedBool,
) -> PyResult<Vec<String>> {
    let legacy = legacy_fallback.0;
    py.detach(|| {
        guard("graph_schedule", || {
            let pairs = edge_pairs(&edges);
            topo_order("graph_schedule", &nodes.0, &pairs, legacy)
        })
    })
    .map_err(|error| error.to_pyerr())
}

/// Python: `graph_submit(nodes: list[str], edges: list[tuple[str, str]], *, legacy_fallback: bool = True) -> str`
/// Rust: `fn graph_submit(nodes: CheckedIdList, edges: CheckedEdgeList, legacy_fallback: CheckedBool) -> PyResult<String>`
/// Placeholder boundary: validates locally and returns a deterministic
/// receipt (`"graph/1:<fnv1a-hex>"`); it performs no I/O, no persistence and
/// no scheduling. The future graph service replaces this body, not its sign.
/// Types/error map/ownership/GIL: as `graph_validate`.
#[pyfunction(name = "graph_submit")]
#[pyo3(signature = (nodes, edges, *, legacy_fallback=CheckedBool(true)))]
fn graph_submit(
    py: Python<'_>,
    nodes: CheckedIdList,
    edges: CheckedEdgeList,
    legacy_fallback: CheckedBool,
) -> PyResult<String> {
    // Validate fully (including cycle check via the report), then receipt.
    let report = graph_validate(py, nodes, edges, legacy_fallback)?;
    Ok(report.submission_id)
}

/// Python: `registry_lookup(name: str) -> str | None`
/// Rust: `fn registry_lookup(name: CheckedId) -> PyResult<Option<&'static str>>`
/// Placeholder boundary: an in-process table for `graph`/`scheduler`/`chat`;
/// unknown names return `None` (unclassified, no retry), never an error.
/// Types: checked `str`. Error map: wrong type -> `TypeError` (`E-INPUT`).
/// Ownership: a fresh `str` copy or `None`. GIL: held, trivial work.
#[pyfunction(name = "registry_lookup")]
fn registry_lookup(name: CheckedId) -> PyResult<Option<&'static str>> {
    Ok(registry_version(&name.0))
}

/// Python: `error_from_py(exc: BaseException) -> str`
/// Rust: `fn error_from_py(exc: &Bound<'_, PyAny>) -> PyResult<String>`
/// Python-except -> typed Rust error: reads the exception's type name and
/// message, maps them through `bridge_from_py_type`, and returns the wire
/// string (`E-INPUT: error_from_py failed: ...`). Never raises for a real
/// exception; a non-exception argument raises `NativeInputError` (`E-INPUT`).
/// Ownership: a fresh `str`. GIL: held (reads Python objects).
#[pyfunction(name = "error_from_py")]
fn error_from_py(exc: &Bound<'_, PyAny>) -> PyResult<String> {
    if !exc.is_instance_of::<::pyo3::exceptions::PyBaseException>() {
        return Err(crate::pyo3_errors::NativeInputError::new_err(
            "E-INPUT: error_from_py failed: argument is not an exception",
        ));
    }
    let type_name = exc
        .get_type()
        .name()
        .map(|name| name.to_string())
        .unwrap_or_else(|_| "<unknown>".to_owned());
    let message = exc
        .str()
        .map(|text| text.to_string())
        .unwrap_or_else(|_| "<unprintable>".to_owned());
    Ok(crate::pyo3_errors::bridge_from_py_type("error_from_py", &type_name, &message).wire())
}

/// Register every additive graph-family export. Called once from `_native`.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(graph_api_version, m)?)?;
    m.add_function(wrap_pyfunction!(graph_validate, m)?)?;
    m.add_function(wrap_pyfunction!(graph_schedule, m)?)?;
    m.add_function(wrap_pyfunction!(graph_submit, m)?)?;
    m.add_function(wrap_pyfunction!(registry_lookup, m)?)?;
    m.add_function(wrap_pyfunction!(error_from_py, m)?)?;
    m.add_class::<GraphReport>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{build_report, registry_version, submission_receipt, topo_order};
    use crate::pyo3_conversion::{CheckedEdge, CheckedEdgeList, CheckedIdList};

    fn nodes(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    fn edges(values: &[(&str, &str)]) -> Vec<(String, String)> {
        values
            .iter()
            .map(|(from, to)| ((*from).to_owned(), (*to).to_owned()))
            .collect()
    }

    #[test]
    fn legacy_order_is_insertion_order_and_native_order_is_topological() {
        let ids = nodes(&["c", "a", "b"]);
        let links = edges(&[("a", "b"), ("b", "c")]);
        assert_eq!(
            topo_order("t", &ids, &links, true),
            Ok(nodes(&["c", "a", "b"]))
        );
        assert_eq!(
            topo_order("t", &ids, &links, false),
            Ok(nodes(&["a", "b", "c"]))
        );
    }

    #[test]
    fn duplicate_unknown_and_cycle_are_rejected() {
        assert!(topo_order("t", &nodes(&["a", "a"]), &[], false).is_err());
        assert!(topo_order("t", &nodes(&["a"]), &edges(&[("a", "b")]), false).is_err());
        assert!(topo_order(
            "t",
            &nodes(&["a", "b"]),
            &edges(&[("a", "b"), ("b", "a")]),
            false
        )
        .is_err());
    }

    #[test]
    fn receipt_is_deterministic_and_registry_is_a_placeholder_table() {
        let ids = nodes(&["a", "b"]);
        let links = edges(&[("a", "b")]);
        assert_eq!(
            submission_receipt(&ids, &links),
            submission_receipt(&ids, &links)
        );
        assert!(submission_receipt(&ids, &links).starts_with("graph/1:"));
        assert_eq!(registry_version("graph"), Some("graph/1"));
        assert_eq!(registry_version("nope"), None);
    }

    /// Pins the `graph_validate` boundary the registry dispatcher serves
    /// (SPE-36785): the native report carries the deterministic topo order,
    /// the legacy report the insertion order, and both are stamped `graph/1`
    /// with the same counts the registry handlers enumerate.
    ///
    /// Note the axis split: the pyo3 boundary's `legacy_fallback` selects
    /// only the *ordering* mode, and native mode (not legacy) is what
    /// refuses cycles here (see `duplicate_unknown_and_cycle_are_rejected`).
    /// The registry's legacy handler is a separate, fully-validating path
    /// that mirrors the legacy planner (`swarm/graph_planner.py`), which
    /// refuses cycles on every route.
    #[test]
    fn graph_validate_report_matches_registry_contract() {
        let checked_nodes = CheckedIdList(nodes(&["c", "a", "b"]));
        let checked_edges = CheckedEdgeList(vec![
            CheckedEdge("a".to_string(), "b".to_string()),
            CheckedEdge("b".to_string(), "c".to_string()),
        ]);
        let native = build_report("graph_validate", &checked_nodes, &checked_edges, false)
            .expect("native report");
        assert_eq!(native.order, nodes(&["a", "b", "c"]));
        assert_eq!(native.node_count, 3);
        assert_eq!(native.edge_count, 2);
        assert_eq!(native.api_version, "graph/1");
        assert!(!native.legacy_fallback);
        let legacy = build_report("graph_validate", &checked_nodes, &checked_edges, true)
            .expect("legacy report");
        assert_eq!(legacy.order, nodes(&["c", "a", "b"]));
        assert!(legacy.legacy_fallback);
    }
}
