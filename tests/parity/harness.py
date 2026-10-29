#!/usr/bin/env python3
"""A1 parity runner for the first read-only route: scripts/orch_status.py reads and scripts/swarm_tool.py catalog.

The eleven read cases follow plan/rust-wasmer-omp-options.md section 7 (n4 §7); the twelfth, transition_matrix, is the write-route
matrix of Phase 14.5 (U3.2c, below). Python is the truth: every invocation runs the
real script, the exit code must equal the expected exit exactly, stdout must parse as one JSON object, the required
top-level keys must be present, and a case predicate checks the parsed object.

Every case gets its own temporary state from tempfile (SWARM_DIR, workspace, HOME, a fresh signing key). Nothing is
written under the repository's .swarm/. Standard library only.

  python3 tests/parity/harness.py          text table; exit 0 only when all twelve cases pass
  python3 tests/parity/harness.py --json   the case table as JSON

The Rust leg is an explicit mode, never detected from the environment (U12.9):
  GSD_ENGINE_BIN=<built gsd_engine> python3 tests/parity/harness.py --rust [--backend host|sandbox]
Under --rust each orch_status leg (happy_path, transition, gate_pass_fail_major --history, validation_error, empty,
large) runs the Python truth (GSD_ENGINE=python, named explicitly so a Rust default cannot move it), then the same argv
plus --json on the gsd_engine binary from the same restored state, as the SDK bridge sends it (the binary passes argv
through and serves only the --json read grammar). --backend names GSD_RUST_BACKEND for the Rust-served legs (default
host). Exit 0 or 1 from the binary must print Python's stdout bytes with Python's exit code. An exit 2 whose code is a
fallback code (E-INTERNAL, E-DEP, E-CAPACITY, E-CONTRACT, E-INPUT) is a recorded refusal, and the script run with
GSD_ENGINE=rust must then print Python's bytes; any other answer must match Python's bytes too. A third leg runs the
script with GSD_ENGINE unset and GSD_ENGINE_BIN set, the default selector, and must print Python's bytes as well. The
legal transition is the one call that mutates state: the wall-clock updated_at it reports is masked on both sides, and
nothing else is. A byte mismatch writes tests/parity/fixtures/<case>-<digest>.json (case, argv, every leg's exit code
and stdout) and fails the case.

Every check records served_by: `rust` (the binary answered), `fallback:<CODE>` (the binary refused with a fallback code,
or fell back from the sandbox backend, and the answer came from the fallback) or `python` (the Python script answered;
every check on the truth leg, the default selector while its default is Python, and the static checks). The evidence
is the answer shape, the exit code, the stderr, and a marker the harness's recording wrapper around GSD_ENGINE_BIN
leaves when the selector actually started the binary. --json-out PATH writes the per-check results and the served_by
counts as one JSON object. Without --rust, GSD_ENGINE_BIN is ignored. --rust with GSD_ENGINE_BIN unset, not a file, or
not executable, or a binary that cannot be started, exits 1 with a message. Nothing is skipped silently.

The twelfth case, transition_matrix (Phase 14.5, U3.2c), runs the cells of tests/parity/_write_matrix.py (the 351 transition vectors
from the real TaskStore plus dry-run, id, grammar and lease cells) through `orch_status --transition` on the Python script and, under
--rust, on the binary (the script with GSD_ENGINE=rust, the binary alone, and the default selector on a sample), each from the same
restored store, WRITE_WORKERS cells at a time. Python alone is checked against what the vectors recorded. A write is judged by its
stdout bytes and exit code (masks: updated_at and notes.verdicts_since), a dump of the tasks, transitions, verdicts and artifacts
tables (ts and updated_at masked), and its effects (run log, live.json, files, the calls a loopback fake of the substrate saw). A cell
the binary serves must have served_by `rust`; a cell on the expected-fallback list (_write_matrix.refusal) must be refused with
write_state not_started and Python's bytes; under --backend sandbox every cell is such a refusal unless --sandbox-write names a binary
built with the sandbox-write feature (then the sandbox backend is held to the host expectations). A divergence found and not fixed is
named in _write_matrix.KNOWN_DIVERGENCES: the cell passes only while it differs in exactly that way. No fixture file is written.
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
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from collections.abc import Set as AbstractSet
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from itertools import zip_longest
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
BACKENDS = ("host", "sandbox")

# Unit R3b makes the binary print this one line to stderr when the sandbox backend faulted and host Rust answered. It
# is parsed in failover_code() and nowhere else; the line is built from the code only.
FAILOVER_NOTICE = re.compile(rb"^gsd_engine: sandbox fault (E-[A-Z]+); answered by host Rust$", re.MULTILINE)

# The selector legs must reach the binary, so SWARM_PURE_PYTHON (which the selector treats as "Python" ahead of
# GSD_ENGINE once Phase 14.3 R5 lands) is not set for them; the truth leg and the direct binary keep it.
SELECTOR_LEG_UNSET = ("SWARM_PURE_PYTHON",)

# What the Rust router answers for argv it does not serve (crates/gsd-adapter/src/route_table.rs route_for_call).
NO_ROUTE_MESSAGE = "no rust route for this entrypoint and flags"

FIXTURES = REPO / "tests" / "parity" / "fixtures"
STATE_DIRS = ("state", "workspace", "home")
WALL_CLOCK_FIELD = re.compile(rb'("updated_at": )[^,\n]+')


class RustLegUnavailable(RuntimeError):
    """The --rust leg cannot run: its binary is missing or does not start. The run exits 1; it never skips."""

sys.path.insert(0, str(REPO))  # in-process setup imports the swarm package of this checkout
sys.path.insert(0, str(Path(__file__).resolve().parent))  # the matrix cells of the transition case sit beside this file
import _write_matrix as matrix  # noqa: E402


@dataclass(frozen=True)
class Check:
    """One assertion: its label, whether it held, and what differed when it did not."""

    label: str
    ok: bool
    detail: str = ""
    served_by: str = "python"  # rust, fallback:<CODE> or python: who answered the call the check inspects


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
    pending: list[str] = field(default_factory=list)  # mirror checks that cannot run yet, each with its reason


class Sandbox:
    """Per-case temporary state: a workspace, SWARM_DIR, HOME and a fresh signing key (never a host key)."""

    def __init__(self, base: Path, case_name: str, rust_bin: Optional[str] = None, backend: str = "host",
                 sandbox_writes: bool = False) -> None:
        self.base = base
        self.case_name = case_name
        self.rust_bin = rust_bin  # the gsd_engine binary under --rust; None leaves the Rust leg off
        self.backend = backend  # GSD_RUST_BACKEND for the Rust-served legs
        self.sandbox_writes = sandbox_writes  # the binary is built with the sandbox-write feature: the sandbox backend serves --transition
        self.parity: list[Check] = []  # Rust-leg checks, appended by orch_status_leg
        self.pending: list[str] = []
        self.workspace = base / "workspace"
        self.swarm_dir = base / "state" / ".swarm"
        self.home = base / "home"
        self.signing_key = secrets.token_hex(32)
        self.started_marker = base / "probe" / "started"  # one byte appended per start of the recorded binary
        self.recorded_bin = base / "probe" / "gsd_engine"
        for directory in (self.workspace, self.swarm_dir, self.home, self.recorded_bin.parent):
            directory.mkdir(parents=True)
        if rust_bin is not None:
            # The selector legs name this wrapper as GSD_ENGINE_BIN: it records that the selector started the binary,
            # then becomes the binary with stdin, argv, environment and exit code untouched.
            self.recorded_bin.write_text(
                f"#!/bin/sh\nprintf x >> {shlex.quote(str(self.started_marker))}\nexec {shlex.quote(rust_bin)} \"$@\"\n",
                encoding="utf-8")
            self.recorded_bin.chmod(0o700)

    def env(self, overrides: Optional[dict[str, str]] = None, unset: tuple[str, ...] = ()) -> dict[str, str]:
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
        for key in unset:
            env.pop(key, None)
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


def seed_tasks(sandbox: Sandbox, *task_ids: str, correlation_id: str = "C-parity", notes: Optional[dict[str, Any]] = None) -> None:
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

    engine "python": scripts/<script>.py with GSD_ENGINE=python and --json appended. This is the truth.
    engine "selector": the same script with GSD_ENGINE=rust, GSD_ENGINE_BIN (the recording wrapper) and GSD_RUST_BACKEND,
        so its Python fallback can answer.
    engine "default": the same script with GSD_ENGINE unset and GSD_ENGINE_BIN set: the selector's own default. The
        backend is left to the binary's default.
    engine "rust": the gsd_engine binary with the same argv, --json included, as the SDK bridge sends it.
    """
    argv = [*args, "--root", str(sandbox.workspace), "--json"]
    if engine == "rust":
        return _invoke_binary(sandbox, script, argv)
    overrides = dict(env or {})
    unset: tuple[str, ...] = ()
    if engine == "python":
        overrides["GSD_ENGINE"] = "python"
    elif engine in ("selector", "default"):
        if sandbox.rust_bin is None:
            raise RuntimeError("the Rust leg is off: no gsd_engine binary was named")
        overrides["GSD_ENGINE_BIN"] = str(sandbox.recorded_bin)
        if engine == "selector":
            overrides.update(GSD_ENGINE="rust", GSD_RUST_BACKEND=sandbox.backend)
        unset = SELECTOR_LEG_UNSET
        sandbox.started_marker.unlink(missing_ok=True)
    else:
        raise ValueError(f"unknown engine {engine!r}")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / f"{script}.py"), *argv],
        cwd=sandbox.workspace,
        env=sandbox.env(overrides, unset),
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
    env = sandbox.env({"GSD_RUST_BACKEND": sandbox.backend})
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


