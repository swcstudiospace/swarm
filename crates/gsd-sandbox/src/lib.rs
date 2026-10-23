//! Wasmtime sandbox host for GSD-Core.
//!
//! Verifies the identity of the GSD-Core guest module (SHA-256 and semver against a manifest).
//! The typed invoke boundary arrives in a later unit.

pub mod wasmtime_limits;
pub mod wasmtime_module_build;
pub mod wasmtime_module_load;

pub use wasmtime_limits::{Limits, LimitsError};
pub use wasmtime_module_build::{
    describe, parse_manifest, verify_module, ModuleIdentity, ModuleManifest, SandboxError,
};
pub use wasmtime_module_load::{GuestInstance, GuestLoader, LoadError, LoadedGuest};
