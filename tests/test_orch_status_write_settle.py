"""orch_status write selector (Phase 14.5, D-1): the row the selector prints and records after a lease settlement.

For a committed `--transition` into a state that settles a mirrored lease (CANCELLED, FAILED, DONE, a rework state) the Python path
prints the task row AFTER `substrate_lease.settle_mirrored` (notes.lease says released, completed or unsettled) and records that same
reply in the `script.orch_status` run-log record. A binary answers with the row it committed, before the settlement the selector
replays; the selector therefore prints and records what Python does. When the settlement moved nothing, or there is no settlement to
see (dry run, broken manifest, failed replay, a refusal, an unknown outcome), the binary's own bytes are printed untouched.

The oracle is the Python path itself: each case runs on two identical copies of one store (one with `SCRIPT.main`, one through a fake
gsd_engine that commits with the real TaskStore, prints the committed row and writes a manifest), each against its own loopback fake
substrate (the one of the parity matrix), and compares what they printed, recorded and called.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conftest import ROOT
import test_orch_status_write_selector as selector_tests
from test_orch_status_write_selector import Fake, _error_reply

from swarm import substrate_client
from swarm.taskstore import TaskStore

sys.path.insert(0, str(Path(__file__).resolve().parent / "parity"))
import _write_matrix as matrix  # noqa: E402

mod = selector_tests.mod  # the module fixture of the selector tests: scripts/orch_status.py in a clean engine environment

TID = "T-settle"
ARGV_TAIL = ["--reason", "why", "--json"]
# the key order a binary prints its reply in, deliberately not Python's (status, task, summary, agent, script): it must survive
BINARY_ORDER = ["script", "task", "summary", "status", "agent"]

# Clock readings no two legs can share, each leg runs at its own instant. Every other byte of what the legs print and record is compared.
pytestmark = pytest.mark.usefixtures("native_extension")

MASKS = (
    # TaskStore stamps every write with time.time()
    re.compile(r'("updated_at"\s*:\s*)-?[0-9][0-9.eE+-]*'),
    # substrate_lease._mirror stamps notes.lease.at with time.time(); inside the `notes` string the quotes are escaped
    re.compile(r'(\\?"at\\?"\s*:\s*)-?[0-9][0-9.eE+-]*'),
    # transition(CHANGES_REQUESTED) writes notes.verdicts_since = time.time() + 0.001
    re.compile(r'(\\?"verdicts_since\\?"\s*:\s*)-?[0-9][0-9.eE+-]*'),
)

FAKE_BODY = '''#!{python}
import json, os, sys
sys.path.insert(0, {repo!r})
from swarm.taskstore import TaskStore
folder = {folder!r}
cfg = json.load(open(os.path.join(folder, "cfg.json")))
with open(os.path.join(folder, "starts"), "a") as fh:
    fh.write("started\\n")
request = json.load(sys.stdin)
row = None
if cfg["commit"] is not None:
    tid, state, reason = cfg["commit"]
    store = TaskStore(root=os.getcwd())
    store.transition(tid, state, actor="A01", reason=reason)
    row = store.get(tid)
    store.conn.close()
if cfg["read"] is not None:
    store = TaskStore(root=os.getcwd())
    row = store.get(cfg["read"])
    store.conn.close()
if cfg["manifest"] is not None:
    text = cfg["manifest"] if isinstance(cfg["manifest"], str) else json.dumps(cfg["manifest"])
    open(request["effects_path"], "w").write(text)
if cfg["reply"] is not None:
    printed = json.dumps({{k: (row if v == "<row>" else v) for k, v in cfg["reply"].items()}}, indent=cfg["indent"], default=str) + "\\n"
else:
    printed = cfg["raw"]
open(os.path.join(folder, "printed"), "w").write(printed)
sys.stdout.write(printed)
sys.stdout.flush()
sys.exit(cfg["exit"])
'''


@dataclass(frozen=True)
class Case:
    """One transition on a store holding a lease mirrored in notes.lease (or not), and what must come of its settlement."""

    before: str
    to: str
    lease: matrix.LeaseScript | None = matrix.SETTLES  # the fake substrate this leg answers with; None leaves the substrate off
    tool: str | None = None  # the substrate tool the settlement calls; None: no call
    after: str | None = None  # notes.lease.state once the settlement is over; None when the task holds no lease
    held: bool = True  # the task starts with a lease mirrored in notes.lease
    notes: dict[str, Any] = field(default_factory=dict)  # notes besides the lease


def _lease(**changes: Any) -> dict[str, Any]:
    base = {"state": "held", "lease_id": "L-settle-1", "agent": "A05", "surface": "swarm-a05-be", "session_id": f"A05@r1:{matrix.LEASE_GRAPH}",
            "graph_id": matrix.LEASE_GRAPH, "ttl_s": 900, "at": 1.0}
    return {**base, **changes}


SETTLING = {
    "cancelled releases": Case("IN_PROGRESS", "CANCELLED", tool="graph_release", after="released"),
    "failed releases": Case("IN_PROGRESS", "FAILED", tool="graph_release", after="released"),
    "done completes": Case("APPROVED", "DONE", tool="graph_complete", after="completed"),
    "rework releases": Case("IN_REVIEW", "CHANGES_REQUESTED", tool="graph_release", after="released", notes={"result": {"summary": "s"}}),
    "a refused release is unsettled": Case("IN_PROGRESS", "CANCELLED", lease=matrix.REFUSES_RELEASE, tool="graph_release", after="unsettled"),
}

# a settlement that moves nothing: the row the binary committed is the row Python prints
UNMOVED = {
    "the runner still owns the task": Case("IN_PROGRESS", "FAILED", after="held", notes={"running": {"run_id": "R-1"}}),
    "the lease is kept while the task holds it": Case("CLAIMED", "IN_PROGRESS", after="held"),
    "the substrate is off": Case("IN_PROGRESS", "CANCELLED", lease=None, after="held"),
    "the task holds no lease": Case("IN_PROGRESS", "CANCELLED", held=False),
}


@pytest.fixture(autouse=True)
def substrate_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No SUBSTRATE_* variable of the host reaches a leg, and the client's outage memory is clean before and after each test."""
    for name in [name for name in os.environ if name.startswith("SUBSTRATE_")]:
        monkeypatch.delenv(name)
    substrate_client.reset()
    yield
    substrate_client.reset()


