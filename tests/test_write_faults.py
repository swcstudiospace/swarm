"""Faults of `orch_status.py --transition` through the engine selector (Phase 14.5 U3.2d): a race of 8 processes, a lock held past the
binary's busy timeout, a reply lost after the commit, a store Python has not migrated, and a key canary that must never leave
the process that holds it.

Every process here is a real `scripts/orch_status.py`. The Rust engine is the real `gsd_engine` binary named by `GSD_ENGINE_BIN`;
the tests that need it are skipped, with a message naming that variable, when it is unset. The store is built by the real
`TaskStore`, on a rollback-journal store and on a WAL store (`SWARM_TASKSTORE_WAL=1`). Nothing here depends on how long anything
takes: the checks are on the typed outcome and the store, and the deadlines are only there so that a hang ends.

Run: `GSD_ENGINE_BIN=<target/debug/gsd_engine> python3 -m pytest tests/test_write_faults.py -q -p no:cacheprovider`.
`ORCH_STATUS_SCRIPT` points the tests at a mutated copy of the script, to show what a test catches.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import ROOT

from swarm.taskstore import TaskStore

SCRIPT = Path(os.environ.get("ORCH_STATUS_SCRIPT") or ROOT / "scripts" / "orch_status.py")
BIN_VAR = "GSD_ENGINE_BIN"
TASKS = ("T-1", "T-2", "T-BIG")
# 20 repeats of every race; WRITE_FAULTS_ROUNDS lowers it for a quick look at a mutated copy of the script
ROUNDS = int(os.environ.get("WRITE_FAULTS_ROUNDS", "20"))
# Generous: only there so that a hang ends. A loaded host takes long to start eight interpreters.
DEADLINE_S = 900
# how long the `lose` wrapper waits for the binary's effects manifest; WRITE_FAULTS_WAIT_S shortens it for a mutated binary
# that never writes one
LOSE_WAIT_S = int(os.environ.get("WRITE_FAULTS_WAIT_S", "600"))
LOCK_HOLD_S = 1.0
# The only variables of the host a call keeps (as tests/conftest.py's allowlist): never a GSD_*, SWARM_* or SUBSTRATE_* value, never a key.
KEEP_ENV = ("PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "PYTHONIOENCODING", "PYTHONUTF8")
JOURNAL_MODES = [pytest.param(False, id="rollback-journal"), pytest.param(True, id="wal")]

# What every process of a race runs after its barrier: the same `scripts/orch_status.py`, with the interpreter and the modules it
# imports already loaded, so that the processes meet at the write lock and not at the import.
DRIVER = r'''
import os, runpy, sys, time
ready, go, script = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.dirname(os.path.dirname(script)))
import swarm.script_base, swarm.taskstore, swarm.runlog, swarm.paths, swarm.errors, swarm.results, swarm.observability
import swarm.substrate_lease, swarm.telemetry
open(ready, "w").close()
limit = time.monotonic() + 900
while not os.path.exists(go):
    if time.monotonic() > limit:
        sys.exit(70)
    time.sleep(0.0005)
sys.argv = [script] + sys.argv[4:]
runpy.run_path(script, run_name="__main__")
'''

# A stand-in for the gsd_engine binary. It records every start, the request it was given and the NAMES (never the values) of the
# environment it was started with, then does what its config says:
#   fake  prints a canned reply, optionally ends itself with SIGKILL;
#   spy   runs the real binary on the same request, passes its answer through, and records it (and the hash of the store before and
#         after, and when it was done);
#   lose  runs the real binary but never reads its stdout, waits for the effects manifest to appear (it is written after the COMMIT),
#         SIGKILLs the binary while it is blocked printing a reply larger than a pipe, and ends itself with SIGKILL: the call
#         was committed and the reply was lost.
WRAPPER = r'''#!{python}
import hashlib, json, os, signal, subprocess, sys, time
cfg = json.load(open({cfg!r}))
folder = cfg["folder"]


def digest():
    try:
        return hashlib.sha256(open(cfg["db"], "rb").read()).hexdigest()
    except OSError:
        return None


def note(name, text):
    with open(os.path.join(folder, name), "w") as handle:
        handle.write(text)


raw = sys.stdin.buffer.read()
with open(os.path.join(folder, "starts"), "a") as handle:
    handle.write("start\n")
with open(os.path.join(folder, "request.json"), "wb") as handle:
    handle.write(raw)
note("env-names.json", json.dumps(sorted(os.environ)))
effects = json.loads(raw)["effects_path"]
mode = cfg["mode"]
if mode == "fake":
    if cfg["kill"]:
        os.kill(os.getpid(), signal.SIGKILL)
    sys.stdout.write(cfg["stdout"])
    sys.stdout.flush()
    sys.exit(cfg["exit"])
note("db-before", str(digest()))
if mode == "spy":
    done = subprocess.run([cfg["real"]], input=raw, stdout=subprocess.PIPE, check=False, env=os.environ)
    note("db-after", str(digest()))
    note("stdout", done.stdout.decode("utf-8", "replace"))
    note("returncode", str(done.returncode))
    try:
        note("manifest", open(effects, "rb").read().decode("utf-8", "replace"))
    except OSError:
        pass
    note("binary_done", "1")
    sys.stdout.buffer.write(done.stdout)
    sys.stdout.flush()
    sys.exit(done.returncode)
child = subprocess.Popen([cfg["real"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ)
child.stdin.write(raw)
child.stdin.close()
limit = time.monotonic() + cfg["wait"]
while time.monotonic() < limit:
    if os.path.exists(effects) and os.path.getsize(effects) > 0:
        break
    if child.poll() is not None:
        break
    time.sleep(0.002)
alive = child.poll() is None
if alive:
    child.kill()
    child.wait()
with open(effects, "rb") as source:
    note("manifest", source.read().decode("utf-8", "replace"))
note("outcome", json.dumps({{"alive_at_manifest": alive, "returncode": child.returncode}}))
os.kill(os.getpid(), signal.SIGKILL)
'''


# ---- stores and worlds -------------------------------------------------------------------------------------------------------------

def _build_template(directory: Path, wal: bool) -> bytes:
    """The store the real `TaskStore` builds: T-1, T-2 and T-BIG (a row far larger than a pipe) IN_PROGRESS, one `NULL>CREATED` each."""
    db = directory / "tasks.db"
    with pytest.MonkeyPatch.context() as patch:
        if wal:
            patch.setenv("SWARM_TASKSTORE_WAL", "1")
        else:
            patch.delenv("SWARM_TASKSTORE_WAL", raising=False)
        store = TaskStore(db)
        for task_id in TASKS:
            extra = {"inputs": ["x" * 400000]} if task_id == "T-BIG" else {}
            store.create(task_id=task_id, correlation_id="C-1", capability="code.backend", title=task_id, **extra)
            store.conn.execute("UPDATE tasks SET state='IN_PROGRESS' WHERE task_id=?", (task_id,))
        store.conn.commit()
        store.conn.close()
    assert not (directory / "tasks.db-wal").exists(), "python left a WAL file behind; the copied database file would be incomplete"
    return db.read_bytes()


@pytest.fixture(scope="session")
def templates(tmp_path_factory: pytest.TempPathFactory) -> dict[bool, bytes]:
    return {wal: _build_template(tmp_path_factory.mktemp(f"template-{'wal' if wal else 'delete'}"), wal) for wal in (False, True)}


@pytest.fixture(scope="session")
def engine_bin() -> str:
    value = os.environ.get(BIN_VAR, "")
    if not value:
        pytest.skip(f"{BIN_VAR} is not set: point it at the gsd_engine binary (cargo build -p gsd-adapter --bin gsd_engine)")
    assert Path(value).is_file() and os.access(value, os.X_OK), f"{BIN_VAR} does not name an executable file"
    return value


@dataclass
class World:
    """A scratch repository: the call's cwd (`root`), its Task Store (`swarm/tasks.db`) and a private temp directory."""
    base: Path
    wal: bool

    @property
    def root(self) -> Path:
        return self.base / "work"

    @property
    def swarm(self) -> Path:
        return self.root / ".swarm"

    @property
    def db(self) -> Path:
        return self.swarm / "tasks.db"

    @property
    def tmp(self) -> Path:
        return self.base / "tmp"