def failover_code(stderr: bytes) -> Optional[str]:
    """The sandbox fault code in unit R3b's failover notice on stderr, else None. This is the one place the notice is
    parsed; the binary prints nothing of the kind until R3b lands, so today it always returns None."""
    match = FAILOVER_NOTICE.search(stderr)
    return match.group(1).decode("ascii") if match else None


def served_by(binary: Probe, answered: Probe, binary_started: bool = True) -> str:
    """Who answered one call: `python`, `rust` or `fallback:<CODE>`.

    `binary` is the gsd_engine binary's own reply to the argv; `answered` is the probe a check judges (the same probe on
    the direct leg, the script's probe on a selector leg); `binary_started` is False when the selector never started the
    binary. A refusal with a fallback code, or a sandbox failover notice on the answer's stderr, is a fallback."""
    if not binary_started:
        return "python"
    refusal = _error_code(binary)
    if refusal in SELECTOR_FALLBACK_CODES:
        return f"fallback:{refusal}"
    fault = failover_code(answered.stderr)
    return f"fallback:{fault}" if fault else "rust"


def _rust_checks(sandbox: Sandbox, before: Path, args: list[str], truth: Probe, mutating: bool) -> list[Check]:
    """The binary's answer to one call, or the selector's fallback where the binary refuses, and the default selector's
    answer, each against Python."""
    label = f"orch_status {' '.join(args)}"
    sandbox.restore(before)
    rust = invoke(sandbox, "orch_status", args, engine="rust")
    refusal = _error_code(rust)
    legs: dict[str, Probe] = {"python": truth, "rust": rust}
    if refusal in SELECTOR_FALLBACK_CODES:
        sandbox.restore(before)
        legs["selector"] = invoke(sandbox, "orch_status", args, engine="selector")
        answer = legs["selector"]
        name = f"{label}: rust refuses {refusal}, the script fallback prints Python's bytes"
        answer_served = served_by(rust, answer, sandbox.started_marker.exists())
    else:
        answer = rust
        suffix = f", refused {refusal}" if refusal else ""
        name = f"{label}: rust answers exit {rust.exit_code}{suffix} with Python's bytes"
        answer_served = served_by(rust, rust)
    sandbox.restore(before)
    legs["default"] = invoke(sandbox, "orch_status", args, engine="default")
    default_served = served_by(rust, legs["default"], sandbox.started_marker.exists())
    # Before Phase 14.3 R5 the selector has no DEFAULT_ENGINE and its default is Python.
    selector = _selector(sandbox)
    rust_serves = getattr(selector, "rust_serves", None)
    script_argv = [*args, "--root", str(sandbox.workspace), "--json"]
    python_expected = getattr(selector, "DEFAULT_ENGINE", "python") == "python" or (
        rust_serves is not None and not rust_serves(script_argv))
    answer_problems = [] if _same(answer, truth, mutating) else [f"exit {answer.exit_code} vs Python exit {truth.exit_code}"]
    default_problems = [] if _same(legs["default"], truth, mutating) else [
        f"exit {legs['default'].exit_code} vs Python exit {truth.exit_code}"]
    if (default_served == "python") != python_expected:
        default_problems.append(f"served_by {default_served}, expected {'python' if python_expected else 'rust or a fallback'}")
    where = _record_mismatch(sandbox, args, legs, mutating) if answer_problems or default_problems else ""
    answer_detail = "; ".join([*answer_problems, f"fixture {where}"]) if answer_problems else f"exit {truth.exit_code}"
    default_detail = "; ".join([*default_problems, f"fixture {where}"]) if default_problems else f"exit {truth.exit_code}"
    return [
        Check(name, not answer_problems, answer_detail, answer_served),
        Check(f"{label}: the default selector (GSD_ENGINE unset) prints Python's bytes", not default_problems, default_detail,
              default_served),
    ]


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
    refused: dict[str, Any] = dict(exit_code=2, keys=("status", "error"), predicate=error_code("E-INPUT"))
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
    refused: dict[str, Any] = dict(exit_code=2, keys=("status", "error"))
    return [
        expect("plan mode refuses a WRITE tool before its handler is imported", plan_mode,
               predicate=all_of(error_code("E-POLICY"), no_leak(sandbox.base)), **refused),
        expect("hand move of a gate task to IN_REVIEW is refused with a scrubbed message", gate_move,
               predicate=all_of(error_code("E-POLICY"), no_leak(sandbox.base)), **refused),
    ]


