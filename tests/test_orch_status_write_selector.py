"""orch_status write selector (Phase 14.5 U3.2b): the `--transition` grammar (rust_serves_write against argparse), the engine
resolution of a write, the outcome table of run_rust_write, the effects replay in the Python path's order, and the environment
the binary is started with, against fake gsd_engine binaries that record their starts, stdin and environment."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import random
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import ROOT

from swarm import script_base, substrate_lease, substrate_tee, telemetry
from swarm.taskstore import TaskStore

# the script under test; ORCH_STATUS_SCRIPT points the file at a mutated copy when the teeth of these tests are checked
SCRIPT = Path(os.environ.get("ORCH_STATUS_SCRIPT") or ROOT / "scripts" / "orch_status.py")
ENGINE_ENV = ("GSD_ENGINE", "GSD_ENGINE_BIN", "GSD_RUST_BACKEND", "GSD_RUST_TIMEOUT_S", "SWARM_PURE_PYTHON",
              "SWARM_TASK_ID", "SWARM_CORRELATION_ID")
SIGNING_ENV = ("SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_REQUIRE_KEY")
VOLATILE = ("ts", "msg_id")
WRITE = ["--json", "--transition", "T-1", "CANCELLED", "--reason", "why"]
OK_REPLY = {"agent": "A01", "script": "orch_status", "status": "ok", "task": {"task_id": "T-1", "state": "CANCELLED"},
            "summary": "T-1 → CANCELLED"}
MANIFEST: dict[str, Any] = {
    "effects": [{"type": "task.transition", "payload": {"task_id": "T-1", "state": "CANCELLED", "reason": "why"}, "task_id": None}],
    "post": [{"kind": "live_refresh"}, {"kind": "lease_settle", "task_id": "T-1"},
             {"kind": "inline_transition", "task_id": "T-1", "from": "CREATED", "reason": "why"}],
}
COMMITTED_TYPES = ["swarm.run.state", "swarm.run.finished", "task.transition", "script.orch_status"]
PYTHON_ORDER = ["refresh_live", "settle:T-1", "inline:CREATED->CANCELLED", "emit:task.transition", "emit:script.orch_status"]


def _rendered(reply: dict[str, Any]) -> str:
    return json.dumps(reply, indent=2) + "\n"


def _committed(tmp_path: Path, reply: dict[str, Any] = OK_REPLY) -> dict[str, Any]:
    """`reply` with the task row a binary that had just committed T-1 → CANCELLED prints: the row as the store holds it (the fake's
    COMMIT changes the state only), the row the selector reads after the replay, which Python would print when nothing settles."""
    committed = TaskStore(root=tmp_path)
    row = committed.get("T-1")
    committed.conn.close()
    return {**reply, "task": {**row, "state": "CANCELLED"}}


def _error_reply(code: str, write_state: str | None = None, **details: Any) -> dict[str, Any]:
    details = details if write_state is None else {**details, "write_state": write_state}
    return {"agent": "A01", "script": "orch_status", "status": "error",
            "error": {"code": code, "message": "canned", "details": details, "handling": "none"}}


@dataclass
class Fake:
    folder: Path
    path: Path

    @property
    def starts(self) -> int:
        marker = self.folder / "starts"
        return len(marker.read_text().splitlines()) if marker.exists() else 0

    def seen(self, name: str) -> Any:
        return json.loads((self.folder / name).read_text(encoding="utf-8"))


FAKE_BODY = '''#!{python}
import json, os, resource, sqlite3, sys, time
folder = {folder!r}
cfg = json.load(open(os.path.join(folder, "cfg.json")))
with open(os.path.join(folder, "starts"), "a") as fh:
    fh.write("started\\n")
request = json.load(sys.stdin)
json.dump(request, open(os.path.join(folder, "stdin.json"), "w"))
json.dump(dict(os.environ), open(os.path.join(folder, "env.json"), "w"))
json.dump(list(resource.getrlimit(resource.RLIMIT_CORE)), open(os.path.join(folder, "rlimit_core.json"), "w"))
effects = request.get("effects_path")
present = bool(effects) and os.path.exists(effects)
seen = {{"exists": present, "size": os.path.getsize(effects) if present else None, "mode": oct(os.stat(effects).st_mode & 0o777) if present else None}}
json.dump(seen, open(os.path.join(folder, "effects.json"), "w"))
if cfg["set_state"]:
    tid, state, *stamp = cfg["set_state"]  # an optional third value is the row's updated_at, as a committing binary stamps it
    conn = sqlite3.connect(os.path.join(request["env"]["SWARM_DIR"], "tasks.db"))  # the binary's environment is an allowlist: the request carries it
    conn.execute("UPDATE tasks SET state=?" + (", updated_at=?" if stamp else "") + " WHERE task_id=?", (state, *stamp, tid))
    conn.commit()
if cfg["manifest"] is not None and effects:
    text = cfg["manifest"] if isinstance(cfg["manifest"], str) else json.dumps(cfg["manifest"])
    open(effects, "w").write(text)
time.sleep(cfg["sleep"])
if cfg["kill"]:
    os.kill(os.getpid(), 9)
sys.stdout.write(cfg["stdout"])
sys.stdout.flush()
sys.exit(cfg["exit"])
'''


@pytest.fixture
def mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/orch_status.py as a module, in a clean engine environment, with the release binary pointed at nothing."""
    for key in (*ENGINE_ENV, *SIGNING_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.setattr(substrate_tee, "tee", lambda record, root=None: None)
    spec = importlib.util.spec_from_file_location("orch_status_write_selector_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RELEASE_BINARY", tmp_path / "no-release" / "gsd_engine")
    return module


@pytest.fixture
def fake_binary(tmp_path: Path) -> Callable[..., Fake]:
    """Builds a fake gsd_engine (a Python script). It records each start, its stdin object, its environment and what it saw at
    `effects_path`; then it can set a task's state in the store (a stand-in for the COMMIT), write a manifest, sleep, kill itself
    with SIGKILL, and print `stdout` with exit status `exit`."""
    made = 0

    def build(*, stdout: str = "", exit: int = 0, manifest: Any = None, set_state: tuple[str, str] | None = None,
              sleep: float = 0, kill: bool = False) -> Fake:
        nonlocal made
        made += 1
        folder = tmp_path / f"fake{made}"
        folder.mkdir()
        cfg = {"stdout": stdout, "exit": exit, "manifest": manifest, "set_state": set_state, "sleep": sleep, "kill": kill}
        (folder / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
        path = folder / "gsd_engine"
        path.write_text(FAKE_BODY.format(python=sys.executable, folder=str(folder)), encoding="utf-8")
        path.chmod(0o755)
        return Fake(folder, path)

    return build


@pytest.fixture
def store(mod: ModuleType, tmp_path: Path) -> None:
    """The task store of the test state, holding T-1 in CREATED."""
    seeded = TaskStore(root=tmp_path)
    seeded.create(task_id="T-1", correlation_id="C-1", capability="backend")
    seeded.conn.close()


@pytest.fixture
def answer(mod: ModuleType, capsysbinary: pytest.CaptureFixture[bytes]) -> Callable[[list[str]], tuple[int, str]]:
    """What `python3 scripts/orch_status.py <argv>` does: the selector, then the Python body when the selector hands the call over."""

    def go(argv: list[str]) -> tuple[int, str]:
        code = mod.select_engine(argv)
        if code is None:
            code = mod.SCRIPT.main(argv)
        return code, capsysbinary.readouterr().out.decode("utf-8")

    return go


@pytest.fixture
def trace(mod: ModuleType, store: None, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The order in which the post-commit steps and run-log writes happen, whichever path runs them."""
    order: list[str] = []
    real_emit, real_inline = script_base.emit, mod.emit_inline_transition

    def emit(event_type: str, payload: Any, **kwargs: Any) -> Any:
        order.append(f"emit:{event_type}")
        return real_emit(event_type, payload, **kwargs)

    def inline(task_store: Any, task: Any, before: str, after: str, **kwargs: Any) -> Any:
        order.append(f"inline:{before}->{after}")
        return real_inline(task_store, task, before, after, **kwargs)

    monkeypatch.setattr(script_base, "emit", emit)
    monkeypatch.setattr(mod, "emit_inline_transition", inline)
    monkeypatch.setattr(substrate_lease, "settle_mirrored", lambda task_store, tid, **kwargs: order.append(f"settle:{tid}") or "off")
    monkeypatch.setattr(telemetry, "refresh_live", lambda *args, **kwargs: order.append("refresh_live") or True)
    return order


def _use(monkeypatch: pytest.MonkeyPatch, mod: ModuleType, fake: Fake, *, explicit: bool) -> None:
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    if explicit:
        monkeypatch.setenv("GSD_ENGINE", "rust")
    else:
        monkeypatch.setattr(mod, "DEFAULT_ENGINE_WRITE", "rust")


def _events(tmp_path: Path, state_dir: str = ".swarm") -> list[dict[str, Any]]:
    log = tmp_path / state_dir / "events.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.exists() else []


def _types(tmp_path: Path) -> list[str]:
    return [event["type"] for event in _events(tmp_path)]


def _state(tmp_path: Path, state_dir: str = ".swarm") -> str:
    with contextlib.closing(sqlite3.connect(tmp_path / state_dir / "tasks.db")) as conn:
        return str(conn.execute("SELECT state FROM tasks WHERE task_id='T-1'").fetchone()[0])


def _cancelled_rows(tmp_path: Path) -> int:
    with contextlib.closing(sqlite3.connect(tmp_path / ".swarm" / "tasks.db")) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM transitions WHERE task_id='T-1' AND to_state='CANCELLED'").fetchone()[0])


def _cleaned(fake: Fake) -> None:
    """The selector made the effects file before the start, empty, and removed it once the call was over."""
    assert fake.seen("effects.json") == {"exists": True, "size": 0, "mode": "0o600"}
    assert not Path(fake.seen("stdin.json")["effects_path"]).exists()


MODES = pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "default"])


# --- the grammar ------------------------------------------------------------------------------------------------------------

SERVED_WRITE = {
    "minimal": ["--json", "--transition", "T-1", "DONE"],
    "json last": ["--transition", "T-1", "DONE", "--json"],
    "json around": ["--json", "--transition", "T-1", "DONE", "--json"],
    "reason": ["--json", "--transition", "T-1", "DONE", "--reason", "why not"],
    "reason inline": ["--json", "--transition", "T-1", "DONE", "--reason=why not"],
    "reason inline with dash": ["--json", "--transition", "T-1", "DONE", "--reason=-x"],
    "reason inline empty": ["--json", "--transition", "T-1", "DONE", "--reason="],
    "reason separate empty": ["--json", "--transition", "T-1", "DONE", "--reason", ""],
    "reason with a dash and a space": ["--json", "--transition", "T-1", "DONE", "--reason", "ok -x"],
    "root": ["--root", "/x", "--json", "--transition", "T-1", "DONE"],
    "repo alias": ["--repo", "/x", "--json", "--transition", "T-1", "DONE"],
    "root inline with an equals sign": ["--root=/x=y", "--json", "--transition", "T-1", "DONE"],
    "dry run": ["--json", "--dry-run", "--transition", "T-1", "DONE"],
    "dry run twice": ["--json", "--dry-run", "--dry-run", "--transition", "T-1", "DONE"],
    "json twice": ["--json", "--json", "--transition", "T-1", "DONE"],
    "ids": ["--json", "--transition", "T-1", "DONE", "--task-id", "t-1", "--correlation-id", "c-1"],
    "ids inline": ["--json", "--transition", "T-1", "DONE", "--task-id=t-1", "--correlation-id=c-1"],
    "everything": ["--repo=/x", "--dry-run", "--json", "--reason", "r", "--task-id", "t", "--correlation-id", "c",
                   "--transition", "T-1", "DONE"],
}

UNSERVED_WRITE = {
    "empty": [],
    "json only (a read)": ["--json"],
    "transition without json": ["--transition", "T-1", "DONE"],
    "transition with one value": ["--json", "--transition", "T-1"],
    "transition with no value": ["--json", "--transition"],
    "transition value is a flag": ["--json", "--transition", "T-1", "--dry-run"],
    "transition first value is a flag": ["--json", "--transition", "--dry-run", "DONE"],
    "transition empty task": ["--json", "--transition", "", "DONE"],
    "transition empty state": ["--json", "--transition", "T-1", ""],
    "transition dash value": ["--json", "--transition", "-T", "DONE"],
    "transition inline": ["--json", "--transition=T-1", "DONE"],
    "transition twice": ["--json", "--transition", "T-1", "DONE", "--transition", "T-2", "DONE"],
    "extra positional": ["--json", "--transition", "T-1", "DONE", "extra"],
    "leading positional": ["extra", "--json", "--transition", "T-1", "DONE"],
    "ingest": ["--json", "--ingest", "f"],
    "ingest with transition": ["--json", "--transition", "T-1", "DONE", "--ingest", "f"],
    "advisory": ["--json", "--transition", "T-1", "DONE", "--advisory"],
    "history": ["--json", "--transition", "T-1", "DONE", "--history", "T-1"],
    "unknown flag": ["--json", "--transition", "T-1", "DONE", "--unknown"],
    "abbreviated transition": ["--json", "--trans", "T-1", "DONE"],
    "abbreviated reason": ["--json", "--transition", "T-1", "DONE", "--rea", "x"],
    "short flag": ["--json", "--transition", "T-1", "DONE", "-x=1"],
    "help": ["--json", "--transition", "T-1", "DONE", "-h"],
    "double dash": ["--json", "--transition", "T-1", "DONE", "--"],
    "json with a value": ["--json=1", "--transition", "T-1", "DONE"],
    "json with an empty value": ["--json=", "--transition", "T-1", "DONE"],
    "dry run with a value": ["--json", "--dry-run=1", "--transition", "T-1", "DONE"],
    "reason without a value": ["--json", "--transition", "T-1", "DONE", "--reason"],
    "reason value is a flag": ["--json", "--transition", "T-1", "DONE", "--reason", "--json"],
    "reason value starts with a dash": ["--json", "--transition", "T-1", "DONE", "--reason", "-x"],
    "reason twice": ["--json", "--transition", "T-1", "DONE", "--reason", "a", "--reason", "b"],
    "root twice": ["--root", "/a", "--root", "/b", "--json", "--transition", "T-1", "DONE"],
    "root and repo": ["--root", "/a", "--repo", "/b", "--json", "--transition", "T-1", "DONE"],
    "task id twice": ["--json", "--transition", "T-1", "DONE", "--task-id", "a", "--task-id", "b"],
    "json hidden in a value": ["--transition", "T-1", "DONE", "--root=--json"],
}


@pytest.mark.parametrize("argv", SERVED_WRITE.values(), ids=SERVED_WRITE.keys())
def test_rust_serves_write_accepts_the_transition_grammar(argv: list[str], mod: ModuleType) -> None:
    assert mod.rust_serves_write(argv) is True


@pytest.mark.parametrize("argv", UNSERVED_WRITE.values(), ids=UNSERVED_WRITE.keys())
def test_rust_serves_write_refuses_everything_else(argv: list[str], mod: ModuleType) -> None:
    assert mod.rust_serves_write(argv) is False


# pieces of a served call (one flag with its value), and pieces that are not (or that repeat a served flag)
SERVED_PIECES = [["--json"], ["--dry-run"], ["--reason", "why"], ["--reason=why"], ["--reason", "a b"], ["--reason", ""], ["--reason=-x"],
                 ["--root", "/r"], ["--repo", "/r"], ["--root=/r=1"], ["--task-id", "t-1"], ["--task-id=t-2"],
                 ["--correlation-id", "c-1"], ["--correlation-id=c-2"]]
OTHER_PIECES = [["--transition", "T-1"], ["--transition=T-1", "DONE"], ["--transition", "", "DONE"], ["--transition", "-x", "DONE"],
                ["--transition", "T-1", "--json"], ["--reason", "-x"], ["--reason"], ["--correlation-id"], ["--ingest", "f"],
                ["--advisory"], ["--history", "T-1"], ["--hist", "T"], ["--trans", "T", "S"], ["-h"], ["extra"], ["--"], ["-1"], [""],
                ["--json=1"], ["--dry-run=1"], ["--unknown"], ["--reason", "a", "--reason", "b"], ["--transition", "T-2", "DONE"]]


def _corpus(count: int) -> list[list[str]]:
    """A seeded sample of argv around the served grammar: a `--json --transition T S` core, valid pieces in any order and number
    (so repeats occur), optional unserved pieces, and token-level damage (a token dropped, repeated or swapped for another)."""
    rng = random.Random(1405)
    corpus = []
    for _ in range(count):
        pieces = [["--json"], ["--transition", "T-1", "DONE"], *rng.choices(SERVED_PIECES, k=rng.randint(0, 4)),
                  *rng.choices(OTHER_PIECES, k=rng.choice((0, 0, 1, 2)))]
        rng.shuffle(pieces)
        tokens = [token for piece in pieces for token in piece]
        if rng.random() < 0.3:
            at = rng.randrange(len(tokens))
            tokens[at:at + 1] = rng.choice(([], [tokens[at]] * 2, [rng.choice(rng.choice(SERVED_PIECES + OTHER_PIECES))]))
        corpus.append(tokens)
    return corpus


def _argparse_of(parser: Any, argv: list[str]) -> Any:
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        try:
            return parser.parse_args(argv)
        except SystemExit:
            return None


def test_rust_serves_write_accepts_only_what_argparse_parses_the_same_way(mod: ModuleType) -> None:
    """The oracle is the script's own argparse parser: every argv the hand parser accepts parses to the same transition, reason,
    root, ids and flags, and no argv argparse rejects is accepted. The corpus is seeded and random, see `_corpus`."""
    parser = mod.SCRIPT.parser()
    corpus = [*_corpus(4000), *(list(argv) for argv in (*SERVED_WRITE.values(), *UNSERVED_WRITE.values()))]
    served = refused_but_parsed = 0
    for argv in corpus:
        call = mod.parse_write_argv(argv)
        parsed = _argparse_of(parser, argv)
        if call is None:
            refused_but_parsed += parsed is not None
            continue
        served += 1
        assert parsed is not None, argv
        assert (parsed.json, parsed.transition, parsed.reason, parsed.root, parsed.dry_run, parsed.task_id, parsed.correlation_id,
                parsed.ingest, parsed.advisory, parsed.history) == (
            True, [call.target, call.state], call.reason, call.root, call.dry_run, call.task_id, call.correlation_id,
            None, False, None), argv
    assert served >= 500 and refused_but_parsed >= 500 and served + refused_but_parsed < len(corpus)  # a corpus with all three kinds


def test_hand_parser_defaults_the_ids_from_the_environment_as_argparse_does(mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_TASK_ID", "env-task")
    monkeypatch.setenv("SWARM_CORRELATION_ID", "")
    argv = ["--json", "--transition", "T-1", "DONE"]
    call = mod.parse_write_argv(argv)
    parsed = mod.SCRIPT.parser().parse_args(argv)
    assert (call.task_id, call.correlation_id) == (parsed.task_id, parsed.correlation_id) == ("env-task", None)
    flagged = mod.parse_write_argv([*argv, "--task-id", "flag-task"])
    assert flagged.task_id == "flag-task"


# --- engine resolution of a write -------------------------------------------------------------------------------------------

# env, DEFAULT_ENGINE, DEFAULT_ENGINE_WRITE, argv, outcome: "python" (binary never started), "rust" (started) or an error code
WRITE_RESOLUTION = {
    "pure python beats explicit rust": ({"SWARM_PURE_PYTHON": "1", "GSD_ENGINE": "rust"}, "rust", "rust", WRITE, "python"),
    "pure python beats an invalid engine": ({"SWARM_PURE_PYTHON": "1", "GSD_ENGINE": "bogus"}, "rust", "rust", WRITE, "python"),
    "explicit python under rust defaults": ({"GSD_ENGINE": "python"}, "rust", "rust", WRITE, "python"),
    "explicit rust under python defaults": ({"GSD_ENGINE": "rust"}, "python", "python", WRITE, "rust"),
    "unset under a python write default": ({}, "python", "python", WRITE, "python"),
    "unset under a python write default and a rust read default": ({}, "rust", "python", WRITE, "python"),
    "empty under a python write default and a rust read default": ({"GSD_ENGINE": ""}, "rust", "python", WRITE, "python"),
    "unset under a rust write default": ({}, "python", "rust", WRITE, "rust"),
    "empty under a rust write default": ({"GSD_ENGINE": ""}, "python", "rust", WRITE, "rust"),
    "a rust write default does not move reads": ({}, "python", "rust", ["--json"], "python"),
    "default rust with an argv outside the grammar": ({}, "python", "rust", [*WRITE, "--advisory"], "python"),
    "default rust with an ingest": ({}, "python", "rust", ["--json", "--ingest", "f"], "python"),
    "explicit rust with an argv outside the grammar": ({"GSD_ENGINE": "rust"}, "python", "python", [*WRITE, "--advisory"], "rust"),
    "unknown value": ({"GSD_ENGINE": "auto"}, "python", "python", WRITE, "E-INPUT"),
    "capitalised value": ({"GSD_ENGINE": "Rust"}, "python", "rust", WRITE, "E-INPUT"),
    "bad backend under explicit rust": ({"GSD_ENGINE": "rust", "GSD_RUST_BACKEND": "gpu"}, "python", "python", WRITE, "E-INPUT"),
}


@pytest.mark.parametrize(("env", "read_default", "write_default", "argv", "outcome"), WRITE_RESOLUTION.values(),
                         ids=WRITE_RESOLUTION.keys())
def test_write_resolution_order(env: dict[str, str], read_default: str, write_default: str, argv: list[str], outcome: str,
                                mod: ModuleType, fake_binary: Callable[..., Fake], store: None, monkeypatch: pytest.MonkeyPatch,
                                capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY), manifest=MANIFEST)
    monkeypatch.setattr(mod, "DEFAULT_ENGINE", read_default)
    monkeypatch.setattr(mod, "DEFAULT_ENGINE_WRITE", write_default)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    code = mod.select_engine(argv)
    out = capsysbinary.readouterr().out

    if outcome == "python":
        assert (code, out, fake.starts) == (None, b"", 0)
    elif outcome == "rust":
        assert fake.starts == 1
    else:
        assert (code, json.loads(out)["error"]["code"], fake.starts) == (2, outcome, 0)


def test_explicit_rust_without_a_binary_is_e_dep_for_a_write(mod: ModuleType, monkeypatch: pytest.MonkeyPatch,
                                                              capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    monkeypatch.setenv("GSD_ENGINE", "rust")

    assert mod.select_engine(WRITE) == 2
    error = json.loads(capsysbinary.readouterr().out)["error"]
    assert (error["code"], error["message"]) == ("E-DEP", "GSD_ENGINE=rust needs GSD_ENGINE_BIN set to the gsd_engine binary")


def test_the_default_write_call_without_a_startable_binary_is_python(mod: ModuleType, fake_binary: Callable[..., Fake],
                                                                       tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = fake_binary()
    plain.path.chmod(0o644)
    monkeypatch.setattr(mod, "DEFAULT_ENGINE_WRITE", "rust")
    for named in (tmp_path / "missing", plain.path):
        monkeypatch.setenv("GSD_ENGINE_BIN", str(named))
        assert mod.select_engine(WRITE) is None
    assert plain.starts == 0


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "default"])
def test_a_binary_that_cannot_be_started_is_python_by_default_and_e_dep_when_explicit(
        explicit: bool, mod: ModuleType, store: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        answer: Callable[[list[str]], tuple[int, str]]) -> None:
    broken = tmp_path / "broken"
    broken.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    broken.chmod(0o755)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(broken))
    if explicit:
        monkeypatch.setenv("GSD_ENGINE", "rust")
    else:
        monkeypatch.setattr(mod, "DEFAULT_ENGINE_WRITE", "rust")

    code, out = answer(WRITE)

    if explicit:
        assert (code, json.loads(out)["error"]["code"], _state(tmp_path)) == (2, "E-DEP", "CREATED")
    else:
        assert (code, _state(tmp_path), _cancelled_rows(tmp_path)) == (0, "CANCELLED", 1)


def test_an_ingest_is_a_write_route_and_a_transition_flip_does_not_start_it(mod: ModuleType, fake_binary: Callable[..., Fake],
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """U3.5e reversed the old premise (ingest stayed a read until it had a route). It is a write route now:
    an explicit rust call carries an effects file, and flipping only the transition default does not start it."""
    fake = fake_binary(stdout=_rendered(OK_REPLY))
    monkeypatch.setattr(mod, "DEFAULT_ENGINE_WRITE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    assert mod.select_engine(["--json", "--ingest", "f"]) is None and fake.starts == 0

    monkeypatch.setenv("GSD_ENGINE", "rust")
    mod.select_engine(["--json", "--ingest", "f"])

    assert fake.starts == 1 and "effects_path" in fake.seen("stdin.json")


# --- what the binary is sent ------------------------------------------------------------------------------------------------

def test_a_write_request_carries_an_effects_file_the_selector_owns(mod: ModuleType, fake_binary: Callable[..., Fake], store: None,
                                                                    tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_binary(stdout=_rendered(_error_reply("E-DEP", "not_started")), exit=2)
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("SWARM_TASK_ID", "T-env")
    monkeypatch.setenv("GSD_RUST_BACKEND", "host")

    mod.select_engine(WRITE)

    sent = fake.seen("stdin.json")
    effects_path = Path(sent.pop("effects_path"))
    assert sent == {"agent": "A01", "script": "orch_status", "argv": WRITE, "root": os.getcwd(),
                    "env": {"SWARM_DIR": str(tmp_path / ".swarm"), "SWARM_TASK_ID": "T-env"}}
    assert effects_path.parent == tmp_path / "tmp"
    _cleaned(fake)


# --- the environment of the binary ------------------------------------------------------------------------------------------

# A recorded decision reverses the premise of this test (design item 13 of phase 14.5, D-P30): the binary's environment was every GSD_* and
# SWARM_* variable minus a deny-list of key names; it is now an explicit allowlist of names (BINARY_ENV_NAMES). The swarm directory and the
# ids reach the binary in the request's `env` (asserted above), GSD_ENGINE and SWARM_TASKSTORE_WAL are read by nobody in it, and a name
# nobody listed is not handed on. The signing variables go only to a transition to APPROVED (the next test).
@pytest.mark.parametrize("argv", [["--json"], WRITE], ids=["read", "write"])
def test_the_binary_gets_an_allowlist_of_names_and_no_signing_keys_outside_approved(
        argv: list[str], mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_binary(stdout=_rendered(_error_reply("E-DEP", "not_started")), exit=2)
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_RUST_BACKEND", "host")
    monkeypatch.setenv("SWARM_TASKSTORE_WAL", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("UNRELATED_TOKEN", "canary-unrelated")
    monkeypatch.setenv("SWARM_AGENT_SESSION", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for name in SIGNING_ENV:
        monkeypatch.setenv(name, f"canary-{name}")

    mod.select_engine(argv)

    environment = fake.seen("env.json")
    assert fake.starts == 1
    assert not set(SIGNING_ENV) & set(environment)
    assert not [value for value in environment.values() if "canary-" in value]
    assert not {"UNRELATED_TOKEN", "GSD_ENGINE", "GSD_ENGINE_BIN", "SWARM_DIR", "SWARM_TASKSTORE_WAL", "SWARM_AGENT_SESSION",
                "XDG_CONFIG_HOME"} & set(environment)
    assert {key: environment.get(key) for key in ("GSD_RUST_BACKEND", "HOME", "LANG")} == {
        "GSD_RUST_BACKEND": "host", "HOME": str(tmp_path / "home"), "LANG": "C.UTF-8"}
    assert environment["PATH"] == os.environ["PATH"]
    assert set(environment) - {"LC_CTYPE"} <= set(mod.BINARY_ENV_NAMES)  # LC_CTYPE: the fake is a Python script and coerces a C locale


APPROVED_GRANT = (*SIGNING_ENV, "SWARM_AGENT_SESSION", "XDG_CONFIG_HOME")


@pytest.mark.parametrize(
    ("target", "flags", "granted"),
    [
        ("CANCELLED", [], False),
        ("VALIDATED", [], False),
        ("DONE", [], False),
        ("approved", [], False),  # not a state name: Rust refuses it and Python raises, so no key is needed
        ("APPROVED", ["--dry-run"], False),  # a dry run verifies nothing
        ("APPROVED", [], True),
        ("APPROVED", ["--reason", "signed"], True),
    ],
    ids=["cancelled", "validated", "done", "lowercase", "approved-dry-run", "approved", "approved-with-reason"],
)
def test_only_a_real_transition_to_approved_is_handed_the_key_variables(
        target: str, flags: list[str], granted: bool, mod: ModuleType, fake_binary: Callable[..., Fake], store: None,
        monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_binary(stdout=_rendered(_error_reply("E-DEP", "not_started")), exit=2)
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", hashlib.sha256(fake.path.read_bytes()).hexdigest())
    monkeypatch.setenv("SWARM_AGENT_SESSION", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    for name in SIGNING_ENV:
        monkeypatch.setenv(name, f"canary-{name}")

    mod.select_engine(["--json", "--transition", "T-1", target, *flags])

    environment = fake.seen("env.json")
    assert fake.starts == 1, "the call reached the binary"
    handed = set(environment) & set(APPROVED_GRANT)
    assert handed == (set(APPROVED_GRANT) if granted else set())
    for name in handed & set(SIGNING_ENV):
        assert environment[name] == f"canary-{name}"
    request_text = json.dumps(fake.seen("stdin.json"))
    assert "canary-" not in request_text, "a key never rides in the request"


@pytest.fixture
def core_limit() -> Any:
    """This process's `RLIMIT_CORE` raised to a non-zero soft limit (the host's own is often 0, which would hide a child that sets none),
    put back afterwards. Yields (soft, hard); a host whose hard limit is 0 cannot show the difference, and the test says so."""
    original = resource.getrlimit(resource.RLIMIT_CORE)
    hard = original[1]
    if hard == 0:
        pytest.skip("the hard RLIMIT_CORE of this host is 0: a child cannot be told apart from one that lowered it")
    soft = 1 << 20 if hard == resource.RLIM_INFINITY else min(hard, 1 << 20)
    resource.setrlimit(resource.RLIMIT_CORE, (soft, hard))
    try:
        yield soft, hard
    finally:
        resource.setrlimit(resource.RLIMIT_CORE, original)


@pytest.mark.parametrize(
    ("target", "flags", "expected_zero"),
    [("APPROVED", [], True), ("APPROVED", ["--dry-run"], False), ("CANCELLED", [], False)],
    ids=["approved", "approved-dry-run", "cancelled"],
)
def test_only_the_approved_child_is_started_with_core_dumps_off(
        target: str, flags: list[str], expected_zero: bool, mod: ModuleType, fake_binary: Callable[..., Fake], store: None,
        monkeypatch: pytest.MonkeyPatch, core_limit: tuple[int, int]) -> None:
    """The process that holds the signing keys is started with `RLIMIT_CORE` 0, soft and hard, so a crash leaves no core file with them. The
    fake binary reports its own limit; every other child inherits this process's, and this process's limit does not change."""
    fake = fake_binary(stdout=_rendered(_error_reply("E-DEP", "not_started")), exit=2)
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))

    mod.select_engine(["--json", "--transition", "T-1", target, *flags])

    assert fake.starts == 1
    assert fake.seen("rlimit_core.json") == ([0, 0] if expected_zero else list(core_limit))
    assert resource.getrlimit(resource.RLIMIT_CORE) == core_limit, "the selector's own limit is not touched"


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "default"])
def test_a_child_that_cannot_be_given_the_core_limit_is_not_started_and_is_a_start_failure(
        explicit: bool, mod: ModuleType, fake_binary: Callable[..., Fake], store: None, monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    """A `preexec_fn` that fails ends the child before its exec, so nothing was started: Python answers by default and E-DEP under
    GSD_ENGINE=rust, exactly as for a binary that cannot be started. It is never an uncaught SubprocessError."""

    def refuse() -> None:
        raise OSError("no limits for you")

    fake = fake_binary(stdout=_rendered(_error_reply("E-DEP", "not_started")), exit=2)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    monkeypatch.setattr(mod, "_no_core_dumps", refuse)
    if explicit:
        monkeypatch.setenv("GSD_ENGINE", "rust")
    else:
        monkeypatch.setattr(mod, "DEFAULT_ENGINE_WRITE", "rust")

    code = mod.select_engine(["--json", "--transition", "T-1", "APPROVED"])

    assert fake.starts == 0
    if explicit:
        assert code == 2 and json.loads(capsysbinary.readouterr().out)["error"]["code"] == "E-DEP"
    else:
        assert code is None, "the Python body answers"


# --- the outcome table ------------------------------------------------------------------------------------------------------

@MODES
@pytest.mark.parametrize("code", ["E-DEP", "E-INTERNAL", "E-INPUT", "E-CAPACITY"])
def test_a_refusal_before_the_first_write_is_answered_by_python_once(
        code: str, explicit: bool, mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, answer: Callable[[list[str]], tuple[int, str]]) -> None:
    fake = fake_binary(stdout=_rendered(_error_reply(code, "not_started")), exit=2)
    _use(monkeypatch, mod, fake, explicit=explicit)

    exit_code, out = answer(WRITE)

    assert (exit_code, json.loads(out)["task"]["state"]) == (0, "CANCELLED")
    assert (fake.starts, _cancelled_rows(tmp_path)) == (1, 1)
    assert _types(tmp_path) == COMMITTED_TYPES  # the Python path's own records, nothing of the binary's
    _cleaned(fake)


# code, write_state: refusals the binary decided itself, with Python's text, which are printed as they are
FINAL_REFUSALS = {
    "illegal transition": ("E-CONTRACT", "final"),
    "manual transition rule": ("E-POLICY", "final"),
    "unknown task without a write state": ("E-INPUT", None),
    "contract without a write state": ("E-CONTRACT", None),
    "policy without a write state": ("E-POLICY", None),
    "dependency without a write state": ("E-DEP", None),
    "internal error that says it is final": ("E-INTERNAL", "final"),
}


@MODES
@pytest.mark.parametrize(("code", "write_state"), FINAL_REFUSALS.values(), ids=FINAL_REFUSALS.keys())
def test_a_refusal_after_the_transaction_began_is_printed_as_it_is(
        code: str, write_state: str | None, explicit: bool, mod: ModuleType, fake_binary: Callable[..., Fake], store: None,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: Callable[[list[str]], tuple[int, str]]) -> None:
    reply = _error_reply(code, write_state)
    fake = fake_binary(stdout=_rendered(reply), exit=2)
    _use(monkeypatch, mod, fake, explicit=explicit)

    assert answer(WRITE) == (2, _rendered(reply))

    assert (fake.starts, _state(tmp_path), _types(tmp_path)) == (1, "CREATED", ["script.orch_status.error"])
    _cleaned(fake)


@MODES
def test_pythons_own_e_timeout_refusal_names_its_task_and_is_printed_as_it_is(
        explicit: bool, mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, answer: Callable[[list[str]], tuple[int, str]]) -> None:
    """`max_attempts exceeded` is E-TIMEOUT too, with details.task_id: a definitive refusal, unlike the deadline's E-TIMEOUT."""
    reply = _error_reply("E-TIMEOUT", task_id="T-1")
    fake = fake_binary(stdout=_rendered(reply), exit=2)
    _use(monkeypatch, mod, fake, explicit=explicit)

    assert answer(WRITE) == (2, _rendered(reply))

    assert (fake.starts, _state(tmp_path), _types(tmp_path)) == (1, "CREATED", ["script.orch_status.error"])


# the binary's answer when the outcome is unknown: Python never runs, nothing falls back
UNKNOWN_OUTCOMES: dict[str, dict[str, Any]] = {
    "E-TIMEOUT": {"stdout": _rendered(_error_reply("E-TIMEOUT")), "exit": 2},
    "E-TIMEOUT that says not started": {"stdout": _rendered(_error_reply("E-TIMEOUT", "not_started")), "exit": 2},
    "E-TIMEOUT of the call deadline with other details": {"stdout": _rendered(_error_reply("E-TIMEOUT", elapsed_s=601)), "exit": 2},
    "E-TIMEOUT whose task id is not text": {"stdout": _rendered(_error_reply("E-TIMEOUT", task_id=7)), "exit": 2},
    "killed with SIGKILL": {"kill": True},
    "exit status 3 with a reply": {"stdout": _rendered(OK_REPLY), "exit": 3},
    "exit status 255": {"stdout": _rendered(OK_REPLY), "exit": 255},
    "garbage on exit 0": {"stdout": "this is not json", "exit": 0},
    "garbage on exit 1": {"stdout": "this is not json", "exit": 1},
    "garbage on exit 2": {"stdout": "this is not json", "exit": 2},
    "nothing on exit 0": {"stdout": "", "exit": 0},
    "a JSON list on exit 0": {"stdout": "[1]", "exit": 0},
    "exit 2 without an error code": {"stdout": _rendered({"status": "error"}), "exit": 2},
    "E-INTERNAL without a write state": {"stdout": _rendered(_error_reply("E-INTERNAL")), "exit": 2},
    "an unknown write state": {"stdout": _rendered(_error_reply("E-CONTRACT", "maybe")), "exit": 2},
}


@MODES
@pytest.mark.parametrize("behaviour", UNKNOWN_OUTCOMES.values(), ids=UNKNOWN_OUTCOMES.keys())
def test_an_unknown_outcome_is_refused_and_never_repeated(
        behaviour: dict[str, Any], explicit: bool, mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, answer: Callable[[list[str]], tuple[int, str]]) -> None:
    fake = fake_binary(**behaviour)
    _use(monkeypatch, mod, fake, explicit=explicit)

    code, out = answer(WRITE)

    error = json.loads(out)["error"]
    assert (code, error["code"], error["details"]) == (2, "E-INTERNAL", {"write_state": "unknown", "task_id": "T-1"})
    assert "outcome unknown" in error["message"] and "NOT repeated" in error["message"] and "--history T-1" in error["message"]
    assert (fake.starts, _state(tmp_path), _cancelled_rows(tmp_path)) == (1, "CREATED", 0)  # Python did not run the call
    recorded = _events(tmp_path)
    assert [event["type"] for event in recorded] == ["script.orch_status.error"]
    assert recorded[0]["payload"]["error"]["code"] == "E-INTERNAL"
    _cleaned(fake)


@MODES
def test_a_binary_past_the_call_deadline_is_an_unknown_outcome_not_a_rerun(
        explicit: bool, mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, answer: Callable[[list[str]], tuple[int, str]]) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY), sleep=30)
    _use(monkeypatch, mod, fake, explicit=explicit)
    monkeypatch.setenv("GSD_RUST_TIMEOUT_S", "2")

    code, out = answer(WRITE)

    error = json.loads(out)["error"]
    assert (code, error["code"], "deadline" in error["message"], _state(tmp_path)) == (2, "E-INTERNAL", True, "CREATED")
    assert (fake.starts, _types(tmp_path)) == (1, ["script.orch_status.error"])
    assert not Path(fake.seen("stdin.json")["effects_path"]).exists()


