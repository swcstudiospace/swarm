#!/usr/bin/env bash
# Runs every parity leg against one gsd_engine binary and retains the outputs with the facts needed to judge them later.
#
# usage: tests/parity/record_parity.sh BINARY OUT_DIR
# Legs (each prints N/N cases pass; the script fails when any leg does not end in that line with N at least 11):
#   python           the Python truth leg (GSD_ENGINE=python, no binary)
#   rust-host        the Rust legs with GSD_RUST_BACKEND=host (direct binary, explicit selector, default selector)
#   rust-sandbox     the same with GSD_RUST_BACKEND=sandbox
#   python-wal, rust-host-wal   the same two with SWARM_TASKSTORE_WAL=1 (a WAL store must read the same)
# Output: OUT_DIR/{facts.txt,<leg>.txt,<leg>.json}. facts.txt holds the binary's sha256 and size, git HEAD and whether the tree is
# dirty, the committed guest digest, python3 and host facts, and /proc/loadavg at the start and the end of every leg. Parity does not
# depend on load, so the load readings are context, not a condition.
# Needs `import swarm._native` (the selector legs run the Python body without SWARM_PURE_PYTHON): build it with
# `maturin develop` first, the way tests/perf/run.sh asks.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: tests/parity/record_parity.sh BINARY OUT_DIR" >&2
  exit 2
fi
binary="$(readlink -f "$1")"
if [[ ! -x "$binary" ]]; then
  echo "record_parity: $1 is not an executable file" >&2
  exit 2
fi
mkdir -p "$2"
out="$(cd "$2" && pwd)"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if ! python3 -c 'import swarm._native' >/dev/null 2>&1; then
  echo "record_parity: swarm._native is not importable from $PWD; build the in-tree extension first (maturin develop)" >&2
  exit 1
fi

{
  echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "binary: $binary"
  echo "binary_sha256: $(sha256sum "$binary" | cut -d' ' -f1)"
  echo "binary_bytes: $(stat -c %s "$binary")"
  echo "git_head: $(git rev-parse HEAD)"
  echo "git_dirty_paths: $(git status --porcelain | wc -l)"
  echo "binary_built_from: ${GSD_BINARY_BUILT_FROM:-unrecorded; set GSD_BINARY_BUILT_FROM to the commit the binary was built from when it is not the HEAD of this tree}"
  echo "guest_digest: $(tr -d '[:space:]' < crates/gsd-sandbox/guest-module.sha256)"
  echo "python: $(python3 --version 2>&1)"
  echo "host: $(uname -srm); $(nproc) CPUs"
} > "$out/facts.txt"

summary=""
run_leg() {
  local name="$1"
  shift
  echo "$name load_start: $(cut -d' ' -f1-3 /proc/loadavg)" >> "$out/facts.txt"
  # The harness sets what each leg needs; only the binary and the leg's own variables are passed from here.
  GSD_ENGINE_BIN="$binary" "$@" --json-out "$out/$name.json" > "$out/$name.txt" 2>&1 || true
  echo "$name load_end: $(cut -d' ' -f1-3 /proc/loadavg)" >> "$out/facts.txt"
  local line
  line="$(grep -Eo '^[0-9]+/[0-9]+ cases pass$' "$out/$name.txt" | tail -n 1 || true)"
  local passed="${line%%/*}" total="${line#*/}"
  total="${total%% *}"
  if [[ -z "$line" || "$passed" != "$total" || "$total" -lt 11 ]]; then
    echo "record_parity: leg $name did not end in 'N/N cases pass' with N >= 11 (found: ${line:-nothing}); see $out/$name.txt" >&2
    exit 1
  fi
  summary+="$name: $line"$'\n'
  echo "record_parity: $name: $line"
}

run_leg python python3 tests/parity/harness.py
run_leg rust-host python3 tests/parity/harness.py --rust --backend host
run_leg rust-sandbox python3 tests/parity/harness.py --rust --backend sandbox
SWARM_TASKSTORE_WAL=1 run_leg python-wal python3 tests/parity/harness.py
SWARM_TASKSTORE_WAL=1 run_leg rust-host-wal python3 tests/parity/harness.py --rust --backend host

printf '%s' "$summary" >> "$out/facts.txt"
echo "record_parity: every leg passed; records in $out"
