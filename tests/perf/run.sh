#!/usr/bin/env bash
# A2 hot-path sampler. Times three Python hot paths in the product configuration (the
# native extension must be importable from this checkout). Every sample runs against a fresh
# SWARM_DIR; the state each sample needs is built before the clock starts, and any sample that
# exits non-zero stops the run, so a failing path can never be timed.
set -euo pipefail

SAMPLES="${A2_SAMPLES:-50}"
WARMUP="${A2_WARMUP:-3}"
# Engine legs (Phase 14.3 R8), off unless A2_ENGINE_LEGS=1: path (c) again, once per engine leg and store (see below).
ENGINE_LEGS="${A2_ENGINE_LEGS:-0}"
# Scheduler paired legs (Phase 14.4, S5.2 and S6), off unless A2_SCHEDULER_LEGS=1: the Python and Rust scheduler, paired and alternating,
# on the path (a) fixture, a 12-task fake-omp run and one-round passes over 50 and 500 ready tasks (tests/perf/scheduler_engines.py
# --scheduler-paired). A2_SCHEDULER_JSON names the raw record and is required; the record is written even when a sample refuses the run.
# A2_SAMPLES and A2_WARMUP are forwarded and A2_WATCHER_LOG is recorded. Scheduler-only: the path timings and the engine legs do not run in
# this mode. Judge the record with: python3 tests/perf/scheduler_engines.py --qualify-json "$A2_SCHEDULER_JSON"
SCHEDULER_LEGS="${A2_SCHEDULER_LEGS:-0}"
QUIET_THRESH="1.5"  # the A2 quiet rule: 1-minute load below this (STATE.md P-A2); not tunable here
PROTOCOL_SAMPLES=50  # the A2 protocol's samples per path
PROTOCOL_WARMUP=3

# A2_SCHEDULER_JSON is meant from the caller's directory: resolve it here, before the cd below (review m3).
if [[ -n "${A2_SCHEDULER_JSON:-}" ]]; then
  A2_SCHEDULER_JSON="$(realpath -m -- "$A2_SCHEDULER_JSON")"
fi
# A2_WATCHER_LOG is read after the cd below: resolve it here too, so a relative path is taken from the caller's directory.
if [[ -n "${A2_WATCHER_LOG:-}" ]]; then
  A2_WATCHER_LOG="$(realpath -m -- "$A2_WATCHER_LOG")"
fi

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

# ---- engine legs (A2_ENGINE_LEGS=1) ---------------------------------------------------------------------------
# Path (c) `orch_status --json` on three engines, each against three stores that tests/perf/make_stores.py builds:
#   engines: GSD_ENGINE=python; GSD_ENGINE=rust with GSD_RUST_BACKEND=host; GSD_ENGINE=rust with GSD_RUST_BACKEND=sandbox
#   stores:  empty; snapshot500 (the 500-task shape of crates/gsd-sandbox/tests/read_route_budget.rs `snapshot()`);
#            signed (signed verdict rows, read through `--history`: the Rust engine refuses signed rows with E-DEP and the
#            selector falls back to Python, so a rust leg there times the refusal plus the Python answer: the D2 cost)
# Needs GSD_ENGINE_BIN (the gsd_engine binary; use a release build for a baseline). Every sample gets its own copy of the
# store (outside the clock) because a run appends to events.jsonl in its SWARM_DIR. A2_STORES_DIR names the directory the
# stores are built in (it must not hold them yet and is kept); by default a temporary directory that is removed on exit.
# A2_WATCHER_LOG names the watch_quiet.sh log; without a quiet A2 result in it the run is not evidence.
LOAD_LABELS=()
LOAD_VALUES=()
LEG_ROWS=()
ENGINE_BIN=""
LEG_STORE=""
WORK=""

# record_load <label>: remember the 1-minute load average under a label; note_load also prints the whole line.
record_load() {
  local line
  line="$(cat /proc/loadavg)"
  LOAD_LABELS+=("$1")
  LOAD_VALUES+=("${line%% *}")
}

note_load() {
  record_load "$1"
  echo "/proc/loadavg ($1): $(cat /proc/loadavg)"
}

# copy_store <dir>: time_path's setup for an engine leg: a fresh copy of the leg's store.
copy_store() {
  cp -a "$LEG_STORE/." "$1/"
}