def test_the_unknown_outcome_message_names_the_signal(mod: ModuleType, fake_binary: Callable[..., Fake], store: None,
                                                      monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    _use(monkeypatch, mod, fake_binary(kill=True), explicit=True)

    mod.select_engine(WRITE)

    assert "killed by signal 9" in json.loads(capsysbinary.readouterr().out)["error"]["message"]


# --- success: the replay ----------------------------------------------------------------------------------------------------

@MODES
@pytest.mark.parametrize("exit_code", [0, 1])
def test_a_committed_answer_is_replayed_recorded_and_printed(
        exit_code: int, explicit: bool, mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, answer: Callable[[list[str]], tuple[int, str]]) -> None:
    reply = _committed(tmp_path)
    fake = fake_binary(stdout=_rendered(reply), exit=exit_code, manifest=MANIFEST, set_state=("T-1", "CANCELLED"))
    _use(monkeypatch, mod, fake, explicit=explicit)

    assert answer(WRITE) == (exit_code, _rendered(reply))

    assert (fake.starts, _state(tmp_path)) == (1, "CANCELLED")
    assert _types(tmp_path) == COMMITTED_TYPES
    _cleaned(fake)


def test_the_replay_runs_in_the_order_of_the_python_path(mod: ModuleType, fake_binary: Callable[..., Fake], store: None,
                                                          trace: list[str], monkeypatch: pytest.MonkeyPatch,
                                                          answer: Callable[[list[str]], tuple[int, str]]) -> None:
    monkeypatch.setenv("GSD_ENGINE", "python")
    answer(WRITE)
    python_order = list(trace)
    assert python_order == PYTHON_ORDER

    with sqlite3.connect(Path(os.environ["SWARM_DIR"]) / "tasks.db") as conn:
        conn.execute("UPDATE tasks SET state='CREATED' WHERE task_id='T-1'")
    trace.clear()
    fake = fake_binary(stdout=_rendered(OK_REPLY), manifest=MANIFEST, set_state=("T-1", "CANCELLED"))
    _use(monkeypatch, mod, fake, explicit=True)
    answer(WRITE)

    assert (fake.starts, trace) == (1, python_order)


def test_the_replay_writes_the_run_log_the_python_path_writes(mod: ModuleType, fake_binary: Callable[..., Fake], tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch,
                                                              answer: Callable[[list[str]], tuple[int, str]]) -> None:
    def seed(directory: str) -> None:
        monkeypatch.setenv("SWARM_DIR", str(tmp_path / directory))
        seeded = TaskStore(root=tmp_path)
        seeded.create(task_id="T-1", correlation_id="C-1", capability="backend")
        seeded.conn.close()

    def stable(directory: str) -> list[dict[str, Any]]:
        return [{key: value for key, value in event.items() if key not in VOLATILE} for event in _events(tmp_path, directory)]

    seed("python-state")
    shutil.copytree(tmp_path / "python-state", tmp_path / "rust-state")  # the same store, created_at included, before either call
    monkeypatch.setenv("GSD_ENGINE", "python")
    python_exit, python_out = answer(WRITE)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / "rust-state"))
    # the binary stamps the commit as Python did, so the row it printed is the row the selector reads: Python's own reply stands
    fake = fake_binary(stdout=python_out, manifest=MANIFEST, set_state=("T-1", "CANCELLED", json.loads(python_out)["task"]["updated_at"]))
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(fake.path))
    rust_exit, rust_out = answer(WRITE)

    assert (rust_exit, rust_out) == (python_exit, python_out)
    assert [event["type"] for event in stable("rust-state")] == COMMITTED_TYPES
    assert stable("rust-state") == stable("python-state")


