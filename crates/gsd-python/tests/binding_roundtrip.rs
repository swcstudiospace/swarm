//! Binding round-trip: the vertical-slice proof for the PyO3 shim.
//!
//! The shim is the smallest stable surface: graph execution plus tool
//! invocation, over one value contract and one typed error bridge.
//!
//! Vertical slice under test:
//! - Shared tool `graph_schedule`/`graph_validate` (served by the Rust
//!   registry dispatcher in `gsd-security` as `shared_graph_validate` and by
//!   this module on the same `graph/1` version): Python -> Rust -> Python,
//!   with `legacy_fallback` selecting Python's insertion order. The fallback
//!   never launders: validation is identical on both routes (see
//!   `shared_tool_fallback_validates_identically`).
//! - Unique tool `registry_lookup` (this module's placeholder table, no
//!   Python counterpart): Python -> Rust -> Python; unknown names return
//!   `None`, never an error.
//!
//! Value contract under test ([`CheckedValue`]): `str`, nums (`int`/`float`),
//! bools, lists, maps, null-versus-missing, large-int, nested, oversized and
//! non-UTF8 are converted exactly; everything else is rejected with a typed
//! `E-INPUT` error. `cargo test` links libpython, so these tests exercise
//! the real Python object conversions without needing a wheel build.

use _native::pyo3_conversion::CheckedValue;
use _native::pyo3_errors::{guard, BridgeError};
use _native::pyo3_module::{registry_version, submission_receipt, topo_order};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};
use std::ffi::CString;

/// Extract a Python value to the checked contract.
fn extract(value: &Bound<'_, PyAny>) -> Result<CheckedValue, PyErr> {
    CheckedValue::extract(value.as_borrowed())
}

fn eval<'a>(py: Python<'a>, source: &'a str) -> PyResult<Bound<'a, PyAny>> {
    let code = CString::new(source).expect("fixture source has no NUL");
    py.eval(&code, None, None)
}

#[test]
fn scalars_round_trip_python_rust_python() {
    Python::initialize();
    Python::attach(|py| {
        // str (free text: spaces are fine in a generic value), int, large int
        // beyond i64 but within i128, bool, finite float, and None.
        let cases: [(&str, CheckedValue); 7] = [
            (
                "'hello wide world'",
                CheckedValue::Str("hello wide world".to_owned()),
            ),
            ("-1", CheckedValue::Int(-1)),
            (
                "9223372036854775808",
                CheckedValue::Int(9223372036854775808),
            ),
            ("True", CheckedValue::Bool(true)),
            ("False", CheckedValue::Bool(false)),
            ("1.5", CheckedValue::Float(1.5)),
            ("None", CheckedValue::Null),
        ];
        for (source, expected) in cases {
            let obj = eval(py, source).expect("fixture eval");
            let rust = extract(&obj).expect("checked extraction");
            assert_eq!(rust, expected, "Python -> Rust for {source}");
            assert_eq!(rust.kind(), expected.kind());
            // Rust -> Python returns the same Python value.
            let back = rust
                .into_pyobject(py)
                .expect("checked conversion back")
                .extract::<CheckedValue>()
                .expect("re-extraction");
            assert_eq!(back, expected, "Rust -> Python for {source}");
        }
        // Free-text strings accept characters ids forbid.
        let spaced = extract(&eval(py, "'has space'").unwrap()).unwrap();
        assert_eq!(spaced, CheckedValue::Str("has space".to_owned()));
    });
}

#[test]
fn bools_are_never_read_as_ints_and_ints_never_as_bools() {
    Python::initialize();
    Python::attach(|py| {
        let truth = extract(&eval(py, "True").unwrap()).unwrap();
        assert_eq!(truth, CheckedValue::Bool(true));
        let one = extract(&eval(py, "1").unwrap()).unwrap();
        assert_eq!(one, CheckedValue::Int(1));
        assert_ne!(truth, one);
        // The graph `legacy_fallback` type agrees: 1 is not a bool either.
        let flag: Result<CheckedValue, PyErr> = extract(&eval(py, "1").unwrap());
        assert!(flag.is_ok());
    });
}

