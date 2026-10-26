#!/usr/bin/env bash
# A2 hot-path sampler. Times three Python hot paths in the product configuration (the
# native extension must be importable from this checkout). Every sample runs against a fresh
# SWARM_DIR; the state each sample needs is built before the clock starts, and any sample that
# exits non-zero stops the run, so a failing path can never be timed.
set -euo pipefail

SAMPLES="${A2_SAMPLES:-50}"
WARMUP="${A2_WARMUP:-3}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if ! python3 -c "import swarm._native" >/dev/null 2>&1; then
  echo "error: swarm._native is not importable from $REPO_ROOT; build the in-tree extension first" >&2
  echo "       (VIRTUAL_ENV=<venv> maturin develop --release)" >&2
  exit 1
fi

print_fingerprint() {
  echo "== hardware fingerprint =="
  echo "uname -a: $(uname -a)"
  echo "nproc: $(nproc)"
  echo "/proc/loadavg (start): $(cat /proc/loadavg)"
  echo "python3 --version: $(python3 --version 2>&1)"
  if command -v bun >/dev/null 2>&1; then
    echo "bun --version: $(bun --version 2>&1)"
  fi
  echo "configuration: product (swarm._native importable)"
  echo "samples per path: ${SAMPLES} (warm-up runs: ${WARMUP})"
}

# plan_fixture <dir>: the state swarm_run needs. Built outside the timed window.
plan_fixture() {
  SWARM_DIR="$1" python3 scripts/orch_plan.py --brief-text "a2 baseline fixture" \
    --pattern feature --risk-class medium --json >/dev/null 2>&1
}

# time_path <label> <setup function or ""> <command...>: warm up, then time SAMPLES runs and
# print p50/p95 in ms. Each run gets its own SWARM_DIR; a non-zero exit aborts the whole run.
time_path() {
  local label="$1" setup="$2"
  shift 2
  local i d start end rc
  local samples=()

  for ((i = 0; i < WARMUP + SAMPLES; i++)); do
    d="$(mktemp -d)"
    if [[ -n "$setup" ]] && ! "$setup" "$d"; then
      rm -rf "$d"
      echo "error: setup failed for $label" >&2
      exit 1
    fi
    start=$(date +%s%N)
    rc=0
    SWARM_DIR="$d" "$@" >/dev/null 2>&1 || rc=$?
    end=$(date +%s%N)
    rm -rf "$d"
    if [[ $rc -ne 0 ]]; then
      echo "error: $label exited $rc; refusing to time a failing path" >&2
      exit 1
    fi
    if ((i >= WARMUP)); then
      samples+=("$((end - start))")
    fi
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

time_path "a:swarm_run --dry-run --json" plan_fixture \
  python3 scripts/swarm_run.py --dry-run --json

time_path "b:qa_gate --dry-run --json" "" \
  python3 scripts/qa_gate.py --dry-run --json

time_path "c:orch_status --json (bridge round-trip proxy)" "" \
  python3 scripts/orch_status.py --json

echo
echo "/proc/loadavg (end): $(cat /proc/loadavg)"
