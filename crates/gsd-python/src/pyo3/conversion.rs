//! Central checked conversions for the additive graph family.
//!
//! Boundary contract (every new export goes through here, no silent coerce):
//! - Python -> Rust: [`CheckedId`], [`CheckedU64`], [`CheckedBool`],
//!   [`CheckedIdList`], [`CheckedEdgeList`] and the generic value type
//!   [`CheckedValue`] are the only argument types `module.rs` accepts. Each
//!   rejects, with `E-INPUT` and a clear message: wrong type (including `bool`
//!   where an int is expected, `bytes`/`int` where a `str` is expected, a
//!   `list` where an edge `tuple` is expected), lossy values (negative or
//!   `> u64::MAX` for an unsigned count, `> i128` for a generic int,
//!   NaN/inf for a float), over-long ids, and sequences over the bounded caps.
//!   No `float` is ever accepted for an int, and no `str` is parsed as a number.
//! - Generic value ([`CheckedValue`]): `str`, int, finite float, `bool`,
//!   list, map (str-keyed), and `None` as `Null`; a missing map key is
//!   absent (no entry), which [`CheckedValue::get`] keeps distinct from a
//!   present `Null`. Nested lists/maps are depth-capped; containers are
//!   item-capped; strings are length-capped. Every other Python type
//!   (`bytes`, `set`, callables, complex, …) is rejected, never coerced.
//!   Type mismatches raise `TypeError` (`E-INPUT`); cap violations raise
//!   `NativeInputError` carrying `E-CAPACITY` (see [`capacity_err`]).
//! - Rust -> Python: [`IntoPyObject`] is implemented for [`CheckedId`],
//!   [`CheckedU64`] and [`CheckedValue`] only, delegating to the inner
//!   value. All other returns are plain `String`/`Vec<String>`/`bool`, so
//!   ownership is a fresh copy on every call and nothing borrows Python
//!   objects after return.
//! - Ownership/GIL: conversions run under the GIL (they read Python objects)
//!   and produce owned Rust values; the GIL is then released for compute.
//! - Caps (conservative, bounded): at most [`MAX_NODES`] nodes, [`MAX_EDGES`]
//!   edges, [`MAX_ID_LEN`] chars per id, [`MAX_VALUE_ITEMS`] items per value
//!   container, [`MAX_VALUE_DEPTH`] value nesting levels and
//!   [`MAX_VALUE_STR`] chars per value string. Exceeding a cap is `E-CAPACITY`.
use std::convert::Infallible;

use ::pyo3::exceptions::{PyTypeError, PyValueError};
use ::pyo3::prelude::*;
use ::pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::pyo3_errors::BridgeError;

/// Conservative bounded caps for the graph family.
pub const MAX_NODES: usize = 10_000;
pub const MAX_EDGES: usize = 20_000;
pub const MAX_ID_LEN: usize = 256;

/// Bounds for the generic value ([`CheckedValue`]).
pub const MAX_VALUE_ITEMS: usize = 10_000;
pub const MAX_VALUE_DEPTH: usize = 32;
pub const MAX_VALUE_STR: usize = 65_536;

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

