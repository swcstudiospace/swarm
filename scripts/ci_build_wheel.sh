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
#
# The gsd-proto crate's build.rs invokes protoc (via prost-build) at compile time, so the
# container must provide protoc before maturin runs. ensure_protoc installs the pinned
# official protoc release (whose bundled include/ carries the Well-Known Types .proto
# files that gsd.proto imports); dnf/yum protobuf-compiler is only a fallback, since the
# manylinux_2_28 package does not ship those WKT files on protoc's search path.
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

PROTOC_VERSION="28.3"
PROTOC_ZIP="protoc-${PROTOC_VERSION}-linux-x86_64.zip"
PROTOC_URL="https://github.com/protocolbuffers/protobuf/releases/download/v${PROTOC_VERSION}/${PROTOC_ZIP}"

protoc_has_wkt() {
  local bin="$1" dir
  dir="$(dirname "$bin")"
  [[ -f "$dir/../include/google/protobuf/timestamp.proto" \
    || -f /usr/include/google/protobuf/timestamp.proto \
    || -f /usr/local/include/google/protobuf/timestamp.proto ]]
}

install_protoc_from_release() {
  local install_dir="${PROTOC_INSTALL_DIR:-/opt/protoc}"
  local zip_path
  zip_path="$(mktemp -d)/${PROTOC_ZIP}"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 -o "$zip_path" "$PROTOC_URL" || return 1
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$zip_path" "$PROTOC_URL" || return 1
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$PROTOC_URL" "$zip_path" || return 1
  else
    return 1
  fi
  mkdir -p "$install_dir" || return 1
  if command -v unzip >/dev/null 2>&1; then
    unzip -q -o "$zip_path" -d "$install_dir" || return 1
  elif command -v python3 >/dev/null 2>&1; then
    python3 -m zipfile -e "$zip_path" "$install_dir" || return 1
  else
    return 1
  fi
  rm -rf "$(dirname "$zip_path")"
  [[ -x "$install_dir/bin/protoc" && -f "$install_dir/include/google/protobuf/timestamp.proto" ]] || return 1
  export PATH="$install_dir/bin:$PATH"
  export PROTOC="$install_dir/bin/protoc"
}

dnf_install_protoc() {
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y protobuf-compiler
  elif command -v yum >/dev/null 2>&1; then
    yum install -y protobuf-compiler
  else
    return 1
  fi
}

ensure_protoc() {
  local existing
  existing="$(command -v protoc || true)"
  if [[ -n "$existing" ]] && protoc_has_wkt "$existing"; then
    export PROTOC="$existing"
    return 0
  fi
  if install_protoc_from_release; then
    return 0
  fi
  echo "ci_build_wheel: downloading official protoc ${PROTOC_VERSION} failed; falling back to dnf/yum protobuf-compiler." >&2
  if dnf_install_protoc; then
    existing="$(command -v protoc || true)"
    if [[ -n "$existing" ]] && protoc_has_wkt "$existing"; then
      export PROTOC="$existing"
      return 0
    fi
  fi
  echo "ci_build_wheel: ERROR: no protoc with Well-Known Types includes (google/protobuf/*.proto) available." >&2
  echo "ci_build_wheel: the official protoc ${PROTOC_VERSION} download failed and the dnf/yum protobuf-compiler package does not ship the WKT .proto files on the protoc search path; gsd.proto imports (timestamp, duration, any, struct, wrappers) will fail with 'File not found'." >&2
  echo "ci_build_wheel: install the official protoc ${PROTOC_VERSION} release (its bundled include/ directory) onto PATH and retry." >&2
  return 1
}

if in_manylinux || [[ "${SWARM_WHEEL_NO_DOCKER:-}" == "1" ]]; then
  ensure_protoc
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
# protoc: gsd-proto's build.rs needs protoc with the Well-Known Types .proto files at
# compile time, so the bash -c guard installs the pinned official protoc release (bundled
# include/) before maturin, falling back to dnf/yum protobuf-compiler only if the download
# fails. The guard mirrors ensure_protoc above step for step.
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