# probe_served <engine> <backend> <store dir> <argv...>: who answers the leg's call, from one untimed call. The selector's
# own rule is applied to the binary's reply: a fallback code (or an unusable reply) means Python answered after the binary
# refused (`fallback:<code>`), a sandbox failover notice on stderr means host Rust answered after a sandbox fault, and
# anything else is `rust`. The python engine is always `python`.
probe_served() {
  local engine="$1" backend="$2" store="$3" d served
  shift 3
  if [[ "$engine" == "python" ]]; then
    echo "python"
    return 0
  fi
  d="$(mktemp -d)"
  cp -a "$store/." "$d/"
  served="$(GSD_RUST_BACKEND="$backend" python3 - "$ENGINE_BIN" "$d" "$@" <<'PY'
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, "scripts")
import orch_status  # noqa: E402  (RUST_FALLBACK_CODES: the selector's own fallback set, not a copy)

binary, swarm_dir, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
request = {"agent": "A01", "script": "orch_status", "argv": argv, "root": os.getcwd(), "env": {"SWARM_DIR": swarm_dir}}
done = subprocess.run([binary], input=json.dumps(request).encode(), capture_output=True, check=False, timeout=600)
try:
    reply = json.loads(done.stdout.decode("utf-8"))
except ValueError:
    reply = None
if not isinstance(reply, dict) or done.returncode not in (0, 1, 2):
    print("fallback:unusable-reply")
    sys.exit(0)
error = reply.get("error")
code = error.get("code") if isinstance(error, dict) else None
if done.returncode == 2 and (not isinstance(code, str) or code in orch_status.RUST_FALLBACK_CODES):
    print(f"fallback:{code or 'untyped'}")
    sys.exit(0)
# the notice the binary prints when the sandbox backend faults and host Rust answers (tests/parity/harness.py FAILOVER_NOTICE)
fault = re.search(rb"^gsd_engine: sandbox fault (E-[A-Z]+); answered by host Rust$", done.stderr, re.MULTILINE)
print(f"fallback:sandbox:{fault.group(1).decode('ascii')}" if fault else "rust")
PY
)"
  rm -rf "$d"
  echo "$served"
}

