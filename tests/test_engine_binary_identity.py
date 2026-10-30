"""The identity check before a child that holds a signing key is started, and the core-dump guard of that child (swarm/engine_select.py:
`identity_refusal`, `child_guard`). The binary is a /bin/sh fake that counts its starts, so a refused start shows as no count."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from swarm import engine_select, substrate_tee
from swarm.script_base import AgentScript

ENGINE_ENV = ("GSD_ENGINE", "GSD_ENGINE_BIN", "GSD_ENGINE_BIN_SHA256", "GSD_RUST_BACKEND", "GSD_RUST_TIMEOUT_S", "SWARM_PURE_PYTHON",
              "SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE")
SERVED = ["--json"]
WRITE_ARGV = ["--json", "--write"]
OK_REPLY = {"agent": "A99", "script": "demo", "status": "ok", "summary": "answered by the fake engine"}
KEY = "SWARM_SIGNING_KEY"
# the grant of each environment route: "keyed" hands the signing key to the binary, "plain" hands nothing
GRANTS = {"plain": frozenset(), "keyed": frozenset({KEY})}


def fake_binary(folder: Path) -> Path:
    """A /bin/sh gsd_engine that appends to `starts`, drops its stdin, and prints OK_REPLY with exit 0."""
    folder.mkdir(parents=True, exist_ok=True)
    reply = folder / "reply.json"
    reply.write_text(json.dumps(OK_REPLY) + "\n", encoding="utf-8")
    path = folder / "gsd_engine"
    path.write_text(f"#!/bin/sh\necho started >> {shlex.quote(str(folder / 'starts'))}\ncat > /dev/null\ncat {shlex.quote(str(reply))}\n"
                    "exit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def starts(folder: Path) -> int:
    marker = folder / "starts"
    return len(marker.read_text().splitlines()) if marker.exists() else 0


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _commit(call: Any, before: Any, effects_path: str, reply: dict[str, Any], stdout: bytes) -> tuple[dict[str, Any], bytes]:
    return reply, stdout


def make_selector(grant: str) -> engine_select.Selector:
    """A selector with a reading and a writing route; both resolve their environment grant to `grant`."""
    def binary_env(route: str) -> dict[str, str]:
        names = engine_select.BINARY_ENV_NAMES | GRANTS[route]
        return {key: value for key, value in os.environ.items() if key in names}

    def parse(argv: list[str]) -> list[str] | None:
        return argv if "--json" in argv else None

    write = engine_select.WriteFlow(before=lambda call: None, commit=_commit, unknown=lambda call: ("Check the demo.", {}),
                                    preexec=lambda route: None)
    routes = (
        engine_select.Route(claims=lambda argv: "--write" in argv, parse=parse, default=lambda: "rust",
                            env_route=lambda argv, call: grant, write=write),
        engine_select.Route(claims=lambda argv: True, parse=parse, default=lambda: "rust", env_route=lambda argv, call: grant),
    )
    script = AgentScript("A99", "demo", lambda args, ctx: {"status": "ok"}, add_args=lambda parser: parser.add_argument("--write", action="store_true"))
    return engine_select.Selector(script, routes, ("SWARM_DIR",), binary_env)


def select(argv: list[str], grant: str = "keyed") -> tuple[int | None, bytes, str]:
    """`select_engine` with stdout as bytes and stderr as text: (answer, stdout, stderr)."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        answer = engine_select.select_engine(make_selector(grant), argv)
    out.flush()
    return answer, out.buffer.getvalue(), err.getvalue()  # type: ignore[attr-defined]


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake binary named by GSD_ENGINE_BIN, a private swarm directory and working directory, and no tee to a substrate."""
    for key in ENGINE_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(substrate_tee, "tee", lambda record, root=None: None)
    monkeypatch.setattr(engine_select, "RELEASE_BINARY", tmp_path / "no-release" / "gsd_engine")
    binary = fake_binary(tmp_path / "fake")
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(binary))
    return binary


def keyed(monkeypatch: pytest.MonkeyPatch, value: str = "11" * 32) -> None:
    """A signing key in this process's environment (the autouse fixture of conftest sets one for every test; each test states its own)."""
    monkeypatch.setenv(KEY, value)


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
@pytest.mark.parametrize("engine", ["rust", ""], ids=["explicit", "default"])
def test_a_key_without_a_digest_is_refused_and_the_binary_is_not_started(fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str],
                                                                         engine: str) -> None:
    keyed(monkeypatch)
    monkeypatch.setenv("GSD_ENGINE", engine)
    answer, out, err = select(argv)
    assert answer is None, "the Python body answers: nothing ran, as for the binary's own not_started"
    assert starts(fake.parent) == 0
    assert out == b""
    assert "E-POLICY" in err and "GSD_ENGINE_BIN_SHA256" in err


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
@pytest.mark.parametrize("digest", ["0" * 64, "A" * 64, "not-a-digest"], ids=["wrong", "uppercase-of-wrong", "malformed"])
def test_a_key_with_a_wrong_digest_is_refused_and_the_binary_is_not_started(fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str],
                                                                            digest: str) -> None:
    keyed(monkeypatch)
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", digest)
    answer, out, err = select(argv)
    assert answer is None
    assert starts(fake.parent) == 0
    assert out == b""
    assert "E-POLICY" in err and "GSD_ENGINE_BIN_SHA256" in err