def _diff(got: AbstractSet[str], want: AbstractSet[str]) -> str:
    return f"missing {sorted(want - got)}, extra {sorted(got - want)}"


# Complete orch_status argv as the selector sees them; "{root}" is the sandbox workspace. The first group is the read
# grammar of parse_orch_status_read plus --json; the rest is outside it, for one reason per entry.
ROUTING_CORPUS: tuple[tuple[str, ...], ...] = (
    ("--json",),
    ("--correlation-id", "C-1", "--json"),
    ("--correlation-id=C-1", "--json"),
    ("--task-id", "T-1", "--json"),
    ("--history", "T-1", "--json"),
    ("--repo", "{root}", "--json"),
    ("--root={root}", "--json"),
    ("--dry-run", "--json"),
    (),  # no --json
    ("--correlation-id", "C-1"),  # no --json
    ("--json=1",),  # an inline value on a flag
    ("--dry-run=1", "--json"),
    ("--transition", "T-1", "CANCELLED", "--json"),  # mutating
    ("--ingest", "result.json", "--json"),  # mutating
    ("--advisory", "--json"),
    ("--reason", "why", "--json"),
    ("--unknown", "--json"),
    ("--corr", "C-1", "--json"),  # argparse's prefix match, not served
    ("--correlation-id", "--json"),  # a value that starts with '-'
    ("--history", "--json"),
    ("positional", "--json"),
)


