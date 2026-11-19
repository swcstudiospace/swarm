#!/usr/bin/env bash
# Build the abi3-py312 manylinux_2_28 x86_64 wheel for the swarm._native extension.
#
# Usage: scripts/ci_build_wheel.sh [OUT_DIR]   (default: dist, relative to the repo root)
#
# maturin's manylinux_2_28 check rejects any wheel whose binary needs glibc newer than 2.28,
# so on a host with a newer glibc the build runs inside the official manylinux_2_28 image
# (requires docker). Set SWARM_WHEEL_NO_DOCKER=1 to build directly on a manylinux_2_28 host.
#
# The Rust toolchain channel is read from rust-toolchain.toml, the source of truth for the pin.
# The container branch never downloads a toolchain. The caller (the CI job or a developer) must
# provide it on the host with rustup, e.g. `rustup toolchain install <channel> --profile minimal`,
# so that ${RUSTUP_HOME:-$HOME/.rustup}/toolchains/<channel>-x86_64-unknown-linux-gnu exists.
# That directory is mounted read-only into the container. The script fails if it is missing.
set -euo pipefail

MANYLINUX_IMAGE="${SWARM_MANYLINUX_IMAGE:-quay.io/pypa/manylinux_2_28_x86_64}"
MATURIN_VERSION="1.15.0"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUST_TOOLCHAIN="$(sed -n 's/^channel = "\(.*\)"$/\1/p' "$repo_root/rust-toolchain.toml")"
if [[ -z "$RUST_TOOLCHAIN" ]]; then
  echo "ci_build_wheel: no toolchain channel found in rust-toolchain.toml" >&2
  exit 1
fi

out_arg="${1:-dist}"
case "$out_arg" in
  /*) out_dir="$out_arg" ;;
  *) out_dir="$repo_root/$out_arg" ;;
esac
mkdir -p "$out_dir"
out_dir="$(cd "$out_dir" && pwd)"

in_manylinux() { [[ "${SWARM_WHEEL_IN_CONTAINER:-}" == "1" ]]; }

if in_manylinux || [[ "${SWARM_WHEEL_NO_DOCKER:-}" == "1" ]]; then
  maturin build \
    --release \
    --locked \
    --manifest-path "$repo_root/crates/gsd-python/Cargo.toml" \
    --compatibility manylinux_2_28 \
    --out "$out_dir"
  exit 0
fi

# Run the same script inside the manylinux_2_28 image. The image ships CPython 3.12 under
# /opt/python/cp312-cp312; maturin is pinned there so the wheel tooling is reproducible.
# Rust: the pinned toolchain comes from the host rustup directory, mounted into the container.
# Rust's Linux std binaries target glibc 2.17, so they run in the 2.28 image.
cargo_mounts=()
host_registry="${CARGO_HOME:-$HOME/.cargo}/registry"
if [[ -d "$host_registry" ]]; then
  cargo_mounts+=(-v "$host_registry:/root/.cargo/registry")
fi
host_toolchain="${RUSTUP_HOME:-$HOME/.rustup}/toolchains/${RUST_TOOLCHAIN}-x86_64-unknown-linux-gnu"
if [[ ! -x "$host_toolchain/bin/cargo" ]]; then
  echo "ci_build_wheel: Rust toolchain ${RUST_TOOLCHAIN} is not installed on the host at ${host_toolchain}." >&2
  echo "ci_build_wheel: install it with rustup (rustup toolchain install ${RUST_TOOLCHAIN} --profile minimal) and retry." >&2
  exit 1
fi
cargo_mounts+=(-v "$host_toolchain:/opt/rust-toolchain:ro")
# gsd-proto's build script (prost-build) needs protoc, and the manylinux image has none. The CI job
# fetches a pinned release into PROTOC_HOME (bin/protoc + include/); mount it read-only and point
# prost at it. Unset PROTOC_HOME keeps the old behaviour: the build fails if protoc is missing.
proto_env=()
if [[ -n "${PROTOC_HOME:-}" ]]; then
  cargo_mounts+=(-v "$PROTOC_HOME/bin/protoc:/opt/protoc/bin/protoc:ro" -v "$PROTOC_HOME/include:/opt/protoc/include:ro")
  proto_env=(-e PROTOC=/opt/protoc/bin/protoc -e PROTOC_INCLUDE=/opt/protoc/include)
fi

exec docker run --rm \
  "${proto_env[@]}" \
  -e SWARM_WHEEL_IN_CONTAINER=1 \
  -e CARGO_TARGET_DIR=/io/target/manylinux_2_28 \
  -v "$repo_root:/io" \
  -v "$out_dir:/out" \
  "${cargo_mounts[@]}" \
  -w /io \
  "$MANYLINUX_IMAGE" \
  bash -c "set -euo pipefail; export PATH=/opt/rust-toolchain/bin:\$PATH; /opt/python/cp312-cp312/bin/python -m pip install --quiet maturin==${MATURIN_VERSION}; /opt/python/cp312-cp312/bin/maturin build --release --locked --manifest-path crates/gsd-python/Cargo.toml --compatibility manylinux_2_28 --out /out"
