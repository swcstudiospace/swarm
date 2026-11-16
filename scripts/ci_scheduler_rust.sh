#!/usr/bin/env bash
# Scheduler gate of Phase 14.4 (unit 14): the CI producer of job `scheduler` in ci.yml. Fail-closed throughout: every pytest leg
# writes a JUnit report that must hold tests, no failure, error or skip, and the named cases of that leg; the continuous suite must
# be byte-identical before and after the run; every cargo and mypy step must pass.
#
# usage: scripts/ci_scheduler_rust.sh OUT_DIR
# Required environment:
#   GSD_ENGINE_BIN              absolute path of an executable gsd_engine binary (the Rust leg and every route child run it)
#   SCHEDULER_CONTINUOUS_SHA256 lowercase sha256 (64 hex digits) of tests/test_scheduler_continuous.py, supplied by the parent
# The script exports GSD_ENGINE_BIN_SHA256, the sha256 of GSD_ENGINE_BIN, for the route children (swarm/engine_select.py).
# It builds and installs nothing: swarm._native must already import with a compiled run_scheduler, and cargo, mypy, pytest and
# PyYAML must already be on PATH.
#
# OUT_DIR is created; it must not exist non-empty. It receives junit-*.xml, logs/, steps.tsv, summary.json and identity.json.
# Exit 0 only when every step passes. Exit 2 when the usage or an input is refused (nothing runs). Otherwise every step runs,
# exit 1 is returned, and each failing step is named on stderr.
set -euo pipefail

usage() {
  echo "usage: scripts/ci_scheduler_rust.sh OUT_DIR" >&2
}

refuse() {
  echo "ci_scheduler_rust: $*" >&2
  exit 2
}