/// A bounded-cap violation: `NativeInputError` carrying `E-CAPACITY`.
/// Used for every [`check_count`] failure; a cap is a capacity problem, not
/// a malformed value, so it never reports `E-INPUT`.
fn capacity_err(op: &'static str, detail: impl Into<String>) -> PyErr {
    BridgeError::Capacity {
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
        // The error type is Infallible: a u64 always fits a Python int, so
        // this arm is unreachable and carries no panic across the FFI.
        match self.0.into_pyobject(py) {
            Ok(value) => Ok(value),
            Err(never) => match never {},
        }
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
        check_count(len, MAX_NODES, "node list").map_err(|detail| capacity_err(op, detail))?;
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
        check_count(len, MAX_EDGES, "edge list").map_err(|detail| capacity_err(op, detail))?;
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
/// The single generic value contract: every Rust entry point that takes an
/// unstructured payload takes a [`CheckedValue`], never a bare `PyObject` or
/// a coerced string. Accepted kinds are exactly these seven: `Null` (Python
/// `None`), `Bool`, `Int` (any Python `int` in `i128` range, whether or not
/// it fits `u64`), `Float` (finite only), `Str` (valid UTF-8 only), `List`
/// (Python `list` or `tuple`) and `Map` (Python `dict` with [`CheckedId`]-style
/// str keys). Any other type (`bytes`, `set`, complex, callables, …) is
/// rejected with `TypeError` (`E-INPUT`), never coerced or stringified.
///
/// Null versus missing: a present `None` is [`CheckedValue::Null`]; an absent
/// map key is absent from the entry list, so [`CheckedValue::get`] returns
/// `None` (missing) versus `Some(&CheckedValue::Null)` (present null) — the
/// two never compare equal and a tool can distinguish "not given" from
/// "given as null".
///
/// Nested lists/maps recurse up to [`MAX_VALUE_DEPTH`]; each container holds
/// at most [`MAX_VALUE_ITEMS`] items and each string at most [`MAX_VALUE_STR`]
/// chars, all `E-CAPACITY` violations when exceeded. A `str` with a lone
/// surrogate is not valid UTF-8 and is rejected as `E-INPUT`. Floats must be
/// finite ([`checked_finite`]). Bools are never read as ints: `True` is
/// [`CheckedValue::Bool`], not `Int(1)`.
#[derive(Debug, Clone, PartialEq)]
pub enum CheckedValue {
    Null,
    Bool(bool),
    Int(i128),
    Float(f64),
    Str(String),
    List(Vec<CheckedValue>),
    Map(Vec<(String, CheckedValue)>),
}

impl CheckedValue {
    /// Stable kind name: `null`, `bool`, `int`, `float`, `str`, `list`, `map`.
    pub fn kind(&self) -> &'static str {
        match self {
            CheckedValue::Null => "null",
            CheckedValue::Bool(_) => "bool",
            CheckedValue::Int(_) => "int",
            CheckedValue::Float(_) => "float",
            CheckedValue::Str(_) => "str",
            CheckedValue::List(_) => "list",
            CheckedValue::Map(_) => "map",
        }
    }

    /// Map lookup distinguishing missing (`None`) from present-null
    /// (`Some(&CheckedValue::Null)`). Non-map values return `None`.
    pub fn get(&self, key: &str) -> Option<&CheckedValue> {
        if let CheckedValue::Map(entries) = self {
            entries.iter().find(|(k, _)| k == key).map(|(_, v)| v)
        } else {
            None
        }
    }
}

fn extract_checked(obj: &Bound<'_, PyAny>, depth: usize) -> Result<CheckedValue, PyErr> {
    let op = "conversion";
    if depth > MAX_VALUE_DEPTH {
        return Err(capacity_err(
            op,
            format!("value exceeds depth cap of {MAX_VALUE_DEPTH}"),
        ));
    }
    if obj.cast::<::pyo3::types::PyNone>().is_ok() {
        return Ok(CheckedValue::Null);
    }
    if obj.cast::<PyBool>().is_ok() {
        let value: bool = obj
            .extract()
            .map_err(|_| input_err(op, "bool extraction failed"))?;
        return Ok(CheckedValue::Bool(value));
    }
    if obj.cast::<PyInt>().is_ok() {
        let value: i128 = obj
            .extract()
            .map_err(|_| input_err(op, "integer is outside the supported range"))?;
        return Ok(CheckedValue::Int(value));
    }
    if obj.cast::<PyFloat>().is_ok() {
        let value: f64 = obj
            .extract()
            .map_err(|_| input_err(op, "float extraction failed"))?;
        checked_finite(value).map_err(|detail| input_err(op, detail))?;
        return Ok(CheckedValue::Float(value));
    }
    if obj.cast::<PyString>().is_ok() {
        let value: String = obj
            .extract()
            .map_err(|_| input_err(op, "str is not valid UTF-8"))?;
        if value.chars().count() > MAX_VALUE_STR {
            return Err(capacity_err(
                op,
                format!("str exceeds cap of {MAX_VALUE_STR} characters"),
            ));
        }
        return Ok(CheckedValue::Str(value));
    }
    if obj.cast::<PyList>().is_ok() || obj.cast::<PyTuple>().is_ok() {
        let len = sequence_len(obj, op, "value")?;
        check_count(len, MAX_VALUE_ITEMS, "value list")
            .map_err(|detail| capacity_err(op, detail))?;
        let mut out = Vec::with_capacity(len);
        if let Ok(list) = obj.cast::<PyList>() {
            for item in list.iter() {
                out.push(extract_checked(&item, depth + 1)?);
            }
            return Ok(CheckedValue::List(out));
        }
        let tuple = obj
            .cast::<PyTuple>()
            .map_err(|_| type_err(op, "sequence", &type_name(obj)))?;
        for item in tuple.iter() {
            out.push(extract_checked(&item, depth + 1)?);
        }
        return Ok(CheckedValue::List(out));
    }
    if let Ok(dict) = obj.cast::<PyDict>() {
        check_count(dict.len(), MAX_VALUE_ITEMS, "value map")
            .map_err(|detail| capacity_err(op, detail))?;
        let mut out = Vec::with_capacity(dict.len());
        for (key, value) in dict.iter() {
            let name: String = key
                .extract()
                .map_err(|_| input_err(op, "map key is not valid UTF-8"))?;
            validate_id(&name).map_err(|detail| input_err(op, detail))?;
            out.push((name, extract_checked(&value, depth + 1)?));
        }
        return Ok(CheckedValue::Map(out));
    }
    Err(type_err(
        op,
        "value (null/bool/int/float/str/list/map)",
        &type_name(obj),
    ))
}

impl<'a, 'py> FromPyObject<'a, 'py> for CheckedValue {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        extract_checked(&obj, 0)
    }
}

