#!/usr/bin/env python3
"""A1 parity runner for the first read-only route: scripts/orch_status.py reads and scripts/swarm_tool.py catalog.

The eleven cases follow plan/rust-wasmer-omp-options.md section 7 (n4 §7). Python is the truth: every invocation runs the
real script, the exit code must equal the expected exit exactly, stdout must parse as one JSON object, the required
top-level keys must be present, and a case predicate checks the parsed object.

Every case gets its own temporary state from tempfile (SWARM_DIR, workspace, HOME, a fresh signing key). Nothing is
written under the repository's .swarm/. Standard library only.

  python3 tests/parity/harness.py          text table; exit 0 only when all eleven cases pass
  python3 tests/parity/harness.py --json   the case table as JSON
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
PROCESS_TIMEOUT_S = 60.0
HOST_ENV_KEYS = ("PATH", "TMPDIR", "LANG")
LARGE_TASKS = 200

sys.path.insert(0, str(REPO))  # in-process setup imports the swarm package of this checkout


@dataclass(frozen=True)
class Check:
    """One assertion: its label, whether it held, and what differed when it did not."""

    label: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Probe:
    """One script invocation exactly as Python produced it."""

    exit_code: int
    stdout: bytes
    stderr: bytes
    payload: Optional[dict[str, Any]]  # stdout parsed as one JSON object, else None


Predicate = Callable[[Probe], Optional[str]]


@dataclass(frozen=True)
class Case:
    """A named case: a route, a description, and the function that runs its invocations against one sandbox."""

    name: str
    route: str
    description: str
    run: Callable[["Sandbox"], list[Check]]


@dataclass(frozen=True)
class CaseResult:
    """The outcome of one case: pass only when every check held."""

    name: str
    route: str
    description: str
    status: str
    checks: list[Check]


class Sandbox:
    """Per-case temporary state: a workspace, SWARM_DIR, HOME and a fresh signing key (never a host key)."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.workspace = base / "workspace"
        self.swarm_dir = base / "state" / ".swarm"
        self.home = base / "home"
        self.signing_key = secrets.token_hex(32)
        for directory in (self.workspace, self.swarm_dir, self.home):
            directory.mkdir(parents=True)

    def env(self, overrides: Optional[dict[str, str]] = None) -> dict[str, str]:
        """A scrubbed child environment: host PATH and locale only, then this sandbox's state."""
        env = {key: value for key, value in os.environ.items() if key in HOST_ENV_KEYS or key.startswith("LC_")}
        env.update(
            HOME=str(self.home),
            SWARM_DIR=str(self.swarm_dir),
            SWARM_SIGNING_KEY=self.signing_key,
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONHASHSEED="0",
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
        )
        env.update(overrides or {})
        return env


@contextmanager
def in_process(sandbox: Sandbox) -> Iterator[None]:
    """Run setup code in this process under the sandbox environment, then restore the host environment."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(sandbox.env())
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@contextmanager
def task_store(sandbox: Sandbox) -> Iterator[Any]:
    """A TaskStore on the sandbox's tasks.db, opened under the sandbox environment and closed afterwards."""
    with in_process(sandbox):
        from swarm.taskstore import TaskStore

        store = TaskStore(path=sandbox.swarm_dir / "tasks.db")
        try:
            yield store
        finally:
            store.conn.close()


def seed_tasks(sandbox: Sandbox, *task_ids: str, correlation_id: str = "C-parity", notes: Optional[dict] = None) -> None:
    """Create CREATED tasks through the real TaskStore, the same path orch_plan.py uses."""
    with task_store(sandbox) as store:
        for task_id in task_ids:
            store.create(task_id=task_id, correlation_id=correlation_id, capability="code.backend", agent_id="A05",
                         notes=notes)


def _parse_object(raw: bytes) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def invoke(sandbox: Sandbox, script: str, args: list[str], *, env: Optional[dict[str, str]] = None) -> Probe:
    """Run scripts/<script>.py in the sandbox with --root <workspace> and --json appended to `args`."""
    argv = [*args, "--root", str(sandbox.workspace), "--json"]
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / f"{script}.py"), *argv],
        cwd=sandbox.workspace,
        env=sandbox.env(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=PROCESS_TIMEOUT_S,
        check=False,
    )
    return Probe(proc.returncode, proc.stdout, proc.stderr, _parse_object(proc.stdout))


