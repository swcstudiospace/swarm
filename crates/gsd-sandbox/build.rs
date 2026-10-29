//! Builds the GSD-Core guest as a WebAssembly component and exports its path to the crate.
//!
//! The guest is built for `wasm32-unknown-unknown` by a nested `cargo build` into this crate's
//! `OUT_DIR`, so it never contends with the outer build for the workspace target directory. The
//! core module is then wrapped as a component. That target links no WASI imports, so the
//! component imports nothing and instantiates with an empty linker. The component path is
//! published as `GSD_GUEST_WASM` for `include_bytes!(env!("GSD_GUEST_WASM"))`. The SHA-256 of the
//! component is written to `OUT_DIR/guest-module.sha256` and published as
//! `GSD_GUEST_DIGEST_FILE`, so a test can compare the build with the committed pin.
//!
//! With this crate's `write-routes` feature the nested build gets the guest's `write-routes` feature, and the committed pin is
//! `guest-module-write.sha256` instead of `guest-module.sha256`. Its path is published as `GSD_GUEST_PIN_FILE`, so the crate and its
//! pin test read the pin of the guest they embed and nothing else.

use std::env;
use std::fs;
use std::path::PathBuf;
use std::process::Command;

use sha2::{Digest, Sha256};
use wit_component::ComponentEncoder;

const GUEST_PACKAGE: &str = "gsd-guest";
const GUEST_TARGET: &str = "wasm32-unknown-unknown";
const CORE_FILE: &str = "gsd_guest.wasm";
const COMPONENT_FILE: &str = "gsd_guest.component.wasm";
const DIGEST_FILE: &str = "guest-module.sha256";
const PIN_FILE: &str = "guest-module.sha256";
const WRITE_PIN_FILE: &str = "guest-module-write.sha256";

fn required_env(name: &str) -> String {
    env::var(name).unwrap_or_else(|_| panic!("cargo did not set {name} for the sandbox build"))
}

fn main() {
    let cargo = required_env("CARGO");
    let manifest_dir = PathBuf::from(required_env("CARGO_MANIFEST_DIR"));
    let out_dir = PathBuf::from(required_env("OUT_DIR"));
    let workspace_root = manifest_dir.join("..").join("..");
    let target_dir = out_dir.join("guest-target");

    let write_routes = env::var_os("CARGO_FEATURE_WRITE_ROUTES").is_some();

    // The outer build's encoded rustflags describe the host build; the guest must read its own
    // flags from the environment and the cargo config files.
    let mut build = Command::new(&cargo);
    build
        .current_dir(&workspace_root)
        .env_remove("CARGO_ENCODED_RUSTFLAGS")
        .args([
            "build",
            "--locked",
            "--release",
            "-p",
            GUEST_PACKAGE,
            "--target",
            GUEST_TARGET,
            "--target-dir",
        ])
        .arg(&target_dir);
    if write_routes {
        build.args(["--features", "write-routes"]);
    }
    let status = build
        .status()
        .unwrap_or_else(|err| panic!("failed to run `{cargo}` for the GSD-Core guest: {err}"));
    if !status.success() {
        panic!(
            "building the GSD-Core guest ({GUEST_PACKAGE} for {GUEST_TARGET}) failed; \
             install the target with `rustup target add {GUEST_TARGET}` and check the cargo output above"
        );
    }

    let core = target_dir
        .join(GUEST_TARGET)
        .join("release")
        .join(CORE_FILE);
    let core_bytes = fs::read(&core).unwrap_or_else(|err| {
        panic!(
            "the GSD-Core guest build left no module at {}: {err}",
            core.display()
        )
    });
    let component = ComponentEncoder::default()
        .module(&core_bytes)
        .and_then(|mut encoder| encoder.encode())
        .unwrap_or_else(|err| panic!("wrapping {} as a component failed: {err:#}", core.display()));
    let output = out_dir.join(COMPONENT_FILE);
    fs::write(&output, &component)
        .unwrap_or_else(|err| panic!("cannot write {}: {err}", output.display()));

    let digest: String = Sha256::digest(&component)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    let digest_path = out_dir.join(DIGEST_FILE);
    fs::write(&digest_path, format!("{digest}\n"))
        .unwrap_or_else(|err| panic!("cannot write {}: {err}", digest_path.display()));

    println!("cargo:rustc-env=GSD_GUEST_WASM={}", output.display());
    let pin_file = if write_routes {
        WRITE_PIN_FILE
    } else {
        PIN_FILE
    };
    println!(
        "cargo:rustc-env=GSD_GUEST_PIN_FILE={}",
        manifest_dir.join(pin_file).display()
    );
    println!(
        "cargo:rustc-env=GSD_GUEST_DIGEST_FILE={}",
        digest_path.display()
    );
    println!("cargo:rerun-if-changed=build.rs");
    println!(
        "cargo:rerun-if-changed={}",
        manifest_dir.join("../gsd-guest").display()
    );
    println!(
        "cargo:rerun-if-changed={}",
        manifest_dir.join("../gsd-route").display()
    );
    println!(
        "cargo:rerun-if-changed={}",
        manifest_dir.join("../gsd-codes").display()
    );
    println!(
        "cargo:rerun-if-changed={}",
        manifest_dir.join("../gsd-guest/wit/gsd-core.wit").display()
    );
    println!(
        "cargo:rerun-if-changed={}",
        manifest_dir
            .join("../gsd-guest/wit/host/gsd-core-host.wit")
            .display()
    );
    println!(
        "cargo:rerun-if-changed={}",
        workspace_root.join("Cargo.lock").display()
    );
    println!(
        "cargo:rerun-if-changed={}",
        workspace_root.join("Cargo.toml").display()
    );
    println!(
        "cargo:rerun-if-changed={}",
        workspace_root.join(".cargo/config.toml").display()
    );
    println!("cargo:rerun-if-env-changed=RUSTFLAGS");
}
