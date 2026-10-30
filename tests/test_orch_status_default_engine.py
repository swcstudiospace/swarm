"""orch_status engine selector (14.3 R5): resolution order, binary discovery, the rust_serves pre-filter, the D1 run-log
record, fallback codes and GSD_RUST_BACKEND validation, against fake gsd_engine binaries that record each start."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
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

from swarm import substrate_tee

SCRIPT = ROOT / "scripts" / "orch_status.py"
ENGINE_ENV = ("GSD_ENGINE", "GSD_ENGINE_BIN", "GSD_RUST_BACKEND", "GSD_RUST_TIMEOUT_S", "SWARM_PURE_PYTHON",
              "SWARM_TASK_ID", "SWARM_CORRELATION_ID")
VOLATILE = ("ts", "msg_id")
FALLBACK_CODES = ("E-INTERNAL", "E-DEP", "E-CAPACITY", "E-CONTRACT", "E-INPUT")
SERVED = ["--json"]
OK_REPLY = {"agent": "A01", "script": "orch_status", "status": "ok", "summary": "answered by the fake engine"}
Answer = Callable[[list[str]], tuple[int, bytes]]


def _rendered(reply: dict[str, Any]) -> bytes:
    return (json.dumps(reply, indent=2) + "\n").encode()


def _error_reply(code: str) -> dict[str, Any]:
    return {"agent": "A01", "script": "orch_status", "status": "error",
            "error": {"code": code, "message": "canned", "details": {}, "handling": "none"}}


@dataclass
class Fake:
    path: Path
    marker: Path
    stdin: Path

    @property
    def starts(self) -> int:
        return len(self.marker.read_text().splitlines()) if self.marker.exists() else 0


@pytest.fixture
def mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/orch_status.py as a module, in a clean engine environment with the release binary pointed at nothing."""
    for key in ENGINE_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(substrate_tee, "tee", lambda record, root=None: None)
    spec = importlib.util.spec_from_file_location("orch_status_selector_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RELEASE_BINARY", tmp_path / "no-release" / "gsd_engine")
    return module


@pytest.fixture
def fake_binary(tmp_path: Path) -> Callable[..., Fake]:
    """Builds a fake gsd_engine. It appends a line to its marker per start, saves its stdin, then prints `stdout` and exits with
    `exit_code`, or runs `tail` (shell text) instead."""
    made = 0

    def build(*, stdout: bytes = b"", exit_code: int = 0, tail: str | None = None, executable: bool = True) -> Fake:
        nonlocal made
        made += 1
        folder = tmp_path / f"fake{made}"
        folder.mkdir()
        fake = Fake(folder / "gsd_engine", folder / "starts", folder / "stdin.json")
        (folder / "reply").write_bytes(stdout)
        body = tail if tail is not None else f"cat {shlex.quote(str(folder / 'reply'))}\nexit {exit_code}"
        fake.path.write_text(f"#!/bin/sh\necho started >> {shlex.quote(str(fake.marker))}\ncat > {shlex.quote(str(fake.stdin))}\n{body}\n",
                             encoding="utf-8")
        fake.path.chmod(0o755 if executable else 0o644)
        return fake

    return build


@pytest.fixture
def answer(mod: ModuleType, capsysbinary: pytest.CaptureFixture[bytes]) -> Answer:
    """What `python3 scripts/orch_status.py <argv>` does: the selector, then the Python body when the selector hands the call over."""

    def go(argv: list[str]) -> tuple[int, bytes]:
        code = mod.select_engine(argv)
        if code is None:
            code = mod.SCRIPT.main(argv)
        return code, capsysbinary.readouterr().out

    return go


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / ".swarm" / "events.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.exists() else []


def _selected(mod: ModuleType, capsysbinary: pytest.CaptureFixture[bytes], argv: list[str] | None = None) -> tuple[int | None, bytes]:
    code = mod.select_engine(SERVED if argv is None else argv)
    return code, capsysbinary.readouterr().out


def _error_of(out: bytes) -> dict[str, Any]:
    return json.loads(out)["error"]


# --- resolution order -------------------------------------------------------------------------------------------------