def _routing_checks(sandbox: Sandbox, rust_serves: Callable[[list[str]], bool]) -> list[Check]:
    """The selector's rust_serves() against the binary's own routing for each argv of ROUTING_CORPUS. The binary serves an
    argv unless it answers exit 2 E-DEP with its no-route message; nothing else it prints counts as a refusal to route."""
    checks = []
    for template in ROUTING_CORPUS:
        argv = [part.replace("{root}", str(sandbox.workspace)) for part in template]
        reply = _invoke_binary(sandbox, "orch_status", argv)
        error = (reply.payload or {}).get("error")
        message = error.get("message") if isinstance(error, dict) else None
        binary_serves = not (_error_code(reply) == "E-DEP" and isinstance(message, str) and NO_ROUTE_MESSAGE in message)
        selector_serves = bool(rust_serves(argv))
        checks.append(Check(f"rust_serves agrees with the binary's routing for argv {list(template)}",
                            selector_serves == binary_serves,
                            f"selector rust_serves {selector_serves}, binary serves {binary_serves}",
                            served_by(reply, reply)))
    return checks


def _default_mirror_checks(sandbox: Sandbox, selector: Any) -> list[Check]:
    """Static and dynamic mirrors of the selector's default. Each one runs only once its symbol exists: the selector's
    DEFAULT_ENGINE and rust_serves() come with Phase 14.3 R5, ORCH_STATUS_READ_DEFAULT with R3a. A missing symbol is
    listed as pending on the case result, never passed. The dynamic one needs the binary, so it runs under --rust."""
    checks: list[Check] = []
    table = (REPO / "crates" / "gsd-adapter" / "src" / "route_table.rs").read_text(encoding="utf-8")
    declared = re.search(r"ORCH_STATUS_READ_DEFAULT\s*:\s*Engine\s*=\s*Engine::(\w+)", table)
    default_engine = getattr(selector, "DEFAULT_ENGINE", None)
    if default_engine is None or declared is None:
        missing = [name for name, found in (("scripts/orch_status.py DEFAULT_ENGINE", default_engine is not None),
                                            ("route_table.rs ORCH_STATUS_READ_DEFAULT", declared is not None))
                   if not found]
        sandbox.pending.append(f"DEFAULT_ENGINE equals ORCH_STATUS_READ_DEFAULT: not defined yet: {', '.join(missing)}")
    else:
        checks.append(Check("scripts/orch_status.py DEFAULT_ENGINE equals route_table.rs ORCH_STATUS_READ_DEFAULT",
                            default_engine == declared.group(1).lower(),
                            f"selector {default_engine!r}, route table {declared.group(1).lower()!r}"))
    rust_serves = getattr(selector, "rust_serves", None)
    if rust_serves is None:
        sandbox.pending.append("rust_serves matches the binary's routing on the argv corpus: "
                               "scripts/orch_status.py has no rust_serves() yet")
    elif sandbox.rust_bin is not None:
        rust_serves_write = getattr(selector, "rust_serves_write", None)
        checks.extend(_routing_checks(sandbox, lambda argv: bool(rust_serves(argv) or (rust_serves_write is not None and rust_serves_write(argv)))))
    return checks