def test_before_is_the_state_read_ahead_of_the_binary_not_the_manifests(
        mod: ModuleType, fake_binary: Callable[..., Fake], store: None, trace: list[str], monkeypatch: pytest.MonkeyPatch,
        answer: Callable[[list[str]], tuple[int, str]]) -> None:
    lying = json.loads(json.dumps(MANIFEST))
    lying["post"][2]["from"] = "IN_PROGRESS"
    fake = fake_binary(stdout=_rendered(OK_REPLY), manifest=lying, set_state=("T-1", "CANCELLED"))
    _use(monkeypatch, mod, fake, explicit=True)

    answer(WRITE)

    assert "inline:CREATED->CANCELLED" in trace


def test_the_manifests_from_stands_in_when_no_state_could_be_read_ahead(mod: ModuleType, store: None, trace: list[str],
                                                                        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    call = mod.parse_write_argv(WRITE)
    manifest = mod.Manifest((), "CREATED", "why")
    with sqlite3.connect(tmp_path / ".swarm" / "tasks.db") as conn:
        conn.execute("UPDATE tasks SET state='CANCELLED' WHERE task_id='T-1'")

    mod._replay(call, manifest, None)

    assert "inline:CREATED->CANCELLED" in trace


def test_state_before_reads_without_creating_or_migrating_anything(mod: ModuleType, store: None, tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    call = mod.parse_write_argv(WRITE)
    assert mod._state_before(call) == "CREATED"
    assert mod._state_before(mod.parse_write_argv(["--json", "--transition", "T-9", "DONE"])) is None

    elsewhere = tmp_path / "empty-state"
    monkeypatch.setenv("SWARM_DIR", str(elsewhere))
    assert mod._state_before(call) is None
    assert not elsewhere.exists()


@pytest.mark.parametrize("manifest", [
    None,
    "",
    "not json",
    "[]",
    {"effects": []},
    {"post": MANIFEST["post"]},
    {"effects": "x", "post": MANIFEST["post"]},
    {"effects": MANIFEST["effects"], "post": MANIFEST["post"][:2]},
    {"effects": MANIFEST["effects"], "post": [*MANIFEST["post"], {"kind": "live_refresh"}]},
    {"effects": MANIFEST["effects"], "post": [*MANIFEST["post"], {"kind": "mystery"}]},
    {"effects": MANIFEST["effects"], "post": [{"kind": "live_refresh"}, {"kind": "lease_settle", "task_id": "T-2"},
                                              MANIFEST["post"][2]]},
    {"effects": MANIFEST["effects"], "post": [MANIFEST["post"][0], MANIFEST["post"][1],
                                              {"kind": "inline_transition", "task_id": "T-2", "from": "CREATED", "reason": "why"}]},
    {"effects": MANIFEST["effects"], "post": [MANIFEST["post"][0], MANIFEST["post"][1],
                                              {"kind": "inline_transition", "task_id": "T-1", "from": "CREATED", "reason": 5}]},
    {"effects": [{"type": "task.transition", "payload": [], "task_id": None}], "post": MANIFEST["post"]},
    {"effects": [{"type": "", "payload": {}, "task_id": None}], "post": MANIFEST["post"]},
    {"effects": [{"type": "task.transition", "payload": {}, "task_id": 7}], "post": MANIFEST["post"]},
], ids=["missing", "empty", "not json", "list", "no post", "no effects", "effects not a list", "post short", "post duplicate",
        "post unknown kind", "settle for another task", "inline for another task", "inline reason not text", "payload not an object",
        "empty type", "task id not text"])
def test_a_missing_or_broken_manifest_after_a_committed_answer_is_a_lost_event_not_a_rerun(
        manifest: Any, mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, answer: Callable[[list[str]], tuple[int, str]]) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY), manifest=manifest, set_state=("T-1", "CANCELLED"))
    _use(monkeypatch, mod, fake, explicit=True)

    assert answer(WRITE) == (0, _rendered(OK_REPLY))

    recorded = _events(tmp_path)
    assert [event["type"] for event in recorded] == ["orch_status.effects_lost", "script.orch_status"]
    assert recorded[0]["payload"] == {"task_id": "T-1", "reason": "effects manifest missing or unparsable"}
    assert (fake.starts, _state(tmp_path)) == (1, "CANCELLED")  # no partial replay, no second run
    _cleaned(fake)


