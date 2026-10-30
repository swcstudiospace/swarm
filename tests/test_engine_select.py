"""swarm/engine_select.py on its own: engine value parsing, default rows, binary resolution, the environment and core-dump hooks, the
outcome tables of the read and the write flow against fake gsd_engine binaries (with the teeth of the write table), the policy for a
route nobody registered, and the six scripts that carry an inert selector block (orch_plan, orch_assign, qa_gate, rev_gate, sec_gate,
rel_plan) through their real entry points.

The fakes are /bin/sh scripts that count their starts, keep their stdin and answer from files, so no test depends on a timing margin."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import resource
import runpy
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import ROOT

from swarm import engine_select, substrate_tee
from swarm.script_base import AgentScript

ENGINE_SELECT = ROOT / "swarm" / "engine_select.py"
ENGINE_ENV = ("GSD_ENGINE", "GSD_ENGINE_BIN", "GSD_RUST_BACKEND", "GSD_RUST_TIMEOUT_S", "SWARM_PURE_PYTHON", "SWARM_TASK_ID", "SWARM_CORRELATION_ID")
SIX = (("orch_plan", "A01"), ("orch_assign", "A01"), ("qa_gate", "A08"), ("rev_gate", "A09"), ("sec_gate", "A10"), ("rel_plan", "A12"))
SERVED = ["--json"]
WRITE_ARGV = ["--json", "--write"]
OK_REPLY = {"agent": "A99", "script": "demo", "status": "ok", "summary": "answered by the fake engine"}
GRANTS = {"plain": frozenset(), "keyed": frozenset({"DEMO_KEY"})}


def _rendered(reply: dict[str, Any]) -> bytes:
    return (json.dumps(reply, indent=2) + "\n").encode()


def _error(code: str | None, write_state: str | None = None, task_id: str | None = None) -> bytes:
    details = {key: value for key, value in (("write_state", write_state), ("task_id", task_id)) if value is not None}
    error: dict[str, Any] = {"message": "canned", "details": details, "handling": "none"}
    if code is not None:
        error["code"] = code
    return _rendered({"agent": "A99", "script": "demo", "status": "error", "error": error})


@dataclass
class Fake:
    """A fake gsd_engine: `path` is the executable, `folder` holds what it kept."""
    path: Path
    folder: Path

    @property
    def starts(self) -> int:
        marker = self.folder / "starts"
        return len(marker.read_text().splitlines()) if marker.exists() else 0

    def saw(self, name: str) -> str:
        return (self.folder / name).read_text(encoding="utf-8")


def build_fake(folder: Path, *, stdout: bytes = b"", exit_code: int = 0, tail: str | None = None, executable: bool = True) -> Fake:
    """A /bin/sh gsd_engine that appends to `starts`, saves its stdin, then prints `stdout` and exits with `exit_code`, or runs `tail`."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "reply").write_bytes(stdout)
    body = tail if tail is not None else f"cat {shlex.quote(str(folder / 'reply'))}\nexit {exit_code}"
    path = folder / "gsd_engine"
    path.write_text(f"#!/bin/sh\necho started >> {shlex.quote(str(folder / 'starts'))}\ncat > {shlex.quote(str(folder / 'stdin.json'))}\n{body}\n",
                    encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return Fake(path, folder)


@pytest.fixture(autouse=True)
def clean_engine_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No engine variable of the host, a private swarm directory and working directory, and no tee to a substrate."""
    for key in ENGINE_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(substrate_tee, "tee", lambda record, root=None: None)
    monkeypatch.setattr(engine_select, "RELEASE_BINARY", tmp_path / "no-release" / "gsd_engine")


def _script() -> AgentScript:
    return AgentScript("A99", "demo", lambda args, ctx: {"status": "ok"}, add_args=lambda parser: parser.add_argument("--write", action="store_true"))


class Demo:
    """A selector over `module` (the real engine_select or a mutated copy of it) with a reading and a writing route, and what its hooks saw."""

    def __init__(self, module: ModuleType, *, read_default: str = "rust", write_default: str = "rust") -> None:
        self.defaults = {"read": read_default, "write": write_default}
        self.committed: list[tuple[Any, ...]] = []
        self.before_calls: list[Any] = []
        self.env_routes: list[str] = []
        self.guard_routes: list[str] = []
        self.module = module
        write = module.WriteFlow(before=self._before, commit=self._commit, unknown=lambda call: ("Check the demo.", {"item": "x"}), preexec=self._preexec)
        routes = (
            module.Route(claims=lambda argv: "--write" in argv, parse=lambda argv: argv if "--json" in argv else None,
                         default=lambda: self.defaults["write"], env_route=self._env_route, write=write),
            module.Route(claims=lambda argv: True, parse=lambda argv: argv if "--json" in argv else None,
                         default=lambda: self.defaults["read"], env_route=self._env_route),
        )
        self.selector = module.Selector(_script(), routes, ("SWARM_DIR",), self._binary_env)

    def _before(self, call: Any) -> str:
        self.before_calls.append(call)
        return "before-state"

    def _commit(self, call: Any, before: Any, effects_path: str, reply: dict[str, Any], stdout: bytes) -> tuple[dict[str, Any], bytes]:
        self.committed.append((call, before, effects_path, reply, stdout))
        return {**reply, "committed": True}, b"COMMITTED\n"

    def _preexec(self, route: str) -> Callable[[], None] | None:
        self.guard_routes.append(route)
        return self.module.no_core_dumps if route == "keyed" else None

    def _env_route(self, argv: list[str], call: Any) -> str:
        route = "keyed" if "--keyed" in argv else "plain"
        self.env_routes.append(route)
        return route

    @staticmethod
    def _binary_env(route: str) -> dict[str, str]:
        names = engine_select.BINARY_ENV_NAMES | GRANTS[route]
        return {key: value for key, value in os.environ.items() if key in names}

    def select(self, argv: list[str], **kwargs: Any) -> tuple[int | None, bytes]:
        """`select_engine` with stdout captured as bytes (the flows write to `sys.stdout.buffer` and print)."""
        sink = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with contextlib.redirect_stdout(sink):
            code = self.module.select_engine(self.selector, argv, **kwargs)
        sink.flush()
        return code, sink.buffer.getvalue()  # type: ignore[attr-defined]


def _records(folder: Path) -> list[str]:
    log = folder / ".swarm" / "events.jsonl"
    return [json.loads(line)["type"] for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.exists() else []


def _body(out: bytes) -> dict[str, Any]:
    return json.loads(out.decode("utf-8"))  # type: ignore[no-any-return]


# --- engine value parsing and default rows -----------------------------------------------------------------------------------------

def test_the_python_values_never_start_the_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", stdout=_rendered(OK_REPLY))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    demo = Demo(engine_select)
    for argv in (SERVED, WRITE_ARGV):
        monkeypatch.setenv("GSD_ENGINE", "python")
        assert demo.select(argv) == (None, b"")
        monkeypatch.setenv("SWARM_PURE_PYTHON", "1")
        monkeypatch.setenv("GSD_ENGINE", "rust")
        assert demo.select(argv) == (None, b""), "SWARM_PURE_PYTHON=1 beats an explicit rust"
        monkeypatch.setenv("GSD_ENGINE", "nonsense")
        assert demo.select(argv) == (None, b""), "and a bad value is not even read"
        monkeypatch.delenv("SWARM_PURE_PYTHON")
    assert fake.starts == 0


@pytest.mark.parametrize("value", ["Rust", "PYTHON", " rust", "rust ", "1", "wasm-secret-value"])
def test_any_other_engine_value_is_refused_with_e_input_and_is_not_echoed(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GSD_ENGINE", value)
    code, out = Demo(engine_select).select(SERVED)
    body = _body(out)
    assert code == 2 and (body["agent"], body["script"], body["status"]) == ("A99", "demo", "error")
    assert body["error"]["code"] == "E-INPUT" and body["error"]["message"] == "GSD_ENGINE must be 'python' or 'rust'"
    assert value not in body["error"]["message"]


@pytest.mark.parametrize(("read_default", "write_default", "argv", "starts"), [
    ("python", "python", SERVED, 0),
    ("rust", "python", SERVED, 1),
    ("rust", "python", WRITE_ARGV, 0),
    ("python", "rust", WRITE_ARGV, 1),
    ("python", "rust", SERVED, 0),
    ("rust", "rust", ["--text"], 0),
    ("rust", "rust", ["--write"], 0),
], ids=["both-python", "read-rust", "write-python-row", "write-rust", "read-python-row", "unserved-read", "unserved-write"])
def test_an_unset_engine_follows_the_default_row_of_the_route_that_claims_the_argv(
        read_default: str, write_default: str, argv: list[str], starts: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", stdout=_error("E-DEP", "not_started"), exit_code=2)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    for empty in (None, ""):
        if empty is not None:
            monkeypatch.setenv("GSD_ENGINE", empty)
        assert Demo(engine_select, read_default=read_default, write_default=write_default).select(argv)[0] is None
    assert fake.starts == 2 * starts


def test_a_default_row_is_read_when_the_call_is_resolved_not_when_the_route_is_built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", stdout=_error("E-DEP", "not_started"), exit_code=2)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    demo = Demo(engine_select, read_default="python")
    demo.select(SERVED)
    assert fake.starts == 0
    demo.defaults["read"] = "rust"
    demo.select(SERVED)
    assert fake.starts == 1


def test_a_default_call_starts_the_binary_only_for_an_argv_the_route_serves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", stdout=_rendered(OK_REPLY))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    demo = Demo(engine_select)
    assert demo.select(["--text"])[0] is None and fake.starts == 0
    assert demo.select(SERVED)[0] == 0 and fake.starts == 1


# --- binary resolution --------------------------------------------------------------------------------------------------------------

def test_gsd_engine_bin_wins_over_the_release_binary_and_the_release_binary_stands_in_when_it_is_unset(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    named = build_fake(tmp_path / "named", stdout=_rendered(OK_REPLY))
    release = build_fake(tmp_path / "release", stdout=_rendered(OK_REPLY))
    demo = Demo(engine_select)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(named.path))
    assert demo.select(SERVED, release_binary=release.path)[0] == 0
    assert (named.starts, release.starts) == (1, 0)
    monkeypatch.delenv("GSD_ENGINE_BIN")
    assert demo.select(SERVED, release_binary=release.path)[0] == 0
    assert (named.starts, release.starts) == (1, 1)
    monkeypatch.setattr(engine_select, "RELEASE_BINARY", release.path)
    assert demo.select(SERVED)[0] == 0 and release.starts == 2, "without an argument the module's name is read when the call runs"


def test_a_named_binary_that_is_not_executable_is_not_replaced_by_the_release_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = build_fake(tmp_path / "plain", stdout=_rendered(OK_REPLY), executable=False)
    release = build_fake(tmp_path / "release", stdout=_rendered(OK_REPLY))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(plain.path))
    demo = Demo(engine_select)
    assert demo.select(SERVED, release_binary=release.path) == (None, b"")
    monkeypatch.setenv("GSD_ENGINE", "rust")
    code, out = demo.select(SERVED, release_binary=release.path)
    assert (code, _body(out)["error"]["code"], plain.starts, release.starts) == (2, "E-DEP", 0, 0)


def test_an_explicit_rust_without_any_binary_is_e_dep_and_a_bad_backend_is_e_input_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GSD_ENGINE", "rust")
    demo = Demo(engine_select)
    code, out = demo.select(SERVED)
    assert code == 2 and _body(out)["error"]["code"] == "E-DEP"
    assert _body(out)["error"]["message"] == "GSD_ENGINE=rust needs GSD_ENGINE_BIN set to the gsd_engine binary"
    monkeypatch.setenv("GSD_RUST_BACKEND", "gpu")
    code, out = demo.select(SERVED)
    assert (code, _body(out)["error"]["code"], _body(out)["error"]["message"]) == (2, "E-INPUT", "GSD_RUST_BACKEND must be 'host' or 'sandbox' when set")


@pytest.mark.parametrize("backend", [None, "host", "sandbox"])
def test_the_known_backends_pass_and_reach_the_binary_environment(backend: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "fake"
    dump = f"env > {shlex.quote(str(folder / 'env.txt'))}\ncat {shlex.quote(str(folder / 'reply'))}"
    fake = build_fake(folder, stdout=_rendered(OK_REPLY), tail=dump)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    if backend is not None:
        monkeypatch.setenv("GSD_RUST_BACKEND", backend)
    assert Demo(engine_select).select(SERVED)[0] == 0
    assert ("GSD_RUST_BACKEND=" + str(backend) in fake.saw("env.txt").splitlines()) is (backend is not None)


@pytest.mark.parametrize(("raw", "seconds"), [("", 660), ("0", 660), ("-5", 660), ("1.5", 660), ("abc", 660), ("٣", 660), ("7", 7), ("123", 123)])
def test_the_outer_timeout_is_a_positive_ascii_integer_or_the_default(raw: str, seconds: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GSD_RUST_TIMEOUT_S", raw)
    assert engine_select.rust_timeout_s() == seconds
    assert engine_select.RUST_CALL_TIMEOUT_S == 660


def test_a_binary_that_does_not_answer_in_time_is_e_timeout_for_a_read_and_an_unknown_outcome_for_a_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", tail="exec sleep 120")  # exec: the process that is killed is the one holding the pipe
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_RUST_TIMEOUT_S", "1")
    for explicit in (False, True):
        if explicit:
            monkeypatch.setenv("GSD_ENGINE", "rust")
        code, out = Demo(engine_select).select(SERVED)
        assert (code, _body(out)["error"]["code"]) == (2, "E-TIMEOUT")
        code, out = Demo(engine_select).select(WRITE_ARGV)
        error = _body(out)["error"]
        assert (code, error["code"], error["details"]["write_state"]) == (2, "E-INTERNAL", "unknown")
        assert "did not answer within the call deadline" in error["message"] and "NOT repeated" in error["message"]


def test_a_binary_that_cannot_be_started_is_python_by_default_and_e_dep_when_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = tmp_path / "broken"
    broken.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    broken.chmod(0o755)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(broken))
    for argv in (SERVED, WRITE_ARGV):
        assert Demo(engine_select).select(argv) == (None, b"")
    monkeypatch.setenv("GSD_ENGINE", "rust")
    for argv in (SERVED, WRITE_ARGV):
        code, out = Demo(engine_select).select(argv)
        assert (code, _body(out)["error"]["code"]) == (2, "E-DEP")


def test_an_argument_the_binary_cannot_take_is_python_s_in_every_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", stdout=_rendered(OK_REPLY))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    lone_surrogate = ["--json", "\udcff"]
    for engine in (None, "rust"):
        if engine:
            monkeypatch.setenv("GSD_ENGINE", engine)
        assert Demo(engine_select).select(lone_surrogate) == (None, b"")
        assert Demo(engine_select).select([*lone_surrogate, "--write"]) == (None, b"")
    assert fake.starts == 0


# --- the environment and the child guard, by route --------------------------------------------------------------------------------

SHELL_ADDS = {"PWD", "OLDPWD", "SHLVL", "_"}


@pytest.mark.parametrize("flow", ["read", "write"])
@pytest.mark.parametrize(("flag", "route", "granted"), [([], "plain", False), (["--keyed"], "keyed", True)])
def test_the_child_environment_is_the_allowlist_plus_the_grant_of_the_route_the_hook_named(
        flow: str, flag: list[str], route: str, granted: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "fake"
    fake = build_fake(folder, stdout=_rendered(OK_REPLY), tail=f"env > {shlex.quote(str(folder / 'env.txt'))}\ncat {shlex.quote(str(folder / 'reply'))}")
    for name, value in {"DEMO_KEY": "k", "SWARM_FUTURE_KEY": "f", "GSD_FUTURE": "g", "SWARM_SIGNING_KEY": "s", "HOME": "/h", "LANG": "C"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GSD_RUST_BACKEND", "host")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    demo = Demo(engine_select)

    code, _ = demo.select([*SERVED, *flag, *(["--write"] if flow == "write" else [])])

    assert code in (0, None) and fake.starts == 1
    seen = dict(line.split("=", 1) for line in fake.saw("env.txt").splitlines() if "=" in line and line.split("=", 1)[0] not in SHELL_ADDS)
    expected = {key for key in os.environ if key in engine_select.BINARY_ENV_NAMES | GRANTS[route]}
    assert set(seen) == expected and ("DEMO_KEY" in seen) is granted
    assert seen["GSD_RUST_BACKEND"] == "host" and seen["HOME"] == "/h" and seen["LANG"] == "C"
    assert not {"SWARM_FUTURE_KEY", "GSD_FUTURE", "SWARM_SIGNING_KEY", "SWARM_DIR"} & set(seen), "no prefix, no unlisted name"
    assert set(demo.env_routes) == {route}


def test_the_base_allowlist_is_what_a_process_needs_plus_the_one_variable_the_binary_reads() -> None:
    assert engine_select.BINARY_ENV_NAMES == frozenset({"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "GSD_RUST_BACKEND"})


def test_a_route_without_a_grant_table_entry_is_a_key_error_never_a_silent_run_without_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", stdout=_rendered(OK_REPLY))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    selector = engine_select.Selector(_script(), Demo(engine_select).selector.routes)  # the default binary_env: no route is known
    with pytest.raises(KeyError):
        engine_select.select_engine(selector, SERVED)
    assert fake.starts == 0


def test_the_write_child_of_a_keyed_route_starts_with_core_dumps_off_and_the_parent_keeps_its_limit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "fake"
    fake = build_fake(folder, stdout=_rendered(OK_REPLY), tail=f"ulimit -c > {shlex.quote(str(folder / 'core.txt'))}\ncat {shlex.quote(str(folder / 'reply'))}")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    baseline = subprocess.run(["sh", "-c", "ulimit -c"], capture_output=True, text=True, check=True).stdout.strip()
    parent = resource.getrlimit(resource.RLIMIT_CORE)
    demo = Demo(engine_select)

    demo.select([*WRITE_ARGV, "--keyed"])
    assert fake.saw("core.txt").strip() == "0"
    demo.select(WRITE_ARGV)
    assert fake.saw("core.txt").strip() == baseline
    assert resource.getrlimit(resource.RLIMIT_CORE) == parent
    assert demo.guard_routes == ["keyed", "plain"]


def test_a_child_that_cannot_be_given_the_core_limit_was_never_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", stdout=_error("E-DEP", "not_started"), exit_code=2)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))

    def refuse() -> None:
        raise OSError("no limits for you")

    monkeypatch.setattr(engine_select, "no_core_dumps", refuse)  # the demo reads the name when its hook runs
    assert Demo(engine_select).select([*WRITE_ARGV, "--keyed"]) == (None, b"")
    monkeypatch.setenv("GSD_ENGINE", "rust")
    code, out = Demo(engine_select).select([*WRITE_ARGV, "--keyed"])
    assert (code, _body(out)["error"]["code"], fake.starts) == (2, "E-DEP", 0)


# --- the outcome tables ----------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    name: str
    stdout: bytes
    exit_code: int
    kind: str  # commit | python | final | unknown (write); answer | python | refusal (read)
    tail: str | None = None


WRITE_CASES = (
    Case("committed", _rendered(OK_REPLY), 0, "commit"),
    Case("committed with a failing verdict", _rendered({**OK_REPLY, "status": "fail"}), 1, "commit"),
    Case("not started E-DEP", _error("E-DEP", "not_started"), 2, "python"),
    Case("not started E-INTERNAL", _error("E-INTERNAL", "not_started"), 2, "python"),
    Case("not started E-INPUT", _error("E-INPUT", "not_started"), 2, "python"),
    Case("not started E-CAPACITY", _error("E-CAPACITY", "not_started"), 2, "python"),
    Case("final E-CONTRACT", _error("E-CONTRACT", "final"), 2, "final"),
    Case("final E-INTERNAL", _error("E-INTERNAL", "final"), 2, "final"),
    Case("no write_state E-POLICY", _error("E-POLICY"), 2, "final"),
    Case("no write_state E-INPUT", _error("E-INPUT"), 2, "final"),
    Case("E-TIMEOUT that names its task", _error("E-TIMEOUT", None, "T-1"), 2, "final"),
    Case("E-TIMEOUT without a task", _error("E-TIMEOUT"), 2, "unknown"),
    Case("E-INTERNAL without a write_state", _error("E-INTERNAL"), 2, "unknown"),
    Case("an unrecognised write_state", _error("E-POLICY", "maybe"), 2, "unknown"),
    Case("an unrecognised write_state on E-DEP", _error("E-DEP", "unknown"), 2, "unknown"),
    Case("exit 2 without a typed code", _error(None), 2, "unknown"),
    Case("exit 3 with a JSON object", _rendered(OK_REPLY), 3, "unknown"),
    Case("not JSON", b"not json\n", 0, "unknown"),
    Case("JSON that is not an object", b"[1]\n", 0, "unknown"),
    Case("no output", b"", 0, "unknown"),
    Case("killed by a signal", b"", 0, "unknown", tail="kill -9 $$"),
)
READ_CASES = (
    Case("answered", _rendered(OK_REPLY), 0, "answer"),
    Case("answered with a failing verdict", _rendered({**OK_REPLY, "status": "fail"}), 1, "answer"),
    *(Case(f"{code} falls back", _error(code), 2, "python") for code in ("E-INTERNAL", "E-DEP", "E-CAPACITY", "E-CONTRACT", "E-INPUT")),
    Case("E-POLICY is the binary's own refusal", _error("E-POLICY"), 2, "refusal"),
    Case("exit 2 without a typed code falls back", _error(None), 2, "python"),
    Case("not JSON falls back", b"not json\n", 0, "python"),
    Case("exit 3 falls back", _rendered(OK_REPLY), 3, "python"),
    Case("a signal falls back", b"", 0, "python", tail="kill -9 $$"),
)


def check_write_case(module: ModuleType, case: Case, explicit: bool, folder: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the write flow of `module` did for `case` that the outcome table does not allow; an empty list when it kept the table."""
    fake = build_fake(folder, stdout=case.stdout, exit_code=case.exit_code, tail=case.tail)
    monkeypatch.setenv("SWARM_DIR", str(folder / ".swarm"))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    if explicit:
        monkeypatch.setenv("GSD_ENGINE", "rust")
    else:
        monkeypatch.delenv("GSD_ENGINE", raising=False)
    demo = Demo(module)
    code, out = demo.select(WRITE_ARGV)
    records = _records(folder)
    wrong: list[str] = []
    if fake.starts != 1:
        wrong.append(f"the binary started {fake.starts} times")
    match case.kind:
        case "commit":
            if (code, out, len(demo.committed), records) != (case.exit_code, b"COMMITTED\n", 1, ["script.demo"]):
                wrong.append(f"a committed answer ended as {code!r}, {out!r}, {len(demo.committed)} commits, {records}")
            elif not Path(demo.committed[0][2]).name.startswith("gsd-effects-") or Path(demo.committed[0][2]).exists() or demo.committed[0][1] != "before-state":
                wrong.append("the effects file was not the selector's own, or was left behind, or the state read before was not handed on")
        case "python":
            if (code, out, demo.committed, records) != (None, b"", [], []):
                wrong.append(f"a refusal before the first write was not left to Python: {code!r}, {out!r}, {records}")
        case "final":
            if (code, out, demo.committed, records) != (2, case.stdout, [], ["script.demo.error"]):
                wrong.append(f"the binary's own refusal was not printed as it is and recorded: {code!r}, {out!r}, {records}")
        case "unknown":
            error = _body(out)["error"] if code == 2 and out.startswith(b"{") else {}
            if (code, error.get("code"), error.get("details"), demo.committed, records) != (
                    2, "E-INTERNAL", {"write_state": "unknown", "item": "x"}, [], ["script.demo.error"]):
                wrong.append(f"an unknown outcome was not refused as E-INTERNAL, not repeated and recorded: {code!r}, {out!r}, {records}")
            elif not str(error.get("message", "")).startswith("outcome unknown: ") or not str(error["message"]).endswith("the call was NOT repeated. Check the demo."):
                wrong.append(f"the unknown-outcome message is wrong: {error.get('message')!r}")
    return wrong


def check_read_case(module: ModuleType, case: Case, explicit: bool, folder: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    fake = build_fake(folder, stdout=case.stdout, exit_code=case.exit_code, tail=case.tail)
    monkeypatch.setenv("SWARM_DIR", str(folder / ".swarm"))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    if explicit:
        monkeypatch.setenv("GSD_ENGINE", "rust")
    else:
        monkeypatch.delenv("GSD_ENGINE", raising=False)
    code, out = Demo(module).select(SERVED)
    records = _records(folder)
    wrong: list[str] = []
    if fake.starts != 1:
        wrong.append(f"the binary started {fake.starts} times")
    if "effects_path" in fake.saw("stdin.json"):
        wrong.append("a read request carries an effects file")
    match case.kind:
        case "answer":
            if (code, out, records) != (case.exit_code, case.stdout, ["script.demo"]):
                wrong.append(f"an answer was not recorded and printed byte for byte: {code!r}, {out!r}, {records}")
        case "python":
            if (code, out, records) != (None, b"", []):
                wrong.append(f"the call was not left to Python: {code!r}, {out!r}, {records}")
        case "refusal":
            if (code, out, records) != (2, case.stdout, ["script.demo.error"]):
                wrong.append(f"the binary's refusal was not printed as it is and recorded: {code!r}, {out!r}, {records}")
    return wrong


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
@pytest.mark.parametrize("case", WRITE_CASES, ids=[case.name for case in WRITE_CASES])
def test_the_write_outcome_table(case: Case, explicit: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert check_write_case(engine_select, case, explicit, tmp_path / "case", monkeypatch) == []


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
@pytest.mark.parametrize("case", READ_CASES, ids=[case.name for case in READ_CASES])
def test_the_read_outcome_table(case: Case, explicit: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert check_read_case(engine_select, case, explicit, tmp_path / "case", monkeypatch) == []


def test_the_effects_file_is_the_selector_s_own_and_the_request_names_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "fake"
    probe = (f"sed -n 's/.*\"effects_path\": \"\\([^\"]*\\)\".*/\\1/p' {shlex.quote(str(folder / 'stdin.json'))} > {shlex.quote(str(folder / 'path.txt'))}\n"
             f"test -f \"$(cat {shlex.quote(str(folder / 'path.txt'))})\" && echo yes > {shlex.quote(str(folder / 'exists.txt'))}\n"
             f"cat {shlex.quote(str(folder / 'reply'))}")
    fake = build_fake(folder, stdout=_rendered(OK_REPLY), tail=probe)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    demo = Demo(engine_select)
    assert demo.select(WRITE_ARGV)[0] == 0
    sent = json.loads(fake.saw("stdin.json"))
    assert sent == {"agent": "A99", "script": "demo", "argv": WRITE_ARGV, "root": os.getcwd(), "env": {"SWARM_DIR": str(tmp_path / ".swarm")},
                    "effects_path": fake.saw("path.txt").strip()}
    assert fake.saw("exists.txt").strip() == "yes" and not Path(sent["effects_path"]).exists()


def mutate(tmp_path: Path, old: str, new: str, name: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """A copy of swarm/engine_select.py with the one statement `old` changed to `new`, loaded under `name`."""
    source = ENGINE_SELECT.read_text(encoding="utf-8")
    assert source.count(old) == 1, f"the statement this mutation changes moved or is not unique: {old!r}"
    path = tmp_path / f"{name}.py"
    path.write_text(source.replace(old, new), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)  # a dataclass looks its module up by name
    spec.loader.exec_module(module)
    return module


NOT_STARTED_LINE = 'if state == "not_started":'
MUTATIONS = {
    # the write table: each row is a statement of run_write's table turned wrong, and the case that has to notice
    "not_started no longer falls back to Python": (NOT_STARTED_LINE, "if False:", "write", "not started E-DEP"),
    "final is no longer final": ('state not in (None, "final")', 'state not in (None,)', "write", "final E-CONTRACT"),
    "an unrecognised write_state is accepted": ('state not in (None, "final")', 'state not in (None, "final", "maybe")', "write",
                                                "an unrecognised write_state"),
    "E-INTERNAL without a write_state is accepted": ("(state is None and failure == ErrorCode.E_INTERNAL.value)", "False", "write",
                                                      "E-INTERNAL without a write_state"),
    "an untyped refusal is accepted": ("if failure is None or state", "if state", "write", "exit 2 without a typed code"),
    "an E-TIMEOUT without a task is accepted": ('failure == ErrorCode.E_TIMEOUT.value and not isinstance(details.get("task_id"), str)', "False",
                                                 "write", "E-TIMEOUT without a task"),
    "an exit code outside 0, 1, 2 is accepted": ("if code not in (AgentScript.EXIT_OK, AgentScript.EXIT_FAIL, AgentScript.EXIT_ERROR):\n        how",
                                                 "if False:\n        how", "write", "exit 3 with a JSON object"),
    "an unparsable reply is a refusal, not an unknown outcome": (
        'return unknown_outcome(selector, route, argv, call, "the gsd_engine binary printed no JSON object")', "return None", "write", "not JSON"),
    "an unstartable binary falls back under explicit": ("        if not explicit:\n            return None  # fallback: nothing was started",
                                                        "        if True:\n            return None  # fallback: nothing was started", "unstartable", ""),
    # the read table
    "a fallback code is printed instead of left to Python": ("if code is None or code in RUST_FALLBACK_CODES:", "if code is None:", "read",
                                                              "E-DEP falls back"),
    "an untyped exit-2 reply is printed": ("if code is None or code in RUST_FALLBACK_CODES:", "if code in RUST_FALLBACK_CODES:", "read",
                                           "exit 2 without a typed code falls back"),
    "an unparsable read reply is not a fallback": ("    reply = reply_object(done.stdout)\n    if reply is None:\n        return None\n    if done.returncode not in",
                                                  "    reply = reply_object(done.stdout) or {}\n    if done.returncode not in", "read", "not JSON falls back"),
}


@pytest.mark.parametrize("label", MUTATIONS)
def test_the_outcome_table_checks_have_teeth(label: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each mutation of the tables above changes the flow of a copy of the module; the named case, which passes on the real module, must fail."""
    old, new, flow, case_name = MUTATIONS[label]
    module = mutate(tmp_path, old, new, "engine_select_mutant", monkeypatch)
    if flow == "unstartable":
        broken = tmp_path / "broken"
        broken.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
        broken.chmod(0o755)
        monkeypatch.setenv("GSD_ENGINE_BIN", str(broken))
        monkeypatch.setenv("GSD_ENGINE", "rust")
        code, _ = Demo(module).select(WRITE_ARGV)
        assert code is None, f"nothing noticed: {label}"
        return
    table, check = (WRITE_CASES, check_write_case) if flow == "write" else (READ_CASES, check_read_case)
    case = next(candidate for candidate in table if candidate.name == case_name)
    failures = [wrong for explicit in (False, True) for wrong in check(module, case, explicit, tmp_path / f"case-{explicit}", monkeypatch)]
    assert failures, f"nothing noticed: {label}"


def test_a_binary_that_cannot_start_under_explicit_is_e_dep_on_the_real_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control of the `unstartable` mutation: the real module answers E-DEP where the mutant answers None."""
    broken = tmp_path / "broken"
    broken.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    broken.chmod(0o755)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(broken))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    code, out = Demo(engine_select).select(WRITE_ARGV)
    assert (code, _body(out)["error"]["code"]) == (2, "E-DEP")


# --- a route nobody registered ----------------------------------------------------------------------------------------------------

def _unregistered() -> engine_select.Selector:
    return engine_select.Selector(_script(), ())


def _run(selector: engine_select.Selector, argv: list[str]) -> tuple[int | None, bytes]:
    sink = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    with contextlib.redirect_stdout(sink):
        code = engine_select.select_engine(selector, argv)
    sink.flush()
    return code, sink.buffer.getvalue()  # type: ignore[attr-defined]


def test_a_route_nobody_registered_is_python_whatever_the_engine_and_never_starts_the_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", stdout=_error("E-DEP", "not_started"), exit_code=2)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    for engine in (None, "", "python", "rust"):
        if engine is not None:
            monkeypatch.setenv("GSD_ENGINE", engine)
        for argv in (SERVED, WRITE_ARGV, [], ["--ingest", "f"]):
            assert _run(_unregistered(), argv) == (None, b""), (engine, argv)
    assert fake.starts == 0


def test_a_selector_with_routes_treats_an_argv_none_of_them_claims_as_unregistered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", stdout=_rendered(OK_REPLY))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    write_only = Demo(engine_select)
    selector = engine_select.Selector(_script(), write_only.selector.routes[:1], binary_env=write_only._binary_env)
    assert _run(selector, ["--json"]) == (None, b"") and fake.starts == 0
    assert _run(selector, WRITE_ARGV)[0] == 0 and fake.starts == 1


def test_an_unregistered_route_keeps_the_refusals_an_explicit_rust_meets_before_the_binary_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GSD_ENGINE", "rust")
    code, out = _run(_unregistered(), SERVED)
    assert (code, _body(out)["error"]["code"]) == (2, "E-DEP")
    monkeypatch.setenv("GSD_RUST_BACKEND", "gpu")
    code, out = _run(_unregistered(), SERVED)
    assert (code, _body(out)["error"]["code"]) == (2, "E-INPUT")
    monkeypatch.delenv("GSD_RUST_BACKEND")
    plain = build_fake(tmp_path / "plain", executable=False)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(plain.path))
    code, out = _run(_unregistered(), SERVED)
    assert (code, _body(out)["error"]["code"]) == (2, "E-DEP") and "could not be started" in _body(out)["error"]["message"]
    monkeypatch.setenv("GSD_ENGINE", "oops")
    code, out = _run(_unregistered(), SERVED)
    assert (code, _body(out)["error"]["message"]) == (2, "GSD_ENGINE must be 'python' or 'rust'")
    monkeypatch.setenv("SWARM_PURE_PYTHON", "1")
    assert _run(_unregistered(), SERVED) == (None, b"")


# --- orch_status: the selector moved here without changing what is importable from it -----------------------------------------------

ORCH_STATUS = ROOT / "scripts" / "orch_status.py"
STILL_IMPORTED = ("RUST_FALLBACK_CODES", "RUST_ENV_KEYS", "RUST_CALL_TIMEOUT_S", "DEFAULT_ENGINE", "DEFAULT_ENGINE_WRITE", "_binary_env", "ROUTE_KEY_ENV",
                  "APPROVED_KEY_ENV", "SIGNING_KEY_ENV", "BINARY_ENV_NAMES", "RELEASE_BINARY", "READ_VALUE_FLAGS", "WRITE_VALUE_FLAGS", "POST_KINDS",
                  "rust_serves", "rust_serves_write", "parse_write_argv", "WriteCall", "Manifest", "_read_manifest", "_state_before", "_replay", "run_rust",
                  "run_rust_write", "select_engine", "_rust_timeout_s", "_no_core_dumps", "SCRIPT", "SELECTOR", "finish_transition", "ingest")


@pytest.fixture
def orch_status() -> ModuleType:
    spec = importlib.util.spec_from_file_location("orch_status_under_engine_select_test", ORCH_STATUS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_orch_status_still_exports_what_tests_the_harness_and_the_docs_import_from_it(orch_status: ModuleType) -> None:
    assert [name for name in STILL_IMPORTED if not hasattr(orch_status, name)] == []
    assert orch_status.RUST_FALLBACK_CODES is engine_select.RUST_FALLBACK_CODES
    assert orch_status.BINARY_ENV_NAMES is engine_select.BINARY_ENV_NAMES
    assert orch_status.SELECTOR.script is orch_status.SCRIPT
    assert orch_status.SELECTOR.routes == (orch_status.WRITE_ROUTE, orch_status.READ_ROUTE)
    assert [route.write is not None for route in orch_status.SELECTOR.routes] == [True, False], "the transition route claims first, the read route is the rest"


def test_orch_status_resolves_its_default_rows_and_its_release_binary_from_its_own_module_at_call_time(
        orch_status: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = build_fake(tmp_path / "fake", stdout=_error("E-DEP", "not_started"), exit_code=2)
    monkeypatch.setattr(orch_status, "RELEASE_BINARY", fake.path)
    assert orch_status.select_engine(SERVED) is None and fake.starts == 0
    monkeypatch.setattr(orch_status, "DEFAULT_ENGINE", "rust")
    assert orch_status.select_engine(SERVED) is None and fake.starts == 1
    assert orch_status.select_engine(["--json", "--transition", "T-1", "CANCELLED"]) is None and fake.starts == 1
    monkeypatch.setattr(orch_status, "DEFAULT_ENGINE_WRITE", "rust")
    assert orch_status.select_engine(["--json", "--transition", "T-1", "CANCELLED"]) is None and fake.starts == 2


# --- the six scripts with an inert selector block ---------------------------------------------------------------------------------

DRY = ["--dry-run", "--json"]


def run_entry(name: str, argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any], list[Any]]:
    """The script's real `__main__` block, in this process: its exit code, the JSON it printed and the subprocesses it started."""
    started: list[Any] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def counting_run(*args: Any, **kwargs: Any) -> Any:
        started.append(args)
        return real_run(*args, **kwargs)

    class CountingPopen(real_popen):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            started.append(args)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    monkeypatch.setattr(subprocess, "Popen", CountingPopen)
    monkeypatch.setattr(sys, "argv", [name, *argv])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(ROOT / "scripts" / f"{name}.py"), run_name="__main__")
    out = capsys.readouterr().out
    return int(exit_info.value.code or 0), json.loads(out[out.index("{"):]), started


def _gist(body: dict[str, Any]) -> tuple[Any, ...]:
    return body.get("status"), body.get("verdict"), (body.get("error") or {}).get("code")


@pytest.mark.parametrize(("name", "agent"), SIX)
def test_without_gsd_engine_or_with_python_the_script_answers_as_before_and_starts_nothing(
        name: str, agent: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    baseline_code, baseline, spawned = run_entry(name, DRY, monkeypatch, capsys)
    assert spawned == [], "a dry run of the script starts no process of its own"
    for value in (None, "", "python"):
        if value is not None:
            monkeypatch.setenv("GSD_ENGINE", value)
        code, body, spawned = run_entry(name, DRY, monkeypatch, capsys)
        assert (code, _gist(body), spawned) == (baseline_code, _gist(baseline), []), value
        assert body.get("agent", agent) == agent


@pytest.mark.parametrize(("name", "agent"), SIX)
def test_an_unset_gsd_engine_does_not_even_import_the_selector(name: str, agent: str, tmp_path: Path) -> None:
    driver = (
        "import json, runpy, subprocess, sys\n"
        "started = []\n"
        "real = subprocess.run\n"
        "subprocess.run = lambda *a, **k: (started.append(a), real(*a, **k))[1]\n"
        f"sys.argv = [{name!r}, '--dry-run', '--json']\n"
        "code = 0\n"
        "try:\n"
        f"    runpy.run_path({str(ROOT / 'scripts' / (name + '.py'))!r}, run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    code = exc.code or 0\n"
        "sys.stdout.flush()\n"
        "print('\\n@@' + json.dumps({'imported': 'swarm.engine_select' in sys.modules, 'started': len(started), 'code': code}))\n"
    )
    env = {key: value for key, value in os.environ.items() if key not in ENGINE_ENV}
    env.update({"SWARM_DIR": str(tmp_path / ".swarm"), "SWARM_STATE_DIR": str(tmp_path / "state")})
    done = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, env=env, cwd=tmp_path, check=False, timeout=300)
    facts = json.loads(done.stdout.rsplit("\n@@", 1)[1])
    assert facts["imported"] is False and facts["started"] == 0, done.stderr[-2000:]
    assert facts["code"] in (0, 2)


@pytest.mark.parametrize(("name", "agent"), SIX)
def test_a_bad_gsd_engine_value_is_refused_with_e_input_exactly_as_orch_status_refuses_it(
        name: str, agent: str, orch_status: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("GSD_ENGINE", "wasm-secret-value")
    code, body, spawned = run_entry(name, DRY, monkeypatch, capsys)
    assert code == 2 and spawned == []
    assert "wasm-secret-value" not in json.dumps(body)
    assert orch_status.select_engine(["--json"]) == 2
    reference = json.loads(capsys.readouterr().out)
    assert body == {**reference, "agent": agent, "script": name}
    monkeypatch.setenv("SWARM_PURE_PYTHON", "1")
    code, body, _ = run_entry(name, DRY, monkeypatch, capsys)
    assert body.get("error", {}).get("code") != "E-INPUT", "SWARM_PURE_PYTHON=1 comes first, as for orch_status"


@pytest.mark.parametrize(("name", "agent"), SIX)
def test_an_explicit_rust_for_a_script_without_a_route_is_answered_as_an_unserved_route_without_starting_the_binary(
        name: str, agent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    baseline_code, baseline, _ = run_entry(name, DRY, monkeypatch, capsys)
    monkeypatch.setenv("GSD_ENGINE", "rust")

    code, body, spawned = run_entry(name, DRY, monkeypatch, capsys)
    assert (code, body["agent"], body["script"], body["error"]["code"]) == (2, agent, name, "E-DEP"), "rust needs a binary, as for orch_status"
    assert body["error"]["message"] == "GSD_ENGINE=rust needs GSD_ENGINE_BIN set to the gsd_engine binary" and spawned == []

    fake = build_fake(tmp_path / "fake", stdout=_error("E-DEP", "not_started"), exit_code=2)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    code, body, spawned = run_entry(name, DRY, monkeypatch, capsys)
    assert (code, _gist(body), spawned, fake.starts) == (baseline_code, _gist(baseline), [], 0), "Python answers; the binary is not started"

    monkeypatch.setenv("GSD_RUST_BACKEND", "gpu")
    code, body, _ = run_entry(name, DRY, monkeypatch, capsys)
    assert (code, body["error"]["code"], fake.starts) == (2, "E-INPUT", 0)


@pytest.mark.parametrize(("name", "agent"), SIX)
def test_the_block_hands_the_call_to_the_selector_once_a_route_is_registered(
        name: str, agent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The inert block is a seam: a route in ENGINE_ROUTES reaches the selector even with GSD_ENGINE unset (its default row decides)."""
    source = (ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
    routes, selector = "ENGINE_ROUTES: tuple = ()", "engine_select.Selector(script, ENGINE_ROUTES)"
    assert source.count(routes) == 1 and source.count(selector) == 1
    fake = build_fake(tmp_path / "fake", stdout=_rendered({"agent": agent, "script": name, "status": "ok", "summary": "from the fake engine"}))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    route = engine_select.Route(claims=lambda argv: True, parse=lambda argv: argv, default=lambda: "rust", env_route=lambda argv, call: "plain")
    monkeypatch.setattr(engine_select, "ROUTE_UNDER_TEST", route, raising=False)
    # what a unit that registers a route writes in the block: the route, and the environment policy of its routes
    patched = source.replace(routes, "ENGINE_ROUTES: tuple = (__import__('swarm.engine_select').engine_select.ROUTE_UNDER_TEST,)").replace(
        selector, "engine_select.Selector(script, ENGINE_ROUTES, binary_env=lambda route: {'PATH': os.environ['PATH']})")
    real = ROOT / "scripts" / f"{name}.py"
    monkeypatch.setattr(sys, "argv", [name, *DRY])
    with pytest.raises(SystemExit) as exit_info:
        exec(compile(patched, str(real), "exec"), {"__name__": "__main__", "__file__": str(real)})  # noqa: S102
    out = capsys.readouterr().out
    assert exit_info.value.code == 0 and json.loads(out)["summary"] == "from the fake engine" and fake.starts == 1