# env, DEFAULT_ENGINE, expected outcome: "python" (selector hands over, binary never started), "rust" (binary answers), or an error code
RESOLUTION = {
    "pure python beats explicit rust": ({"SWARM_PURE_PYTHON": "1", "GSD_ENGINE": "rust"}, "python", "python"),
    "pure python beats an invalid engine": ({"SWARM_PURE_PYTHON": "1", "GSD_ENGINE": "bogus"}, "python", "python"),
    "pure python beats a rust default": ({"SWARM_PURE_PYTHON": "1"}, "rust", "python"),
    "pure python beats a rust default with empty engine": ({"SWARM_PURE_PYTHON": "1", "GSD_ENGINE": ""}, "rust", "python"),
    "only the value 1 is pure python": ({"SWARM_PURE_PYTHON": "0", "GSD_ENGINE": "rust"}, "python", "rust"),
    "explicit python under a rust default": ({"GSD_ENGINE": "python"}, "rust", "python"),
    "explicit python ignores a bad backend": ({"GSD_ENGINE": "python", "GSD_RUST_BACKEND": "gpu"}, "rust", "python"),
    "explicit rust under a python default": ({"GSD_ENGINE": "rust"}, "python", "rust"),
    "explicit rust under a rust default": ({"GSD_ENGINE": "rust"}, "rust", "rust"),
    "unset under a python default": ({}, "python", "python"),
    "empty under a python default": ({"GSD_ENGINE": ""}, "python", "python"),
    "unset under a rust default": ({}, "rust", "rust"),
    "empty under a rust default": ({"GSD_ENGINE": ""}, "rust", "rust"),
    "capitalised value": ({"GSD_ENGINE": "Rust"}, "python", "E-INPUT"),
    "unknown value under a python default": ({"GSD_ENGINE": "auto"}, "python", "E-INPUT"),
    "unknown value under a rust default": ({"GSD_ENGINE": "auto"}, "rust", "E-INPUT"),
    "blank value": ({"GSD_ENGINE": " "}, "rust", "E-INPUT"),
}


@pytest.mark.parametrize(("env", "default", "expected"), RESOLUTION.values(), ids=RESOLUTION.keys())
def test_engine_resolution_order(env: dict[str, str], default: str, expected: str, mod: ModuleType, fake_binary: Callable[..., Fake],
                                 monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", default)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    code, out = _selected(mod, capsysbinary)

    if expected == "python":
        assert (code, out, fake.starts) == (None, b"", 0)
    elif expected == "rust":
        assert (code, out, fake.starts) == (0, _rendered(OK_REPLY), 1)
    else:
        assert (code, fake.starts, _error_of(out)["code"]) == (2, 0, expected)
        assert _error_of(out)["message"] == "GSD_ENGINE must be 'python' or 'rust'"


def test_unset_engine_follows_the_default_the_module_ships(mod: ModuleType, fake_binary: Callable[..., Fake],
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))

    mod.select_engine(SERVED)

    assert mod.DEFAULT_ENGINE in ("python", "rust")
    assert fake.starts == (1 if mod.DEFAULT_ENGINE == "rust" else 0)


# --- binary discovery -------------------------------------------------------------------------------------------------

def test_default_call_finds_the_binary_in_the_environment(mod: ModuleType, fake_binary: Callable[..., Fake],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))

    assert mod.select_engine(SERVED) == 0
    assert fake.starts == 1