def test_a_replay_that_fails_is_a_lost_event_and_the_answer_still_stands(
        mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        answer: Callable[[list[str]], tuple[int, str]]) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the run log is gone")

    fake = fake_binary(stdout=_rendered(OK_REPLY), manifest=MANIFEST, set_state=("T-1", "CANCELLED"))
    _use(monkeypatch, mod, fake, explicit=True)
    monkeypatch.setattr(mod, "emit_inline_transition", broken)

    assert answer(WRITE) == (0, _rendered(OK_REPLY))

    recorded = _events(tmp_path)
    assert [event["type"] for event in recorded] == ["orch_status.effects_lost", "script.orch_status"]
    assert recorded[0]["payload"]["reason"] == "replay failed: RuntimeError"
    assert fake.starts == 1


def test_a_dry_run_answer_needs_no_manifest(mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch, answer: Callable[[list[str]], tuple[int, str]]) -> None:
    reply = {"agent": "A01", "script": "orch_status", "status": "ok", "summary": "dry-run: would transition T-1 → CANCELLED"}
    fake = fake_binary(stdout=_rendered(reply))
    _use(monkeypatch, mod, fake, explicit=True)

    assert answer([*WRITE, "--dry-run"]) == (0, _rendered(reply))

    assert (_types(tmp_path), _state(tmp_path), fake.starts) == (["script.orch_status"], "CREATED", 1)


