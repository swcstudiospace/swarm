//! Verified loading and instantiation of the GSD-Core guest component under wasmtime.
//!
//! Identity is checked before any compilation. Instantiation links no WASI and no host imports,
//! so a component with any import fails with `E-DEP`.

use gsd_failure::ErrorCode;
use wasmtime::component::{Component, Instance, Linker};
use wasmtime::{Config, Engine, Store, StoreLimits};

use crate::wasmtime_limits::{apply, Limits, LimitsError};
use crate::wasmtime_module_build::{verify_module, ModuleIdentity, ModuleManifest, SandboxError};

/// Refusals raised while loading or instantiating a guest component.
#[derive(Debug, thiserror::Error)]
pub enum LoadError {
    /// The module bytes failed the identity check.
    #[error(transparent)]
    Identity(SandboxError),
    /// The module bytes do not compile as a component.
    #[error("the module bytes do not compile as a component")]
    Compile,
    /// The component could not be instantiated: an import is unresolved or the instance failed.
    #[error(
        "the component could not be instantiated: an import is unresolved or the instance failed"
    )]
    Instantiate,
    /// The guest limits are invalid.
    #[error(transparent)]
    Limits(LimitsError),
}

impl LoadError {
    /// Maps every load refusal to the shared `E-DEP` code.
    pub fn error_code(&self) -> ErrorCode {
        match self {
            Self::Identity(inner) => inner.error_code(),
            Self::Limits(inner) => inner.error_code(),
            Self::Compile | Self::Instantiate => ErrorCode::Dep,
        }
    }
}

impl From<SandboxError> for LoadError {
    fn from(err: SandboxError) -> Self {
        Self::Identity(err)
    }
}

impl From<LimitsError> for LoadError {
    fn from(err: LimitsError) -> Self {
        Self::Limits(err)
    }
}

/// Wasmtime engine configured for the component model.
pub struct GuestLoader {
    engine: Engine,
}

impl GuestLoader {
    /// Builds a loader with the component model, fuel and epoch interruption enabled.
    pub fn new() -> Result<Self, LoadError> {
        let mut config = Config::new();
        config.wasm_component_model(true);
        config.consume_fuel(true);
        config.epoch_interruption(true);
        let engine = Engine::new(&config).map_err(|_| LoadError::Compile)?;
        Ok(Self { engine })
    }

    /// Verifies `bytes` against `manifest` and `expected_semver`, then compiles them.
    ///
    /// The identity check runs first: unverified bytes are never compiled.
    pub fn load_verified(
        &self,
        bytes: &[u8],
        manifest: &ModuleManifest,
        expected_semver: &str,
    ) -> Result<LoadedGuest, LoadError> {
        let identity = verify_module(bytes, manifest, expected_semver)?;
        let component =
            Component::from_binary(&self.engine, bytes).map_err(|_| LoadError::Compile)?;
        Ok(LoadedGuest {
            identity,
            component,
        })
    }

    /// Instantiates a verified guest under `limits` with an empty linker: no WASI and no host imports.
    pub fn instantiate(
        &self,
        guest: &LoadedGuest,
        limits: &Limits,
    ) -> Result<GuestInstance, LoadError> {
        limits.validate()?;
        let linker: Linker<GuestState> = Linker::new(&self.engine);
        let mut store = Store::new(
            &self.engine,
            GuestState {
                limits: StoreLimits::default(),
            },
        );
        apply(&mut store, limits).map_err(|_| LoadError::Instantiate)?;
        let instance = linker
            .instantiate(&mut store, &guest.component)
            .map_err(|_| LoadError::Instantiate)?;
        Ok(GuestInstance { store, instance })
    }
}

/// A compiled guest component whose identity matched its manifest.
pub struct LoadedGuest {
    identity: ModuleIdentity,
    component: Component,
}

impl LoadedGuest {
    /// Identity verified for these bytes.
    pub fn identity(&self) -> &ModuleIdentity {
        &self.identity
    }
}

/// A live guest instance and the store that owns it.
pub struct GuestInstance {
    store: Store<GuestState>,
    instance: Instance,
}

/// Per-store state: the resource limiter installed by `wasmtime_limits::apply`.
pub(crate) struct GuestState {
    pub(crate) limits: StoreLimits,
}

impl GuestInstance {
    /// True when the instance exports an item named `name`.
    pub fn has_export(&mut self, name: &str) -> bool {
        self.instance
            .get_export_index(&mut self.store, None, name)
            .is_some()
    }