@pytest.mark.parametrize("engine", [{}, {"GSD_ENGINE": "rust"}], ids=["default", "explicit"])
def test_release_binary_is_found_when_the_environment_names_none(engine: dict[str, str], mod: ModuleType, fake_binary: Callable[..., Fake],
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    release = fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.setattr(mod, "RELEASE_BINARY", release.path)
    monkeypatch.setenv("GSD_ENGINE_BIN", "")
    for key, value in engine.items():
        monkeypatch.setenv(key, value)

    assert mod.select_engine(SERVED) == 0
    assert release.starts == 1


def test_environment_binary_wins_over_the_release_binary(mod: ModuleType, fake_binary: Callable[..., Fake],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    configured, release = fake_binary(stdout=_rendered(OK_REPLY)), fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.setattr(mod, "RELEASE_BINARY", release.path)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(configured.path))

    mod.select_engine(SERVED)

    assert (configured.starts, release.starts) == (1, 0)


# what GSD_ENGINE_BIN names (None leaves it unset), and what the release path holds ("ok", "plain" file without x, None absent)
UNUSABLE_DEFAULT = {
    "nothing anywhere": (None, None),
    "environment names a missing file": ("missing", "ok"),
    "environment names a file without the x bit": ("plain", "ok"),
    "environment names a directory": ("directory", "ok"),
    "release file without the x bit": (None, "plain"),
}


@pytest.mark.parametrize(("configured", "release"), UNUSABLE_DEFAULT.values(), ids=UNUSABLE_DEFAULT.keys())
def test_default_call_without_a_usable_binary_runs_python_silently(
        configured: str | None, release: str | None, mod: ModuleType, fake_binary: Callable[..., Fake], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, answer: Answer) -> None:
    release_fake = fake_binary(stdout=_rendered(OK_REPLY), executable=release == "ok")
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.setattr(mod, "RELEASE_BINARY", release_fake.path if release is not None else tmp_path / "absent")
    monkeypatch.setenv("GSD_ENGINE", "python")
    expected = answer(SERVED)
    monkeypatch.delenv("GSD_ENGINE")
    plain = fake_binary(stdout=_rendered(OK_REPLY), executable=False)
    named = {"missing": tmp_path / "missing", "plain": plain.path, "directory": tmp_path}
    if configured is not None:
        monkeypatch.setenv("GSD_ENGINE_BIN", str(named[configured]))

    assert answer(SERVED) == expected
    assert (release_fake.starts, plain.starts) == (0, 0)


# configuration of an explicit `rust` call that has no startable binary, and the refusal it gets
EXPLICIT_WITHOUT_BINARY = {
    "no binary anywhere": (None, "GSD_ENGINE=rust needs GSD_ENGINE_BIN set to the gsd_engine binary"),
    "environment names a missing file": ("missing", "the gsd_engine binary named by GSD_ENGINE_BIN could not be started"),
    "environment names a file without the x bit": ("plain", "the gsd_engine binary named by GSD_ENGINE_BIN could not be started"),
}


@pytest.mark.parametrize(("configured", "message"), EXPLICIT_WITHOUT_BINARY.values(), ids=EXPLICIT_WITHOUT_BINARY.keys())
def test_explicit_rust_without_a_startable_binary_is_e_dep(configured: str | None, message: str, mod: ModuleType,
                                                           fake_binary: Callable[..., Fake], tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch,
                                                           capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    plain = fake_binary(stdout=_rendered(OK_REPLY), executable=False)
    monkeypatch.setenv("GSD_ENGINE", "rust")
    if configured is not None:
        monkeypatch.setenv("GSD_ENGINE_BIN", str({"missing": tmp_path / "missing", "plain": plain.path}[configured]))

    code, out = _selected(mod, capsysbinary)

    assert code == 2
    assert _error_of(out)["code"] == "E-DEP"
    assert _error_of(out)["message"] == message
    assert plain.starts == 0


# --- pre-filter -------------------------------------------------------------------------------------------------------

UNSERVED = {
    "text mode": ["--root", "."],
    "history without json": ["--history", "T-1"],
    "transition": ["--json", "--transition", "T-1", "DONE"],
    "ingest": ["--json", "--ingest", "result.json"],
    "positional": ["--json", "extra"],
    "unknown flag": ["--json", "--unknown"],
}


@pytest.mark.parametrize("argv", UNSERVED.values(), ids=UNSERVED.keys())
def test_default_call_never_starts_the_binary_for_an_argv_it_does_not_serve(argv: list[str], mod: ModuleType, fake_binary: Callable[..., Fake],
                                                                            monkeypatch: pytest.MonkeyPatch,
                                                                            capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    default_fake, explicit_fake = fake_binary(stdout=_rendered(OK_REPLY)), fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(default_fake.path))

    assert _selected(mod, capsysbinary, argv) == (None, b"")
    assert default_fake.starts == 0

    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(explicit_fake.path))
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", hashlib.sha256(explicit_fake.path.read_bytes()).hexdigest())
    mod.select_engine(argv)
    assert explicit_fake.starts == 1


# --- what the binary is sent ------------------------------------------------------------------------------------------

def test_binary_gets_argv_root_and_only_the_allowlisted_environment(mod: ModuleType, fake_binary: Callable[..., Fake], tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("SWARM_TASK_ID", "T-1")
    monkeypatch.setenv("SWARM_CORRELATION_ID", "")
    monkeypatch.setenv("GSD_RUST_BACKEND", "host")
    argv = ["--json", "--history", "T-ü"]

    mod.select_engine(argv)

    sent = json.loads(fake.stdin.read_text(encoding="utf-8"))
    assert sent == {"agent": "A01", "script": "orch_status", "argv": argv, "root": os.getcwd(),
                    "env": {"SWARM_DIR": str(tmp_path / ".swarm"), "SWARM_TASK_ID": "T-1"}}


# --- D1: the run-log record of a Rust answer --------------------------------------------------------------------------

# argv of a read the Python path answers with exit 0
RECORDED_READS = {
    "list": ["--json"],
    "ids carried": ["--json", "--task-id", "T-1", "--correlation-id", "corr-1"],
    "dry run": ["--json", "--dry-run"],
}


def _stable(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in record.items() if key not in VOLATILE} for record in records]


@pytest.mark.parametrize("argv", RECORDED_READS.values(), ids=RECORDED_READS.keys())
def test_rust_answer_writes_the_run_log_record_the_python_path_writes(
        argv: list[str], mod: ModuleType, fake_binary: Callable[..., Fake], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        answer: Answer) -> None:
    monkeypatch.setenv("GSD_ENGINE", "python")
    python_exit, python_out = answer(argv)
    python_records = _records(tmp_path)
    assert (python_exit, [r["type"] for r in python_records]) == (0, ["script.orch_status"])

    fake = fake_binary(stdout=python_out)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / "rust-state"))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    rust_exit, rust_out = answer(argv)

    rust_records = [json.loads(line) for line in (tmp_path / "rust-state" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert (rust_exit, rust_out, fake.starts) == (0, python_out, 1)
    assert _stable(rust_records) == _stable(python_records)
    assert rust_records[0]["msg_id"] != python_records[0]["msg_id"]


# served reply, exit code, run-log event type
SERVED_ANSWERS = {
    "ok": (OK_REPLY, 0, "script.orch_status"),
    "fail": ({**OK_REPLY, "status": "fail"}, 1, "script.orch_status"),
    "typed error the body does not serve": (_error_reply("E-POLICY"), 2, "script.orch_status.error"),
    "timeout reported by the binary": (_error_reply("E-TIMEOUT"), 2, "script.orch_status.error"),
}


@pytest.mark.parametrize(("reply", "exit_code", "event_type"), SERVED_ANSWERS.values(), ids=SERVED_ANSWERS.keys())
def test_rust_answer_is_printed_verbatim_and_recorded_once(reply: dict[str, Any], exit_code: int, event_type: str, mod: ModuleType,
                                                           fake_binary: Callable[..., Fake], tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch, answer: Answer) -> None:
    fake = fake_binary(stdout=_rendered(reply), exit_code=exit_code)
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))

    assert answer(SERVED) == (exit_code, _rendered(reply))

    [record] = _records(tmp_path)
    assert (record["type"], record["source"], record["payload"]) == (event_type, "A01@orch_status", reply)
    assert fake.starts == 1