def case_classifier(sandbox: Sandbox) -> list[Check]:
    """Static mirror check: ErrorCode, the retry partitions, omp/src/bridge.ts and classify() must agree.

    scripts/orch_status.py RUST_FALLBACK_CODES must equal SELECTOR_FALLBACK_CODES (checked below), and the selector's
    default and routing grammar must equal the Rust side's (_default_mirror_checks).
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
    selector = _selector(sandbox)
    selector_codes = set(selector.RUST_FALLBACK_CODES)
    checks.append(Check(
        "scripts/orch_status.py RUST_FALLBACK_CODES equals the harness copy",
        selector_codes == SELECTOR_FALLBACK_CODES,
        _diff(selector_codes, SELECTOR_FALLBACK_CODES),
    ))
    checks.extend(_default_mirror_checks(sandbox, selector))
    return checks


# --- the transition matrix: `orch_status --transition` on both engines (Phase 14.5, unit U3.2c) ---------------------------------------
#
# Each cell of `_write_matrix.load_cells()` is one store state and one argv. From the same restored state the cell runs, in turn: the
# Python script (the truth), the script with GSD_ENGINE=rust (the selector: it starts the binary, replays its effects, or lets Python
# answer), the binary alone (with an effects file, as the selector calls it), and, for a sample of cells, the default selector. Every leg
# is compared with Python on stdout and exit code (masks: `updated_at` and `notes.verdicts_since`; `notes.lease.at` in a lease cell), on
# a dump of the four store tables (`ts` and `updated_at` masked), and on the effects: the run log without `ts` and `msg_id`, live.json
# without `seq`, `pid` and the readings of its clock, the files left in the state directory, and the calls a loopback fake of the
# substrate saw. A cell the binary answers must not fall back; a cell on the expected-fallback list (`_write_matrix.refusal`) must
# fall back with `write_state not_started` and the store untouched. Under --backend sandbox every cell is refused that way.

WRITE_WORKERS = 16  # each cell is a few short processes on a host that is usually saturated: more of them in flight finishes sooner
SANDBOX_REFUSES = "the sandbox backend of a binary built without the sandbox-write feature refuses a write call (U3.3b)"


@dataclass(frozen=True)
class WriteLeg:
    """One leg of one cell: what it printed and what it left behind."""

    probe: Probe
    dump: str  # the store tables, masked
    effects: str  # the run log, live.json, the state directory's files and the substrate's calls, masked
    scrubbed: str  # the same without the difference the cell's known divergence names (equal to `effects` for a cell without one)
    started: bool  # the selector started the binary


@contextmanager
def _substrate(lease: Optional[matrix.LeaseScript]) -> Iterator[Optional[matrix.FakeSubstrate]]:
    """The fake substrate for one leg, or nothing when the cell has no lease (the substrate stays off, as in the other cells)."""
    if lease is None:
        yield None
        return
    with matrix.FakeSubstrate(lease) as fake:
        yield fake


def _write_leg(sandbox: Sandbox, cell: matrix.Cell, argv: list[str], initial: Path, engine: str,
               lease: Optional[matrix.LeaseScript]) -> WriteLeg:
    """One script leg of a cell, from the restored initial state."""
    sandbox.restore(initial)
    with _substrate(lease) as fake:
        probe = invoke(sandbox, "orch_status", argv, engine=engine, env=None if fake is None else fake.env)
        calls = [] if fake is None else list(fake.calls)
    effects = matrix.effects_of(sandbox.swarm_dir, calls, *cell.masks)
    scrubbed = effects if cell.known is None else matrix.effects_of(sandbox.swarm_dir, calls, *cell.masks, known=cell.known)
    return WriteLeg(probe, matrix.dump_store(sandbox.swarm_dir / "tasks.db", *cell.masks), effects, scrubbed,
                    engine != "python" and sandbox.started_marker.exists())


def _write_direct(sandbox: Sandbox, cell: matrix.Cell, argv: list[str], initial: Path, env_keys: tuple[str, ...]) -> tuple[WriteLeg, Path]:
    """The binary alone, given the request the selector builds (with an effects file it owns); the leg and the effects file."""
    if sandbox.rust_bin is None:
        raise RuntimeError("the Rust leg is off: no gsd_engine binary was named")
    sandbox.restore(initial)
    manifest = sandbox.base / "probe" / "effects.json"
    manifest.write_bytes(b"")
    env = sandbox.env({"GSD_RUST_BACKEND": sandbox.backend})
    workspace = str(sandbox.workspace)
    request = {"agent": "A01", "script": "orch_status", "argv": [*argv, "--root", workspace, "--json"], "root": workspace,
               "env": {key: env[key] for key in env_keys if env.get(key)}, "effects_path": str(manifest)}
    try:
        proc = subprocess.run([sandbox.rust_bin], input=json.dumps(request, ensure_ascii=False).encode("utf-8"), capture_output=True,
                              cwd=sandbox.workspace, env=env, timeout=PROCESS_TIMEOUT_S, check=False)
    except OSError as exc:
        raise RustLegUnavailable(f"the gsd_engine binary could not be started ({exc.strerror or type(exc).__name__})") from exc
    probe = Probe(proc.returncode, proc.stdout, proc.stderr, _parse_object(proc.stdout))
    effects = matrix.effects_of(sandbox.swarm_dir, [], *cell.masks)
    return WriteLeg(probe, matrix.dump_store(sandbox.swarm_dir / "tasks.db", *cell.masks), effects, effects, False), manifest


def _not_started(probe: Probe) -> Optional[str]:
    """The error code of a refusal that says nothing was written (`error.details.write_state == "not_started"`), else None."""
    error = probe.payload.get("error") if probe.exit_code == 2 and probe.payload is not None else None
    details = error.get("details") if isinstance(error, dict) else None
    if not (isinstance(details, dict) and details.get("write_state") == "not_started"):
        return None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else "E-?"


def _first_difference(left: str, right: str) -> str:
    """Where two texts first differ, as one short line."""
    for number, (one, other) in enumerate(zip_longest(left.splitlines(), right.splitlines(), fillvalue="<missing>"), start=1):
        if one != other:
            return f"line {number}: {one[:150]!r} vs {other[:150]!r}"
    return "same lines, different ends"


def _compare(who: str, truth: WriteLeg, other: WriteLeg, cell: matrix.Cell, effects: bool, known: Optional[str]) -> list[str]:
    """What differs between Python's leg and `other`: exit code, stdout, store dump and, when `effects` is on, the effects and stderr.
    With `known`, the one difference that divergence names is scrubbed from stdout and the run-log records first."""
    problems: list[str] = []
    if truth.probe.exit_code != other.probe.exit_code:
        problems.append(f"{who}: exit {other.probe.exit_code} vs Python {truth.probe.exit_code}")
    want = matrix.mask(matrix.scrubbed_stdout(known, truth.probe.stdout.decode("utf-8", "backslashreplace")), *cell.masks)
    got = matrix.mask(matrix.scrubbed_stdout(known, other.probe.stdout.decode("utf-8", "backslashreplace")), *cell.masks)
    if want != got:
        problems.append(f"{who}: stdout differs, {_first_difference(want, got)}")
    if truth.dump != other.dump:
        problems.append(f"{who}: store dump differs, {_first_difference(truth.dump, other.dump)}")
    left, right = (truth.effects, other.effects) if known is None else (truth.scrubbed, other.scrubbed)
    if effects and left != right:
        problems.append(f"{who}: effects differ, {_first_difference(left, right)}")
    if effects and truth.probe.stderr != other.probe.stderr:
        problems.append(f"{who}: stderr differs, {truth.probe.stderr[:100]!r} vs {other.probe.stderr[:100]!r}")
    return problems


def _differences(who: str, truth: WriteLeg, other: WriteLeg, cell: matrix.Cell, *, effects: bool = True,
                 known: Optional[str] = None) -> list[str]:
    """The problems of one leg against Python's. A leg that names a known divergence (`matrix.KNOWN_DIVERGENCES`) is allowed to differ
    in that way only, and is a problem the day it does not differ at all: the entry must then be deleted."""
    raw = _compare(who, truth, other, cell, effects, None)
    if known is None:
        return raw
    if not raw:
        return [f"{who}: known divergence {known} did not occur; delete it from KNOWN_DIVERGENCES in _write_matrix.py"]
    return _compare(who, truth, other, cell, effects, known)


def _manifest_problems(cell: matrix.Cell, probe: Probe, manifest: Path, selector: Any, argv: list[str]) -> list[str]:
    """The effects file of the binary alone: empty unless a transition committed, `no effects` for a dry run, else a manifest the
    selector reads."""
    text = manifest.read_bytes().decode("utf-8", "backslashreplace")
    if probe.exit_code != 0:
        return [] if not text else [f"an effects manifest was written by a call that did not commit: {text[:100]!r}"]
    if "--dry-run" in argv:
        try:
            body = json.loads(text)
        except ValueError:
            return [f"the dry run's effects file is not JSON: {text[:100]!r}"]
        return [] if body == {"effects": [], "post": []} else [f"the dry run's effects file names effects: {text[:100]!r}"]
    if cell.task_id is None or selector._read_manifest(str(manifest), cell.task_id) is None:
        return [f"the effects manifest is missing or the selector cannot read it: {text[:100]!r}"]
    return []


def _vector_problem(cell: matrix.Cell, probe: Probe, db: Path) -> Optional[str]:
    """Where Python's own answer to the command line differs from what the vectors recorded (the real TaskStore), else None."""
    expect = cell.expect
    if expect is None:
        return None
    outcome, payload = expect["outcome"], probe.payload
    row, audit = (None, []) if cell.task_id is None else matrix.task_state(db, cell.task_id)
    if payload is None:
        return f"exit {probe.exit_code} without a JSON reply"
    if outcome == "ok":
        if probe.exit_code != 0:
            return f"exit {probe.exit_code}, the vectors say the transition commits"
        assert row is not None
        for name, value in expect["row_diff"].items():
            if name == "updated_at" or (name == "notes" and not cell.strict) or (name not in ("state", "attempt", "rework_loops") and not cell.strict):
                continue
            if name == "notes":
                left, right = json.loads(row["notes"] or "{}"), json.loads(value or "{}")
                left.pop("verdicts_since", None)
                right.pop("verdicts_since", None)
                if left != right:
                    return f"notes {left!r} vs the vectors' {right!r}"
            elif row[name] != value:
                return f"column {name} is {row[name]!r}, the vectors say {value!r}"
        keep = 4 if cell.strict else 2  # from, to, actor, reason; the vector's actor and reason are not the command line's otherwise
        wanted = [(one["from_state"], one["to_state"], one["actor"], one["reason"])[:keep] for one in expect["audit"]]
        found = [(one["from_state"], one["to_state"], one["actor"], one["reason"])[:keep] for one in audit]
        return None if wanted == found else f"audit rows {found!r} vs the vectors' {wanted!r}"
    error = payload.get("error")
    if probe.exit_code != 2 or not isinstance(error, dict):
        return f"exit {probe.exit_code}, the vectors say the call is refused"
    if outcome == "error":
        recorded = expect["error"]
        same = error.get("code") == recorded["code"] and error.get("message") == recorded["message"] \
            and error.get("details") == recorded["details"]
        return None if same else f"error {error.get('code')} {error.get('message')!r} vs the vectors' {recorded['code']} {recorded['message']!r}"
    wanted_message = f"{expect['exception']}: {expect['text']}"
    return None if error.get("message") == wanted_message else f"error {error.get('message')!r} vs the vectors' {wanted_message!r}"


