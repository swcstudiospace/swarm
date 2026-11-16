//! `gsd-adapter`: the Strangler seam between legacy subagents and GSD-Core.
//!
//! The crate is the Strangler seam: legacy routes are gradually redirected to
//! GSD-Core while the existing call surface keeps working.
//!
//! See `docs/assumptions.md` for n6 ambiguity resolutions with defaults (GSD-Core contracts, decomposition, execution model, pool sizing, result aggregation). Refs SPE-33068 n6.8 etc, n7 synthesize, Notion from graph, plan files.
pub mod gates;
pub mod join_affinity_guard;
pub mod legacy_shim_registry;
pub mod route_table;
pub mod sandbox_engine;
#[cfg(feature = "sandbox-write")]
pub mod sandbox_shadow;
pub mod strangler_seam;
pub mod write_route;
