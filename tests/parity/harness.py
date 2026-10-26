#!/usr/bin/env python3
"""A1 parity runner for the first read-only route: scripts/orch_status.py reads and scripts/swarm_tool.py catalog.

The eleven cases follow plan/rust-wasmer-omp-options.md section 7 (n4 §7). Python is the truth: every invocation runs the
real script, the exit code must equal the expected exit exactly, stdout must parse as one JSON object, the required
top-level keys must be present, and a case predicate checks the parsed object.

Every case gets its own temporary state from tempfile (SWARM_DIR, workspace, HOME, a fresh signing key). Nothing is
written under the repository's .swarm/. Standard library only.

  python3 tests/parity/harness.py          text table; exit 0 only when all eleven cases pass
  python3 tests/parity/harness.py --json   the case table as JSON

The Rust leg is an explicit mode, never detected from the environment (U12.9):
  GSD_ENGINE_BIN=<built gsd_engine> python3 tests/parity/harness.py --rust
Under --rust each orch_status leg (happy_path, transition, gate_pass_fail_major --history, validation_error, empty,
large) runs the Python truth, then the same argv on the gsd_engine binary from the same restored state. Exit 0 or 1 from
the binary must print Python's stdout bytes with Python's exit code. An exit 2 whose code is a fallback code (E-INTERNAL,
E-DEP, E-CAPACITY, E-CONTRACT, E-INPUT) is a recorded refusal, and the script run with GSD_ENGINE=rust must then print
Python's bytes; any other answer must match Python's bytes too. The legal transition is the one call that mutates
state: the wall-clock updated_at it reports is masked on both sides, and nothing else is. A byte mismatch writes
tests/parity/fixtures/<case>-<digest>.json (case, argv, every leg's exit code and stdout) and fails the case.
Without --rust, GSD_ENGINE_BIN is ignored. --rust with GSD_ENGINE_BIN unset, not a file, or not executable, or a binary
that cannot be started, exits 1 with a message. Nothing is skipped silently.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
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

# The selector's fallback codes as literals. scripts/orch_status.py RUST_FALLBACK_CODES must equal this set; the
# classifier case checks that it does, so changing either side fails a check instead of moving the leg's answer.
SELECTOR_FALLBACK_CODES = frozenset({"E-INTERNAL", "E-DEP", "E-CAPACITY", "E-CONTRACT", "E-INPUT"})

FIXTURES = REPO / "tests" / "parity" / "fixtures"
STATE_DIRS = ("state", "workspace", "home")
WALL_CLOCK_FIELD = re.compile(rb'("updated_at": )[^,\n]+')


class RustLegUnavailable(RuntimeError):
    """The --rust leg cannot run: its binary is missing or does not start. The run exits 1; it never skips."""

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

    def __init__(self, base: Path, case_name: str, rust_bin: Optional[str] = None) -> None:
        self.base = base
        self.case_name = case_name
        self.rust_bin = rust_bin  # the gsd_engine binary under --rust; None leaves the Rust leg off
        self.parity: list[Check] = []  # Rust-leg checks, appended by orch_status_leg
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
            SWARM_PURE_PYTHON="1",
            SWARM_SIGNING_KEY=self.signing_key,
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONHASHSEED="0",
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
        )
        env.update(overrides or {})
        return env

    def snapshot(self, name: str) -> Path:
        """Copies the state directories aside, so a later restore() puts them back to this exact point."""
        dest = self.base / "snapshots" / name
        shutil.rmtree(dest, ignore_errors=True)
        for part in STATE_DIRS:
            shutil.copytree(self.base / part, dest / part)
        return dest

    def restore(self, snap: Path) -> None:
        """Puts the state directories back as snapshot() saw them."""
        for part in STATE_DIRS:
            shutil.rmtree(self.base / part)
            shutil.copytree(snap / part, self.base / part)


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


def invoke(sandbox: Sandbox, script: str, args: list[str], *, engine: str = "python",
           env: Optional[dict[str, str]] = None) -> Probe:
    """Runs one invocation with --root <workspace> appended to `args`.

    engine "python": scripts/<script>.py with GSD_ENGINE unset and --json appended. This is the truth.
    engine "selector": the same script with GSD_ENGINE=rust and GSD_ENGINE_BIN, so its Python fallback can answer.
    engine "rust": the gsd_engine binary with the same argv; the binary appends --json itself, as the SDK bridge does.
    """
    argv = [*args, "--root", str(sandbox.workspace)]
    if engine == "rust":
        return _invoke_binary(sandbox, script, argv)
    if engine not in ("python", "selector"):
        raise ValueError(f"unknown engine {engine!r}")
    overrides = dict(env or {})
    if engine == "selector":
        if sandbox.rust_bin is None:
            raise RuntimeError("the Rust leg is off: no gsd_engine binary was named")
        overrides.update(GSD_ENGINE="rust", GSD_ENGINE_BIN=sandbox.rust_bin)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / f"{script}.py"), *argv, "--json"],
        cwd=sandbox.workspace,
        env=sandbox.env(overrides),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=PROCESS_TIMEOUT_S,
        check=False,
    )
    return Probe(proc.returncode, proc.stdout, proc.stderr, _parse_object(proc.stdout))


def _invoke_binary(sandbox: Sandbox, script: str, argv: list[str]) -> Probe:
    """Runs the gsd_engine binary directly with the stdin object that scripts/orch_status.py builds for it."""
    if sandbox.rust_bin is None:
        raise RuntimeError("the Rust leg is off: no gsd_engine binary was named")
    env = sandbox.env()
    keys = _selector(sandbox).RUST_ENV_KEYS
    request = {"agent": "A01", "script": script, "argv": argv, "root": str(sandbox.workspace),
               "env": {key: env[key] for key in keys if env.get(key)}}
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    try:
        proc = subprocess.run([sandbox.rust_bin], input=payload, capture_output=True, cwd=sandbox.workspace,
                              env=env, timeout=PROCESS_TIMEOUT_S, check=False)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise RustLegUnavailable(f"the gsd_engine binary could not be started ({reason})") from exc
    return Probe(proc.returncode, proc.stdout, proc.stderr, _parse_object(proc.stdout))


def _selector(sandbox: Sandbox) -> Any:
    """scripts/orch_status.py loaded as a module, so the env keys come from the selector itself."""
    spec = importlib.util.spec_from_file_location("parity_orch_status", SCRIPTS / "orch_status.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("scripts/orch_status.py cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    with in_process(sandbox):
        spec.loader.exec_module(module)
    return module


def _error_code(probe: Probe) -> Optional[str]:
    """The taxonomy code of an exit-2 error reply, else None."""
    if probe.exit_code != 2 or probe.payload is None:
        return None
    error = probe.payload.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _masked(probe: Probe, mutating: bool) -> bytes:
    """The stdout that is compared. A mutating call's wall-clock updated_at is masked; nothing else is."""
    return WALL_CLOCK_FIELD.sub(rb"\g<1><clock>", probe.stdout) if mutating else probe.stdout