def test_an_answer_for_an_argv_outside_the_grammar_is_printed_and_its_effects_are_reported_lost(
        mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        answer: Callable[[list[str]], tuple[int, str]]) -> None:
    fake = fake_binary(stdout=_rendered(OK_REPLY), manifest=MANIFEST)
    _use(monkeypatch, mod, fake, explicit=True)

    assert answer([*WRITE, "--advisory"]) == (0, _rendered(OK_REPLY))

    recorded = _events(tmp_path)
    assert [event["type"] for event in recorded] == ["orch_status.effects_lost", "script.orch_status"]
    assert recorded[0]["payload"] == {"task_id": None, "reason": "argv outside the write grammar"}


def test_an_argv_outside_the_grammar_under_explicit_rust_reaches_the_binary_and_python_answers_its_refusal(
        mod: ModuleType, fake_binary: Callable[..., Fake], store: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        answer: Callable[[list[str]], tuple[int, str]]) -> None:
    argv = [*WRITE, "--advisory"]
    fake = fake_binary(stdout=_rendered(_error_reply("E-DEP", "not_started")), exit=2)
    _use(monkeypatch, mod, fake, explicit=True)

    code, out = answer(argv)

    assert (code, json.loads(out)["task"]["state"], fake.starts) == (0, "CANCELLED", 1)
    assert fake.seen("stdin.json")["argv"] == argv