@pytest.fixture
def new_world(tmp_path: Path, templates: dict[bool, bytes]) -> Callable[..., World]:
    made = 0

    def build(wal: bool = False) -> World:
        nonlocal made
        made += 1
        world = World(tmp_path / f"world{made}", wal)
        world.swarm.mkdir(parents=True)
        world.tmp.mkdir()
        (world.tmp / "home").mkdir()
        world.db.write_bytes(templates[wal])
        return world

    return build


def call_env(world: World, *, engine: str, binary: str | None = None, canary: str | None = None) -> dict[str, str]:
    """The environment of one call: KEEP_ENV of the host and nothing else of it (no token, no key), the state dirs of the world, and the
    canary, when there is one, as both signing keys."""
    passed = (name for name in os.environ.get("WRITE_FAULTS_PASS_ENV", "").split(",") if name)  # e.g. GSD_MUT for a mutated binary
    env = {key: os.environ[key] for key in (*KEEP_ENV, *passed) if key in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "SWARM_GSD_STATE_FILE": "", "SWARM_CLAUDE_STATE_DIR": str(world.tmp / "claude-state"),
                "SWARM_DIR": str(world.swarm), "TMPDIR": str(world.tmp), "HOME": str(world.tmp / "home"), "GSD_ENGINE": engine})
    if binary is not None:
        env["GSD_ENGINE_BIN"] = binary
    if canary is not None:
        env["SWARM_SIGNING_KEY"] = canary
        env["SWARM_ED25519_KEY"] = canary
    return env


