//! Test-only access to the frozen omp contract records in `tests/omp_contract/vectors.json`.

use serde_json::Value;

const VECTORS: &str = include_str!("../../../tests/omp_contract/vectors.json");

/// Every record of the frozen vector file, in file order.
pub(crate) fn all() -> Vec<Value> {
    serde_json::from_str(VECTORS).expect("vectors.json is a JSON array of records")
}

/// The record with `id`. A missing id is a test wiring error, so it panics.
pub(crate) fn record(id: &str) -> Value {
    all()
        .into_iter()
        .find(|r| r["id"] == id)
        .unwrap_or_else(|| panic!("vector {id} is not in vectors.json"))
}

/// The `value` of the record with `id`.
pub(crate) fn value(id: &str) -> Value {
    record(id)["value"].clone()
}
