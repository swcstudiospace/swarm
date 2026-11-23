//! Central checked conversions for the additive graph family.
//!
//! Boundary contract (every new export goes through here, no silent coerce):
//! - Python -> Rust: [`CheckedId`], [`CheckedU64`], [`CheckedBool`],
//!   [`CheckedIdList`] and [`CheckedEdgeList`] are the only argument types
//!   `module.rs` accepts. Each rejects, with `E-INPUT` and a clear message:
//!   wrong type (including `bool` where an int is expected, `bytes`/`int`
//!   where a `str` is expected, a `list` where an edge `tuple` is expected),
//!   lossy values (negative or `> u64::MAX` for an unsigned count, NaN/inf
//!   for a float), over-long ids, and sequences over the bounded caps.
//!   No `float` is ever accepted for an int, and no `str` is parsed as a number.
//! - Rust -> Python: [`IntoPyObject`] is implemented for [`CheckedId`] and
//!   [`CheckedU64`] only, delegating to the inner value. All other returns
//!   are plain `String`/`Vec<String>`/`bool`, so ownership is a fresh copy
//!   on every call and nothing borrows Python objects after return.
//! - Ownership/GIL: conversions run under the GIL (they read Python objects)
//!   and produce owned Rust values; the GIL is then released for compute.
//! - Caps (conservative, bounded): at most [`MAX_NODES`] nodes, [`MAX_EDGES`]
//!   edges, [`MAX_ID_LEN`] chars per id. Exceeding a cap is `E-CAPACITY`.

use std::convert::Infallible;

use ::pyo3::exceptions::{PyTypeError, PyValueError};
use ::pyo3::prelude::*;
use ::pyo3::types::{PyBool, PyInt, PyList, PyString, PyTuple};

use crate::pyo3_errors::BridgeError;

/// Conservative bounded caps for the graph family.
pub const MAX_NODES: usize = 10_000;
pub const MAX_EDGES: usize = 20_000;
pub const MAX_ID_LEN: usize = 256;

/// Pure id check. Returns the offending detail on failure.
pub fn validate_id(value: &str) -> Result<(), String> {
    if value.is_empty() {
        return Err("id must not be empty".to_owned());
    }
    if value.chars().count() > MAX_ID_LEN {
        return Err(format!("id exceeds {MAX_ID_LEN} characters"));
    }
    for ch in value.chars() {
        let ok = ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.' | '/' | ':' | '@');
        if !ok {
            return Err(format!("id contains forbidden character {ch:?}"));
        }
    }
    Ok(())
}

/// Pure count check.
pub fn check_count(len: usize, max: usize, what: &str) -> Result<(), String> {
    if len > max {
        return Err(format!("{what} exceeds cap of {max}"));
    }
    Ok(())
}

/// Pure lossy check: `i128` -> `u64`, rejecting negatives and overflow.
pub fn checked_u64_from_i128(value: i128) -> Result<u64, String> {
    u64::try_from(value).map_err(|_| format!("integer {value} is outside the u64 range"))
}

/// Pure finite check for floats (no NaN/inf silently passed through).
pub fn checked_finite(value: f64) -> Result<f64, String> {
    if value.is_finite() {
        Ok(value)
    } else {
        Err("float must be finite (not NaN or infinite)".to_owned())
    }
}

fn input_err(op: &'static str, detail: impl Into<String>) -> PyErr {
    BridgeError::InvalidInput {
        op,
        detail: detail.into(),
    }
    .to_pyerr()
}

fn type_name(obj: &Bound<'_, PyAny>) -> String {
    obj.get_type()
        .name()
        .and_then(|name| name.to_str().map(|s| s.to_owned()))
        .unwrap_or_else(|_| "<unknown>".to_owned())
}

fn type_err(op: &'static str, expected: &str, actual: &str) -> PyErr {
    // Keep the E-INPUT wire prefix even for type mismatches: callers and
    // log scrapers match on the prefix, not on the exception class.
    PyTypeError::new_err(format!(
        "E-INPUT: {op} failed: expected {expected}, got {actual}"
    ))
}

/// A checked node/registry id: a `str` of at most [`MAX_ID_LEN`] chars from
/// the allowed set. `bytes`, `int`, `None` and subclasses with surrogates
/// are rejected (surrogates fail UTF-8 extraction before this runs).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckedId(pub String);

impl<'a, 'py> FromPyObject<'a, 'py> for CheckedId {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        let op = "conversion";
        if obj.cast::<PyString>().is_err() {
            return Err(type_err(op, "str", &type_name(&obj)));
        }
        let value: String = obj
            .extract()
            .map_err(|_| input_err(op, "str is not valid UTF-8"))?;
        validate_id(&value).map_err(|detail| input_err(op, detail))?;
        Ok(CheckedId(value))
    }
}

impl<'py> IntoPyObject<'py> for CheckedId {
    type Target = PyString;
    type Output = Bound<'py, PyString>;
    type Error = Infallible;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        Ok(PyString::new(py, &self.0))
    }
}

/// A checked unsigned count: a Python `int` (never `bool`) in `u64` range.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CheckedU64(pub u64);

impl<'a, 'py> FromPyObject<'a, 'py> for CheckedU64 {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        let op = "conversion";
        if obj.cast::<PyBool>().is_ok() {
            return Err(type_err(op, "int (not bool)", "bool"));
        }
        if obj.cast::<PyInt>().is_err() {
            return Err(type_err(op, "int", &type_name(&obj)));
        }
        let value: i128 = obj
            .extract()
            .map_err(|_| input_err(op, "integer is outside the supported range"))?;
        checked_u64_from_i128(value).map_err(|detail| input_err(op, detail))?;
        Ok(CheckedU64(value as u64))
    }
}

