//! Rust mirror of the omp host contract in `omp/src`, with the bridge that reproduces it.
//!
//! `omp/src` stays the authority and is never edited from this crate. The frozen contract is
//! `tests/omp_contract/freeze.md`, with its records in `tests/omp_contract/vectors.json`. Each module
//! lists the record ids it replays in `VECTOR_IDS`; `tests/omp_contract_coverage.rs` checks that every
//! record is replayed by exactly one module or listed there as Python-only.

#[cfg(test)]
mod contract_vectors;

pub mod omp_bridge_pins;
pub mod omp_bridge_runner;
pub mod omp_classifier_rule;
pub mod omp_constants_mirror;
pub mod omp_surface_pins;
pub mod omp_tool_argv;