def _same(left: Probe, right: Probe, mutating: bool) -> bool:
    return left.exit_code == right.exit_code and _masked(left, mutating) == _masked(right, mutating)


def _leg(sandbox: Sandbox, probe: Probe) -> dict[str, Any]:
    """One leg as a fixture records it. The sandbox's signing key is redacted from the text."""
    def text(raw: bytes) -> str:
        return raw.decode("utf-8", "backslashreplace").replace(sandbox.signing_key, "<redacted>")

    return {"exit_code": probe.exit_code, "stdout": text(probe.stdout),
            "stdout_base64": base64.b64encode(probe.stdout).decode("ascii"), "stderr": text(probe.stderr)}


def _record_mismatch(sandbox: Sandbox, args: list[str], legs: dict[str, Probe], mutating: bool) -> str:
    """Writes the fixture for one byte mismatch and returns its path relative to the repository."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(json.dumps([sandbox.case_name, args]).encode("utf-8")).hexdigest()[:12]
    path = FIXTURES / f"{sandbox.case_name}-{digest}.json"
    body = {"case": sandbox.case_name, "script": "orch_status", "argv": args, "mutating": mutating,
            "legs": {name: _leg(sandbox, probe) for name, probe in legs.items()}}
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path.relative_to(REPO))


def _rust_checks(sandbox: Sandbox, before: Path, args: list[str], truth: Probe, mutating: bool) -> list[Check]:
    """The binary's answer to one call, or the selector's fallback where the binary refuses, against Python."""
    label = f"orch_status {' '.join(args)}"
    fallback_codes = SELECTOR_FALLBACK_CODES
    sandbox.restore(before)
    rust = invoke(sandbox, "orch_status", args, engine="rust")
    refusal = _error_code(rust)
    legs: dict[str, Probe] = {"python": truth, "rust": rust}
    if refusal in fallback_codes:
        sandbox.restore(before)
        legs["selector"] = invoke(sandbox, "orch_status", args, engine="selector")
        answer = legs["selector"]
        name = f"{label}: rust refuses {refusal}, the script fallback prints Python's bytes"
    else:
        answer = rust
        suffix = f", refused {refusal}" if refusal else ""
        name = f"{label}: rust answers exit {rust.exit_code}{suffix} with Python's bytes"
    if _same(answer, truth, mutating):
        return [Check(name, True, f"exit {truth.exit_code}")]
    where = _record_mismatch(sandbox, args, legs, mutating)
    return [Check(name, False, f"exit {answer.exit_code} vs Python exit {truth.exit_code}; fixture {where}")]


def orch_status_leg(sandbox: Sandbox, args: list[str], *, mutating: bool = False) -> Probe:
    """The Python truth for one orch_status call. Under --rust it is also answered by the binary and by the
    selector's fallback, each from the state Python saw; the comparison goes to sandbox.parity."""
    if sandbox.rust_bin is None:
        return invoke(sandbox, "orch_status", args)
    before = sandbox.snapshot("before")
    truth = invoke(sandbox, "orch_status", args)
    after = sandbox.snapshot("after")
    try:
        sandbox.parity.extend(_rust_checks(sandbox, before, args, truth, mutating))
    finally:
        sandbox.restore(after)
    return truth


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
    status = orch_status_leg(sandbox, ["--correlation-id", "C-happy"])
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
    legal = orch_status_leg(sandbox, ["--transition", "T-legal-1", "CANCELLED", "--reason", "parity legal"],
                            mutating=True)
    illegal = orch_status_leg(sandbox, ["--transition", "T-illegal-1", "DONE", "--reason", "parity illegal"])
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
    passed = orch_status_leg(sandbox, ["--history", "T-gate-pass"])
    failed = orch_status_leg(sandbox, ["--history", "T-gate-fail"])
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
    bad_id = orch_status_leg(sandbox, ["--task-id", "bad..id"])
    unknown = orch_status_leg(sandbox, ["--history", "T-parity-missing"])
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
    store_run = orch_status_leg(sandbox, [])
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
    status = orch_status_leg(sandbox, ["--correlation-id", "C-large"])
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
    """Static mirror check: ErrorCode, the retry partitions, omp/src/bridge.ts and classify() must agree.

    scripts/orch_status.py RUST_FALLBACK_CODES must equal SELECTOR_FALLBACK_CODES (checked below).
    """
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
    selector_codes = set(_selector(sandbox).RUST_FALLBACK_CODES)
    checks.append(Check(
        "scripts/orch_status.py RUST_FALLBACK_CODES equals the harness copy",
        selector_codes == SELECTOR_FALLBACK_CODES,
        _diff(selector_codes, SELECTOR_FALLBACK_CODES),
    ))
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
         "ErrorCode, retry partitions, bridge.ts, classify() and the selector's fallback set agree.",
         case_classifier),
)


