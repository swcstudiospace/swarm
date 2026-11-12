"""The scheduler CI gate (Phase 14.4, unit 14): scripts/ci_scheduler_rust.sh runs its real steps with python3 and cargo replaced on
PATH by stubs. The stubs write a fixture JUnit report for each pytest call and record every call, so these tests exercise the gate
protocol (input refusals, suite digests, JUnit checks, exit codes, summary and identity records) and never run the native suite.
Each test works in its own temporary repository holding a copy of the script and a fixture continuous suite."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci_scheduler_rust.sh"

SUITE_TEXT = b"# fixture stand-in for tests/test_scheduler_continuous.py\n"
SUITE_SHA256 = hashlib.sha256(SUITE_TEXT).hexdigest()
NATIVE_TEXT = b"fixture swarm._native extension\n"
ENGINE_TEXT = b"fixture gsd_engine binary\n"
ENGINE_SHA256 = hashlib.sha256(ENGINE_TEXT).hexdigest()

TRACER = ["tests/parity/test_scheduler_binding.py::test_native_compiled_entry_tracer_started_and_failed"]
THREADS = [
    "tests/parity/test_scheduler_threads.py::test_every_worker_completes_once_on_its_own_thread_and_none_is_left_behind[8]",
    "tests/parity/test_scheduler_threads.py::test_every_worker_completes_once_on_its_own_thread_and_none_is_left_behind[32]",
    "tests/parity/test_scheduler_threads.py::test_workers_that_release_the_interpreter_all_complete_with_their_results",
    "tests/parity/test_scheduler_threads.py::test_idle_final_drain_without_a_cancel_stops_closed",
]
INVENTORY = [
    "tests/typecheck/test_native_stubs.py::test_extension_exports_exactly_the_expected_names",
    "tests/typecheck/test_scheduler_ports_typecheck.py::test_the_port_adapter_exposes_exactly_the_sixteen_callbacks",
]
ENGINE_CASES = [
    "tests/test_scheduler_ports.py::test_headless_child_gets_only_its_own_identity_and_the_deny_policy",
    "tests/test_scheduler_ports.py::test_worker_failure_carrying_key_canary_never_reaches_the_observer",
]
ALL_NAMED = TRACER + THREADS + INVENTORY + ENGINE_CASES

# The stand-in for python3: the preflight import of swarm._native answers from STUB_NATIVE_PATH; pytest and mypy calls are
# recorded (pytest also copies STUB_JUNIT_SRC to its --junitxml target); every other call runs the real interpreter.
STUB_PYTHON = r'''
import os
import shutil
import sys

REAL = os.environ["STUB_REAL_PYTHON"]
LOG = os.environ["STUB_LOG"]
args = sys.argv[1:]


def record(line):
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def env(name):
    return os.environ.get(name, "-")


if len(args) >= 2 and args[0] == "-c" and "swarm._native" in args[1]:
    if os.environ.get("STUB_NATIVE_ABSENT"):
        record("native-preflight absent")
        sys.exit(1)
    record("native-preflight")
    print(os.environ["STUB_NATIVE_PATH"])
    sys.exit(0)
if len(args) >= 2 and args[0] == "-m" and args[1] == "pytest":
    junit = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml="))
    targets = [arg for arg in args[2:] if arg.endswith(".py")]
    record("pytest engine=%s pure=%s bin=%s sha=%s targets=%s" % (
        env("SWARM_TEST_ENGINE"), env("SWARM_PURE_PYTHON"), env("GSD_ENGINE_BIN"), env("GSD_ENGINE_BIN_SHA256"), " ".join(targets)))
    witness = os.environ.get("SWARM_SCHEDULER_WITNESS")  # a child run appends its engine, as swarm_run does; STUB_* picks what it wrote
    if witness and not os.environ.get("STUB_NO_CHILDREN"):
        seen = os.environ.get("STUB_CHILD_ENGINE") or env("SWARM_TEST_ENGINE")
        with open(witness, "a", encoding="utf-8") as handle:
            handle.write((seen + "\n") * 3)
    if os.environ.get("STUB_MUTATE"):
        with open(os.environ["STUB_MUTATE"], "a", encoding="utf-8") as handle:
            handle.write("# changed during the run\n")
    if os.environ.get("STUB_JUNIT_SRC"):
        shutil.copyfile(os.environ["STUB_JUNIT_SRC"], junit)
    sys.exit(int(os.environ.get("STUB_PYTEST_RC", "0")))
if len(args) >= 2 and args[0] == "-m" and args[1].split(".")[0] == "mypy":
    record(" ".join(args[1:]))
    sys.exit(0)
os.execv(REAL, [REAL] + args)
'''

CARGO_STUB = '#!/bin/sh\necho "cargo $*" >> "$STUB_LOG"\nexit "${STUB_CARGO_RC:-0}"\n'


def _junit(nodeids: Iterable[str], *, inject: str = "", target: str | None = None, tests: int | None = None) -> str:
    """A JUnit report with one testcase per nodeid (classname = dotted module path, as pytest writes it). `inject` is a child
    element (failure, error or skipped) put into the testcase named `target`."""
    cases = []
    for nodeid in nodeids:
        module, _, name = nodeid.partition("::")
        classname = module[: -len(".py")].replace("/", ".")
        child = inject if nodeid == target else ""
        cases.append(f'<testcase classname="{classname}" name="{name}" time="0.01">{child}</testcase>')
    count = len(cases) if tests is None else tests
    failures = 1 if "<failure" in inject else 0
    errors = 1 if "<error" in inject else 0
    skipped = 1 if "<skipped" in inject else 0
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuites><testsuite name="pytest" tests="{count}" failures="{failures}" errors="{errors}" skipped="{skipped}">'
        + "".join(cases)
        + "</testsuite></testsuites>\n"
    )


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _clean_environ() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


@dataclass
class Gate:
    base: Path

    @property
    def root(self) -> Path:
        return self.base / "repo"

    @property
    def suite(self) -> Path:
        return self.root / "tests" / "test_scheduler_continuous.py"

    @property
    def engine(self) -> Path:
        return self.base / "engine" / "gsd_engine"

    @property
    def native(self) -> Path:
        return self.base / "native" / "_native.so"

    @property
    def log(self) -> Path:
        return self.base / "stub.log"

    @property
    def junit_src(self) -> Path:
        return self.base / "junit-full.xml"

    @property
    def out(self) -> Path:
        return self.base / "out"

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def pytest_calls(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("pytest ")]

    def env(self, **changes: str | None) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "GSD_", "SWARM_", "STUB_"))}
        env.update(
            {
                "PATH": f"{self.base / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
                "STUB_LOG": str(self.log),
                "STUB_REAL_PYTHON": sys.executable,
                "STUB_NATIVE_PATH": str(self.native),
                "STUB_JUNIT_SRC": str(self.junit_src),
                "GSD_ENGINE_BIN": str(self.engine),
                "SCHEDULER_CONTINUOUS_SHA256": SUITE_SHA256,
            }
        )
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "ci_scheduler_rust.sh"), *args],
            cwd=self.base,
            env=self.env() if env is None else env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


@pytest.fixture
def gate(tmp_path: Path) -> Gate:
    g = Gate(tmp_path)
    (g.root / "scripts").mkdir(parents=True)
    (g.root / "tests").mkdir()
    shutil.copy2(SCRIPT, g.root / "scripts" / "ci_scheduler_rust.sh")
    g.suite.write_bytes(SUITE_TEXT)
    g.engine.parent.mkdir()
    g.engine.write_bytes(ENGINE_TEXT)
    g.engine.chmod(0o755)
    g.native.parent.mkdir()
    g.native.write_bytes(NATIVE_TEXT)
    g.junit_src.write_text(_junit(ALL_NAMED), encoding="utf-8")
    stub_dir = g.base / "stub"
    stub_dir.mkdir()
    (stub_dir / "stub_python.py").write_text(STUB_PYTHON, encoding="utf-8")
    bin_dir = g.base / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "python3",
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(stub_dir / 'stub_python.py'))} \"$@\"\n",
    )
    _executable(bin_dir / "cargo", CARGO_STUB)
    git = ["git", "-c", "user.name=gate", "-c", "user.email=gate@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(g.root)], check=True, capture_output=True, env=_clean_environ())
    subprocess.run([*git, "-C", str(g.root), "commit", "-q", "--allow-empty", "-m", "fixture"], check=True,
                   capture_output=True, env=_clean_environ())
    return g


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_missing_out_dir_is_refused(gate: Gate) -> None:
    done = gate.run(env=gate.env())
    assert done.returncode == 2, done.stderr
    assert "usage: scripts/ci_scheduler_rust.sh OUT_DIR" in done.stderr
    assert gate.calls() == []


def test_non_empty_out_dir_is_refused(gate: Gate) -> None:
    gate.out.mkdir()
    (gate.out / "old.txt").write_text("keep", encoding="utf-8")
    done = gate.run(str(gate.out))
    assert done.returncode == 2, done.stderr
    assert "not empty" in done.stderr
    assert gate.calls() == []
    assert (gate.out / "old.txt").read_text(encoding="utf-8") == "keep"


def test_empty_existing_out_dir_is_accepted(gate: Gate) -> None:
    gate.out.mkdir()
    done = gate.run(str(gate.out))
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("value", [None, "", "gsd_engine"], ids=["missing", "empty", "relative"])
def test_missing_or_relative_engine_binary_is_refused(gate: Gate, value: str | None) -> None:
    done = gate.run(str(gate.out), env=gate.env(GSD_ENGINE_BIN=value))
    assert done.returncode == 2, done.stderr
    assert "GSD_ENGINE_BIN" in done.stderr
    assert gate.pytest_calls() == []


def test_non_executable_engine_binary_is_refused(gate: Gate) -> None:
    gate.engine.chmod(0o644)
    done = gate.run(str(gate.out))
    assert done.returncode == 2, done.stderr
    assert "not an executable file" in done.stderr
    assert gate.pytest_calls() == []


@pytest.mark.parametrize("value", [None, "", "not-a-digest", "0" * 64], ids=["missing", "empty", "malformed", "wrong"])
def test_suite_digest_input_is_refused_before_any_run(gate: Gate, value: str | None) -> None:
    done = gate.run(str(gate.out), env=gate.env(SCHEDULER_CONTINUOUS_SHA256=value))
    assert done.returncode == 2, done.stderr
    assert "SCHEDULER_CONTINUOUS_SHA256" in done.stderr or "before the run" in done.stderr
    assert gate.pytest_calls() == []


def test_suite_changed_before_the_run_is_refused(gate: Gate) -> None:
    gate.suite.write_bytes(b"changed before the run\n")
    done = gate.run(str(gate.out))
    assert done.returncode == 2, done.stderr
    assert "before the run" in done.stderr
    assert gate.pytest_calls() == []


def test_suite_changed_during_the_run_fails_the_gate(gate: Gate) -> None:
    done = gate.run(str(gate.out), env=gate.env(STUB_MUTATE=str(gate.suite)))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FAILED step: continuous suite digest after the runs" in done.stderr
    assert gate.pytest_calls()


def test_native_extension_that_does_not_import_is_refused(gate: Gate) -> None:
    done = gate.run(str(gate.out), env=gate.env(STUB_NATIVE_ABSENT="1"))
    assert done.returncode == 2, done.stderr
    assert "swarm._native" in done.stderr
    assert gate.pytest_calls() == []


def test_a_complete_passing_run_is_accepted_and_recorded(gate: Gate) -> None:
    done = gate.run(str(gate.out))
    assert done.returncode == 0, done.stdout + done.stderr
    for name in ("junit-binding.xml", "junit-threads.xml", "junit-types.xml", "junit-vectors.xml", "junit-python.xml",
                 "junit-rust.xml"):
        assert (gate.out / name).is_file(), name
    summary = json.loads((gate.out / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is True
    assert summary["failed"] == []
    assert summary["steps"] and all(step["exit"] == 0 for step in summary["steps"])
    identity = json.loads((gate.out / "identity.json").read_text(encoding="utf-8"))
    assert identity["engine_binary"] == {"path": str(gate.engine), "sha256": ENGINE_SHA256}
    assert identity["continuous_suite"] == {
        "path": "tests/test_scheduler_continuous.py",
        "sha256_before": SUITE_SHA256,
        "sha256_after": SUITE_SHA256,
    }
    assert identity["native_extension"]["sha256"] == hashlib.sha256(NATIVE_TEXT).hexdigest()
    assert len(identity["git_head"]) == 40 and len(identity["git_tree"]) == 40
    assert isinstance(identity["git_dirty_entries"], int) and identity["git_dirty_entries"] >= 0


def test_both_engine_legs_run_with_the_binary_identity_exported(gate: Gate) -> None:
    done = gate.run(str(gate.out))
    assert done.returncode == 0, done.stdout + done.stderr
    suite_legs = [call for call in gate.pytest_calls() if "tests/test_scheduler_continuous.py" in call]
    python_leg = [call for call in suite_legs if "engine=python" in call]
    rust_leg = [call for call in suite_legs if "engine=rust" in call]
    assert len(python_leg) == 1 and "pure=1" in python_leg[0]
    assert len(rust_leg) == 1
    assert f"bin={gate.engine}" in rust_leg[0]
    assert f"sha={ENGINE_SHA256}" in rust_leg[0]
    assert "tests/test_scheduler_ports.py" in rust_leg[0]


def test_a_leg_whose_children_ran_on_another_engine_fails_its_witness(gate: Gate) -> None:
    done = gate.run(str(gate.out), env=gate.env(STUB_CHILD_ENGINE="python"))
    assert done.returncode != 0
    summary = json.loads((gate.out / "summary.json").read_text(encoding="utf-8"))
    assert "continuous and ports, rust engine witness" in summary["failed"]
    assert "continuous and ports, python engine witness" not in summary["failed"]


def test_a_leg_with_no_witnessed_child_fails_its_witness(gate: Gate) -> None:
    done = gate.run(str(gate.out), env=gate.env(STUB_NO_CHILDREN="1"))
    assert done.returncode != 0
    summary = json.loads((gate.out / "summary.json").read_text(encoding="utf-8"))
    assert {"continuous and ports, python engine witness", "continuous and ports, rust engine witness"} <= set(summary["failed"])


def test_the_strict_type_and_stub_checks_run_after_the_legs(gate: Gate) -> None:
    done = gate.run(str(gate.out))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "mypy --strict swarm/ tests/typecheck/test_scheduler_ports_typecheck.py" in gate.calls()
    assert "mypy.stubtest swarm._native" in gate.calls()
    assert any(call.startswith("cargo clippy -p gsd-sched-core -p gsd-python --all-targets --locked") for call in gate.calls())


@pytest.mark.parametrize(
    ("kind", "child"),
    [("failure", '<failure message="boom"/>'), ("error", '<error message="boom"/>'), ("skipped", '<skipped message="skip"/>')],
    ids=["failure", "error", "skipped"],
)
def test_a_report_with_a_failure_error_or_skip_fails_the_gate(gate: Gate, kind: str, child: str) -> None:
    gate.junit_src.write_text(_junit(ALL_NAMED, inject=child, target=TRACER[0]), encoding="utf-8")
    done = gate.run(str(gate.out))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FAILED step: native binding JUnit report" in done.stderr
    assert "not clean" in done.stdout


def test_a_report_with_zero_tests_fails_the_gate(gate: Gate) -> None:
    gate.junit_src.write_text(_junit([], tests=0), encoding="utf-8")
    done = gate.run(str(gate.out))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FAILED step: native binding JUnit report" in done.stderr
    assert "zero tests" in done.stdout


def test_a_malformed_report_fails_the_gate(gate: Gate) -> None:
    gate.junit_src.write_text("<testsuites><testsuite", encoding="utf-8")
    done = gate.run(str(gate.out))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FAILED step: native binding JUnit report" in done.stderr
    assert "malformed" in done.stdout


def test_a_missing_report_fails_the_gate(gate: Gate) -> None:
    done = gate.run(str(gate.out), env=gate.env(STUB_JUNIT_SRC=None))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FAILED step: native binding JUnit report" in done.stderr
    assert "JUnit report is missing" in done.stdout
    assert not (gate.out / "junit-binding.xml").exists()


@pytest.mark.parametrize("missing", [TRACER[0], THREADS[0], INVENTORY[0], ENGINE_CASES[0]],
                         ids=["tracer", "thread", "inventory", "engine"])
def test_a_report_missing_a_named_case_fails_the_gate(gate: Gate, missing: str) -> None:
    gate.junit_src.write_text(_junit([nodeid for nodeid in ALL_NAMED if nodeid != missing]), encoding="utf-8")
    done = gate.run(str(gate.out))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FAILED step:" in done.stderr
    assert "named cases missing" in done.stdout
    assert missing in done.stdout


def test_a_failing_pytest_leg_is_named_and_later_steps_still_run(gate: Gate) -> None:
    done = gate.run(str(gate.out), env=gate.env(STUB_PYTEST_RC="3"))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FAILED step: native binding (tracer)" in done.stderr
    assert "FAILED step: continuous and ports, rust engine" in done.stderr
    assert "mypy.stubtest swarm._native" in gate.calls()
    summary = json.loads((gate.out / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is False
    assert "native binding (tracer)" in summary["failed"]


def test_a_failing_cargo_step_is_named_and_fails_the_gate(gate: Gate) -> None:
    done = gate.run(str(gate.out), env=gate.env(STUB_CARGO_RC="1"))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "FAILED step: cargo clippy (gsd-sched-core, gsd-python, warnings are errors)" in done.stderr