@dataclass
class Answer:
    code: int
    stdout: str
    stderr: str

    @property
    def reply(self) -> dict[str, Any]:
        body = json.loads(self.stdout)
        assert isinstance(body, dict), self.stdout[:300]
        return body

    @property
    def error(self) -> dict[str, Any]:
        error = self.reply.get("error")
        assert isinstance(error, dict), self.stdout[:300]
        return error


def run_selector(world: World, argv: list[str], env: dict[str, str]) -> Answer:
    done = subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=world.root, env=env, capture_output=True, check=False,
                          timeout=DEADLINE_S)
    return Answer(done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))


@contextlib.contextmanager
def reading(db: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db, timeout=60)
    try:
        yield conn
    finally:
        conn.close()


def audit(db: Path, task: str) -> list[str]:
    with reading(db) as conn:
        rows = conn.execute("SELECT from_state, to_state, reason FROM transitions WHERE task_id=? ORDER BY id", (task,)).fetchall()
    return [f"{from_state or 'NULL'}>{to_state}|{reason}" for from_state, to_state, reason in rows]


def state_of(db: Path, task: str) -> str:
    with reading(db) as conn:
        return conn.execute("SELECT state FROM tasks WHERE task_id=?", (task,)).fetchone()[0]


def integrity(db: Path) -> str:
    with reading(db) as conn:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]


def logical_dump(db: Path) -> list[str]:
    """Every statement of `iterdump`: the whole content and schema of the store as SQL text."""
    with reading(db) as conn:
        return list(conn.iterdump())


def events(world: World) -> list[dict[str, Any]]:
    path = world.swarm / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ---- the key canary ----------------------------------------------------------------------------------------------------------------

# The canary of a test is `secrets.token_hex(32)`: a random 64-hex value made at run time, never written to a file or printed.

def assert_no_canary(canary: str, where: str, data: bytes) -> None:
    """Fails WITHOUT showing the value (no `assert x in y`: its failure message would print both sides)."""
    if canary.encode() in data or bytes.fromhex(canary) in data:
        pytest.fail(f"the key canary is in {where}")


def assert_world_has_no_canary(canary: str, world: World, outputs: list[tuple[str, bytes]]) -> None:
    """The canary is in no file below the world (the store and its journal or WAL, the run log, live.json, anything a script wrote)
    and in none of the captured outputs."""
    for path in sorted(world.base.rglob("*")):
        if path.is_file():
            assert_no_canary(canary, f"the file {path.relative_to(world.base)}", path.read_bytes())
    for name, data in outputs:
        assert_no_canary(canary, name, data)


def outputs_of(*answers: Answer) -> list[tuple[str, bytes]]:
    return [(f"{kind} of call {index}", text.encode("utf-8", "replace"))
            for index, answer in enumerate(answers) for kind, text in (("stdout", answer.stdout), ("stderr", answer.stderr))]


