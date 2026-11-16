//! Wasmtime sandbox host for GSD-Core.
//!
//! Verifies the identity of the GSD-Core guest component (SHA-256 and semver against a manifest),
//! loads it, and calls its exports through the typed WIT boundary. The read route links no
//! imports. [`wasmtime_host_bridge`] links the `gsd:core-host` imports (`log`, `time`, `rand`,
//! `storage`) for a component that declares them, and each import is gated by a capability token.
//! `build.rs` builds the guest for `wasm32-unknown-unknown`, publishes its path as
//! `GSD_GUEST_WASM` and the digest of that component as `GSD_GUEST_DIGEST_FILE`.

pub mod wasmtime_guest_invoke;
pub mod wasmtime_host_bridge;
pub mod wasmtime_limits;
pub mod wasmtime_module_build;
pub mod wasmtime_module_load;

pub use wasmtime_guest_invoke::GuestInvokeError;
pub use wasmtime_host_bridge::{
    CallRecord, HostBridge, HostCall, HostCallError, HostInstance, SessionStorage,
    StorageUnavailable, LOG_BATCH_CAP_BYTES,
};
pub use wasmtime_limits::{Limits, LimitsError};
pub use wasmtime_module_build::{
    describe, parse_manifest, verify_module, ModuleIdentity, ModuleManifest, SandboxError,
};
pub use wasmtime_module_load::{GuestInstance, GuestLoader, LoadError, LoadedGuest};

/// The built GSD-Core guest component and the SHA-256 pin committed beside this crate, as
/// `(component bytes, pin)`. The pin is lowercase hex with a trailing newline. It is `guest-module.sha256`, or
/// `guest-module-write.sha256` when the crate is built with its `write-routes` feature.
pub const GUEST_MODULE: (&[u8], &str) = (
    include_bytes!(env!("GSD_GUEST_WASM")),
    include_str!(env!("GSD_GUEST_PIN_FILE")),
);

#[cfg(test)]
mod guest_digest {
    use sha2::{Digest, Sha256};
    use std::fs;
    use std::path::Path;

    const GUEST: &[u8] = include_bytes!(env!("GSD_GUEST_WASM"));

    #[test]
    fn built_guest_digest_matches_the_committed_pin() {
        let built = fs::read_to_string(env!("GSD_GUEST_DIGEST_FILE"))
            .expect("build.rs writes the guest digest to OUT_DIR");
        let pinned = fs::read_to_string(env!("GSD_GUEST_PIN_FILE"))
            .expect("the guest pin is committed next to Cargo.toml");
        let actual: String = Sha256::digest(GUEST)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        assert_eq!(
            built.trim(),
            actual,
            "the build output must digest the embedded guest"
        );
        assert_eq!(
            pinned.trim(),
            actual,
            "the guest must match its committed pin (guest-module.sha256, or guest-module-write.sha256 with write-routes)"
        );
    }

    #[test]
    fn digest_and_pin_file_names_differ() {
        let digest = Path::new(env!("GSD_GUEST_DIGEST_FILE"))
            .file_name()
            .expect("the digest path names a file");
        let pin = Path::new(env!("GSD_GUEST_PIN_FILE"))
            .file_name()
            .expect("the pin path names a file");
        assert_ne!(
            digest, pin,
            "the build digest must not overwrite the committed pin's file name"
        );
    }
}