@pytest.fixture
def twins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[Case], tuple[Path, Path]]:
    """Builds the two identical workspaces of a case, python and rust: one task T-settle in `before`, byte-for-byte the same store."""

    def build(case: Case) -> tuple[Path, Path]:
        python, rust = tmp_path / "py", tmp_path / "rs"
        monkeypatch.setenv("SWARM_DIR", str(python / ".swarm"))
        notes = {"gates": [], **case.notes, **({"lease": _lease()} if case.held else {})}
        store = TaskStore(root=python)
        store.create(task_id=TID, correlation_id="C-1", capability="code.backend", agent_id="A05", notes=notes)
        store.conn.execute("UPDATE tasks SET state=? WHERE task_id=?", (case.before, TID))
        store.conn.commit()
        store.conn.close()
        shutil.copytree(python / ".swarm", rust / ".swarm")
        return python, rust

    return build


@pytest.fixture
def commit_binary(tmp_path: Path) -> Callable[..., Fake]:
    """A fake gsd_engine that commits with the real TaskStore (or not), writes `manifest`, and prints `reply` (with the row it read after
    the commit in place of "<row>") rendered with `indent`, or `raw`, and exits with `exit`. `printed` in its folder is what it printed."""
    made = 0

    def build(*, commit: tuple[str, str, str] | None = None, read: str | None = None, manifest: Any = None,
              reply: dict[str, Any] | None = None, indent: int | None = 4, raw: str = "", exit: int = 0) -> Fake:
        nonlocal made
        made += 1
        folder = tmp_path / f"settle-fake{made}"
        folder.mkdir()
        cfg = {"commit": commit, "read": read, "manifest": manifest, "reply": reply, "indent": indent, "raw": raw, "exit": exit}
        (folder / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
        path = folder / "gsd_engine"
        path.write_text(FAKE_BODY.format(python=sys.executable, repo=str(ROOT), folder=str(folder)), encoding="utf-8")
        path.chmod(0o755)
        return Fake(folder, path)

    return build


def _manifest(before: str, to: str) -> dict[str, Any]:
    return {"effects": [{"type": "task.transition", "payload": {"task_id": TID, "state": to, "reason": "why"}, "task_id": None}],
            "post": [{"kind": "live_refresh"}, {"kind": "lease_settle", "task_id": TID},
                     {"kind": "inline_transition", "task_id": TID, "from": before, "reason": "why"}]}


def _reply(to: str) -> dict[str, Any]:
    return {"script": "orch_status", "task": "<row>", "summary": f"{TID} → {to}", "status": "ok", "agent": "A01"}


def _argv(to: str, *flags: str) -> list[str]:
    return ["--transition", TID, to, *flags, *ARGV_TAIL]


@dataclass
class Leg:
    code: int | None
    out: bytes
    records: list[dict[str, Any]]  # the run log without `ts` and `msg_id`
    calls: list[dict[str, Any]]  # what the fake substrate saw
    row: dict[str, Any]  # the stored task row
    handed_over: bool = False  # the selector returned None: the Python body answered


def masked(text: str) -> str:
    for pattern in MASKS:
        text = pattern.sub(r"\1<clock>", text)
    return text


def _dump(value: Any) -> str:
    return masked(json.dumps(value, sort_keys=True, indent=1, default=str))


def _run(mod: ModuleType, monkeypatch: pytest.MonkeyPatch, capture: pytest.CaptureFixture[bytes], home: Path, argv: list[str],
         lease: matrix.LeaseScript | None, binary: Fake | None) -> Leg:
    """One leg: `SCRIPT.main` on the store at `home`, or (with `binary`) the selector on GSD_ENGINE=rust and then, when it hands the call
    over, the Python body, as `python3 scripts/orch_status.py` does."""
    monkeypatch.setenv("SWARM_DIR", str(home / ".swarm"))
    monkeypatch.chdir(home)
    capture.readouterr()
    with contextlib.ExitStack() as stack:
        calls: list[dict[str, Any]] = []
        if lease is not None:
            fake = stack.enter_context(matrix.FakeSubstrate(lease))
            for name, value in fake.env.items():
                monkeypatch.setenv(name, value)
            calls = fake.calls
        else:
            monkeypatch.delenv("SUBSTRATE_URL", raising=False)
        handed_over = False
        if binary is None:
            code: int | None = mod.SCRIPT.main(argv)
        else:
            monkeypatch.setenv("GSD_ENGINE", "rust")
            monkeypatch.setenv("GSD_ENGINE_BIN", str(binary.path))
            code = mod.select_engine(argv)
            if code is None:
                handed_over = True
                code = mod.SCRIPT.main(argv)
        out = capture.readouterr().out
        seen = list(calls)
    log = home / ".swarm" / "events.jsonl"
    records = [{key: value for key, value in json.loads(line).items() if key not in ("ts", "msg_id")}
               for line in log.read_text(encoding="utf-8").splitlines() if line.strip()] if log.exists() else []
    with contextlib.closing(sqlite3.connect(home / ".swarm" / "tasks.db")) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM tasks WHERE task_id=?", (TID,)).fetchone())
    return Leg(code, out, records, seen, row, handed_over)


def _lease_state(row: dict[str, Any]) -> str | None:
    lease = json.loads(row["notes"] or "{}").get("lease")
    return None if lease is None else str(lease["state"])


def _script_record(leg: Leg) -> dict[str, Any]:
    (record,) = [record for record in leg.records if record["type"] == "script.orch_status"]
    return record


# --- a settlement that moved the row ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", SETTLING.values(), ids=SETTLING.keys())
def test_the_selector_prints_and_records_the_row_after_the_settlement(
        case: Case, mod: ModuleType, twins: Callable[[Case], tuple[Path, Path]], commit_binary: Callable[..., Fake],
        monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    python, rust = twins(case)
    binary = commit_binary(commit=(TID, case.to, "why"), manifest=_manifest(case.before, case.to), reply=_reply(case.to))

    truth = _run(mod, monkeypatch, capsysbinary, python, _argv(case.to), case.lease, None)
    other = _run(mod, monkeypatch, capsysbinary, rust, _argv(case.to), case.lease, binary)

    assert (truth.code, other.code, binary.starts, other.handed_over) == (0, 0, 1, False)
    assert _lease_state(truth.row) == _lease_state(other.row) == case.after  # the settlement ran in both
    assert [call["tool"] for call in other.calls] == [case.tool] and other.calls == truth.calls
    printed = json.loads(other.out)
    assert list(printed) == BINARY_ORDER  # the reply's own key order, with the settled row in the place of the committed one
    assert json.loads(printed["task"]["notes"])["lease"]["state"] == printed["task"]["notes_json"]["lease"]["state"] == case.after
    python_reply = json.loads(truth.out)
    # Python's reply and layout, in the binary's key order, but for the clock readings
    assert masked(other.out.decode("utf-8")) == masked(json.dumps({key: python_reply[key] for key in BINARY_ORDER}, indent=2) + "\n")
    assert other.out == (json.dumps(printed, indent=2, default=str) + "\n").encode("utf-8")  # as AgentScript._print renders --json
    assert other.out != Path(binary.folder / "printed").read_bytes()  # not the binary's own bytes: its row held the lease still
    assert _script_record(other)["payload"]["task"]["notes_json"]["lease"]["state"] == case.after  # the log holds the reply that was printed
    assert [_dump(record) for record in other.records] == [_dump(record) for record in truth.records]
    assert _dump(other.row) == _dump(truth.row)


# --- a settlement that moved nothing, and the answers it never touches ----------------------------------------------------------------

@pytest.mark.parametrize("case", UNMOVED.values(), ids=UNMOVED.keys())
def test_a_settlement_that_moves_nothing_leaves_the_binarys_bytes_as_they_are(
        case: Case, mod: ModuleType, twins: Callable[[Case], tuple[Path, Path]], commit_binary: Callable[..., Fake],
        monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    python, rust = twins(case)
    # indent 4 is not what Python prints: the binary's bytes are the only way these bytes can come out
    binary = commit_binary(commit=(TID, case.to, "why"), manifest=_manifest(case.before, case.to), reply=_reply(case.to), indent=4)

    truth = _run(mod, monkeypatch, capsysbinary, python, _argv(case.to), case.lease, None)
    other = _run(mod, monkeypatch, capsysbinary, rust, _argv(case.to), case.lease, binary)

    assert (truth.code, other.code, other.handed_over) == (0, 0, False)
    assert other.out == (binary.folder / "printed").read_bytes() and other.out.startswith(b'{\n    "script"')
    assert other.calls == truth.calls == []
    assert _lease_state(other.row) == _lease_state(truth.row) == case.after
    assert _script_record(other)["payload"]["task"]["state"] == case.to
    assert _dump(json.loads(other.out)) == _dump(json.loads(truth.out))  # the same reply as Python's, in the binary's key order and layout
    assert [_dump(record) for record in other.records] == [_dump(record) for record in truth.records]


def test_a_dry_run_prints_the_binarys_bytes_and_settles_nothing(
        mod: ModuleType, twins: Callable[[Case], tuple[Path, Path]], commit_binary: Callable[..., Fake],
        monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    case = SETTLING["cancelled releases"]
    # the dry-run reply of a binary names no task; this one prints the held row, to show that nothing is put in its place
    binary = commit_binary(read=TID, manifest={"effects": [], "post": []}, reply=_reply("CANCELLED"), indent=4)

    python, rust = twins(case)
    truth = _run(mod, monkeypatch, capsysbinary, python, _argv("CANCELLED", "--dry-run"), case.lease, None)
    other = _run(mod, monkeypatch, capsysbinary, rust, _argv("CANCELLED", "--dry-run"), case.lease, binary)

    assert (other.code, other.handed_over) == (0, False)
    assert other.out == (binary.folder / "printed").read_bytes()
    assert (other.calls, _lease_state(other.row), other.row["state"]) == ([], "held", "IN_PROGRESS")
    assert [record["type"] for record in other.records] == [record["type"] for record in truth.records]


BROKEN_MANIFESTS = {
    "not JSON": "{not json",
    "empty": "",
    "a post step missing": {"effects": [], "post": [{"kind": "live_refresh"}, {"kind": "lease_settle", "task_id": TID}]},
    "the lease step names another task": {**_manifest("IN_PROGRESS", "CANCELLED"), "post": [
        {"kind": "live_refresh"}, {"kind": "lease_settle", "task_id": "T-other"},
        {"kind": "inline_transition", "task_id": TID, "from": "IN_PROGRESS", "reason": "why"}]},
    "an event type this route does not emit": {**_manifest("IN_PROGRESS", "CANCELLED"), "effects": [
        {"type": "swarm.other", "payload": {}, "task_id": None}]},
}


@pytest.mark.parametrize("manifest", BROKEN_MANIFESTS.values(), ids=BROKEN_MANIFESTS.keys())
def test_a_manifest_or_replay_that_fails_prints_the_binarys_bytes(
        manifest: Any, mod: ModuleType, twins: Callable[[Case], tuple[Path, Path]], commit_binary: Callable[..., Fake],
        monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    case = SETTLING["cancelled releases"]
    _, rust = twins(case)
    binary = commit_binary(commit=(TID, "CANCELLED", "why"), manifest=manifest, reply=_reply("CANCELLED"), indent=4)

    other = _run(mod, monkeypatch, capsysbinary, rust, _argv("CANCELLED"), case.lease, binary)

    assert (other.code, other.handed_over) == (0, False)
    assert other.out == (binary.folder / "printed").read_bytes()  # whatever the replay did, there is no settled row to put in place
    assert "orch_status.effects_lost" in [record["type"] for record in other.records]
    assert _script_record(other)["payload"]["task"]["notes_json"]["lease"]["state"] == "held"  # the binary's row, as it printed it


def test_a_reply_without_a_task_object_is_printed_as_the_binary_wrote_it(
        mod: ModuleType, twins: Callable[[Case], tuple[Path, Path]], commit_binary: Callable[..., Fake],
        monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    case = SETTLING["cancelled releases"]
    _, rust = twins(case)
    binary = commit_binary(commit=(TID, "CANCELLED", "why"), manifest=_manifest(case.before, "CANCELLED"), indent=4,
                           reply={"script": "orch_status", "summary": "done", "status": "ok", "agent": "A01"})

    other = _run(mod, monkeypatch, capsysbinary, rust, _argv("CANCELLED"), case.lease, binary)

    assert (other.code, other.handed_over, _lease_state(other.row)) == (0, False, "released")  # the settlement itself ran
    assert other.out == (binary.folder / "printed").read_bytes() and "task" not in json.loads(other.out)
    assert "task" not in _script_record(other)["payload"]


def test_a_refusal_the_binary_made_is_printed_as_it_is(
        mod: ModuleType, twins: Callable[[Case], tuple[Path, Path]], commit_binary: Callable[..., Fake],
        monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    case = SETTLING["cancelled releases"]
    _, rust = twins(case)
    binary = commit_binary(reply=_error_reply("E-CONTRACT", "final"), indent=2, exit=2)

    other = _run(mod, monkeypatch, capsysbinary, rust, _argv("CANCELLED"), case.lease, binary)

    assert (other.code, other.handed_over) == (2, False)
    assert other.out == (binary.folder / "printed").read_bytes()
    assert (other.calls, _lease_state(other.row)) == ([], "held")


def test_an_unknown_outcome_is_the_typed_refusal_and_settles_nothing(
        mod: ModuleType, twins: Callable[[Case], tuple[Path, Path]], commit_binary: Callable[..., Fake],
        monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    case = SETTLING["cancelled releases"]
    _, rust = twins(case)
    binary = commit_binary(commit=(TID, "CANCELLED", "why"), manifest=_manifest(case.before, "CANCELLED"), raw="not a reply\n")

    other = _run(mod, monkeypatch, capsysbinary, rust, _argv("CANCELLED"), case.lease, binary)

    error = json.loads(other.out)["error"]
    assert (other.code, other.handed_over, error["code"], error["details"]["write_state"]) == (2, False, "E-INTERNAL", "unknown")
    assert (other.calls, _lease_state(other.row), other.row["state"]) == ([], "held", "CANCELLED")  # committed, and not settled by a guess


def test_a_call_the_binary_hands_back_is_answered_by_python_with_the_settled_row(
        mod: ModuleType, twins: Callable[[Case], tuple[Path, Path]], commit_binary: Callable[..., Fake],
        monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    case = SETTLING["cancelled releases"]
    python, rust = twins(case)
    binary = commit_binary(reply=_error_reply("E-DEP", "not_started"), indent=2, exit=2)

    truth = _run(mod, monkeypatch, capsysbinary, python, _argv("CANCELLED"), case.lease, None)
    other = _run(mod, monkeypatch, capsysbinary, rust, _argv("CANCELLED"), case.lease, binary)

    assert (other.handed_over, binary.starts, other.code) == (True, 1, 0)
    assert masked(other.out.decode("utf-8")) == masked(truth.out.decode("utf-8"))
    assert json.loads(other.out)["task"]["notes_json"]["lease"]["state"] == "released"
    assert [_dump(record) for record in other.records] == [_dump(record) for record in truth.records]


# --- the replay of a committed transition releases its store ------------------------------------------------------------------------

class _Conn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_replay_closes_store(mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from swarm import telemetry

    stores: list[Any] = []

    class PatchedStore:
        def __init__(self, *, root: Any) -> None:
            self.path = tmp_path / "tasks.db"
            self.conn = _Conn()
            stores.append(self)

    monkeypatch.setattr(mod, "TaskStore", PatchedStore)
    monkeypatch.setattr(telemetry, "refresh_live", lambda **kwargs: None)
    call = mod.WriteCall(target=TID, state="CANCELLED", reason="why", root=str(tmp_path), dry_run=False,
                         task_id=TID, correlation_id="C-1")
    manifest = mod.Manifest(effects=(), inline_from="IN_PROGRESS", inline_reason="why")

    monkeypatch.setattr(mod, "finish_transition", lambda store, ctx, tid, **kwargs: {"state": "CANCELLED"})
    assert mod._replay(call, manifest, "IN_PROGRESS") == {"state": "CANCELLED"}
    assert [store.conn.closed for store in stores] == [True]

    def settlement_fails(store: Any, ctx: Any, tid: str, **kwargs: Any) -> None:
        raise RuntimeError("settlement failed")

    monkeypatch.setattr(mod, "finish_transition", settlement_fails)
    with pytest.raises(RuntimeError):
        mod._replay(call, manifest, "IN_PROGRESS")
    assert [store.conn.closed for store in stores] == [True, True]