_MISSING = object()


def _walk(node: Any, path: tuple[str | int, ...]) -> Any:
    for key in path:
        if isinstance(key, int):
            if not isinstance(node, list) or key >= len(node):
                return _MISSING
            node = node[key]
        elif isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return _MISSING
    return node


def _dotted(path: tuple[str | int, ...]) -> str:
    return ".".join(str(key) for key in path)


def equals(path: tuple[str | int, ...], expected: Any) -> Predicate:
    """The value at `path` equals `expected`."""

    def check(probe: Probe) -> Optional[str]:
        got = _walk(probe.payload, path)
        if got is _MISSING:
            return f"{_dotted(path)} missing"
        return None if got == expected else f"{_dotted(path)}={got!r}, expected {expected!r}"

    return check


def one_of(path: tuple[str | int, ...], allowed: tuple[Any, ...]) -> Predicate:
    """The value at `path` is one of `allowed`."""

    def check(probe: Probe) -> Optional[str]:
        got = _walk(probe.payload, path)
        if got is _MISSING:
            return f"{_dotted(path)} missing"
        return None if got in allowed else f"{_dotted(path)}={got!r}, expected one of {allowed!r}"

    return check


def length(path: tuple[str | int, ...], expected: int) -> Predicate:
    """The list or object at `path` has exactly `expected` entries."""

    def check(probe: Probe) -> Optional[str]:
        got = _walk(probe.payload, path)
        if not isinstance(got, (list, dict)):
            return f"{_dotted(path)} is not a list or object"
        return None if len(got) == expected else f"{_dotted(path)} has {len(got)} entries, expected {expected}"

    return check


def contains(path: tuple[str | int, ...], needle: str) -> Predicate:
    """The string at `path` contains `needle`."""

    def check(probe: Probe) -> Optional[str]:
        got = _walk(probe.payload, path)
        if not isinstance(got, str):
            return f"{_dotted(path)} is not a string"
        return None if needle in got else f"{_dotted(path)} does not mention {needle!r}"

    return check