# engine_preflight: GSD_ENGINE_BIN is required, never skipped; builds the stores. Sets ENGINE_BIN, WORK, STORES_DIR, STORE_ROWS.
engine_preflight() {
  ENGINE_BIN="${GSD_ENGINE_BIN:-}"
  if [[ -z "$ENGINE_BIN" ]]; then
    echo "error: A2_ENGINE_LEGS=1 needs GSD_ENGINE_BIN set to the gsd_engine binary (the rust legs cannot be skipped)" >&2
    exit 1
  fi
  if [[ ! -f "$ENGINE_BIN" || ! -x "$ENGINE_BIN" ]]; then
    echo "error: GSD_ENGINE_BIN=$ENGINE_BIN is not an executable file" >&2
    exit 1
  fi
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT
  STORES_DIR="${A2_STORES_DIR:-$WORK/stores}"
  # no tracked file is written while sampling: the .claude projection goes to the work dir, the STATE.md mirror is off
  export SWARM_CLAUDE_STATE_DIR="${SWARM_CLAUDE_STATE_DIR:-$WORK/claude}"
  export SWARM_GSD_STATE_FILE="${SWARM_GSD_STATE_FILE-}"
  # a throwaway signing key for the signed store: made now, held in this shell only, never written or printed
  A2_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  STORES_JSON="$(SWARM_SIGNING_KEY="$A2_KEY" python3 tests/perf/make_stores.py --out "$STORES_DIR" --key-from-env)"
  mapfile -t STORE_ROWS < <(python3 -c '
import json
import sys

for name, s in json.load(sys.stdin)["stores"].items():
    print("\t".join([name, s["swarm_dir"], str(s["tasks"]), str(s["verdicts"]), str(s["signed_verdicts"]), " ".join(s["argv"])]))
' <<<"$STORES_JSON")
}

print_engine_header() {
  echo
  echo "== A2 engine legs (Phase 14.3 R8) =="
  echo "NOT EVIDENCE unless tests/perf/watch_quiet.sh reported a quiet host (1-min load below ${QUIET_THRESH} for 20 consecutive"
  echo "checks 30 s apart) AND this run's own load readings stay below ${QUIET_THRESH}. The run status block at the end says which;"
  echo "a run that fails either test is marked contended."
  echo "GSD_ENGINE_BIN: $ENGINE_BIN ($(stat -c '%s bytes, modified %y' "$ENGINE_BIN"))"
  echo "stores: $STORES_DIR (signed rows are signed with a throwaway key made for this run, never written or printed)"
  printf "store\ttasks\tverdicts\tsigned_verdicts\tworkload\n"
  local row name dir tasks verdicts signed workload
  for row in "${STORE_ROWS[@]}"; do
    IFS=$'\t' read -r name dir tasks verdicts signed workload <<<"$row"
    printf "%s\t%s\t%s\t%s\torch_status %s\n" "$name" "$tasks" "$verdicts" "$signed" "$workload"
  done
}

# run_engine_legs: time every engine against every store with the sampler above (A2_WARMUP and A2_SAMPLES).
run_engine_legs() {
  # the key reaches the timed Python answers through the environment only, so their signature check can pass; any other
  # signing variable would take precedence over it
  export SWARM_SIGNING_KEY="$A2_KEY"
  unset SWARM_ED25519_KEY SWARM_ED25519_KEY_FILE SWARM_REQUIRE_KEY SWARM_ALLOW_INSECURE_DEV_KEY SWARM_AGENT_SESSION
  echo
  echo "== engine legs: path (c) orch_status (ms), a fresh copy of the store per sample =="
  echo "D2: the Rust engine refuses signed verdict rows (E-DEP). On the signed store a rust leg is therefore answered by"
  echo "Python after the refusal, and its time is the Rust start-up and refusal plus the Python answer (the D2 fallback cost)."
  printf "leg\tengine\tbackend\tstore\tserved_by\tsamples\tp50_ms\tp95_ms\n"
  local spec engine backend row name dir workload served label n p50 p95 leg=0
  local argv=() cmd=()
  for spec in "python:-" "rust:host" "rust:sandbox"; do
    engine="${spec%%:*}"
    backend="${spec#*:}"
    for row in "${STORE_ROWS[@]}"; do
      IFS=$'\t' read -r name dir _ _ _ workload <<<"$row"
      read -r -a argv <<<"$workload"
      leg=$((leg + 1))
      note_load "before leg $leg"
      cmd=(env -u GSD_RUST_BACKEND -u GSD_ENGINE_BIN -u SWARM_PURE_PYTHON "GSD_ENGINE=$engine")
      if [[ "$engine" == "rust" ]]; then
        cmd+=("GSD_ENGINE_BIN=$ENGINE_BIN" "GSD_RUST_BACKEND=$backend")
      fi
      cmd+=(python3 scripts/orch_status.py "${argv[@]}")
      LEG_STORE="$dir"
      served="$(probe_served "$engine" "$backend" "$dir" "${argv[@]}")"
      label="c:orch_status ${engine}/${backend} ${name}"
      row="$(time_path "$label" copy_store "${cmd[@]}")"
      IFS=$'\t' read -r _ n p50 p95 <<<"$row"
      printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" "$leg" "$engine" "$backend" "$name" "$served" "$n" "$p50" "$p95"
      LEG_ROWS+=("$(printf "%s\t%s\t%s\t%s\t%s\t%s" "$engine" "$backend" "$name" "$served" "$p50" "$p95")")
    done
  done
  note_load "after legs"
  echo
  echo "== D2 fallback cost: signed store, orch_status --history (rust leg minus python leg) =="
  printf '%s\n' "${LEG_ROWS[@]}" | python3 -c '
import sys

rows = [line.rstrip("\n").split("\t") for line in sys.stdin]
python = next(r for r in rows if r[0] == "python" and r[2] == "signed")
for engine, backend, store, served, p50, p95 in rows:
    if engine != "rust" or store != "signed":
        continue
    kind = "fallback to Python" if served.startswith("fallback:") else "answered by the binary, no fallback"
    print(f"{backend}\t{served}\t({kind})\tp50 {float(p50) - float(python[4]):+.3f} ms ({(float(p50) / float(python[4]) - 1) * 100:+.1f}%)"
          f"\tp95 {float(p95) - float(python[5]):+.3f} ms ({(float(p95) / float(python[5]) - 1) * 100:+.1f}%)")
'
}

# print_run_status: contended or quiet, and whether the run can count as evidence.
print_run_status() {
  local i hot=0 reasons=() last=""
  echo
  echo "== run status =="
  for i in "${!LOAD_VALUES[@]}"; do
    echo "load1 (${LOAD_LABELS[$i]}): ${LOAD_VALUES[$i]}"
    if awk -v l="${LOAD_VALUES[$i]}" -v t="$QUIET_THRESH" 'BEGIN { exit !(l >= t) }'; then
      hot=$((hot + 1))
    fi
  done
  echo "readings at or above ${QUIET_THRESH}: ${hot} of ${#LOAD_VALUES[@]}"
  if [[ -n "${A2_WATCHER_LOG:-}" && -f "$A2_WATCHER_LOG" ]]; then
    last="$(awk 'END { print }' "$A2_WATCHER_LOG")"
  fi
  echo "watcher (A2_WATCHER_LOG=${A2_WATCHER_LOG:-unset}) last line: ${last:-none}"
  if [[ "$hot" -gt 0 ]]; then
    reasons+=("this run's load readings reached ${QUIET_THRESH} or more")
  fi
  if [[ "$last" != "result: quiet "*"rule: A2)" ]]; then
    reasons+=("no quiet A2 result from watch_quiet.sh (THRESH 1.5, NEED 20, 30 s checks)")
  fi
  if [[ "$SAMPLES" -lt "$PROTOCOL_SAMPLES" ]]; then
    reasons+=("${SAMPLES} samples per path, the A2 protocol takes ${PROTOCOL_SAMPLES}")
  fi
  if [[ "$WARMUP" -lt "$PROTOCOL_WARMUP" ]]; then
    reasons+=("${WARMUP} warm-up runs, the A2 protocol takes ${PROTOCOL_WARMUP}")
  fi
  if [[ "$ENGINE_BIN" == */debug/* ]]; then
    reasons+=("GSD_ENGINE_BIN is a debug build")
  fi
  if [[ "$hot" -gt 0 || "$last" != "result: quiet "*"rule: A2)" ]]; then
    echo "run: contended"
  else
    echo "run: quiet"
  fi
  if [[ ${#reasons[@]} -gt 0 ]]; then
    echo "evidence: NOT EVIDENCE: $(printf '%s; ' "${reasons[@]}")"
  else
    echo "evidence: candidate for the quiet-host baseline (the 10-second load readings during the run are the orchestrator's to add)"
  fi
}

if [[ "$ENGINE_LEGS" != "0" && "$ENGINE_LEGS" != "1" ]]; then
  echo "error: A2_ENGINE_LEGS must be 0 or 1, got '$ENGINE_LEGS'" >&2
  exit 1
fi
if [[ "$SCHEDULER_LEGS" != "0" && "$SCHEDULER_LEGS" != "1" ]]; then
  echo "error: A2_SCHEDULER_LEGS must be 0 or 1, got '$SCHEDULER_LEGS'" >&2
  exit 1
fi
if [[ "$SCHEDULER_LEGS" == "1" ]]; then
  if [[ -z "${A2_SCHEDULER_JSON:-}" ]]; then
    echo "error: A2_SCHEDULER_LEGS=1 needs A2_SCHEDULER_JSON=<path> for the raw paired record" >&2
    exit 1
  fi
  if [[ "$ENGINE_LEGS" == "1" ]]; then
    echo "error: A2_SCHEDULER_LEGS and A2_ENGINE_LEGS are separate modes; set one of them" >&2
    exit 1
  fi
  watcher_args=()
  if [[ -n "${A2_WATCHER_LOG:-}" ]]; then
    watcher_args=(--watcher-log "$A2_WATCHER_LOG")
  fi
  echo "== scheduler paired legs (A2_SCHEDULER_LEGS=1): python against rust, ${SAMPLES} samples and ${WARMUP} warm-up pairs per fixture =="
  rc=0
  python3 tests/perf/scheduler_engines.py --scheduler-paired --json-out "$A2_SCHEDULER_JSON" \
    --samples "$SAMPLES" --warmup "$WARMUP" "${watcher_args[@]+"${watcher_args[@]}"}" || rc=$?
  exit "$rc"
fi
if [[ "$ENGINE_LEGS" == "1" ]]; then
  engine_preflight
fi

print_fingerprint
if [[ "$ENGINE_LEGS" == "1" ]]; then
  record_load "start"
  print_engine_header
fi
echo
echo "== timings (ms) =="
printf "path\tsamples\tp50_ms\tp95_ms\n"

time_path "a:swarm_run --dry-run --json" plan_fixture \
  python3 scripts/swarm_run.py --dry-run --json

time_path "b:qa_gate --dry-run --json" "" \
  python3 scripts/qa_gate.py --dry-run --json

time_path "c:orch_status --json (bridge round-trip proxy)" "" \
  python3 scripts/orch_status.py --json

if [[ "$ENGINE_LEGS" == "1" ]]; then
  run_engine_legs
fi

echo
echo "/proc/loadavg (end): $(cat /proc/loadavg)"

if [[ "$ENGINE_LEGS" == "1" ]]; then
  record_load "end"
  print_run_status
fi