def test_a_digest_of_another_file_is_refused(fake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = fake_binary(fake.parent / "other")
    keyed(monkeypatch)
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", sha256_of(other))  # the digest of another build; GSD_ENGINE_BIN names `fake`
    answer, _, err = select(SERVED)
    assert answer is None and starts(fake.parent) == 0 and "does not match" in err


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
@pytest.mark.parametrize("engine", ["rust", ""], ids=["explicit", "default"])
def test_a_key_with_the_right_digest_starts_the_binary(fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], engine: str) -> None:
    keyed(monkeypatch)
    monkeypatch.setenv("GSD_ENGINE", engine)
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", sha256_of(fake))
    answer, out, err = select(argv)
    assert answer == AgentScript.EXIT_OK
    assert starts(fake.parent) == 1
    assert json.loads(out.decode("utf-8")) == OK_REPLY
    assert "E-POLICY" not in err


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
def test_no_key_handed_is_unchanged_and_needs_no_digest(fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    # the key is set in this process but its route's grant does not hand it on: the child holds no key
    keyed(monkeypatch)
    answer, out, err = select(argv, grant="plain")
    assert answer == AgentScript.EXIT_OK
    assert starts(fake.parent) == 1
    assert json.loads(out.decode("utf-8")) == OK_REPLY
    assert "GSD_ENGINE_BIN_SHA256" not in err


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
def test_granted_route_without_a_key_in_this_process_needs_no_digest(fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    monkeypatch.delenv(KEY, raising=False)  # the grant names the key, but there is none to hand
    answer, _, _ = select(argv, grant="keyed")
    assert answer == AgentScript.EXIT_OK
    assert starts(fake.parent) == 1


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
def test_a_child_that_holds_a_key_is_started_with_core_dumps_off(fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    keyed(monkeypatch)
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", sha256_of(fake))
    preexec: list[Any] = []
    real_run = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        preexec.append(kwargs.get("preexec_fn"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(engine_select.subprocess, "run", spy)
    answer, _, _ = select(argv)
    assert answer == AgentScript.EXIT_OK
    # the write route's own hook answers None; a child that holds a key gets the guard whatever the hook says
    assert preexec == [engine_select.no_core_dumps]


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
def test_a_child_without_a_key_keeps_its_route_hook(fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    keyed(monkeypatch)
    preexec: list[Any] = []
    real_run = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        preexec.append(kwargs.get("preexec_fn"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(engine_select.subprocess, "run", spy)
    answer, _, _ = select(argv, grant="plain")
    assert answer == AgentScript.EXIT_OK
    assert preexec == [None]
