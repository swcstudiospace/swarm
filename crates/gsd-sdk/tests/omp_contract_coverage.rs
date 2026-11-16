//! Coverage of the frozen omp contract vectors.
//!
//! Every record in `tests/omp_contract/vectors.json` is replayed by exactly one module of this crate
//! (its `VECTOR_IDS`), or it is Python-only. No module may name a record that does not exist, so a
//! renamed or removed record fails here instead of silently leaving the replay.

use std::collections::{BTreeMap, BTreeSet};

use gsd_sdk::{
    omp_bridge_pins, omp_bridge_runner, omp_classifier_rule, omp_constants_mirror,
    omp_surface_pins, omp_tool_argv,
};

const VECTORS: &str = include_str!("../../../tests/omp_contract/vectors.json");

/// Records no Rust module replays. `A-LIFECYCLE-*` is the second Python spawner in `commands.ts`
/// (freeze §7.1) and the inline Python it runs; the Python replay checks them against the source.
fn python_only(id: &str) -> bool {
    id.starts_with("A-LIFECYCLE-")
}

fn record_ids() -> Vec<String> {
    let records: Vec<serde_json::Value> =
        serde_json::from_str(VECTORS).expect("vectors.json is a JSON array");
    records
        .iter()
        .map(|record| {
            record["id"]
                .as_str()
                .expect("every vector record has a string id")
                .to_string()
        })
        .collect()
}

fn claims() -> Vec<(&'static str, &'static [&'static str])> {
    vec![
        ("omp_bridge_pins", omp_bridge_pins::VECTOR_IDS),
        ("omp_bridge_runner", omp_bridge_runner::VECTOR_IDS),
        ("omp_classifier_rule", omp_classifier_rule::VECTOR_IDS),
        ("omp_constants_mirror", omp_constants_mirror::VECTOR_IDS),
        ("omp_surface_pins", omp_surface_pins::VECTOR_IDS),
        ("omp_tool_argv", omp_tool_argv::VECTOR_IDS),
    ]
}

#[test]
fn no_record_is_replayed_by_two_modules() {
    let mut owners: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for (module, ids) in claims() {
        for id in ids {
            owners.entry(id).or_default().push(module);
        }
    }
    let doubled: Vec<_> = owners
        .iter()
        .filter(|(_, modules)| modules.len() > 1)
        .collect();
    assert!(
        doubled.is_empty(),
        "records claimed by more than one module: {doubled:?}"
    );
}

#[test]
fn every_claim_names_a_real_record() {
    let known: BTreeSet<String> = record_ids().into_iter().collect();
    let phantom: Vec<&str> = claims()
        .iter()
        .flat_map(|(_, ids)| ids.iter().copied())
        .filter(|id| !known.contains(*id))
        .collect();
    assert!(
        phantom.is_empty(),
        "modules name records absent from vectors.json: {phantom:?}"
    );
}

#[test]
fn every_record_is_replayed_or_python_only() {
    let claimed: BTreeSet<&str> = claims()
        .iter()
        .flat_map(|(_, ids)| ids.iter().copied())
        .collect();
    let ids = record_ids();
    let unreplayed: Vec<&String> = ids
        .iter()
        .filter(|id| !claimed.contains(id.as_str()) && !python_only(id))
        .collect();
    assert!(
        unreplayed.is_empty(),
        "records no Rust module replays: {unreplayed:?}"
    );
}

#[test]
fn python_only_records_are_not_claimed_by_rust() {
    let claimed: BTreeSet<&str> = claims()
        .iter()
        .flat_map(|(_, ids)| ids.iter().copied())
        .collect();
    let contradictory: Vec<&str> = claimed
        .iter()
        .copied()
        .filter(|id| python_only(id))
        .collect();
    assert!(
        contradictory.is_empty(),
        "python-only records also claimed by Rust: {contradictory:?}"
    );
}

#[test]
fn record_ids_are_unique_in_the_vector_file() {
    let ids = record_ids();
    let unique: BTreeSet<&String> = ids.iter().collect();
    assert_eq!(unique.len(), ids.len(), "vectors.json repeats a record id");
}
