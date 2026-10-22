#!/usr/bin/env bash
set -euo pipefail

SAMPLES=50
WARMUP=3

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

SWARM_DIR="$(mktemp -d)"
export SWARM_DIR
cleanup() {
  rm -rf "$SWARM_DIR"
}
trap cleanup EXIT

print_fingerprint() {
  echo "== hardware fingerprint =="
  echo "uname -a: $(uname -a)"
  echo "nproc: $(nproc)"
  echo "/proc/loadavg (start): $(cat /proc/loadavg)"
  echo "python3 --version: $(python3 --version 2>&1)"
  if command -v bun >/dev/null 2>&1; then
    echo "bun --version: $(bun --version 2>&1)"
  fi
  echo "samples per path: ${SAMPLES} (warm-up runs: ${WARMUP})"
}

# time_path <label> <command...>: warm up, then time SAMPLES runs and print p50/p95 in ms.
time_path() {
  local label="$1"
  shift
  local i start end
  local samples=()

  for ((i = 0; i < WARMUP; i++)); do
    "$@" >/dev/null 2>&1
  done

  for ((i = 0; i < SAMPLES; i++)); do
    start=$(date +%s%N)
    "$@" >/dev/null 2>&1
    end=$(date +%s%N)
    samples+=("$((end - start))")
  done

  python3 - "$label" "${samples[@]}" <<'PY'
import math
import sys

label = sys.argv[1]
ns = sorted(int(v) for v in sys.argv[2:])
ms = [v / 1e6 for v in ns]


def pct(p):
    # nearest-rank percentile over the sorted samples
    idx = max(0, math.ceil(p / 100 * len(ms)) - 1)
    return ms[idx]


print(f"{label}\t{len(ms)}\t{pct(50):.3f}\t{pct(95):.3f}")
PY
}

print_fingerprint
echo
echo "== timings (ms) =="
printf "path\tsamples\tp50_ms\tp95_ms\n"

time_path "a:swarm_run --dry-run --json" \
  python3 scripts/swarm_run.py --dry-run --json

time_path "b:qa_gate --dry-run --json" \
  python3 scripts/qa_gate.py --dry-run --json

time_path "c:orch_status --json (bridge round-trip proxy)" \
  python3 scripts/orch_status.py --json

echo
echo "/proc/loadavg (end): $(cat /proc/loadavg)"