def _run_write_cell(base: Path, cell: matrix.Cell, template: Path, rust_bin: Optional[str], backend: str, sandbox_writes: bool,
                    selector: Any, env_keys: tuple[str, ...]) -> list[Check]:
    """All legs of one cell, each in its own copy of the template store, and the checks they make."""
    if rust_bin is None and cell.expect is None:
        return []  # without the binary only the cells the vectors speak for are run
    sandbox = Sandbox(base / cell.cell_id, cell.cell_id, rust_bin, backend, sandbox_writes)
    try:
        shutil.copyfile(template, sandbox.swarm_dir / "tasks.db")
        if cell.row is not None:
            matrix.insert_row(sandbox.swarm_dir / "tasks.db", cell.row)
        argv = [part.replace("{root}", str(sandbox.workspace)) for part in cell.argv]
        initial = sandbox.snapshot("initial")
        truth = _write_leg(sandbox, cell, argv, initial, "python", cell.lease)
        if rust_bin is None:
            problem = _vector_problem(cell, truth.probe, sandbox.swarm_dir / "tasks.db")
            return [Check(f"cell {cell.cell_id}: Python answers the command line as the vectors recorded", problem is None,
                          problem or f"exit {truth.probe.exit_code}")]
        return _rust_cell_checks(sandbox, cell, argv, initial, truth, selector, env_keys)
    finally:
        shutil.rmtree(sandbox.base, ignore_errors=True)