impl<'py> IntoPyObject<'py> for CheckedU64 {
    type Target = PyInt;
    type Output = Bound<'py, PyInt>;
    type Error = Infallible;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        Ok(self.0.into_pyobject(py).expect("u64 fits in a Python int"))
    }
}

/// A checked boolean: only a real `bool`, never `1`/`0` or a truthy object.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CheckedBool(pub bool);

impl<'a, 'py> FromPyObject<'a, 'py> for CheckedBool {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        let op = "conversion";
        if obj.cast::<PyBool>().is_err() {
            return Err(type_err(op, "bool", &type_name(&obj)));
        }
        let value: bool = obj
            .extract()
            .map_err(|_| input_err(op, "bool extraction failed"))?;
        Ok(CheckedBool(value))
    }
}

/// A checked directed edge `(from, to)`: a 2-tuple of [`CheckedId`] only.
/// A list is rejected so callers do not silently mix the two shapes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckedEdge(pub String, pub String);

impl<'a, 'py> FromPyObject<'a, 'py> for CheckedEdge {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        let op = "conversion";
        let tuple = obj
            .cast::<PyTuple>()
            .map_err(|_| type_err(op, "tuple[str, str] edge", &type_name(&obj)))?;
        if tuple.len() != 2 {
            return Err(input_err(op, "edge tuple must have exactly 2 items"));
        }
        let from: CheckedId = tuple.get_item(0)?.extract()?;
        let to: CheckedId = tuple.get_item(1)?.extract()?;
        Ok(CheckedEdge(from.0, to.0))
    }
}

fn sequence_len(obj: &Bound<'_, PyAny>, op: &'static str, what: &str) -> Result<usize, PyErr> {
    if let Ok(list) = obj.cast::<PyList>() {
        return Ok(list.len());
    }
    if let Ok(tuple) = obj.cast::<PyTuple>() {
        return Ok(tuple.len());
    }
    Err(type_err(
        op,
        &format!("list[{what}] or tuple[{what}]"),
        &type_name(obj),
    ))
}

/// A checked list/tuple of ids, capped at [`MAX_NODES`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckedIdList(pub Vec<String>);

impl<'a, 'py> FromPyObject<'a, 'py> for CheckedIdList {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        let op = "conversion";
        let len = sequence_len(&obj, op, "str")?;
        check_count(len, MAX_NODES, "node list").map_err(|detail| input_err(op, detail))?;
        if let Ok(list) = obj.cast::<PyList>() {
            let mut out = Vec::with_capacity(len);
            for item in list.iter() {
                let id: CheckedId = item.extract()?;
                out.push(id.0);
            }
            return Ok(CheckedIdList(out));
        }
        let tuple = obj
            .cast::<PyTuple>()
            .map_err(|_| type_err(op, "sequence", &type_name(&obj)))?;
        let mut out = Vec::with_capacity(len);
        for item in tuple.iter() {
            let id: CheckedId = item.extract()?;
            out.push(id.0);
        }
        Ok(CheckedIdList(out))
    }
}

/// A checked list/tuple of edges, capped at [`MAX_EDGES`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CheckedEdgeList(pub Vec<CheckedEdge>);

impl<'a, 'py> FromPyObject<'a, 'py> for CheckedEdgeList {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        let op = "conversion";
        let len = sequence_len(&obj, op, "tuple[str, str]")?;
        check_count(len, MAX_EDGES, "edge list").map_err(|detail| input_err(op, detail))?;
        if let Ok(list) = obj.cast::<PyList>() {
            let mut out = Vec::with_capacity(len);
            for item in list.iter() {
                let edge: CheckedEdge = item.extract()?;
                out.push(edge);
            }
            return Ok(CheckedEdgeList(out));
        }
        let tuple = obj
            .cast::<PyTuple>()
            .map_err(|_| type_err(op, "sequence", &type_name(&obj)))?;
        let mut out = Vec::with_capacity(len);
        for item in tuple.iter() {
            let edge: CheckedEdge = item.extract()?;
            out.push(edge);
        }
        Ok(CheckedEdgeList(out))
    }
}

// Keep PyValueError referenced so a future typed ValueError stays one import away.
#[allow(dead_code)]
fn _unused(_: PyValueError) {}

#[cfg(test)]
mod tests {
    use super::{check_count, checked_finite, checked_u64_from_i128, validate_id};

    #[test]
    fn ids_accept_task_like_values_and_reject_the_rest() {
        assert!(validate_id("T-2484-be").is_ok());
        assert!(validate_id("node/a.b:c@d").is_ok());
        assert!(validate_id("").is_err());
        assert!(validate_id("has space").is_err());
        assert!(validate_id("semi;colon").is_err());
        assert!(validate_id(&"x".repeat(257)).is_err());
    }

    #[test]
    fn counts_reject_negative_lossy_and_over_cap() {
        assert_eq!(checked_u64_from_i128(0), Ok(0));
        assert_eq!(checked_u64_from_i128(u64::MAX as i128), Ok(u64::MAX));
        assert!(checked_u64_from_i128(-1).is_err());
        assert!(checked_u64_from_i128(u64::MAX as i128 + 1).is_err());
        assert!(check_count(10_001, 10_000, "node list").is_err());
        assert!(check_count(10_000, 10_000, "node list").is_ok());
    }

    #[test]
    fn floats_must_be_finite() {
        assert!(checked_finite(1.5).is_ok());
        assert!(checked_finite(f64::NAN).is_err());
        assert!(checked_finite(f64::INFINITY).is_err());
    }
}
