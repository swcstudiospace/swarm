#!/usr/bin/env bash
# Install the Rust toolchain pinned in rust-toolchain.toml with the components and target CI needs.
# Idempotent; needs rustup, no root and no Docker. Does not edit rust-toolchain.toml.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v rustup >/dev/null 2>&1; then
  echo "ci_rust_toolchain: rustup is not installed on this host; install it from https://rustup.rs and re-run" >&2
  exit 1
fi

channel="$(sed -n 's/^channel = "\(.*\)"$/\1/p' rust-toolchain.toml)"
if [ -z "$channel" ]; then
  echo "ci_rust_toolchain: no channel found in rust-toolchain.toml" >&2
  exit 1
fi

rustup toolchain install "$channel" --profile minimal \
  --component rustfmt --component clippy \
  --target wasm32-unknown-unknown --no-self-update

cargo -V