def _rust_cell_checks(sandbox: Sandbox, cell: matrix.Cell, argv: list[str], initial: Path, truth: WriteLeg, selector: Any,
                      env_keys: tuple[str, ...]) -> list[Check]:
    chosen = _write_leg(sandbox, cell, argv, initial, "selector", cell.lease)
    direct, manifest = _write_direct(sandbox, cell, argv, initial, env_keys)
    # The binary alone runs no lease settlement (the selector replays it), so its oracle is Python with the substrate off.
    oracle = truth if cell.lease is None else _write_leg(sandbox, cell, argv, initial, "python", None)
    refused = _not_started(direct.probe)
    refuses_all = sandbox.backend == "sandbox" and not sandbox.sandbox_writes
    expected = SANDBOX_REFUSES if refuses_all else cell.fallback
    served = f"fallback:{refused}" if refused is not None else "rust"
    problems = [] if (refused is not None) == (expected is not None) else [
        f"the binary {'refused with write_state not_started' if refused else 'answered'}, the matrix expects it to "
        f"{'answer' if expected is None else 'refuse: ' + expected}"]
    known = cell.known if refused is None else None  # a refusal is answered by Python: nothing diverges
    via_script = [*problems, *_differences("selector leg", truth, chosen, cell, known=known)]
    if cell.lease is not None:  # the effects comparison above is not vacuous: Python's own run reached the fake substrate as expected
        seen = truth.effects.count('"tool": "graph_')
        if seen != (cell.settle is not None) or (cell.settle is not None and f'"tool": "{cell.settle}"' not in truth.effects):
            via_script.append(f"Python's run made {seen} substrate call(s), the cell expects {cell.settle or 'none'}")
    if not chosen.started:
        via_script.append("the selector did not start the binary")
    alone = list(problems)
    if refused is not None:
        untouched = matrix.dump_store(initial / "state" / ".swarm" / "tasks.db", *cell.masks)
        if direct.dump != untouched:
            alone.append(f"a refused call changed the store: {_first_difference(untouched, direct.dump)}")
        if direct.probe.exit_code == 0 or manifest.read_bytes():
            alone.append("a refused call exited 0 or wrote an effects manifest")
    else:
        alone += _differences("binary alone", oracle, direct, cell, effects=False)
        alone += _manifest_problems(cell, direct.probe, manifest, selector, argv)
    note = " (sandbox backend: refused before any write, Python answers)" if refuses_all else ""
    divergence = f" [KNOWN DIVERGENCE {known}: only in that way]" if known is not None else ""
    checks = [
        Check(f"cell {cell.cell_id}: GSD_ENGINE=rust prints Python's bytes, store dump and effects{note}{divergence}", not via_script,
              "; ".join(via_script) or f"exit {truth.probe.exit_code}" + (f"; {matrix.KNOWN_DIVERGENCES[known]}" if known else ""), served),
        Check(f"cell {cell.cell_id}: the binary alone {'refuses with write_state not_started' if refused else 'answers as Python does'}"
              f"{note}", not alone, "; ".join(alone) or f"exit {direct.probe.exit_code}", served),
    ]
    if cell.sample:
        default = _write_leg(sandbox, cell, argv, initial, "default", cell.lease)
        script_argv = [*argv, "--root", str(sandbox.workspace), "--json"]
        python_expected = selector.DEFAULT_ENGINE_WRITE == "python" or not selector.rust_serves_write(script_argv)
        defaulted = _differences("default selector", truth, default, cell)
        if default.started == python_expected:
            defaulted.append(f"the default selector {'started' if default.started else 'did not start'} the binary, expected "
                             f"{'Python' if python_expected else 'the binary'}")
        checks.append(Check(f"cell {cell.cell_id}: the default selector (GSD_ENGINE unset) prints Python's bytes, store dump and effects",
                            not defaulted, "; ".join(defaulted) or f"exit {truth.probe.exit_code}", served if default.started else "python"))
    return checks


def run_write_cells(root: Path, cells: list[matrix.Cell], rust_bin: Optional[str], backend: str = "host",
                    sandbox_writes: bool = False) -> list[tuple[matrix.Cell, list[Check]]]:
    """Runs `cells` (WRITE_WORKERS at a time, each in its own directory under `root`) and returns the checks of each, in order. One
    template store is built once and copied for every cell. Without `rust_bin` only the cells the vectors speak for run, on Python."""
    anchor = Sandbox(root / "anchor", "transition_matrix", rust_bin, backend, sandbox_writes)
    selector = _selector(anchor)
    env_keys = tuple(selector.RUST_ENV_KEYS)
    template = root / "template.db"
    with in_process(anchor):
        from swarm.taskstore import TaskStore

        TaskStore(path=template).conn.close()
    done: list[tuple[matrix.Cell, list[Check]]] = []
    with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as pool:
        futures = [(cell, pool.submit(_run_write_cell, root / "cells", cell, template, rust_bin, backend, sandbox_writes, selector, env_keys))
                   for cell in cells]
        for cell, future in futures:
            try:
                done.append((cell, future.result()))
            except RustLegUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - one broken cell must not hide the others
                done.append((cell, [Check(f"cell {cell.cell_id} ran to completion", False, f"{type(exc).__name__}: {exc}")]))
    return done


