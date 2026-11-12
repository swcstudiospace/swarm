"""orch_status rollback drill (14.3 R6): what an operator relies on when they switch the Rust engine off, on a real `.swarm/` store.

The store is built the way tests/parity/harness.py builds its sandboxes (a TaskStore on a throwaway SWARM_DIR, signed review
verdicts, a signing key that exists only in the child environment) and every call runs `scripts/orch_status.py` as a process.
Nothing here names a default: the expectation follows the module's DEFAULT_ENGINE read at test time, so the file holds before and
after the flip. The selector's own rules (resolution order, discovery, pre-filter, fallback codes) are in
tests/test_orch_status_default_engine.py."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "orch_status.py"
VOLATILE = ("ts", "msg_id")
STATE_PARTS = ("state", "workspace", "home")
HOST_ENV_KEYS = ("PATH", "TMPDIR", "LANG")

pytestmark = pytest.mark.skipif(importlib.util.find_spec("swarm._native") is None,
                                reason="the default and GSD_ENGINE=python legs need swarm._native (no SWARM_PURE_PYTHON)")

# the read calls of the drill, without the --root and --json the harness appends
ARGVS = {"list": [], "history": ["--history", "T-gate-fail"]}
# the switches that make Python answer
OPT_OUTS = {"GSD_ENGINE=python": {"GSD_ENGINE": "python"}, "SWARM_PURE_PYTHON=1": {"SWARM_PURE_PYTHON": "1"},
            "both": {"GSD_ENGINE": "python", "SWARM_PURE_PYTHON": "1"}}

CANNED = {"agent": "A01", "script": "orch_status", "status": "ok", "summary": "answered by the fake engine"}
CANNED_E_DEP = {"agent": "A01", "script": "orch_status", "status": "error",
                "error": {"code": "E-DEP", "message": "canned", "details": {}, "handling": "none"}}
# what each fake binary does after it has recorded its start and read its stdin; a kind that is not here runs "answer"
BINARY_TAILS = {
    "answer": f"printf '%s\\n' {shlex.quote(json.dumps(CANNED, indent=2))}",
    "crash": "exit 101",
    "garbage": "echo 'not json at all'",
    "e-dep": f"printf '%s\\n' {shlex.quote(json.dumps(CANNED_E_DEP, indent=2))}\nexit 2",
    "hang": "exec sleep 30",  # exec: the timeout kill reaches the sleeper and the stdout pipe closes
}


@dataclass(frozen=True)
class Fingerprint:
    """What a read must not change: the store's bytes and the files around it. The run log is kept line by line."""

    db: str
    files: tuple[str, ...]
    log: tuple[bytes, ...]


@dataclass
class Call:
    code: int
    out: bytes
    err: bytes
    before: Fingerprint
    after: Fingerprint
    gained: list[dict[str, Any]]  # the run-log records this call appended
    starts: int  # how many times the fake binary it was given has been started so far (0 when it got none)


@dataclass
class Fake:
    path: Path
    marker: Path