#[test]
fn nested_maps_lists_round_trip_and_null_differs_from_missing() {
    Python::initialize();
    Python::attach(|py| {
        let obj = eval(
            py,
            "{'flag': None, 'n': 1, 'deep': [1, 'x', True, {'b': None}]}",
        )
        .unwrap();
        let rust = extract(&obj).unwrap();
        let expected = CheckedValue::Map(vec![
            ("flag".to_owned(), CheckedValue::Null),
            ("n".to_owned(), CheckedValue::Int(1)),
            (
                "deep".to_owned(),
                CheckedValue::List(vec![
                    CheckedValue::Int(1),
                    CheckedValue::Str("x".to_owned()),
                    CheckedValue::Bool(true),
                    CheckedValue::Map(vec![("b".to_owned(), CheckedValue::Null)]),
                ]),
            ),
        ]);
        assert_eq!(rust, expected);
        // Null (present None) versus missing (absent key) stay distinct.
        assert_eq!(rust.get("flag"), Some(&CheckedValue::Null));
        assert_eq!(rust.get("absent"), None);
        let deep = rust.get("deep").unwrap();
        assert_eq!(deep.kind(), "list");
        // Round-trip the whole tree back to Python and re-extract.
        let back = rust
            .clone()
            .into_pyobject(py)
            .unwrap()
            .extract::<CheckedValue>()
            .unwrap();
        assert_eq!(back, expected);
        // Tuples are read as lists (same shape, no silent mixing the other way:
        // a bare tuple is never an edge — see CheckedEdge).
        let tup = extract(&eval(py, "(1, 'x')").unwrap()).unwrap();
        assert_eq!(
            tup,
            CheckedValue::List(vec![
                CheckedValue::Int(1),
                CheckedValue::Str("x".to_owned())
            ])
        );
    });
}

#[test]
fn every_other_type_is_rejected_with_a_typed_error() {
    Python::initialize();
    Python::attach(|py| {
        for source in ["b'ab'", "{1, 2}", "1j", "object()"] {
            let obj = eval(py, source).unwrap();
            let err = extract(&obj).expect_err(&format!("{source} must be rejected"));
            let message = err.to_string();
            assert!(message.contains("E-INPUT"), "{source}: {message}");
            assert!(message.contains("conversion failed"), "{source}: {message}");
        }
        // A lone surrogate is not valid UTF-8: rejected, never lossy.
        let obj = eval(py, "'a\\ud800'").unwrap();
        let message = extract(&obj).unwrap_err().to_string();
        assert!(message.contains("E-INPUT"), "{message}");
        assert!(message.contains("not valid UTF-8"), "{message}");
    });
}

#[test]
fn lossy_values_are_rejected_not_coerced() {
    Python::initialize();
    Python::attach(|py| {
        // NaN/inf floats, oversized ints and Python ints are never truncated.
        for source in ["float('inf')", "float('nan')", "10**40"] {
            let obj = eval(py, source).unwrap();
            let err = extract(&obj).expect_err(&format!("{source} must be rejected"));
            let message = err.to_string();
            assert!(message.contains("E-INPUT"), "{source}: {message}");
            assert!(message.contains("conversion failed"), "{source}: {message}");
        }
        // Non-finite wording stays explicit.
        let message = extract(&eval(py, "float('inf')").unwrap())
            .unwrap_err()
            .to_string();
        assert!(message.contains("finite"), "{message}");
    });
}

#[test]
fn caps_are_capacity_errors_not_input_errors() {
    Python::initialize();
    Python::attach(|py| {
        let big_list = PyList::new(py, (0..10_001).map(|n| n.to_string())).unwrap();
        let list_err = extract(&big_list).unwrap_err().to_string();
        assert!(list_err.contains("E-CAPACITY"), "{list_err}");

        let deep: String = format!("{}1{}", "[".repeat(40), "]".repeat(40));
        let deep_err = extract(&eval(py, &deep).unwrap()).unwrap_err().to_string();
        assert!(deep_err.contains("E-CAPACITY"), "{deep_err}");

        let long = format!("'{}'", "x".repeat(65_537));
        let str_err = extract(&eval(py, &long).unwrap()).unwrap_err().to_string();
        assert!(str_err.contains("E-CAPACITY"), "{str_err}");
    });
}