def case_transition_matrix(sandbox: Sandbox) -> list[Check]:
    """Every cell of the matrix on the Python script and, under --rust, on the binary; without it, Python against the vectors."""
    cells = matrix.load_cells()
    cases = matrix.vector_cases()
    disagreements = matrix.cross_check(cases)
    checks = [
        Check("the derived Rust refusals agree with the vectors' own flags on every manual vector case", not disagreements,
              "; ".join(disagreements[:4]) or f"{len(cases)} vector cases"),
        Check("every vector case is a cell, and the added cells follow them", len(cells) > len(cases) and len({c.cell_id for c in cells}) == len(cells),
              f"{len(cells)} cells"),
    ]
    for _cell, cell_checks in run_write_cells(sandbox.base / "matrix", cells, sandbox.rust_bin, sandbox.backend, sandbox.sandbox_writes):
        checks.extend(cell_checks)
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
    Case("transition_matrix", "orch_status --transition",
         "Every (from, to) pair, rework, claim, gate, state-name and unknown-task vector plus dry-run, id, grammar and lease cells: "
         "the binary leaves Python's stdout, store and effects, or refuses before any write and Python answers.",
         case_transition_matrix),
)


def run_case(case: Case, rust_bin: Optional[str] = None, backend: str = "host", sandbox_writes: bool = False) -> CaseResult:
    """Run one case in a fresh temporary directory, removed afterwards; rust_bin is set under --rust and backend names
    the Rust backend of its Rust-served legs."""
    base = Path(tempfile.mkdtemp(prefix=f"parity-{case.name}-"))
    sandbox: Optional[Sandbox] = None
    try:
        resolved = base.resolve()
        if resolved == REPO or REPO in resolved.parents:
            raise RuntimeError(f"temporary state {resolved} lies inside the repository")
        sandbox = Sandbox(base, case.name, rust_bin, backend, sandbox_writes)
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
    return CaseResult(case.name, case.route, case.description, status, checks, sandbox.pending if sandbox else [])


def render_table(results: list[CaseResult]) -> str:
    lines = [f"{'CASE':<30}{'ROUTE':<28}{'RESULT':<8}CHECKS"]
    for result in results:
        passed = sum(check.ok for check in result.checks)
        lines.append(f"{result.name:<30}{result.route:<28}{result.status:<8}{passed}/{len(result.checks)}")
        lines.extend(f"    FAIL {check.label}: {check.detail}" for check in result.checks if not check.ok)
        lines.extend(f"    PENDING {note}" for note in result.pending)
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


def _require_native_extension() -> None:
    """The selector legs run without SWARM_PURE_PYTHON, so the Python body they fall back to needs swarm._native."""
    env = {key: os.environ[key] for key in HOST_ENV_KEYS if key in os.environ}
    done = subprocess.run([sys.executable, "-c", "import swarm._native"], cwd=REPO, env=env, capture_output=True,
                          timeout=PROCESS_TIMEOUT_S, check=False)
    if done.returncode != 0:
        raise RustLegUnavailable("swarm._native cannot be imported; the selector legs run without SWARM_PURE_PYTHON "
                                 "(build it with `pip install .`)")


def summarize(results: list[CaseResult], *, rust: bool, backend: str) -> dict[str, Any]:
    """The per-check results with the served_by counts, as --json-out writes them."""
    counts: dict[str, int] = {}
    for result in results:
        for check in result.checks:
            counts[check.served_by] = counts.get(check.served_by, 0) + 1
    return {
        "mode": {"rust": rust, "backend": backend if rust else None},
        "cases_passed": sum(result.status == "pass" for result in results),
        "cases_total": len(results),
        "served_by_counts": dict(sorted(counts.items())),
        "pending": [f"{result.name}: {note}" for result in results for note in result.pending],
        "cases": [asdict(result) for result in results],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A1 parity runner: 12 cases (11 read-only, the --transition matrix), Python as truth.")
    parser.add_argument("--json", action="store_true", help="print the case table as JSON")
    parser.add_argument("--rust", action="store_true",
                        help="also compare each orch_status leg with the gsd_engine binary at GSD_ENGINE_BIN")
    parser.add_argument("--backend", choices=BACKENDS, default=None,
                        help="GSD_RUST_BACKEND for the Rust-served legs under --rust (default host)")
    parser.add_argument("--sandbox-write", action="store_true",
                        help="with --backend sandbox: the binary is built with the sandbox-write feature, so the sandbox backend serves "
                             "--transition like the host one (without it every write cell is expected to be refused)")
    parser.add_argument("--json-out", metavar="PATH", default=None,
                        help="write the per-check results and the served_by counts to PATH as one JSON object")
    args = parser.parse_args(argv)
    if args.backend is not None and not args.rust:
        parser.error("--backend applies to the Rust leg: pass --rust")
    if args.sandbox_write and args.backend != "sandbox":
        parser.error("--sandbox-write names a sandbox-backend binary: pass --rust --backend sandbox")
    backend = args.backend or "host"
    try:
        rust_bin = _rust_binary() if args.rust else None
        if args.rust:
            _require_native_extension()
        results = [run_case(case, rust_bin, backend, args.sandbox_write) for case in CASES]
    except RustLegUnavailable as exc:
        print(f"parity: the --rust leg cannot run: {exc}", file=sys.stderr)
        return 1
    if args.json_out is not None:
        try:
            Path(args.json_out).write_text(
                json.dumps(summarize(results, rust=args.rust, backend=backend), indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"parity: cannot write {args.json_out}: {exc.strerror or type(exc).__name__}", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print(render_table(results))
    return 0 if all(result.status == "pass" for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