def test_rust_answer_whose_record_cannot_be_written_is_left_to_the_python_body(
        mod: ModuleType, fake_binary: Callable[..., Fake], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    argv = [*SERVED, "--task-id", "../escape"]  # the binary may serve it; the shape check of the record refuses it

    code, out = _selected(mod, capsysbinary, argv)

    assert (code, out, fake.starts) == (None, b"", 1)
    assert _records(tmp_path) == []


# --- fallback ---------------------------------------------------------------------------------------------------------

# stdout and exit code of a binary whose answer the Python body replaces
FALLBACKS = {
    "crash": (b"", 101),
    "garbage": (b"not json at all\n", 0),
    "not utf-8": (b"\xff\xfe\n", 0),
    "json array": (b"[1, 2]\n", 0),
    "exit code outside 0 to 2": (_rendered(OK_REPLY), 3),
    "exit 2 without a code": (_rendered({"status": "error"}), 2),
    "exit 2 with an untyped error": (_rendered({"status": "error", "error": "boom"}), 2),
    **{f"exit 2 {code}": (_rendered(_error_reply(code)), 2) for code in FALLBACK_CODES},
}


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
@pytest.mark.parametrize(("stdout", "exit_code"), FALLBACKS.values(), ids=FALLBACKS.keys())
def test_unusable_rust_answer_gives_pythons_bytes_after_one_start(
        stdout: bytes, exit_code: int, explicit: bool, mod: ModuleType, fake_binary: Callable[..., Fake], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, answer: Answer) -> None:
    monkeypatch.setenv("GSD_ENGINE", "python")
    expected = answer(SERVED)
    records_before = len(_records(tmp_path))
    fake = fake_binary(stdout=stdout, exit_code=exit_code)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    if explicit:
        monkeypatch.setenv("GSD_ENGINE", "rust")
    else:
        monkeypatch.delenv("GSD_ENGINE")
        monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")

    assert answer(SERVED) == expected
    assert fake.starts == 1
    assert len(_records(tmp_path)) == 2 * records_before  # the fallback run wrote the one record the Python body writes


def test_binary_that_cannot_be_executed_is_python_under_the_default_and_e_dep_when_explicit(
        mod: ModuleType, fake_binary: Callable[..., Fake], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Answer) -> None:
    broken = tmp_path / "broken"
    broken.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    broken.chmod(0o755)
    monkeypatch.setenv("GSD_ENGINE", "python")
    expected = answer(SERVED)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(broken))
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.delenv("GSD_ENGINE")

    assert answer(SERVED) == expected

    monkeypatch.setenv("GSD_ENGINE", "rust")
    code, out = answer(SERVED)
    assert code == 2
    assert _error_of(out)["code"] == "E-DEP"
    assert _error_of(out)["message"] == "the gsd_engine binary named by GSD_ENGINE_BIN could not be started"


