#!/usr/bin/env bash
# Parity gate of the read routes: the Python truth leg and the Rust legs (host and sandbox backend) of tests/parity/harness.py, the
# read-route fault tests and the read-route budget record. One command, the same locally and in CI (job `parity-rust` of ci.yml).
#
# usage: scripts/ci_parity_rust.sh OUT_DIR
# Writes OUT_DIR/parity-python.txt, parity-rust.txt, parity-rust.json, parity-rust-sandbox.txt, parity-rust-sandbox.json,
# read-route-faults.txt and read-route-budget.txt. Exit 0 only when every harness leg prints `N/N cases pass` with N at least 11
# and every cargo test passes. The budget test is a record, not a threshold.
#
# Needs cargo with the pinned toolchain (scripts/ci_rust_toolchain.sh) and python3 with venv. The harness's selector legs run the
# Python body without SWARM_PURE_PYTHON, so `import swarm._native` must work: when it does not, this script builds the extension into
# a venv with `maturin develop` (network access to PyPI for the pinned maturin), which leaves a gitignored swarm/_native*.so in the tree.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: scripts/ci_parity_rust.sh OUT_DIR" >&2
  exit 2
fi
mkdir -p "$1"
out="$(cd "$1" && pwd)"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

MIN_CASES=11
MATURIN_VERSION=1.15.0

python=python3
if ! python3 -c 'import swarm._native' >/dev/null 2>&1; then
  echo "== build the swarm._native extension (not importable) =="
  venv="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/parity-venv.XXXXXX")"
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip install --quiet "maturin==$MATURIN_VERSION"
  VIRTUAL_ENV="$venv" "$venv/bin/maturin" develop --locked
  python="$venv/bin/python"
fi

echo "== build the gsd_engine binary =="
cargo build -p gsd-adapter --bin gsd_engine --locked
target_dir="$(cargo metadata --format-version 1 --no-deps --locked | python3 -c 'import json, sys; print(json.load(sys.stdin)["target_directory"])')"
export GSD_ENGINE_BIN="$target_dir/debug/gsd_engine"

# A harness that exits 0 but prints no case line (or fewer cases than MIN_CASES, the eleven read cases that were the whole harness
# before its twelfth, transition_matrix) must not pass.
require_cases() {
  local file="$1" counts
  counts="$(grep -Eo '^[0-9]+/[0-9]+ cases pass$' "$file" | tail -n 1 | cut -d' ' -f1 || true)"
  if [[ -z "$counts" || "${counts%/*}" != "${counts#*/}" || "${counts#*/}" -lt "$MIN_CASES" ]]; then
    echo "ci_parity_rust: $file does not end in 'N/N cases pass' with N >= $MIN_CASES (found: ${counts:-nothing})" >&2
    exit 1
  fi
  echo "ci_parity_rust: $file: $counts cases pass"
}

echo "== harness: Python truth leg =="
"$python" tests/parity/harness.py | tee "$out/parity-python.txt"
require_cases "$out/parity-python.txt"

echo "== harness: Rust leg, host backend =="
"$python" tests/parity/harness.py --rust --json-out "$out/parity-rust.json" | tee "$out/parity-rust.txt"
require_cases "$out/parity-rust.txt"

echo "== harness: Rust leg, sandbox backend =="
"$python" tests/parity/harness.py --rust --backend sandbox --json-out "$out/parity-rust-sandbox.json" | tee "$out/parity-rust-sandbox.txt"
require_cases "$out/parity-rust-sandbox.txt"

echo "== read-route fault tests =="
cargo test -p gsd-adapter --test read_route_faults --locked | tee "$out/read-route-faults.txt"

echo "== read-route budget record =="
cargo test -p gsd-sandbox --test read_route_budget --locked -- --nocapture | tee "$out/read-route-budget.txt"

echo "ci_parity_rust: all legs passed; records in $out"