def test_the_canary_scan_finds_the_value_in_both_spellings_and_never_prints_it() -> None:
    canary = secrets.token_hex(32)
    for name, haystack in (("text", b"prefix " + canary.encode() + b" suffix"), ("bytes", b"\0\0" + bytes.fromhex(canary) + b"\xff")):
        with pytest.raises(pytest.fail.Exception) as caught:
            assert_no_canary(canary, name, haystack)
        assert canary not in str(caught.value), "the failure message prints the key"
    assert_no_canary(canary, "clean", b"nothing to see here")


# ---- wrappers: stand-ins and spies for the binary -----------------------------------------------------------------------------------

@dataclass
class Wrapper:
    folder: Path
    path: Path

    @property
    def starts(self) -> int:
        marker = self.folder / "starts"
        return len(marker.read_text().splitlines()) if marker.exists() else 0

    def text(self, name: str) -> str:
        return (self.folder / name).read_text(encoding="utf-8")

    def captured(self) -> list[tuple[str, bytes]]:
        """What the spy saw of the binary, for the canary scan: its request, its stdout and the manifest it wrote (when it got that far)."""
        return [(f"the binary's {name}", (self.folder / name).read_bytes()) for name in ("request.json", "stdout", "manifest")
                if (self.folder / name).exists()]


