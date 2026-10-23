//! Wasmtime sandbox host for GSD-Core.
//!
//! Verifies the identity of the GSD-Core guest module (SHA-256 and semver against a manifest).
//! Wasmtime loading and the typed invoke boundary arrive in U5.2.

pub mod wasmtime_module_build;

pub use wasmtime_module_build::{
    describe, parse_manifest, verify_module, ModuleIdentity, ModuleManifest, SandboxError,
};