impl<'py> IntoPyObject<'py> for CheckedValue {
    type Target = PyAny;
    type Output = Bound<'py, PyAny>;
    type Error = Infallible;

    fn into_pyobject(self, py: Python<'py>) -> Result<Self::Output, Self::Error> {
        // Every child conversion is Infallible; errors are unreachable.
        Ok(match self {
            CheckedValue::Null => py.None().into_bound(py),
            CheckedValue::Bool(b) => PyBool::new(py, b).to_owned().into_any(),
            CheckedValue::Int(v) => match v.into_pyobject(py) {
                Ok(value) => value.into_any(),
                Err(never) => match never {},
            },
            CheckedValue::Float(v) => match v.into_pyobject(py) {
                Ok(value) => value.into_any(),
                Err(never) => match never {},
            },
            CheckedValue::Str(s) => PyString::new(py, &s).into_any(),
            CheckedValue::List(values) => {
                let list = PyList::empty(py);
                for value in values {
                    let item = match value.into_pyobject(py) {
                        Ok(item) => item,
                        Err(never) => match never {},
                    };
                    let _ = list.append(item);
                }
                list.into_any()
            }
            CheckedValue::Map(entries) => {
                let dict = PyDict::new(py);
                for (key, value) in entries {
                    let item = match value.into_pyobject(py) {
                        Ok(item) => item,
                        Err(never) => match never {},
                    };
                    let _ = dict.set_item(key, item);
                }
                dict.into_any()
            }
        })
    }
}

// Keep PyValueError referenced so a future typed ValueError stays one import away.
#[allow(dead_code)]
fn _unused(_: PyValueError) {}

#[cfg(test)]
mod tests {
    use super::{check_count, checked_finite, checked_u64_from_i128, validate_id, CheckedValue};

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

    #[test]
    fn value_kinds_stay_distinct_and_map_lookup_separates_null_from_missing() {
        assert_eq!(CheckedValue::Null.kind(), "null");
        assert_eq!(CheckedValue::Bool(true).kind(), "bool");
        assert_eq!(CheckedValue::Int(-1).kind(), "int");
        assert_eq!(CheckedValue::Float(1.5).kind(), "float");
        assert_eq!(CheckedValue::Str("s".to_owned()).kind(), "str");
        assert_eq!(CheckedValue::List(vec![]).kind(), "list");
        assert_eq!(CheckedValue::Map(vec![]).kind(), "map");
        // Bool is never read as int (and vice versa).
        assert_ne!(CheckedValue::Bool(true), CheckedValue::Int(1));
        let map = CheckedValue::Map(vec![("flag".to_owned(), CheckedValue::Null)]);
        assert_eq!(map.get("flag"), Some(&CheckedValue::Null));
        assert_eq!(map.get("absent"), None);
        assert_ne!(map.get("flag"), map.get("absent"));
        assert_eq!(CheckedValue::List(vec![]).get("flag"), None);
    }
}