# --- the script as a process ------------------------------------------------------------------------------------------------

def test_script_process_answers_a_killed_binary_with_the_typed_refusal_and_no_rerun(
        fake_binary: Callable[..., Fake], tmp_path: Path) -> None:
    state = tmp_path / "proc-state"
    seeded = subprocess.run([sys.executable, "-c",
                             "import sys; sys.path.insert(0, sys.argv[1]); from swarm.taskstore import TaskStore; "
                             "s = TaskStore(root=sys.argv[2]); s.create(task_id='T-1', correlation_id='C-1', capability='backend')",
                             str(ROOT), str(tmp_path)], env={**os.environ, "SWARM_DIR": str(state)}, check=False,
                            capture_output=True, timeout=120)
    assert seeded.returncode == 0, seeded.stderr
    fake = fake_binary(kill=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GSD_", "SWARM_"))}
    env.update({"GSD_ENGINE": "rust", "GSD_ENGINE_BIN": str(fake.path), "SWARM_DIR": str(state)})

    proc = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path), *WRITE], env=env, capture_output=True, timeout=120,
                          check=False)

    error = json.loads(proc.stdout)["error"]
    assert (proc.returncode, error["code"], fake.starts) == (2, "E-INTERNAL", 1)
    with contextlib.closing(sqlite3.connect(state / "tasks.db")) as conn:
        assert conn.execute("SELECT state FROM tasks WHERE task_id='T-1'").fetchone()[0] == "CREATED"
    records = [json.loads(line)["type"] for line in (state / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records == ["script.orch_status.error"]
