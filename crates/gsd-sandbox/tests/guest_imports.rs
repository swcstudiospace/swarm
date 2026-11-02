//! The embedded guest component imports nothing from its host (phase 14.5, unit U3.4e part 1: no key, and no way to ask for one).
//!
//! The allowed import list is derived from the host code, not from the guest: [`GuestLoader::instantiate`] (the only way the sandbox
//! engine of `gsd-adapter` starts a guest) calls `instantiate_with(.., |_| Ok(()))`, an EMPTY linker, so every import of a component
//! stays unresolved and the instantiation fails with `E-DEP`. The `gsd:core-host` bridge (`log`, `time`, `rand`, `storage`) is linked
//! only by `HostBridge`, which no engine uses for this guest, and the WIT world of the guest (`gsd-guest/wit/gsd-core.wit`) declares
//! an export and no import. The allowed set is therefore empty, and any import at all (the bridge's included, WASI above all) is a
//! change that needs a reviewed edit of [`ALLOWED_IMPORTS`].
//!
//! The test runs on whichever guest the crate was built with: the default one, or the write one under `--features write-routes`.
//! Run both: `cargo test -p gsd-sandbox --test guest_imports` and the same with `--features write-routes`.

use gsd_sandbox::wasmtime_module_build::{describe, MODULE_NAME};
use gsd_sandbox::{GuestLoader, Limits, LoadError};
use wasmtime::component::Component;
use wasmtime::{Config, Engine};

const GUEST: &[u8] = include_bytes!(env!("GSD_GUEST_WASM"));
const SEMVER: &str = "0.1.0";

/// The imports the production linker provides: none.
const ALLOWED_IMPORTS: &[&str] = &[];

/// A component that imports the log interface of the host bridge: an import the empty linker cannot resolve.
const BRIDGE_IMPORT: &str = r#"(component
  (import "gsd:core-host/log@0.2.0" (instance
    (export "emit" (func (param "message" string))))))"#;

/// A component that imports a WASI interface, the one a key could be read through.
const WASI_IMPORT: &str = r#"(component
  (import "wasi:cli/environment@0.2.0" (instance
    (export "get-arguments" (func (result (list string)))))))"#;

/// The engine configured as the sandbox's loader configures its own, so component types resolve the same way.
fn engine() -> Engine {
    let mut config = Config::new();
    config.wasm_component_model(true);
    Engine::new(&config).expect("a component-model engine builds")
}

/// The names of every import of the component in `bytes`, in the order the component type lists them.
fn imports_of(engine: &Engine, bytes: &[u8]) -> Vec<String> {
    let component = Component::from_binary(engine, bytes).expect("the component compiles");
    component
        .component_type()
        .imports(engine)
        .map(|(name, _)| name.to_string())
        .collect()
}

/// How the imports `names` differ from the `allowed` list, and which of them are WASI, whatever `allowed` says.
#[derive(Debug, PartialEq, Eq)]
struct Drift {
    /// Imports the host does not provide.
    extra: Vec<String>,
    /// Provided interfaces that are not imported.
    missing: Vec<String>,
    /// Imports from a `wasi` namespace.
    wasi: Vec<String>,
}

impl Drift {
    /// The import set equals the allowed set and nothing is WASI.
    fn is_none(&self) -> bool {
        self.extra.is_empty() && self.missing.is_empty() && self.wasi.is_empty()
    }
}

fn import_drift(names: &[String], allowed: &[&str]) -> Drift {
    Drift {
        extra: names
            .iter()
            .filter(|name| !allowed.contains(&name.as_str()))
            .cloned()
            .collect(),
        missing: allowed
            .iter()
            .filter(|name| !names.iter().any(|have| have == **name))
            .map(|name| (*name).to_string())
            .collect(),
        wasi: names
            .iter()
            .filter(|name| name.contains("wasi"))
            .cloned()
            .collect(),
    }
}

#[test]
fn the_embedded_guest_imports_exactly_the_interfaces_the_linker_provides() {
    let engine = engine();
    let drift = import_drift(&imports_of(&engine, GUEST), ALLOWED_IMPORTS);
    assert_eq!(
        drift,
        Drift {
            extra: Vec::new(),
            missing: Vec::new(),
            wasi: Vec::new()
        }
    );
}

