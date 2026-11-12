"""The identity check before a child that holds a signing key is started, the core-dump guard of that child, and the isolated home of a
child that holds a key or a key-policy grant (swarm/engine_select.py: `identity_refusal`, `identity_refused`, `child_guard`,
`child_home`). The binary is a /bin/sh fake that counts its starts, so a refused start shows as no count."""
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
# the grant of each environment route: "keyed" hands the signing key to the binary, "keyfile" an absolute key-file reference, and each
# other route one key-policy variable; "plain" hands nothing
GRANTS = {"plain": frozenset(), "keyed": frozenset({KEY}), "keyfile": frozenset({"SWARM_ED25519_KEY_FILE"}),
          "xdg": frozenset({"XDG_CONFIG_HOME"}), "require": frozenset({"SWARM_REQUIRE_KEY"}),
          "dev": frozenset({"SWARM_ALLOW_INSECURE_DEV_KEY"}), "session": frozenset({"SWARM_AGENT_SESSION"})}


def fake_binary(folder: Path, probe: str = "") -> Path:
    """A /bin/sh gsd_engine that appends to `starts`, drops its stdin, runs the shell lines `probe`, and prints OK_REPLY with exit 0."""
    folder.mkdir(parents=True, exist_ok=True)
    reply = folder / "reply.json"
    reply.write_text(json.dumps(OK_REPLY) + "\n", encoding="utf-8")
    path = folder / "gsd_engine"
    path.write_text(f"#!/bin/sh\necho started >> {shlex.quote(str(folder / 'starts'))}\ncat > /dev/null\n{probe}cat {shlex.quote(str(reply))}\n"
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


def assert_refused(answer: int | None, out: bytes, err: str, *, explicit: bool) -> None:
    """An identity refusal, with its reason on stderr. Under an explicit `rust` call it is the call's answer: exit 2 and an E-POLICY
    reply that names GSD_ENGINE_BIN_SHA256 (Decision 2, 2026-10-30), so Python does not run. Under the default engine the Python body
    answers, as for the binary's own not_started, and no reply is printed."""
    assert "E-POLICY" in err and "GSD_ENGINE_BIN_SHA256" in err
    if explicit:
        assert answer == AgentScript.EXIT_ERROR
        reply = json.loads(out.decode("utf-8"))
        assert reply["status"] == "error" and reply["error"]["code"] == "E-POLICY"
        assert "GSD_ENGINE_BIN_SHA256" in reply["error"]["message"]
    else:
        assert answer is None, "the Python body answers: nothing ran, as for the binary's own not_started"
        assert out == b""


# what the probe binary records, in its own folder (@OUT@), of the environment it is started with
PROBE = (
    'printf %s "$HOME" > @OUT@/home.txt\n'
    'printf %s "$XDG_CONFIG_HOME" > @OUT@/xdg.txt\n'
    'ls -A "$HOME" > @OUT@/listing.txt\n'
    'if [ -e "${XDG_CONFIG_HOME:-$HOME/.config}/agent-swarm/signing-key.hex" ]; then echo present > @OUT@/conventional.txt; '
    'else echo absent > @OUT@/conventional.txt; fi\n'
    'printf %s "$SWARM_ED25519_KEY_FILE" > @OUT@/keypath.txt\n'
    '[ -n "$SWARM_ED25519_KEY_FILE" ] && cat "$SWARM_ED25519_KEY_FILE" > @OUT@/keyread.txt\n'
)


def probe_binary(fake: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """GSD_ENGINE_BIN names a fake that records its environment (PROBE) in its own folder, which is returned; the digest is its own."""
    folder = fake.parent / "probe"
    binary = fake_binary(folder, probe=PROBE.replace("@OUT@", shlex.quote(str(folder))))
    monkeypatch.setenv("GSD_ENGINE_BIN", str(binary))
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", sha256_of(binary))
    return folder


def users_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This process's HOME and XDG_CONFIG_HOME, which hold the user's conventional signing-key file. Returns HOME."""
    home = tmp_path / "users-home"
    (home / ".config" / "agent-swarm").mkdir(parents=True)
    (home / ".config" / "agent-swarm" / "signing-key.hex").write_text("22" * 32 + "\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
@pytest.mark.parametrize("engine", ["rust", ""], ids=["explicit", "default"])
def test_a_key_without_a_digest_is_refused_and_the_binary_is_not_started(fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str],
                                                                         engine: str) -> None:
    keyed(monkeypatch)
    monkeypatch.setenv("GSD_ENGINE", engine)
    answer, out, err = select(argv)
    assert_refused(answer, out, err, explicit=engine == "rust")
    assert starts(fake.parent) == 0


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
@pytest.mark.parametrize("digest", ["0" * 64, "A" * 64, "not-a-digest"], ids=["wrong", "uppercase-of-wrong", "malformed"])
def test_a_key_with_a_wrong_digest_is_refused_and_the_binary_is_not_started(fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str],
                                                                            digest: str) -> None:
    keyed(monkeypatch)
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", digest)
    answer, out, err = select(argv)
    assert_refused(answer, out, err, explicit=True)  # the fixture sets GSD_ENGINE=rust
    assert starts(fake.parent) == 0


def test_a_digest_of_another_file_is_refused(fake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = fake_binary(fake.parent / "other")
    keyed(monkeypatch)
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", sha256_of(other))  # the digest of another build; GSD_ENGINE_BIN names `fake`
    answer, out, err = select(SERVED)
    assert_refused(answer, out, err, explicit=True)
    assert starts(fake.parent) == 0 and "does not match" in err


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


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
@pytest.mark.parametrize("refusal", ["no-digest", "wrong-digest", "other-file"])
def test_an_explicit_rust_call_refused_by_the_identity_check_exits_2_with_e_policy_and_never_starts_the_binary(
        fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], refusal: str) -> None:
    keyed(monkeypatch)
    if refusal == "wrong-digest":
        monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", "0" * 64)
    elif refusal == "other-file":
        monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", sha256_of(fake_binary(fake.parent / "other")))
    answer, out, err = select(argv)
    assert answer == AgentScript.EXIT_ERROR, "an explicit rust call refused here is answered here: Python does not run"
    reply = json.loads(out.decode("utf-8"))
    assert reply["status"] == "error" and reply["error"]["code"] == "E-POLICY"
    assert "GSD_ENGINE_BIN_SHA256" in reply["error"]["message"]
    assert starts(fake.parent) == 0


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
@pytest.mark.parametrize("refusal", ["no-digest", "wrong-digest"])
def test_a_default_call_refused_by_the_identity_check_is_answered_by_python(
        fake: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], refusal: str) -> None:
    keyed(monkeypatch)
    monkeypatch.setenv("GSD_ENGINE", "")  # the default engine: unset or empty
    if refusal == "wrong-digest":
        monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", "0" * 64)
    answer, out, err = select(argv)
    assert_refused(answer, out, err, explicit=False)
    assert starts(fake.parent) == 0


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
def test_a_keyed_child_runs_with_an_empty_home_that_is_not_the_users_and_that_directory_is_removed_after_the_call(
        fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    home = users_home(tmp_path, monkeypatch)
    keyed(monkeypatch)
    folder = probe_binary(fake, monkeypatch)
    answer, _, _ = select(argv)
    assert answer == AgentScript.EXIT_OK and starts(folder) == 1
    child_home = (folder / "home.txt").read_text(encoding="utf-8")
    assert (folder / "xdg.txt").read_text(encoding="utf-8") == child_home, "HOME and XDG_CONFIG_HOME name one directory"
    assert not child_home.startswith(str(home)), "the child's home is not the user's home, nor under it"
    assert (folder / "listing.txt").read_text(encoding="utf-8") == "", "the directory the child is given is empty"
    assert (folder / "conventional.txt").read_text(encoding="utf-8").strip() == "absent", "the conventional key file is not visible"
    assert not Path(child_home).exists(), "the directory is removed when the call ends"
    assert (home / ".config" / "agent-swarm" / "signing-key.hex").is_file(), "the user's own file is untouched"


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
def test_an_unkeyed_child_keeps_the_callers_home(fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    home = users_home(tmp_path, monkeypatch)
    keyed(monkeypatch)  # a key in this process that the plain grant does not hand on
    folder = probe_binary(fake, monkeypatch)
    answer, _, _ = select(argv, grant="plain")
    assert answer == AgentScript.EXIT_OK and starts(folder) == 1
    assert (folder / "home.txt").read_text(encoding="utf-8") == str(home), "HOME is the caller's"
    assert (folder / "xdg.txt").read_text(encoding="utf-8") == "", "XDG_CONFIG_HOME is not granted, so it is not handed on"
    assert (folder / "conventional.txt").read_text(encoding="utf-8").strip() == "present"


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
def test_an_absolute_key_file_reference_still_resolves_in_an_isolated_child(
        fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    home = users_home(tmp_path, monkeypatch)
    key_file = tmp_path / "keys" / "signing.hex"
    key_file.parent.mkdir()
    key_file.write_text("33" * 32 + "\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_ED25519_KEY_FILE", str(key_file))
    folder = probe_binary(fake, monkeypatch)
    answer, _, _ = select(argv, grant="keyfile")
    assert answer == AgentScript.EXIT_OK and starts(folder) == 1
    assert (folder / "keypath.txt").read_text(encoding="utf-8") == str(key_file), "the reference is handed on unchanged"
    assert (folder / "keyread.txt").read_text(encoding="utf-8") == "33" * 32 + "\n", "and the absolute path still reads"
    assert (folder / "home.txt").read_text(encoding="utf-8") != str(home), "the child is isolated all the same"


def assert_isolated_run(folder: Path, home: Path) -> None:
    """The one probe run was started in an empty home that is not the user's, and that directory is removed when the call ends."""
    child_home = (folder / "home.txt").read_text(encoding="utf-8")
    assert child_home != str(home) and (folder / "xdg.txt").read_text(encoding="utf-8") == child_home
    assert not Path(child_home).exists()


# require and dev are below: with no key file beside them the child is isolated, and with one the parent answers in Python
@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
@pytest.mark.parametrize("grant", ["xdg", "session"])
def test_a_child_whose_grant_names_another_key_policy_variable_is_isolated_too(
        fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], grant: str) -> None:
    home = users_home(tmp_path, monkeypatch)
    (name,) = GRANTS[grant]
    monkeypatch.setenv(name, "1")
    folder = probe_binary(fake, monkeypatch)
    answer, _, _ = select(argv, grant=grant)
    assert answer == AgentScript.EXIT_OK and starts(folder) == 1
    assert_isolated_run(folder, home)


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
@pytest.mark.parametrize("grant", ["require", "dev"])
def test_a_child_whose_grant_names_require_or_dev_is_isolated_too_when_no_key_file_exists(
        fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], grant: str) -> None:
    home = users_home(tmp_path, monkeypatch)
    (home / ".config" / "agent-swarm" / "signing-key.hex").unlink()  # nothing beside the child, so the parent does not answer
    (name,) = GRANTS[grant]
    monkeypatch.setenv(name, "1")
    folder = probe_binary(fake, monkeypatch)
    answer, _, _ = select(argv, grant=grant)
    assert answer == AgentScript.EXIT_OK and starts(folder) == 1
    assert_isolated_run(folder, home)


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
@pytest.mark.parametrize("grant", ["require", "dev"])
def test_a_conventional_key_file_beside_an_isolated_child_is_answered_by_python_and_the_binary_is_not_started(
        fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], grant: str) -> None:
    """The isolated binary cannot see the user's key file, so the parent does not start it: Python reads the file and answers."""
    users_home(tmp_path, monkeypatch)
    (name,) = GRANTS[grant]
    monkeypatch.setenv(name, "1")
    folder = probe_binary(fake, monkeypatch)
    answer, out, _ = select(argv, grant=grant)
    assert answer is None and out == b"", "the call is handed to the Python body, which prints nothing here"
    assert starts(folder) == 0


@pytest.mark.parametrize("argv", [SERVED, WRITE_ARGV], ids=["read", "write"])
def test_a_raw_ed25519_key_in_the_callers_environment_keeps_the_binary_call_beside_a_key_file(
        fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    """Raw signing variables take precedence over the conventional file, so the parent starts the binary and does not answer."""
    users_home(tmp_path, monkeypatch)
    monkeypatch.setenv("SWARM_ED25519_KEY", "11" * 32)
    monkeypatch.setenv("SWARM_REQUIRE_KEY", "1")
    folder = probe_binary(fake, monkeypatch)
    answer, _, _ = select(argv, grant="require")
    assert answer == AgentScript.EXIT_OK and starts(folder) == 1