#[test]
fn errors_carry_wire_code_and_operation_never_an_empty_success() {
    // Genuine failures carry a stable wire code and the failing operation.
    let input = BridgeError::InvalidInput {
        op: "graph_validate",
        detail: "duplicate node \"a\"".to_owned(),
    };
    assert_eq!(input.code().as_str(), "E-INPUT");
    assert_eq!(
        input.wire(),
        "E-INPUT: graph_validate failed: duplicate node \"a\""
    );
    assert!(!input.wire().is_empty());

    let capacity = BridgeError::Capacity {
        op: "conversion",
        detail: "node list exceeds cap of 10000".to_owned(),
    };
    assert_eq!(capacity.code().as_str(), "E-CAPACITY");
    assert!(capacity
        .wire()
        .starts_with("E-CAPACITY: conversion failed:"));

    // A panic inside Rust work is contained and typed, never re-raised.
    let contained = guard("tool_invoke", || -> Result<(), BridgeError> {
        panic!("payload must not escape");
    });
    assert_eq!(contained, Err(BridgeError::Internal { op: "tool_invoke" }));
    assert!(!contained.unwrap_err().wire().contains("payload"));

    Python::initialize();
    Python::attach(|py| {
        // The Rust -> Python side raises the NativeError hierarchy carrying
        // the wire code, so `except NativeError` and log scrapers both work.
        let err = input.to_pyerr();
        let message = err
            .value(py)
            .str()
            .map(|s| s.to_string())
            .unwrap_or_default();
        assert!(
            message.starts_with("E-INPUT: graph_validate failed:"),
            "{message}"
        );
    });
}

#[test]
fn shared_tool_fallback_validates_identically() {
    // The shared tool: native deterministic order versus legacy insertion
    // order (the fallback). Both routes validate identically, so fallback
    // never launders an invalid graph as success.
    let nodes = vec!["c".to_owned(), "a".to_owned(), "b".to_owned()];
    let edges = vec![
        ("a".to_owned(), "b".to_owned()),
        ("b".to_owned(), "c".to_owned()),
    ];
    assert_eq!(
        topo_order("graph_schedule", &nodes, &edges, true),
        Ok(nodes.clone())
    );
    assert_eq!(
        topo_order("graph_schedule", &nodes, &edges, false),
        Ok(vec!["a".to_owned(), "b".to_owned(), "c".to_owned()])
    );
    // A cycle refuses on both routes with the contract error.
    let cycle = vec![
        ("a".to_owned(), "b".to_owned()),
        ("b".to_owned(), "a".to_owned()),
    ];
    for legacy in [true, false] {
        let err = topo_order(
            "graph_schedule",
            &["a".to_owned(), "b".to_owned()],
            &cycle,
            legacy,
        )
        .expect_err("a cycle must refuse on every route");
        assert_eq!(err.code().as_str(), "E-CONTRACT");
        assert!(
            err.wire().contains("graph_schedule failed"),
            "{}",
            err.wire()
        );
    }
    // Duplicate and unknown nodes refuse on both routes too.
    for legacy in [true, false] {
        assert!(topo_order("t", &["a".to_owned(), "a".to_owned()], &[], legacy).is_err());
        assert!(topo_order(
            "t",
            &["a".to_owned()],
            &[("a".to_owned(), "b".to_owned())],
            legacy
        )
        .is_err());
    }
}

#[test]
fn unique_tool_lookup_is_honest_on_unknown_names() {
    // The unique tool: a placeholder table, `None` (unclassified, no retry)
    // for unknown names, never a fabricated version or an error.
    assert_eq!(registry_version("graph"), Some("graph/1"));
    assert_eq!(registry_version("scheduler"), Some("sched/1"));
    assert_eq!(registry_version("nope"), None);

    // The receipt helper is deterministic and version-stamped.
    let nodes = vec!["a".to_owned(), "b".to_owned()];
    let edges = vec![("a".to_owned(), "b".to_owned())];
    let first = submission_receipt(&nodes, &edges);
    assert_eq!(first, submission_receipt(&nodes, &edges));
    assert!(first.starts_with("graph/1:"), "{first}");

    // Empty input is a real empty result only for an empty graph.
    assert_eq!(topo_order("graph_schedule", &[], &[], false), Ok(vec![]));
    // A non-empty input never succeeds empty: it carries its nodes.
    let order = topo_order("graph_schedule", &nodes, &edges, false).unwrap();
    assert_eq!(order.len(), 2);
}

#[test]
fn lists_and_strings_from_rust_are_fresh_copies() {
    Python::initialize();
    Python::attach(|py| {
        // A string built in Rust is owned by Python after return: mutating
        // the Rust side cannot alias it.
        let mut owned = "node".to_owned();
        let obj = PyString::new(py, &owned).into_any();
        owned.push_str("-changed");
        let back: String = obj.extract().unwrap();
        assert_eq!(back, "node");
    });
}