exec docker run --rm \
  -e SWARM_WHEEL_IN_CONTAINER=1 \
  -e SWARM_MATURIN_VERSION="${MATURIN_VERSION}" \
  -e CARGO_TARGET_DIR=/io/target/manylinux_2_28 \
  -v "$repo_root:/io" \
  -v "$out_dir:/out" \
  "${cargo_mounts[@]}" \
  -w /io \
  "$MANYLINUX_IMAGE" \
  bash -c 'set -euo pipefail
export PATH=/opt/rust-toolchain/bin:$PATH
PROTOC_VERSION=28.3
PROTOC_ZIP="protoc-${PROTOC_VERSION}-linux-x86_64.zip"
PROTOC_URL="https://github.com/protocolbuffers/protobuf/releases/download/v${PROTOC_VERSION}/${PROTOC_ZIP}"
protoc_has_wkt() {
  local bin="$1" dir
  dir="$(dirname "$bin")"
  [ -f "$dir/../include/google/protobuf/timestamp.proto" ] \
    || [ -f /usr/include/google/protobuf/timestamp.proto ] \
    || [ -f /usr/local/include/google/protobuf/timestamp.proto ]
}
install_protoc_from_release() {
  local install_dir="${PROTOC_INSTALL_DIR:-/opt/protoc}"
  local zip_path
  zip_path="$(mktemp -d)/${PROTOC_ZIP}"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 -o "$zip_path" "$PROTOC_URL" || return 1
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$zip_path" "$PROTOC_URL" || return 1
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$PROTOC_URL" "$zip_path" || return 1
  else
    return 1
  fi
  mkdir -p "$install_dir" || return 1
  if command -v unzip >/dev/null 2>&1; then
    unzip -q -o "$zip_path" -d "$install_dir" || return 1
  elif command -v python3 >/dev/null 2>&1; then
    python3 -m zipfile -e "$zip_path" "$install_dir" || return 1
  else
    return 1
  fi
  rm -rf "$(dirname "$zip_path")"
  [ -x "$install_dir/bin/protoc" ] && [ -f "$install_dir/include/google/protobuf/timestamp.proto" ] || return 1
  export PATH="$install_dir/bin:$PATH"
  export PROTOC="$install_dir/bin/protoc"
}
dnf_install_protoc() {
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y protobuf-compiler
  elif command -v yum >/dev/null 2>&1; then
    yum install -y protobuf-compiler
  else
    return 1
  fi
}
ensure_protoc() {
  local existing
  existing="$(command -v protoc || true)"
  if [ -n "$existing" ] && protoc_has_wkt "$existing"; then
    export PROTOC="$existing"
    return 0
  fi
  if install_protoc_from_release; then
    return 0
  fi
  echo "ci_build_wheel: downloading official protoc ${PROTOC_VERSION} failed; falling back to dnf/yum protobuf-compiler." >&2
  if dnf_install_protoc; then
    existing="$(command -v protoc || true)"
    if [ -n "$existing" ] && protoc_has_wkt "$existing"; then
      export PROTOC="$existing"
      return 0
    fi
  fi
  echo "ci_build_wheel: ERROR: no protoc with Well-Known Types includes (google/protobuf/*.proto) available." >&2
  echo "ci_build_wheel: the official protoc ${PROTOC_VERSION} download failed and the dnf/yum protobuf-compiler package does not ship the WKT .proto files on the protoc search path; gsd.proto imports (timestamp, duration, any, struct, wrappers) will fail with File not found." >&2
  echo "ci_build_wheel: install the official protoc ${PROTOC_VERSION} release (its bundled include/ directory) onto PATH and retry." >&2
  return 1
}
ensure_protoc
/opt/python/cp312-cp312/bin/python -m pip install --quiet maturin==$SWARM_MATURIN_VERSION
/opt/python/cp312-cp312/bin/maturin build --release --locked --manifest-path crates/gsd-python/Cargo.toml --compatibility manylinux_2_28 --out /out'