#[test]
fn the_embedded_guest_exports_the_invoke_world_and_nothing_to_read_a_key_through() {
    let engine = engine();
    let component = Component::from_binary(&engine, GUEST).expect("the guest compiles");
    let exports: Vec<String> = component
        .component_type()
        .exports(&engine)
        .map(|(name, _)| name.to_string())
        .collect();
    assert_eq!(exports, vec!["invoke".to_string()]);
}

#[test]
fn the_production_loader_runs_the_embedded_guest_with_the_empty_linker() {
    let loader = GuestLoader::new().expect("loader builds");
    let manifest = describe(GUEST, MODULE_NAME, SEMVER);
    let guest = loader
        .load_verified(GUEST, &manifest, SEMVER)
        .expect("the built guest verifies and compiles");
    loader
        .instantiate(&guest, &limits())
        .expect("a guest without imports instantiates with an empty linker");
}

fn limits() -> Limits {
    Limits {
        fuel: 1_000_000,
        memory_bytes: 16 * 1024 * 1024,
        deadline_ticks: None,
    }
}

/// Teeth: the same checks, run on components that DO import something, must fail.
#[test]
fn a_scratch_import_is_reported_and_the_allowlist_is_the_only_way_to_admit_it() {
    let engine = engine();
    for (text, name, is_wasi) in [
        (BRIDGE_IMPORT, "gsd:core-host/log@0.2.0", false),
        (WASI_IMPORT, "wasi:cli/environment@0.2.0", true),
    ] {
        let bytes = wat::parse_str(text).expect("the component text parses");
        let names = imports_of(&engine, &bytes);
        assert_eq!(names, vec![name.to_string()]);
        let drift = import_drift(&names, ALLOWED_IMPORTS);
        assert!(!drift.is_none());
        assert_eq!(drift.extra, vec![name.to_string()]);
        assert!(drift.missing.is_empty());
        assert_eq!(drift.wasi.len(), usize::from(is_wasi));
        // Adding the name to a copy of the allowlist is what admits a non-WASI import; WASI stays flagged either way.
        let widened = [ALLOWED_IMPORTS, &[name]].concat();
        let admitted = import_drift(&names, &widened);
        assert!(admitted.extra.is_empty() && admitted.missing.is_empty());
        assert_eq!(admitted.is_none(), !is_wasi);
    }
}

#[test]
fn an_allowed_import_the_guest_does_not_have_is_reported_as_missing() {
    let drift = import_drift(&[], &["gsd:core-host/log@0.2.0"]);
    assert!(drift.extra.is_empty() && drift.wasi.is_empty());
    assert_eq!(drift.missing, vec!["gsd:core-host/log@0.2.0".to_string()]);
}

/// Defence in depth: a component that imports anything never runs on the production loader, whatever the check above says.
#[test]
fn the_production_loader_refuses_a_component_with_an_import() {
    let loader = GuestLoader::new().expect("loader builds");
    for text in [BRIDGE_IMPORT, WASI_IMPORT] {
        let bytes = wat::parse_str(text).expect("the component text parses");
        let manifest = describe(&bytes, MODULE_NAME, SEMVER);
        let guest = loader
            .load_verified(&bytes, &manifest, SEMVER)
            .expect("the component verifies and compiles");
        assert!(matches!(
            loader.instantiate(&guest, &limits()),
            Err(LoadError::Instantiate)
        ));
    }
}

/// The committed pins of the two guests: the default read guest and the write guest (`write-routes`).
const DEFAULT_PIN: &str = include_str!("../guest-module.sha256");
const WRITE_PIN: &str = include_str!("../guest-module-write.sha256");

/// Positive control for the run with `--features write-routes` (the CI step of the key tests): the guest this binary embeds is the
/// one the feature selects and not the other one, so the import checks above ran on the write guest and not on the default one.
/// The default build is only held apart from the write pin: its own pin is not compared here.
#[test]
fn the_embedded_guest_is_the_flavour_the_feature_selects() {
    let digest = describe(GUEST, MODULE_NAME, SEMVER).sha256;
    let other = if cfg!(feature = "write-routes") {
        DEFAULT_PIN
    } else {
        WRITE_PIN
    };
    assert_ne!(
        digest,
        other.trim(),
        "the embedded guest is the other flavour"
    );
    #[cfg(feature = "write-routes")]
    assert_eq!(
        digest,
        WRITE_PIN.trim(),
        "the embedded guest is not the pinned write guest"
    );
}