def run_case(case: Case, rust_bin: Optional[str] = None) -> CaseResult:
    """Run one case in a fresh temporary directory, removed afterwards; rust_bin is set under --rust."""
    base = Path(tempfile.mkdtemp(prefix=f"parity-{case.name}-"))
    sandbox: Optional[Sandbox] = None
    try:
        resolved = base.resolve()
        if resolved == REPO or REPO in resolved.parents:
            raise RuntimeError(f"temporary state {resolved} lies inside the repository")
        sandbox = Sandbox(base, case.name, rust_bin)
        checks = case.run(sandbox)
    except RustLegUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - one broken case must not hide the rest of the table
        checks = [Check("case ran to completion", False, f"{type(exc).__name__}: {exc}")]
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if sandbox is not None:
        checks = [*checks, *sandbox.parity]
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


def _rust_binary() -> str:
    """The gsd_engine binary named by GSD_ENGINE_BIN, resolved. Unset or not executable is RustLegUnavailable."""
    value = os.environ.get("GSD_ENGINE_BIN", "")
    if not value:
        raise RustLegUnavailable("GSD_ENGINE_BIN is not set; name the built gsd_engine binary")
    path = Path(value).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RustLegUnavailable(f"GSD_ENGINE_BIN is not an executable file: {path}")
    return str(path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A1 parity runner: 11 cases for the read-only route, Python as truth.")
    parser.add_argument("--json", action="store_true", help="print the case table as JSON")
    parser.add_argument("--rust", action="store_true",
                        help="also compare each orch_status leg with the gsd_engine binary at GSD_ENGINE_BIN")
    args = parser.parse_args(argv)
    try:
        rust_bin = _rust_binary() if args.rust else None
        results = [run_case(case, rust_bin) for case in CASES]
    except RustLegUnavailable as exc:
        print(f"parity: the --rust leg cannot run: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print(render_table(results))
    return 0 if all(result.status == "pass" for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