    /// The store and the instance, for crate tests that call exports.
    #[cfg(test)]
    pub(crate) fn parts(&mut self) -> (&mut Store<GuestState>, Instance) {
        (&mut self.store, self.instance)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wasmtime_module_build::{describe, MODULE_NAME};

    const SEMVER: &str = "0.1.0";

    const LIMITS: Limits = Limits {
        fuel: 1_000_000,
        memory_bytes: 65536,
        deadline_ticks: None,
    };

    fn component_bytes(text: &str) -> Vec<u8> {
        wat::parse_str(text).expect("test WAT parses")
    }

    #[test]
    fn load_refuses_a_flipped_byte_before_compiling() {
        let loader = GuestLoader::new().expect("loader builds");
        let bytes = component_bytes("(component)");
        let manifest = describe(&bytes, MODULE_NAME, SEMVER);
        let mut tampered = bytes.clone();
        let last = tampered.len() - 1;
        tampered[last] ^= 0x01;
        match loader.load_verified(&tampered, &manifest, SEMVER) {
            Err(LoadError::Identity(SandboxError::DigestMismatch)) => {}
            _ => panic!("flipped byte must be refused by the identity check"),
        }
    }

    #[test]
    fn load_refuses_bytes_that_are_not_a_component() {
        let loader = GuestLoader::new().expect("loader builds");
        let bytes = b"\x00asm-not-a-component";
        let manifest = describe(bytes, MODULE_NAME, SEMVER);
        match loader.load_verified(bytes, &manifest, SEMVER) {
            Err(LoadError::Compile) => {}
            _ => panic!("non-component bytes must fail at compile"),
        }
    }

    #[test]
    fn instantiates_a_component_with_no_imports() {
        let loader = GuestLoader::new().expect("loader builds");
        let bytes = component_bytes("(component)");
        let manifest = describe(&bytes, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(&bytes, &manifest, SEMVER)
            .expect("verified component loads");
        assert!(loader.instantiate(&guest, &LIMITS).is_ok());
    }

    #[test]
    fn refuses_an_unresolved_import_with_dep() {
        let loader = GuestLoader::new().expect("loader builds");
        let bytes = component_bytes(
            r#"(component (import "gsd:test/host@0.1.0" (instance (export "f" (func)))))"#,
        );
        let manifest = describe(&bytes, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(&bytes, &manifest, SEMVER)
            .expect("verified component loads");
        match loader.instantiate(&guest, &LIMITS) {
            Err(err) => assert_eq!(err.error_code(), ErrorCode::Dep),
            Ok(_) => panic!("unresolved import must refuse instantiation"),
        }
    }

    #[test]
    fn instantiate_refuses_invalid_limits_with_input() {
        let loader = GuestLoader::new().expect("loader builds");
        let bytes = component_bytes("(component)");
        let manifest = describe(&bytes, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(&bytes, &manifest, SEMVER)
            .expect("verified component loads");
        let limits = Limits {
            fuel: 0,
            memory_bytes: 65536,
            deadline_ticks: None,
        };
        match loader.instantiate(&guest, &limits) {
            Err(err) => assert_eq!(err.error_code(), ErrorCode::Input),
            Ok(_) => panic!("zero fuel must refuse instantiation"),
        }
    }

    #[test]
    fn every_load_error_maps_to_the_dep_code() {
        let variants = [
            LoadError::Identity(SandboxError::EmptyModule),
            LoadError::Identity(SandboxError::DigestMismatch),
            LoadError::Identity(SandboxError::VersionMismatch {
                expected: "0.2.0".to_owned(),
                found: "0.1.0".to_owned(),
            }),
            LoadError::Identity(SandboxError::InvalidManifest("reason".to_owned())),
            LoadError::Compile,
            LoadError::Instantiate,
        ];
        for variant in &variants {
            assert_eq!(variant.error_code(), ErrorCode::Dep);
        }
    }

    #[test]
    fn refusals_hold_no_module_bytes() {
        let loader = GuestLoader::new().expect("loader builds");
        let bytes = component_bytes("(component)");
        let manifest = describe(&bytes, MODULE_NAME, SEMVER);
        let mut tampered = bytes.clone();
        let last = tampered.len() - 1;
        tampered[last] ^= 0x01;
        let err = match loader.load_verified(&tampered, &manifest, SEMVER) {
            Err(err) => err,
            Ok(_) => panic!("flipped byte must be refused"),
        };
        let text = err.to_string();
        assert_eq!(
            text,
            "guest module bytes do not match the manifest SHA-256 digest"
        );
        assert!(!text.contains("(component)"));
    }
}
