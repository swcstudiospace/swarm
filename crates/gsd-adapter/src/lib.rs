//! `gsd-adapter`: the Strangler seam between legacy subagents and GSD-Core.
//!
//! The crate is the Strangler seam: legacy routes are gradually redirected to
//! GSD-Core while the existing call surface keeps working.

pub mod gates;
pub mod join_affinity_guard;
pub mod legacy_shim_registry;
pub mod route_table;
pub mod sandbox_engine;
pub mod strangler_seam;
pub mod write_route;