if [[ $# -ne 1 || -z "$1" ]]; then
  usage
  exit 2
fi
case "$1" in
  /*) out_arg="$1" ;;
  *) out_arg="$PWD/$1" ;;
esac
if [[ -e "$out_arg" ]]; then
  [[ -d "$out_arg" ]] || refuse "OUT_DIR $1 exists and is not a directory"
  if [[ -n "$(find "$out_arg" -mindepth 1 -print -quit)" ]]; then
    refuse "OUT_DIR $1 exists and is not empty; choose a new directory"
  fi
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."

sha256_of() {
  sha256sum -- "$1" | cut -d' ' -f1
}

engine_bin="${GSD_ENGINE_BIN:-}"
[[ -n "$engine_bin" ]] || refuse "GSD_ENGINE_BIN is required (the absolute path of an executable gsd_engine binary)"
[[ "$engine_bin" == /* ]] || refuse "GSD_ENGINE_BIN must be an absolute path, got: $engine_bin"
[[ -f "$engine_bin" && -x "$engine_bin" ]] || refuse "GSD_ENGINE_BIN is not an executable file: $engine_bin"
engine_sha="$(sha256_of "$engine_bin")"
export GSD_ENGINE_BIN="$engine_bin"
export GSD_ENGINE_BIN_SHA256="$engine_sha"

suite="tests/test_scheduler_continuous.py"
expected_suite="${SCHEDULER_CONTINUOUS_SHA256:-}"
[[ "$expected_suite" =~ ^[0-9a-f]{64}$ ]] || refuse "SCHEDULER_CONTINUOUS_SHA256 must be the lowercase sha256 (64 hex digits) of $suite"
[[ -f "$suite" ]] || refuse "$suite is missing"
suite_before="$(sha256_of "$suite")"
[[ "$suite_before" == "$expected_suite" ]] || refuse "$suite has sha256 $suite_before before the run, expected $expected_suite"

# The compiled entry must be present: run_scheduler is a builtin of the extension, not a Python stand-in.
read -r -d '' native_probe <<'PY' || true
import inspect
import swarm._native as native
if not inspect.isbuiltin(native.run_scheduler):
    raise SystemExit("run_scheduler is not the compiled entry")
print(native.__file__)
PY
native_path="$(python3 -c "$native_probe")" \
  || refuse "swarm._native does not import with a compiled run_scheduler; build it before this gate (this script builds nothing)"
[[ -f "$native_path" ]] || refuse "swarm._native reports no file: $native_path"
native_sha="$(sha256_of "$native_path")"

git_head="$(git rev-parse HEAD)" || refuse "git HEAD is unavailable"
# The working tree the legs ran on, as a tree id, without touching the real index or refs: a throwaway index in a temporary directory
# is filled from HEAD and the working tree. A dirty tree is recorded, so the record names what was tested, not only the commit.
index_dir="$(mktemp -d)"
git_tree="$(GIT_INDEX_FILE="$index_dir/index" bash -c 'git read-tree HEAD && git add -A && git write-tree')" \
  || { rm -rf "$index_dir"; refuse "the working-tree id is unavailable"; }
rm -rf "$index_dir"
git_dirty="$(git status --porcelain | wc -l)"
python_version="$(python3 -V 2>&1)"
rust_version="$(rustc -V 2>/dev/null || echo unavailable)"

mkdir -p "$out_arg/logs"
out="$(cd "$out_arg" && pwd)"

steps=0
failures=0
failed_names=()
suite_after=""

# witness_check ENGINE FILE: every child run of this leg ran on ENGINE, and at least one ran (T-14.4-08). swarm_run appends the
# engine of each child to FILE through SWARM_SCHEDULER_WITNESS, which the leg's pytest command sets.
witness_check() {
  python3 - "$1" "$2" <<'PY'
import sys
engine, path = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as handle:
        seen = [line.strip() for line in handle if line.strip()]
except OSError as exc:
    sys.exit(f"witness: cannot read {path}: {exc}")
if not seen:
    sys.exit(f"witness: no child run wrote its scheduler engine to {path}; the {engine} leg is unwitnessed")
wrong = sorted(set(seen) - {engine})
if wrong:
    sys.exit(f"witness: the {engine} leg ran children on {', '.join(wrong)}")
print(f"witness: {len(seen)} child runs, all on {engine}")
PY
}

# step NAME COMMAND...: runs the command, logs it, and records its status. Later steps still run, so every failure is reported.
step() {
  local name="$1" slug rc=0
  shift
  steps=$((steps + 1))
  slug="${name//[^A-Za-z0-9]/-}"
  echo "== $name =="
  "$@" 2>&1 | tee "$out/logs/$slug.log" || rc=$?
  printf '%s\t%s\n' "$name" "$rc" >> "$out/steps.tsv"
  if [[ $rc -eq 0 ]]; then
    echo "ci_scheduler_rust: $name: PASS"
  else
    echo "ci_scheduler_rust: $name: FAIL (exit $rc)"
    failures=$((failures + 1))
    failed_names+=("$name")
  fi
}

compare_digest() {
  local before="$1" after="$2"
  echo "continuous suite sha256 before the run: $before"
  echo "continuous suite sha256 after the runs:  $after"
  if [[ -z "$after" || "$before" != "$after" ]]; then
    echo "ci_scheduler_rust: $suite changed during the run" >&2
    return 1
  fi
}

# junit_check REPORT NODEID...: the report must exist, parse, hold tests, and have no failure, error or skip; each NODEID
# (path::name, as pytest prints it) must be one of its testcases. A testcase's classname is its module path, dotted.
read -r -d '' junit_py <<'PY' || true
import sys
import xml.etree.ElementTree as ET

report, required = sys.argv[1], sys.argv[2:]


def fail(message):
    print(f"ci_scheduler_rust: {report}: {message}", file=sys.stderr)
    sys.exit(1)


try:
    root = ET.parse(report).getroot()
except FileNotFoundError:
    fail("JUnit report is missing")
except (OSError, ET.ParseError) as exc:
    fail(f"JUnit report is malformed: {exc}")
if root.tag not in ("testsuite", "testsuites"):
    fail(f"JUnit root is <{root.tag}>, not a testsuite")
suites = list(root.iter("testsuite"))
cases = list(root.iter("testcase"))
if not suites:
    fail("JUnit report has no testsuite element")


def count(element, attribute):
    try:
        return int(element.get(attribute, "0"))
    except ValueError:
        fail(f"<testsuite> attribute {attribute}={element.get(attribute)!r} is not an integer")


tests = sum(count(suite, "tests") for suite in suites)
if tests <= 0 or not cases:
    fail("report holds zero tests")
if tests != len(cases):
    fail(f"report claims {tests} tests but holds {len(cases)} testcase elements")
unclean = []
for suite in suites:
    for attribute in ("failures", "errors", "skipped"):
        if count(suite, attribute):
            unclean.append(f"{attribute}={count(suite, attribute)}")
for case in cases:
    for kind in ("failure", "error", "skipped"):
        if case.find(kind) is not None:
            unclean.append(f"{kind} in {case.get('classname')}::{case.get('name')}")
if unclean:
    fail("not clean: " + "; ".join(unclean))
seen = {(case.get("classname"), case.get("name")) for case in cases}
missing = []
for nodeid in required:
    module, _, name = nodeid.partition("::")
    if (module[: -len(".py")].replace("/", "."), name) not in seen:
        missing.append(nodeid)
if missing:
    fail("named cases missing: " + ", ".join(missing))
print(f"ci_scheduler_rust: {report}: {tests} tests, no failure, error or skip, {len(required)} named cases present")
PY
junit_check() {
  python3 -c "$junit_py" "$@"
}

# The named cases each leg must have run (exact pytest nodeids, parametrized ids in brackets).
TRACER=(
  "tests/parity/test_scheduler_binding.py::test_native_compiled_entry_tracer_started_and_failed"
)
THREADS=(
  "tests/parity/test_scheduler_threads.py::test_every_worker_completes_once_on_its_own_thread_and_none_is_left_behind[8]"
  "tests/parity/test_scheduler_threads.py::test_every_worker_completes_once_on_its_own_thread_and_none_is_left_behind[32]"
  "tests/parity/test_scheduler_threads.py::test_workers_that_release_the_interpreter_all_complete_with_their_results"
  "tests/parity/test_scheduler_threads.py::test_idle_final_drain_without_a_cancel_stops_closed"
)
INVENTORY=(
  "tests/typecheck/test_native_stubs.py::test_extension_exports_exactly_the_expected_names"
  "tests/typecheck/test_scheduler_ports_typecheck.py::test_the_port_adapter_exposes_exactly_the_sixteen_callbacks"
)
ENGINE_CASES=(
  "tests/test_scheduler_ports.py::test_headless_child_gets_only_its_own_identity_and_the_deny_policy"
  "tests/test_scheduler_ports.py::test_worker_failure_carrying_key_canary_never_reaches_the_observer"
)

# The native legs run with SWARM_PURE_PYTHON unset, so the extension is the engine under test.
step "native binding (tracer)" env -u SWARM_PURE_PYTHON python3 -m pytest tests/parity/test_scheduler_binding.py \
  -p no:cacheprovider --junitxml="$out/junit-binding.xml"
step "native binding JUnit report" junit_check "$out/junit-binding.xml" "${TRACER[@]}"

step "native threads (thread)" env -u SWARM_PURE_PYTHON python3 -m pytest tests/parity/test_scheduler_threads.py \
  -p no:cacheprovider --junitxml="$out/junit-threads.xml"
step "native threads JUnit report" junit_check "$out/junit-threads.xml" "${THREADS[@]}"

step "native stubs and port types (inventory)" env -u SWARM_PURE_PYTHON python3 -m pytest \
  tests/typecheck/test_native_stubs.py tests/typecheck/test_scheduler_ports_typecheck.py \
  -p no:cacheprovider --junitxml="$out/junit-types.xml"
step "native stubs and port types JUnit report" junit_check "$out/junit-types.xml" "${INVENTORY[@]}"

step "vectors (scheduler parity)" env -u SWARM_PURE_PYTHON python3 -m pytest tests/parity/test_scheduler_parity.py \
  -p no:cacheprovider --junitxml="$out/junit-vectors.xml"
step "vectors JUnit report" junit_check "$out/junit-vectors.xml"

step "continuous and ports, python engine" env SWARM_TEST_ENGINE=python SWARM_PURE_PYTHON=1 SWARM_SCHEDULER_WITNESS="$out/witness-python.txt" \
  python3 -m pytest tests/test_scheduler_continuous.py tests/test_scheduler_ports.py -p no:cacheprovider --junitxml="$out/junit-python.xml"
step "continuous and ports, python engine JUnit report" junit_check "$out/junit-python.xml" "${ENGINE_CASES[@]}"
step "continuous and ports, python engine witness" witness_check python "$out/witness-python.txt"

step "continuous and ports, rust engine" env SWARM_TEST_ENGINE=rust SWARM_PURE_PYTHON=1 SWARM_SCHEDULER_WITNESS="$out/witness-rust.txt" \
  python3 -m pytest tests/test_scheduler_continuous.py tests/test_scheduler_ports.py -p no:cacheprovider --junitxml="$out/junit-rust.xml"
step "continuous and ports, rust engine JUnit report" junit_check "$out/junit-rust.xml" "${ENGINE_CASES[@]}"
step "continuous and ports, rust engine witness" witness_check rust "$out/witness-rust.txt"

suite_after="$(sha256_of "$suite" || true)"
step "continuous suite digest after the runs" compare_digest "$suite_before" "$suite_after"

step "cargo test (gsd-sched-core, gsd-scheduler, gsd-python)" \
  cargo test -p gsd-sched-core -p gsd-scheduler -p gsd-python --locked --no-fail-fast
step "cargo clippy (gsd-sched-core, gsd-python, warnings are errors)" \
  cargo clippy -p gsd-sched-core -p gsd-python --all-targets --locked -- -D warnings
step "mypy --strict (swarm, ports typecheck)" python3 -m mypy --strict swarm/ tests/typecheck/test_scheduler_ports_typecheck.py
step "stubtest (swarm._native)" python3 -m mypy.stubtest swarm._native

# summary.json and identity.json are written whether or not the steps passed.
read -r -d '' report_py <<'PY' || true
import json
import os
import sys

out, engine_bin, engine_sha, suite_before, suite_after, native_path, native_sha, python_version, rust_version, git_head, git_tree, git_dirty = sys.argv[1:]
rows = []
try:
    with open(os.path.join(out, "steps.tsv"), encoding="utf-8") as handle:
        for line in handle:
            name, rc = line.rstrip("\n").split("\t")
            rows.append({"step": name, "exit": int(rc)})
except FileNotFoundError:
    pass
failed = [row["step"] for row in rows if row["exit"] != 0]
summary = {"passed": bool(rows) and not failed, "steps": rows, "failed": failed}
identity = {
    "engine_binary": {"path": engine_bin, "sha256": engine_sha},
    "continuous_suite": {
        "path": "tests/test_scheduler_continuous.py",
        "sha256_before": suite_before,
        "sha256_after": suite_after,
    },
    "native_extension": {"path": native_path, "sha256": native_sha},
    "python": python_version,
    "rust": rust_version,
    "git_head": git_head,
    "git_tree": git_tree,
    "git_dirty_entries": int(git_dirty),
}
for filename, payload in (("summary.json", summary), ("identity.json", identity)):
    with open(os.path.join(out, filename), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
PY
python3 -c "$report_py" "$out" "$engine_bin" "$engine_sha" "$suite_before" "$suite_after" "$native_path" "$native_sha" \
  "$python_version" "$rust_version" "$git_head" "$git_tree" "$git_dirty" \
  || { echo "ci_scheduler_rust: could not write summary.json and identity.json" >&2; exit 1; }

if [[ $failures -ne 0 ]]; then
  for name in "${failed_names[@]}"; do
    echo "ci_scheduler_rust: FAILED step: $name" >&2
  done
  echo "ci_scheduler_rust: $failures of $steps steps failed; records in $out" >&2
  exit 1
fi
echo "ci_scheduler_rust: all $steps steps passed; records in $out"