def lists_tool(name: str) -> Predicate:
    """The catalog's `tools` rows include a tool called `name`."""

    def check(probe: Probe) -> Optional[str]:
        rows = _walk(probe.payload, ("tools",))
        names = [row.get("name") for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        return None if name in names else f"tool {name} is not in the catalog"

    return check


def no_leak(*paths: Path) -> Predicate:
    """Neither stdout nor stderr carries any of the temporary paths."""

    def check(probe: Probe) -> Optional[str]:
        blob = probe.stdout + probe.stderr
        for path in paths:
            if str(path).encode("utf-8") in blob:
                return "output carries a temporary path"
        return None

    return check


def error_code(code: str) -> Predicate:
    """The error envelope names taxonomy code `code`."""
    return equals(("error", "code"), code)


def all_of(*predicates: Predicate) -> Predicate:
    """Every predicate holds; the first failure message wins."""

    def check(probe: Probe) -> Optional[str]:
        for predicate in predicates:
            message = predicate(probe)
            if message:
                return message
        return None

    return check


def expect(label: str, probe: Probe, *, exit_code: int, keys: tuple[str, ...] = (),
           predicate: Optional[Predicate] = None) -> Check:
    """Compare the exit code exactly, require one JSON object with `keys`, then apply `predicate`."""
    problems: list[str] = []
    if probe.exit_code != exit_code:
        problems.append(f"exit {probe.exit_code}, expected {exit_code}")
    if probe.payload is None:
        problems.append("stdout is not one JSON object")
    else:
        absent = [key for key in keys if key not in probe.payload]
        if absent:
            problems.append(f"missing keys {absent}")
        if predicate is not None:
            message = predicate(probe)
            if message:
                problems.append(message)
    return Check(label, not problems, "; ".join(problems) or f"exit {probe.exit_code}")


def _swarm_tool_limit(sandbox: Sandbox) -> int:
    """The payload cap as scripts/swarm_tool.py defines it (read from the script, not copied)."""
    spec = importlib.util.spec_from_file_location("parity_swarm_tool", SCRIPTS / "swarm_tool.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("scripts/swarm_tool.py cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    with in_process(sandbox):
        spec.loader.exec_module(module)
    return int(module.MAX_INPUT_BYTES)


def case_happy_path(sandbox: Sandbox) -> list[Check]:
    seed_tasks(sandbox, "T-happy-1", correlation_id="C-happy")
    status = invoke(sandbox, "orch_status", ["--correlation-id", "C-happy"])
    catalog = invoke(sandbox, "swarm_tool", ["catalog", "--caller", "A05", "--kind", "tools"])
    call = invoke(sandbox, "swarm_tool", ["call", "--caller", "a05-backend", "--name", "be_route_norm",
                                          "--input", '{"path": "/users/:id/"}'])
    return [
        expect("orch_status reads the seeded task", status, exit_code=0,
               keys=("status", "correlation_id", "tasks", "counts", "complete", "summary"),
               predicate=all_of(equals(("tasks", 0, "task_id"), "T-happy-1"), equals(("counts", "CREATED"), 1))),
        expect("catalog lists the caller's tools", catalog, exit_code=0,
               keys=("status", "caller", "caller_slug", "kind", "tools", "summary"),
               predicate=all_of(equals(("caller",), "A05"), lists_tool("be_route_norm"))),
        expect("call returns the registry result", call, exit_code=0,
               keys=("status", "caller", "name", "result", "summary"),
               predicate=one_of(("result", "state"), ("SUCCESS", "PARTIAL_SUCCESS"))),
    ]


def case_transition(sandbox: Sandbox) -> list[Check]:
    seed_tasks(sandbox, "T-legal-1", "T-illegal-1")
    legal = invoke(sandbox, "orch_status", ["--transition", "T-legal-1", "CANCELLED", "--reason", "parity legal"])
    illegal = invoke(sandbox, "orch_status", ["--transition", "T-illegal-1", "DONE", "--reason", "parity illegal"])
    return [
        expect("legal transition CREATED -> CANCELLED", legal, exit_code=0,
               keys=("status", "task", "summary"), predicate=equals(("task", "state"), "CANCELLED")),
        expect("illegal transition CREATED -> DONE refused", illegal, exit_code=2,
               keys=("status", "error"), predicate=error_code("E-CONTRACT")),
    ]


def case_gate_pass_fail_major(sandbox: Sandbox) -> list[Check]:
    with task_store(sandbox) as store:
        from swarm.gates import make_finding, make_verdict

        for task_id in ("T-gate-pass", "T-gate-fail"):
            store.create(task_id=task_id, correlation_id="C-gate", capability="code.backend", agent_id="A09")
        store.record_verdict("T-gate-pass", make_verdict(
            gate="review", task_id="T-gate-pass", agent_id="A09", correlation_id="C-gate",
            root=sandbox.workspace, audit=False))
        major = make_finding("R-1", "major", "functional", "parity fail-major probe")
        store.record_verdict("T-gate-fail", make_verdict(
            gate="review", task_id="T-gate-fail", agent_id="A09", findings=[major], correlation_id="C-gate",
            root=sandbox.workspace, audit=False))
    history_keys = ("status", "task_id", "history", "verdicts", "missing_gate_reasons", "summary")
    passed = invoke(sandbox, "orch_status", ["--history", "T-gate-pass"])
    failed = invoke(sandbox, "orch_status", ["--history", "T-gate-fail"])
    return [
        expect("gate pass verdict reads back as pass", passed, exit_code=0, keys=history_keys,
               predicate=equals(("verdicts", "review", "verdict"), "pass")),
        expect("gate fail-major verdict reads back as fail with its major finding", failed, exit_code=0,
               keys=history_keys,
               predicate=all_of(equals(("verdicts", "review", "verdict"), "fail"),
                                equals(("verdicts", "review", "findings", 0, "severity"), "major"))),
    ]


def case_boundary_100kb(sandbox: Sandbox) -> list[Check]:
    limit = _swarm_tool_limit(sandbox)
    overhead = len(json.dumps({"path": ""}).encode("utf-8"))
    at_limit = json.dumps({"path": "x" * (limit - overhead)})
    over_limit = json.dumps({"path": "x" * (limit + 1 - overhead)})
    sizes = (len(at_limit.encode("utf-8")), len(over_limit.encode("utf-8")))
    sized = Check(f"boundary payloads are exactly {limit} and {limit + 1} bytes",
                  sizes == (limit, limit + 1), f"{sizes[0]} and {sizes[1]} bytes")
    at = invoke(sandbox, "swarm_tool", ["call", "--caller", "A05", "--name", "parity_unregistered",
                                        "--input", at_limit])
    over = invoke(sandbox, "swarm_tool", ["call", "--caller", "A05", "--name", "parity_unregistered",
                                          "--input", over_limit])
    return [
        sized,
        expect(f"{limit} bytes is accepted (unregistered name gives INVALID_INPUT)", at, exit_code=1,
               keys=("status", "caller", "name", "result", "summary"),
               predicate=equals(("result", "state"), "INVALID_INPUT")),
        expect(f"{limit + 1} bytes is refused as E-INPUT", over, exit_code=2, keys=("status", "error"),
               predicate=all_of(error_code("E-INPUT"), contains(("error", "message"), "exceeds"))),
    ]


def case_validation_error(sandbox: Sandbox) -> list[Check]:
    bad_id = invoke(sandbox, "orch_status", ["--task-id", "bad..id"])
    unknown = invoke(sandbox, "orch_status", ["--history", "T-parity-missing"])
    caller = invoke(sandbox, "swarm_tool", ["catalog", "--caller", "nobody-here"])
    return [
        expect("task id containing '..' is refused before any work", bad_id, exit_code=2,
               keys=("status", "error"), predicate=error_code("E-INPUT")),
        expect("history of an unknown task is refused", unknown, exit_code=2,
               keys=("status", "error"), predicate=error_code("E-INPUT")),
        expect("unknown caller is refused", caller, exit_code=2,
               keys=("status", "error"), predicate=error_code("E-INPUT")),
    ]


def case_empty(sandbox: Sandbox) -> list[Check]:
    store_run = invoke(sandbox, "orch_status", [])
    query = invoke(sandbox, "swarm_tool", ["catalog", "--caller", "A05", "--kind", "tools",
                                           "--query", "zz-no-such-tool"])
    return [
        expect("empty store reads as success with no tasks", store_run, exit_code=0,
               keys=("status", "tasks", "summary", "db", "swarm_dir"), predicate=equals(("tasks",), [])),
        expect("catalog query matching nothing returns an empty list", query, exit_code=0,
               keys=("status", "tools", "summary"), predicate=equals(("tools",), [])),
    ]


def case_large(sandbox: Sandbox) -> list[Check]:
    seed_tasks(sandbox, *(f"T-large-{i:03d}" for i in range(LARGE_TASKS)), correlation_id="C-large")
    status = invoke(sandbox, "orch_status", ["--correlation-id", "C-large"])
    catalog = invoke(sandbox, "swarm_tool", ["catalog", "--caller", "A01", "--kind", "all"])
    return [
        expect(f"orch_status reads {LARGE_TASKS} tasks in one correlation", status, exit_code=0,
               keys=("status", "correlation_id", "tasks", "counts", "escalated", "complete", "summary"),
               predicate=all_of(length(("tasks",), LARGE_TASKS), equals(("counts", "CREATED"), LARGE_TASKS))),
        expect("full catalog for A01 (tools and skills)", catalog, exit_code=0,
               keys=("status", "caller", "kind", "tools", "skills", "summary"), predicate=equals(("caller",), "A01")),
    ]


def case_malformed(sandbox: Sandbox) -> list[Check]:
    not_json = invoke(sandbox, "swarm_tool", ["call", "--caller", "A05", "--name", "be_route_norm", "--input", "{oops"])
    not_object = invoke(sandbox, "swarm_tool", ["call", "--caller", "A05", "--name", "be_route_norm", "--input", "[1]"])
    no_name = invoke(sandbox, "swarm_tool", ["call", "--caller", "A05", "--input", "{}"])
    refused = dict(exit_code=2, keys=("status", "error"), predicate=error_code("E-INPUT"))
    return [
        expect("input that is not JSON is E-INPUT, never uncaught", not_json, **refused),
        expect("input that is not a JSON object is E-INPUT", not_object, **refused),
        expect("call without --name is E-INPUT", no_name, **refused),
    ]


PROBE_TOOL = {
    "name": "parity_slow_probe",
    "owner": "shared",
    "purpose": "Parity harness probe whose handler outlives its timeout_s.",
    "side_effect_tier": "READ",
    "idempotency": "idempotent",
    "permitted_callers": ["A05"],
    "entrypoint": "parity_slow_probe.py",
    "handler": "parity_slow_probe:run",
    "input_schema": {"type": "object", "properties": {}},
    "timeout_s": 0.1,
}


def case_timeout(sandbox: Sandbox) -> list[Check]:
    handlers = sandbox.base / "handlers"
    handlers.mkdir()
    (handlers / "parity_slow_probe.py").write_text(
        "import time\n\n\ndef run(args):\n    time.sleep(2.0)\n    return {\"state\": \"SUCCESS\"}\n", encoding="utf-8")
    document = json.loads((REPO / "agents.json").read_text(encoding="utf-8"))
    document.setdefault("shared_tools", []).append(PROBE_TOOL)
    registry = sandbox.base / "agents.json"
    registry.write_text(json.dumps(document), encoding="utf-8")
    probe = invoke(sandbox, "swarm_tool", ["call", "--caller", "A05", "--name", "parity_slow_probe", "--input", "{}"],
                   env={"SWARM_AGENTS_FILE": str(registry), "PYTHONPATH": str(handlers)})
    return [
        expect("handler past timeout_s is reported as TIMEOUT (status fail, exit 1)", probe, exit_code=1,
               keys=("status", "caller", "name", "result", "summary"), predicate=equals(("result", "state"), "TIMEOUT")),
    ]


def case_policy(sandbox: Sandbox) -> list[Check]:
    seed_tasks(sandbox, "T-foreign-gate", correlation_id="C-parity", notes={"gate": "review"})
    plan_mode = invoke(sandbox, "swarm_tool", ["call", "--caller", "A01", "--name", "orch_reconcile",
                                               "--plan-mode", "--input", "{}"])
    gate_move = invoke(sandbox, "orch_status", ["--transition", "T-foreign-gate", "IN_REVIEW",
                                                "--reason", "parity foreign gate"])
    refused = dict(exit_code=2, keys=("status", "error"))
    return [
        expect("plan mode refuses a WRITE tool before its handler is imported", plan_mode,
               predicate=all_of(error_code("E-POLICY"), no_leak(sandbox.base)), **refused),
        expect("hand move of a gate task to IN_REVIEW is refused with a scrubbed message", gate_move,
               predicate=all_of(error_code("E-POLICY"), no_leak(sandbox.base)), **refused),
    ]


def _diff(got: set[str], want: set[str]) -> str:
    return f"missing {sorted(want - got)}, extra {sorted(got - want)}"


def case_classifier(sandbox: Sandbox) -> list[Check]:
    """Static mirror check: ErrorCode, the retry partitions, omp/src/bridge.ts and classify() must agree."""
    with in_process(sandbox):
        from swarm.errors import ErrorCode, SwarmError, classify
        from swarm.retry import PERMANENT_CODES, TRANSIENT_CODES

    enum_codes = {code.value for code in ErrorCode}
    transient, permanent = set(TRANSIENT_CODES), set(PERMANENT_CODES)
    bridge = (REPO / "omp" / "src" / "bridge.ts").read_text(encoding="utf-8")
    union = re.search(r"export type ErrorCode\s*=([^;]*);", bridge)
    table = re.search(r"const ERROR_CODES:[^=]*=\s*\{(.*?)\};", bridge, re.DOTALL)
    union_codes = set(re.findall(r'"(E-[A-Z]+)"', union.group(1))) if union else set()
    table_codes = set(re.findall(r'"(E-[A-Z]+)"\s*:\s*true', table.group(1))) if table else set()
    partitioned = transient | permanent | {"E-INTERNAL"}  # E-INTERNAL is decided by message, so it sits in neither set

    checks = [
        Check("retry TRANSIENT and PERMANENT sets are disjoint", not (transient & permanent),
              f"shared {sorted(transient & permanent)}"),
        Check("retry partitions plus E-INTERNAL equal ErrorCode", partitioned == enum_codes,
              _diff(partitioned, enum_codes)),
        Check("omp/src/bridge.ts ERROR_CODES keys equal ErrorCode", table_codes == enum_codes,
              _diff(table_codes, enum_codes)),
        Check("omp/src/bridge.ts ErrorCode union equals ErrorCode", union_codes == enum_codes,
              _diff(union_codes, enum_codes)),
    ]
    for code in sorted(enum_codes):
        got = classify(SwarmError(ErrorCode(code), "parity probe")).value
        checks.append(Check(f"classify(SwarmError({code})) keeps {code}", got == code, f"got {got}"))
    # PermissionError is deliberately not asserted: swarm/errors.py tests OSError before PermissionError, so it maps to
    # E-DEP. That ordering is reported to the owner rather than enshrined here.
    expected_map = {TimeoutError: "E-TIMEOUT", FileNotFoundError: "E-INPUT", ValueError: "E-INPUT",
                    KeyError: "E-INPUT", ConnectionError: "E-DEP", RuntimeError: "E-INTERNAL"}
    for exc_type, code in expected_map.items():
        got = classify(exc_type("parity probe")).value
        checks.append(Check(f"classify({exc_type.__name__}) is {code}", got == code, f"got {got}"))
    return checks


CASES: tuple[Case, ...] = (
    Case("happy_path", "orch_status + swarm_tool",
         "Seeded task read back; catalog lists the caller's tools; call returns a registry result.", case_happy_path),
    Case("transition_legal_illegal", "orch_status",
         "Legal CREATED -> CANCELLED succeeds; illegal CREATED -> DONE is refused with E-CONTRACT.", case_transition),
    Case("gate_pass_fail_major", "orch_status",
         "Signed review verdicts read back via --history: a clean pass, and a fail carrying a major finding.",
         case_gate_pass_fail_major),
    Case("boundary_100kb", "swarm_tool",
         "--input of exactly the cap is accepted; one byte more is refused as E-INPUT.", case_boundary_100kb),
    Case("validation_error", "orch_status + swarm_tool",
         "Bad task id, unknown task and unknown caller each exit 2 with E-INPUT.", case_validation_error),
    Case("empty", "orch_status + swarm_tool",
         "Empty store and a catalog query matching nothing are success-empty, not a crash.", case_empty),
    Case("large", "orch_status + swarm_tool",
         f"{LARGE_TASKS} tasks in one correlation and the full A01 catalog both read back.", case_large),
    Case("malformed", "swarm_tool",
         "Non-JSON input, non-object input and a missing --name are each E-INPUT with exit 2.", case_malformed),
    Case("timeout", "swarm_tool",
         "A handler that outlives its timeout_s reports state TIMEOUT (status fail, exit 1).", case_timeout),
    Case("policy", "orch_status + swarm_tool",
         "Plan mode refuses a WRITE tool; a hand move of a gate task is refused; both as E-POLICY with no paths leaked.",
         case_policy),
    Case("classifier_quadruple_mirror", "errors + retry + omp bridge",
         "ErrorCode, the retry partitions, bridge.ts (object and union) and classify() agree on the taxonomy.",
         case_classifier),
)


def run_case(case: Case) -> CaseResult:
    """Run one case in a fresh temporary directory, removed afterwards."""
    base = Path(tempfile.mkdtemp(prefix=f"parity-{case.name}-"))
    try:
        resolved = base.resolve()
        if resolved == REPO or REPO in resolved.parents:
            raise RuntimeError(f"temporary state {resolved} lies inside the repository")
        checks = case.run(Sandbox(base))
    except Exception as exc:  # noqa: BLE001 - one broken case must not hide the rest of the table
        checks = [Check("case ran to completion", False, f"{type(exc).__name__}: {exc}")]
    finally:
        shutil.rmtree(base, ignore_errors=True)
    status = "pass" if checks and all(check.ok for check in checks) else "fail"
    return CaseResult(case.name, case.route, case.description, status, checks)


def render_table(results: list[CaseResult]) -> str:
    lines = [f"{'CASE':<30}{'ROUTE':<28}{'RESULT':<8}CHECKS"]
    for result in results:
        passed = sum(check.ok for check in result.checks)
        lines.append(f"{result.name:<30}{result.route:<28}{result.status:<8}{passed}/{len(result.checks)}")
        lines.extend(f"    FAIL {check.label}: {check.detail}" for check in result.checks if not check.ok)
    lines.append(f"{sum(result.status == 'pass' for result in results)}/{len(results)} cases pass")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A1 parity runner: 11 cases for the read-only route, Python as truth.")
    parser.add_argument("--json", action="store_true", help="print the case table as JSON")
    args = parser.parse_args(argv)
    results = [run_case(case) for case in CASES]
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print(render_table(results))
    return 0 if all(result.status == "pass" for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