@pytest.fixture
def wrapper(tmp_path: Path) -> Callable[..., Wrapper]:
    made = 0

    def build(mode: str, *, db: Path, real: str | None = None, stdout: str = "", exit: int = 0, kill: bool = False) -> Wrapper:
        nonlocal made
        made += 1
        folder = tmp_path / f"wrapper{made}"
        folder.mkdir()
        cfg = {"mode": mode, "folder": str(folder), "db": str(db), "real": real, "stdout": stdout, "exit": exit, "kill": kill,
               "wait": LOSE_WAIT_S}
        (folder / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
        path = folder / "gsd_engine"
        path.write_text(WRAPPER.format(python=sys.executable, cfg=str(folder / "cfg.json")), encoding="utf-8")
        path.chmod(0o755)
        return Wrapper(folder, path)

    return build


# ---- (a) race ----------------------------------------------------------------------------------------------------------------------

def start_racer(world: World, index: int, engine: str, binary: str | None, canary: str) -> subprocess.Popen[bytes]:
    env = call_env(world, engine=engine, binary=binary, canary=canary)
    ready, go = world.tmp / f"ready-{index}", world.tmp / "go"
    argv = ["--json", "--transition", "T-1", "IN_REVIEW", "--reason", f"p{index}"]
    return subprocess.Popen([sys.executable, "-c", DRIVER, str(ready), str(go), str(SCRIPT), *argv], cwd=world.root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def race(world: World, engines: list[str], binary: str | None, canary: str) -> list[Answer]:
    """One process per entry of `engines` ("python" or "rust"), each asking for the same transition of T-1. The test holds the write
    lock while it opens the barrier and lets go of it a moment later, so that they all meet the lock together."""
    procs = [start_racer(world, index, engine, binary, canary) for index, engine in enumerate(engines)]
    try:
        deadline = time.monotonic() + DEADLINE_S
        while not all((world.tmp / f"ready-{index}").exists() for index in range(len(procs))):
            assert time.monotonic() < deadline, "a racer never got ready"
            assert all(proc.poll() is None for proc in procs), "a racer ended before the barrier"
            time.sleep(0.01)
        holder = sqlite3.connect(world.db, timeout=60, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        (world.tmp / "go").touch()
        time.sleep(LOCK_HOLD_S)
        holder.execute("ROLLBACK")
        holder.close()
        answers = []
        for proc in procs:
            out, err = proc.communicate(timeout=DEADLINE_S)
            answers.append(Answer(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")))
        return answers
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def check_race(world: World, engines: list[str], answers: list[Answer], canary: str, where: str,
               captured: list[tuple[str, bytes]]) -> int:
    """One winner; every loser a typed refusal of the state machine; the store whole. Returns the winner's index."""
    winners = [index for index, answer in enumerate(answers) if answer.code == 0]
    assert len(winners) == 1, f"{where}: winners {winners}: {[(a.code, a.stdout[:200]) for a in answers]}"
    winner = winners[0]
    assert answers[winner].reply["status"] == "ok", where
    for index, answer in enumerate(answers):
        assert "Traceback" not in answer.stderr, f"{where}: process {index} crashed: {answer.stderr[-400:]}"
        if index == winner:
            continue
        error = answer.error
        assert (answer.code, error["code"]) == (2, "E-CONTRACT"), f"{where}: process {index} ({engines[index]}): {answer.stdout[:300]}"
        message = error["message"]
        assert "illegal transition IN_REVIEW → IN_REVIEW for T-1" in message or "concurrent transition on T-1" in message, (
            f"{where}: process {index}: {message}")
        assert error.get("details", {}).get("write_state") not in ("unknown", "not_started"), f"{where}: process {index}: {error}"
    assert audit(world.db, "T-1") == ["NULL>CREATED|created", f"IN_PROGRESS>IN_REVIEW|p{winner}"], f"{where}: one audit row, by the winner"
    assert state_of(world.db, "T-1") == "IN_REVIEW", where
    assert state_of(world.db, "T-2") == "IN_PROGRESS", f"{where}: another task moved"
    assert integrity(world.db) == "ok", where
    assert_world_has_no_canary(canary, world, outputs_of(*answers) + captured)
    return winner


RACES = {
    "mixed-4-rust-4-python": ["rust", "python"] * 4,
    "8-rust": ["rust"] * 8,
    "8-python": ["python"] * 8,
}


@pytest.mark.filterwarnings("ignore:record_property is incompatible:pytest.PytestWarning")  # the winners per engine go to the junit file
@pytest.mark.parametrize("wal", JOURNAL_MODES)
@pytest.mark.parametrize("engines", RACES.values(), ids=RACES.keys())
def test_eight_processes_racing_for_one_transition_have_one_winner_and_never_a_half_state(
        engines: list[str], wal: bool, new_world: Callable[..., World], wrapper: Callable[..., Wrapper],
        request: pytest.FixtureRequest, record_property: Callable[[str, object], None]) -> None:
    binary = request.getfixturevalue("engine_bin") if "rust" in engines else None
    wins = {"python": 0, "rust": 0}
    for round_index in range(ROUNDS):
        world = new_world(wal)
        canary = secrets.token_hex(32)
        spy = wrapper("spy", db=world.db, real=binary) if binary is not None else None
        answers = race(world, engines, str(spy.path) if spy is not None else None, canary)
        where = f"{'wal' if wal else 'rollback-journal'} round {round_index}"
        winner = check_race(world, engines, answers, canary, where, spy.captured() if spy is not None else [])
        wins[engines[winner]] += 1
        if spy is not None:
            # Every Rust process went through the binary, once: no loser was answered by a second run.
            starts, rust_calls = spy.starts, engines.count("rust")
            assert starts == rust_calls, f"{where}: the binary started {starts} times for {rust_calls} rust calls"
    record_property("wins", json.dumps(wins))


# ---- (d) a reply lost after the commit ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("wal", JOURNAL_MODES)
def test_a_reply_lost_after_the_commit_is_an_unknown_outcome_that_is_not_repeated_and_a_replay_is_refused(
        wal: bool, new_world: Callable[..., World], wrapper: Callable[..., Wrapper], engine_bin: str) -> None:
    world = new_world(wal)
    canary = secrets.token_hex(32)
    lose = wrapper("lose", db=world.db, real=engine_bin)
    argv = ["--json", "--transition", "T-BIG", "IN_REVIEW", "--reason", "lost"]

    first = run_selector(world, argv, call_env(world, engine="rust", binary=str(lose.path), canary=canary))

    # The wrapper SIGKILLed the binary while it was blocked printing, after the manifest (written after the COMMIT) had appeared.
    outcome = json.loads(lose.text("outcome"))
    assert outcome == {"alive_at_manifest": True, "returncode": -9}, outcome
    manifest = json.loads(lose.text("manifest"))
    assert [step["kind"] for step in manifest["post"]] == ["live_refresh", "lease_settle", "inline_transition"]
    # The selector says the outcome is unknown, and does not run the call again, on either engine.
    assert first.code == 2, first.stdout[:300]
    error = first.error
    assert (error["code"], error["details"].get("write_state"), error["details"].get("task_id")) == ("E-INTERNAL", "unknown", "T-BIG")
    assert "NOT repeated" in error["message"] and "--history T-BIG" in error["message"], error["message"]
    assert lose.starts == 1, "the binary was started again"
    # One run-log record of the unknown outcome and none of a Python answer after it (which would be E-CONTRACT: the state machine
    # refuses the repeated call).
    errors = [record["payload"]["error"]["code"] for record in events(world) if record["type"] == "script.orch_status.error"]
    assert errors == ["E-INTERNAL"], errors
    # The store has exactly one row for the transition.
    assert audit(world.db, "T-BIG") == ["NULL>CREATED|created", "IN_PROGRESS>IN_REVIEW|lost"]
    assert state_of(world.db, "T-BIG") == "IN_REVIEW"
    assert integrity(world.db) == "ok"
    assert_world_has_no_canary(canary, world, outputs_of(first) + [("the request", lose.text("request.json").encode()),
                                                                    ("the manifest", lose.text("manifest").encode())])
    assert "SWARM_SIGNING_KEY" not in json.loads(lose.text("env-names.json")), "the selector handed the binary a signing key"

    # An explicit replay of the same call, on each engine: the state machine refuses it (no self-loop); nothing is doubled.
    dump = logical_dump(world.db)
    replays = [run_selector(world, argv, call_env(world, engine="python", canary=canary)),
               run_selector(world, argv, call_env(world, engine="rust", binary=engine_bin, canary=canary))]
    for engine, replay in zip(("python", "rust"), replays, strict=True):
        assert replay.code == 2, f"{engine}: {replay.stdout[:300]}"
        assert replay.error["code"] == "E-CONTRACT", f"{engine}: {replay.stdout[:300]}"
        assert "illegal transition IN_REVIEW → IN_REVIEW for T-BIG" in replay.error["message"], f"{engine}: {replay.error['message']}"
        assert replay.error.get("details", {}).get("write_state") != "unknown", engine
    assert logical_dump(world.db) == dump, "a replay changed the store"
    assert audit(world.db, "T-BIG") == ["NULL>CREATED|created", "IN_PROGRESS>IN_REVIEW|lost"]
    assert_world_has_no_canary(canary, world, outputs_of(first, *replays))


# ---- (c) a lock held past the binary's busy timeout, and the fallback after it -----------------------------------------------------------

@pytest.mark.parametrize("wal", JOURNAL_MODES)
def test_a_lock_held_past_the_binarys_timeout_is_refused_as_not_started_and_python_then_commits_exactly_once(
        wal: bool, new_world: Callable[..., World], wrapper: Callable[..., Wrapper], engine_bin: str) -> None:
    """The binary waits 30 s for the write lock (`WriterConfig::new`), so this test takes a little over 30 s whatever the load. The lock
    is held until the binary has answered, then released: the outcome depends on the lock, not on the clock."""
    world = new_world(wal)
    canary = secrets.token_hex(32)
    spy = wrapper("spy", db=world.db, real=engine_bin)
    dump = logical_dump(world.db)

    holder = sqlite3.connect(world.db, timeout=60, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    released = threading.Event()

    def release_when_the_binary_has_answered() -> None:
        limit = time.monotonic() + DEADLINE_S
        while not (spy.folder / "binary_done").exists() and time.monotonic() < limit:
            time.sleep(0.01)
        holder.execute("ROLLBACK")
        released.set()

    watcher = threading.Thread(target=release_when_the_binary_has_answered)
    watcher.start()
    try:
        argv = ["--json", "--transition", "T-1", "IN_REVIEW", "--reason", "late"]
        answer = run_selector(world, argv, call_env(world, engine="rust", binary=str(spy.path), canary=canary))
    finally:
        watcher.join(timeout=DEADLINE_S)
        holder.close()
    assert released.is_set()

    # What the binary said while it was locked out: E-INTERNAL, nothing started, nothing changed.
    said = json.loads(spy.text("stdout"))
    assert spy.text("returncode") == "2", spy.text("stdout")[:300]
    assert (said["error"]["code"], said["error"]["details"]["write_state"]) == ("E-INTERNAL", "not_started"), said
    assert spy.text("db-before") == spy.text("db-after"), "the locked-out binary changed the store file"
    # Python answered once, after the lock was gone, and committed exactly one transition.
    assert spy.starts == 1
    assert (answer.code, answer.reply["status"]) == (0, "ok"), answer.stdout[:300]
    assert audit(world.db, "T-1") == ["NULL>CREATED|created", "IN_PROGRESS>IN_REVIEW|late"]
    assert integrity(world.db) == "ok"
    assert logical_dump(world.db) != dump
    assert_world_has_no_canary(canary, world, outputs_of(answer) + spy.captured())


# ---- (f) a store Python has not migrated -------------------------------------------------------------------------------------------

UNMIGRATED = {
    "no audit table": ["DROP TABLE transitions"],
    "no envelope columns": ["ALTER TABLE verdicts DROP COLUMN envelope_json", "ALTER TABLE verdicts DROP COLUMN sig"],
    "no verdicts table": ["DROP TABLE verdicts"],
    "no artifacts table": ["DROP TABLE artifacts"],
}


@pytest.mark.parametrize("wal", JOURNAL_MODES)
@pytest.mark.parametrize("statements", UNMIGRATED.values(), ids=UNMIGRATED.keys())
def test_a_store_without_the_audit_table_or_the_envelope_columns_is_left_untouched_by_the_binary_and_python_migrates_it(
        statements: list[str], wal: bool, new_world: Callable[..., World], wrapper: Callable[..., Wrapper], engine_bin: str) -> None:
    world = new_world(wal)
    canary = secrets.token_hex(32)
    with reading(world.db) as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    spy = wrapper("spy", db=world.db, real=engine_bin)

    argv = ["--json", "--transition", "T-1", "IN_REVIEW", "--reason", "migrate"]
    answer = run_selector(world, argv, call_env(world, engine="rust", binary=str(spy.path), canary=canary))

    # The binary answered E-DEP, nothing written, and left the store file as it was ...
    said = json.loads(spy.text("stdout"))
    assert (spy.text("returncode"), said["error"]["code"], said["error"]["details"]["write_state"]) == ("2", "E-DEP", "not_started"), said
    assert spy.text("db-before") == spy.text("db-after"), "the binary changed a store it refused"
    assert spy.starts == 1
    # ... and Python, once, migrated it and made the transition.
    assert (answer.code, answer.reply["status"]) == (0, "ok"), answer.stdout[:300]
    assert state_of(world.db, "T-1") == "IN_REVIEW"
    assert audit(world.db, "T-1")[-1] == "IN_PROGRESS>IN_REVIEW|migrate"
    with reading(world.db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        verdict_columns = {row[1] for row in conn.execute("PRAGMA table_info(verdicts)")}
    assert {"tasks", "transitions", "verdicts", "artifacts"} <= tables
    assert {"envelope_json", "sig"} <= verdict_columns
    assert integrity(world.db) == "ok"
    assert_world_has_no_canary(canary, world, outputs_of(answer) + spy.captured())


# ---- (e) the key canary and the request --------------------------------------------------------------------------------------------

NOT_STARTED = json.dumps({"agent": "A01", "script": "orch_status", "status": "error",
                          "error": {"code": "E-DEP", "message": "canned", "details": {"write_state": "not_started"}, "handling": "none"}})


@pytest.mark.parametrize("behaviour", ["python-answers", "killed-binary"])
def test_a_signing_key_never_reaches_the_binary_its_request_or_any_output(
        behaviour: str, new_world: Callable[..., World], wrapper: Callable[..., Wrapper]) -> None:
    world = new_world(False)
    canary = secrets.token_hex(32)
    fake = (wrapper("fake", db=world.db, stdout=NOT_STARTED, exit=2) if behaviour == "python-answers"
            else wrapper("fake", db=world.db, kill=True))

    argv = ["--json", "--transition", "T-1", "IN_REVIEW", "--reason", "keyless"]
    answer = run_selector(world, argv, call_env(world, engine="rust", binary=str(fake.path), canary=canary))

    request = json.loads(fake.text("request.json"))
    assert sorted(request) == ["agent", "argv", "effects_path", "env", "root", "script"], sorted(request)
    assert fake.starts == 1
    names = json.loads(fake.text("env-names.json"))
    for name in ("SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_REQUIRE_KEY"):
        assert name not in names, f"the binary was started with {name}"
    assert_no_canary(canary, "the request handed to the binary", fake.text("request.json").encode())
    if behaviour == "python-answers":
        assert (answer.code, answer.reply["status"]) == (0, "ok"), answer.stdout[:300]  # Python body answered, keyless transition
    else:
        assert (answer.code, answer.error["code"], answer.error["details"]["write_state"]) == (2, "E-INTERNAL", "unknown")
    assert_world_has_no_canary(canary, world, outputs_of(answer))