@pytest.mark.parametrize("engine", [{}, {"GSD_ENGINE": "rust"}], ids=["default", "explicit"])
def test_wedged_binary_is_e_timeout_and_never_falls_back(engine: dict[str, str], mod: ModuleType, fake_binary: Callable[..., Fake],
                                                         monkeypatch: pytest.MonkeyPatch, answer: Answer) -> None:
    fake = fake_binary(tail="exec sleep 30")  # exec: the timeout kill reaches the sleeper and the stdout pipe closes
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_RUST_TIMEOUT_S", "1")
    for key, value in engine.items():
        monkeypatch.setenv(key, value)

    code, out = answer(SERVED)

    assert code == 2
    assert _error_of(out)["code"] == "E-TIMEOUT"
    assert "tasks" not in json.loads(out)
    assert fake.starts == 1


# GSD_RUST_TIMEOUT_S, and the seconds the call may take
TIMEOUTS = {"unset": (None, 660), "empty": ("", 660), "seconds": ("5", 5), "zero": ("0", 660), "negative": ("-3", 660),
            "text": ("soon", 660), "decimal": ("1.5", 660), "non-ascii digits": ("٣", 660)}


@pytest.mark.parametrize(("raw", "seconds"), TIMEOUTS.values(), ids=TIMEOUTS.keys())
def test_call_deadline_is_660_seconds_unless_a_positive_integer_is_set(raw: str | None, seconds: int, mod: ModuleType,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    if raw is not None:
        monkeypatch.setenv("GSD_RUST_TIMEOUT_S", raw)

    assert mod._rust_timeout_s() == seconds


# --- GSD_RUST_BACKEND -------------------------------------------------------------------------------------------------

VALID_BACKENDS = {"unset": None, "empty": "", "host": "host", "sandbox": "sandbox"}
INVALID_BACKENDS = {"capitalised": "Host", "unknown": "gpu", "trailing space": "sandbox ", "none": "none"}


@pytest.mark.parametrize("engine", [{}, {"GSD_ENGINE": "rust"}], ids=["default", "explicit"])
@pytest.mark.parametrize("backend", VALID_BACKENDS.values(), ids=VALID_BACKENDS.keys())
def test_valid_backend_values_start_the_binary(backend: str | None, engine: dict[str, str], mod: ModuleType,
                                               fake_binary: Callable[..., Fake], monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    if backend is not None:
        monkeypatch.setenv("GSD_RUST_BACKEND", backend)
    for key, value in engine.items():
        monkeypatch.setenv(key, value)

    assert mod.select_engine(SERVED) == 0
    assert fake.starts == 1


@pytest.mark.parametrize("engine", [{}, {"GSD_ENGINE": "rust"}], ids=["default", "explicit"])
@pytest.mark.parametrize("backend", INVALID_BACKENDS.values(), ids=INVALID_BACKENDS.keys())
def test_invalid_backend_fails_closed_before_the_binary_starts(backend: str, engine: dict[str, str], mod: ModuleType,
                                                               fake_binary: Callable[..., Fake], monkeypatch: pytest.MonkeyPatch,
                                                               capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_RUST_BACKEND", backend)
    for key, value in engine.items():
        monkeypatch.setenv(key, value)

    code, out = _selected(mod, capsysbinary)

    assert code == 2
    assert _error_of(out)["code"] == "E-INPUT"
    assert _error_of(out)["message"] == "GSD_RUST_BACKEND must be 'host' or 'sandbox' when set"
    assert fake.starts == 0


def test_invalid_backend_is_ignored_when_the_default_call_does_not_use_the_binary(
        mod: ModuleType, fake_binary: Callable[..., Fake], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", "rust")
    monkeypatch.setenv("GSD_RUST_BACKEND", "gpu")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))

    assert _selected(mod, capsysbinary, UNSERVED["text mode"]) == (None, b"")  # the pre-filter keeps the binary out

    monkeypatch.setenv("GSD_ENGINE_BIN", str(tmp_path / "missing"))
    assert _selected(mod, capsysbinary) == (None, b"")  # no binary: Python, silently
    assert fake.starts == 0


def test_explicit_rust_reports_a_bad_backend_before_a_missing_binary(mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
                                                                     capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_RUST_BACKEND", "gpu")

    code, out = _selected(mod, capsysbinary)

    assert (code, _error_of(out)["code"]) == (2, "E-INPUT")


# --- rust_serves ------------------------------------------------------------------------------------------------------

# argv of the read grammar of parse_orch_status_read (crates/gsd-route/src/lib.rs) with --json: served
SERVED_ARGV = {
    "json only": ["--json"],
    "root after json": ["--json", "--root", "/x"],
    "root before json": ["--root", "/x", "--json"],
    "root inline": ["--root=/x", "--json"],
    "root value with an equals sign": ["--json", "--root=/x=y"],
    "repo alias": ["--repo", "/x", "--json"],
    "history": ["--json", "--history", "T-1"],
    "history inline": ["--history=T-1", "--json"],
    "history inline empty": ["--history=", "--json"],
    "history inline dash value": ["--json", "--history=-x"],
    "separate empty value": ["--root", "", "--json"],
    "ids": ["--json", "--correlation-id", "c", "--task-id", "t"],
    "dry run": ["--json", "--dry-run"],
    "json twice": ["--json", "--json"],
    "inline value that looks like a flag": ["--root=--json", "--json"],
}

UNSERVED_ARGV = {
    "empty": [],
    "root without json": ["--root", "/x"],
    "dry run without json": ["--dry-run"],
    "json with a value": ["--json=1"],
    "json with an empty value": ["--json="],
    "dry run with a value": ["--dry-run=1", "--json"],
    "root without a value": ["--json", "--root"],
    "root value is a flag": ["--root", "--json"],
    "history value starts with a dash": ["--json", "--history", "-x"],
    "json hidden in a value": ["--root=--json"],
    "trailing positional": ["--json", "extra"],
    "leading positional": ["extra", "--json"],
    "unknown flag": ["--json", "--unknown"],
    "abbreviated flag": ["--json", "--hist", "T"],
    "short flag with a value": ["--json", "-x=1"],
    "help": ["--json", "-h"],
    "transition": ["--json", "--transition", "T-1", "DONE"],
    "ingest": ["--json", "--ingest", "f"],
    "advisory": ["--json", "--advisory"],
    "reason": ["--json", "--reason", "x"],
}


@pytest.mark.parametrize("argv", SERVED_ARGV.values(), ids=SERVED_ARGV.keys())
def test_rust_serves_accepts_the_read_json_grammar(argv: list[str], mod: ModuleType) -> None:
    assert mod.rust_serves(argv) is True


@pytest.mark.parametrize("argv", UNSERVED_ARGV.values(), ids=UNSERVED_ARGV.keys())
def test_rust_serves_refuses_everything_else(argv: list[str], mod: ModuleType) -> None:
    assert mod.rust_serves(argv) is False


# --- the script as a process ------------------------------------------------------------------------------------------

def test_script_process_prints_the_binary_answer_and_records_it_once(fake_binary: Callable[..., Fake], tmp_path: Path) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY))
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GSD_", "SWARM_"))}
    env.update({"GSD_ENGINE": "rust", "GSD_ENGINE_BIN": str(fake.path), "SWARM_DIR": str(tmp_path / "proc-state")})

    proc = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), "--json"], env=env, capture_output=True,
                          timeout=120, check=False)

    assert (proc.returncode, proc.stdout, fake.starts) == (0, _rendered(OK_REPLY), 1)
    records = (tmp_path / "proc-state" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["type"] for line in records] == ["script.orch_status"]