class Drill:
    """A real store with signed verdict rows, a snapshot of it, and the calls that run against it."""

    def __init__(self, base: Path, key: str) -> None:
        self.base = base
        self.key = key
        self.swarm_dir = base / "state" / ".swarm"
        self.workspace = base / "workspace"
        self.env = {name: value for name, value in os.environ.items() if name in HOST_ENV_KEYS or name.startswith("LC_")}
        self.env.update(HOME=str(base / "home"), SWARM_DIR=str(self.swarm_dir), SWARM_SIGNING_KEY=key, GSD_RUST_TIMEOUT_S="1",
                        PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        self.baseline: dict[str, Call] = {}

    def fingerprint(self) -> Fingerprint:
        files = sorted(str(path.relative_to(self.base)) for path in self.base.rglob("*")
                       if path.is_file() and not path.relative_to(self.base).parts[0].startswith("snapshot"))
        log = self.swarm_dir / "events.jsonl"
        return Fingerprint(hashlib.sha256((self.swarm_dir / "tasks.db").read_bytes()).hexdigest(), tuple(files),
                           tuple(log.read_bytes().splitlines()) if log.exists() else ())

    def snapshot(self, name: str) -> None:
        """Copies the state aside as `snapshot-<name>`, replacing an earlier copy of that name."""
        shutil.rmtree(self.base / f"snapshot-{name}", ignore_errors=True)
        for part in STATE_PARTS:
            shutil.copytree(self.base / part, self.base / f"snapshot-{name}" / part)

    def restore(self, name: str = "start") -> None:
        """Puts the state back as `snapshot(name)` saw it."""
        for part in STATE_PARTS:
            shutil.rmtree(self.base / part)
            shutil.copytree(self.base / f"snapshot-{name}" / part, self.base / part)

    def call(self, args: list[str], config: dict[str, str], fake: Fake | None = None) -> Call:
        """One `python3 scripts/orch_status.py <args> --root <workspace> --json` with `config` added to the child environment."""
        env = {**self.env, **config}
        if fake is not None:
            env["GSD_ENGINE_BIN"] = str(fake.path)
        before = self.fingerprint()
        proc = subprocess.run([sys.executable, str(SCRIPT), *args, "--root", str(self.workspace), "--json"], cwd=self.workspace, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False)
        after = self.fingerprint()
        return Call(proc.returncode, proc.stdout, proc.stderr, before, after,
                    [json.loads(line) for line in after.log[len(before.log):]], fake.marker.read_text().count("x") if fake else 0)


@pytest.fixture(scope="module")
def default_engine() -> str:
    """DEFAULT_ENGINE as scripts/orch_status.py ships it now."""
    spec = importlib.util.spec_from_file_location("orch_status_rollback_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DEFAULT_ENGINE in ("python", "rust")
    return str(module.DEFAULT_ENGINE)


@pytest.fixture(scope="module")
def drill_store(tmp_path_factory: pytest.TempPathFactory) -> Drill:
    """Two tasks with signed review verdicts (a pass and a fail with a major finding), one priming Python call so the run log and
    its .gitignore exist, a snapshot of that state, and Python's answer to each drill call from it."""
    drill = Drill(tmp_path_factory.mktemp("rollback-drill"), secrets.token_hex(32))
    for directory in (drill.swarm_dir, drill.workspace, drill.base / "home"):
        directory.mkdir(parents=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SWARM_DIR", str(drill.swarm_dir))
        patch.setenv("SWARM_SIGNING_KEY", drill.key)
        for name in ("SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY", "SWARM_ALLOW_INSECURE_DEV_KEY"):
            patch.delenv(name, raising=False)
        from swarm.gates import make_finding, make_verdict
        from swarm.taskstore import TaskStore

        store = TaskStore(path=drill.swarm_dir / "tasks.db")
        try:
            for task_id in ("T-gate-pass", "T-gate-fail"):
                store.create(task_id=task_id, correlation_id="C-gate", capability="code.backend", agent_id="A09")
            store.record_verdict("T-gate-pass", make_verdict(gate="review", task_id="T-gate-pass", agent_id="A09", correlation_id="C-gate",
                                                             root=drill.workspace, audit=False))
            major = make_finding("R-1", "major", "functional", "rollback drill major finding")
            store.record_verdict("T-gate-fail", make_verdict(gate="review", task_id="T-gate-fail", agent_id="A09", findings=[major],
                                                             correlation_id="C-gate", root=drill.workspace, audit=False))
        finally:
            store.conn.close()
    assert drill.call(ARGVS["list"], OPT_OUTS["GSD_ENGINE=python"]).code == 0
    drill.snapshot("start")
    for name, args in ARGVS.items():
        drill.restore()
        drill.baseline[name] = drill.call(args, OPT_OUTS["GSD_ENGINE=python"])
    return drill


@pytest.fixture
def drill(drill_store: Drill) -> Drill:
    """The drill store put back to its starting snapshot."""
    drill_store.restore()
    return drill_store


@pytest.fixture
def make_fake(tmp_path: Path) -> Callable[[str], Fake]:
    """A fake gsd_engine that appends an x to its marker per start. Kind "missing" names a path with no file, "plain" a file without
    the x bit; every other kind is a key of BINARY_TAILS."""
    made = 0

    def build(kind: str) -> Fake:
        nonlocal made
        made += 1
        folder = tmp_path / f"fake{made}"
        folder.mkdir()
        fake = Fake(folder / "gsd_engine", folder / "starts")
        fake.marker.write_text("")
        if kind != "missing":
            tail = BINARY_TAILS.get(kind, BINARY_TAILS["answer"])
            fake.path.write_text(f"#!/bin/sh\nprintf x >> {shlex.quote(str(fake.marker))}\ncat > /dev/null\n{tail}\n", encoding="utf-8")
            fake.path.chmod(0o644 if kind == "plain" else 0o755)
        return fake

    return build


def assert_python_answered(call: Call, baseline: Call) -> None:
    """The call printed what the GSD_ENGINE=python run prints, left the store alone, and appended exactly the one run-log record
    the Python path appends."""
    assert (call.code, call.out, call.err) == (baseline.code, baseline.out, baseline.err)
    assert (call.after.db, call.after.files) == (call.before.db, call.before.files)
    assert call.after.log[:len(call.before.log)] == call.before.log
    [record], [expected] = call.gained, baseline.gained
    assert record["type"] == "script.orch_status"
    assert {key: value for key, value in record.items() if key not in VOLATILE} == \
        {key: value for key, value in expected.items() if key not in VOLATILE}


def test_the_baseline_reads_the_signed_verdict_rows_back(drill: Drill) -> None:
    listed, history = (json.loads(drill.baseline[name].out) for name in ARGVS)

    assert [drill.baseline[name].code for name in ARGVS] == [0, 0]
    assert {row["task_id"]: row["verdicts"] for row in listed["tasks"]} == {"T-gate-pass": {"review": "pass"},
                                                                            "T-gate-fail": {"review": "fail"}}
    assert history["verdicts"]["review"]["verdict"] == "fail"
    assert history["verdicts"]["review"]["findings"][0]["severity"] == "major"
    assert all(drill.key.encode() not in call.out + call.err for call in drill.baseline.values())
    assert all(len(call.gained) == 1 for call in drill.baseline.values())


@pytest.mark.parametrize("name", ARGVS)
@pytest.mark.parametrize("config", OPT_OUTS.values(), ids=OPT_OUTS.keys())
def test_opt_out_never_starts_the_binary_and_prints_pythons_bytes(name: str, config: dict[str, str], drill: Drill,
                                                                  make_fake: Callable[[str], Fake]) -> None:
    fake = make_fake("answer")  # an engine that would answer differently from Python, were it started

    call = drill.call(ARGVS[name], config, fake)

    assert call.starts == 0
    assert_python_answered(call, drill.baseline[name])


@pytest.mark.parametrize("name", ARGVS)
@pytest.mark.parametrize("kind", ["crash", "garbage", "e-dep", "missing", "plain", "hang"])
def test_default_call_with_a_broken_binary_gives_pythons_bytes(name: str, kind: str, drill: Drill, make_fake: Callable[[str], Fake],
                                                               default_engine: str) -> None:
    fake = make_fake(kind)
    rust_resolved = default_engine == "rust"

    call = drill.call(ARGVS[name], {}, fake)

    assert call.starts == (1 if rust_resolved and kind not in ("missing", "plain") else 0)
    if kind == "hang" and rust_resolved:
        reply = json.loads(call.out)  # E-TIMEOUT is final: no fallback, and the read left the store as it was
        assert (call.code, reply["error"]["code"]) == (2, "E-TIMEOUT")
        assert "tasks" not in reply and "verdicts" not in reply
        assert (call.after.db, call.after.files) == (call.before.db, call.before.files)
        assert call.after.log[:len(call.before.log)] == call.before.log
    else:
        assert_python_answered(call, drill.baseline[name])


# how the call after the fake binary served one is made: the fake served it when GSD_ENGINE=rust names it, or when the default does
MID_STREAM = {
    "explicit rust then GSD_ENGINE=python, list": ("rust", "GSD_ENGINE=python", "list"),
    "explicit rust then GSD_ENGINE=python, history": ("rust", "GSD_ENGINE=python", "history"),
    "explicit rust then SWARM_PURE_PYTHON=1, list": ("rust", "SWARM_PURE_PYTHON=1", "list"),
    "explicit rust then SWARM_PURE_PYTHON=1, history": ("rust", "SWARM_PURE_PYTHON=1", "history"),
    "default then GSD_ENGINE=python, list": ("default", "GSD_ENGINE=python", "list"),
    "default then SWARM_PURE_PYTHON=1, history": ("default", "SWARM_PURE_PYTHON=1", "history"),
}


@pytest.mark.parametrize(("first", "opt_out", "name"), MID_STREAM.values(), ids=MID_STREAM.keys())
def test_rolling_back_after_a_served_call_leaves_the_store_and_the_earlier_record_alone(
        first: str, opt_out: str, name: str, drill: Drill, make_fake: Callable[[str], Fake], default_engine: str) -> None:
    fake = make_fake("answer")
    served = first == "rust" or default_engine == "rust"

    earlier = drill.call(ARGVS[name], {"GSD_ENGINE": "rust"} if first == "rust" else {}, fake)

    assert earlier.starts == (1 if served else 0)
    if served:
        assert (earlier.code, earlier.out) == (0, (json.dumps(CANNED, indent=2) + "\n").encode())
        [record] = earlier.gained
        assert (record["type"], record["source"], record["payload"]) == ("script.orch_status", "A01@orch_status", CANNED)
    else:
        assert_python_answered(earlier, drill.baseline[name])

    drill.snapshot("after-first")
    rolled_back = drill.call(ARGVS[name], OPT_OUTS[opt_out], fake)
    drill.restore("after-first")
    control = drill.call(ARGVS[name], OPT_OUTS["GSD_ENGINE=python"])  # Python's answer from the state the first call left

    assert rolled_back.starts == earlier.starts
    assert_python_answered(rolled_back, control)
    assert rolled_back.after.log[:len(earlier.after.log)] == earlier.after.log
