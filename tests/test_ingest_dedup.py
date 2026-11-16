"""D-P24: an ingest replayed after a lost reply is applied once.

`apply_result(mode="ingest")` records `{task_id, attempt, digest, reply}` in `notes.ingest_keys` as the last write of the
transaction that applies the result, and a retry whose key is present applies nothing and returns the first reply.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from conftest import run_script

AGENT = "A05"


class Rig:
    """One store plus the captured run-log events, imported after the `swarm_dir` fixture reloads the package."""

    def __init__(self) -> None:
        from swarm import results
        from swarm.errors import SwarmError
        from swarm.taskstore import TaskStore

        self.results, self.SwarmError, self.ts = results, SwarmError, TaskStore()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, etype: str, payload: dict[str, Any], **_: object) -> None:
        self.events.append((etype, payload))

    def deduplicated(self) -> list[dict[str, Any]]:
        return [p for t, p in self.events if t == "task.result.deduplicated"]

    def task(self, tid: str = "A-1", *, upto: str = "IN_PROGRESS", **kw: Any) -> str:
        notes = kw.pop("notes", {"gates": []})
        self.ts.create(task_id=tid, correlation_id="c", capability="code.backend", notes=notes, **kw)
        order = ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"]
        for s in order[: order.index(upto) + 1] if upto in order else []:
            self.ts.transition(tid, s)
        return tid

    def apply(self, tid: str, result: dict[str, Any], *, mode: str = "ingest", meta: dict[str, Any] | None = None) -> str:
        return self.results.apply_result(self.ts, self.ts.get(tid), agent_id=AGENT, result=result, meta=meta or {},
                                         emit=self.emit, mode=mode)

    def keys(self, tid: str) -> list[dict[str, Any]]:
        keys: list[dict[str, Any]] = self.ts.get(tid)["notes_json"].get("ingest_keys", [])
        return keys

    def artifacts(self, tid: str) -> list[str]:
        return [r[0] for r in self.ts.conn.execute("SELECT uri FROM artifacts WHERE task_id=? ORDER BY id", (tid,))]

    def states(self, tid: str) -> list[str]:
        return [h["to_state"] for h in self.ts.history(tid)]


@pytest.fixture()
def rig(swarm_dir):
    return Rig()


def _out(uri: str) -> dict[str, str]:
    return {"kind": "code.backend", "uri": uri, "version": "1", "digest": ""}


def _heartbeat(tid: str, uri: str = "file://x", **extra: Any) -> dict[str, Any]:
    return {"task_id": tid, "state": "IN_PROGRESS", "outputs": [_out(uri)], **extra}


def _review(tid: str, uri: str = "file://x") -> dict[str, Any]:
    return {"task_id": tid, "state": "IN_REVIEW", "outputs": [_out(uri)]}


def _rework(rig: Rig, tid: str) -> None:
    """IN_REVIEW → CHANGES_REQUESTED → IN_PROGRESS: another writer cycles the task back, the only way a replay of
    IN_REVIEW would again be a move the state machine accepts."""
    rig.ts.transition(tid, "CHANGES_REQUESTED")
    rig.ts.transition(tid, "IN_PROGRESS")


# ---- replay applies once ---------------------------------------------------------------------------------------------

def test_heartbeat_replay_adds_one_set_of_artifact_rows_and_returns_the_first_reply(rig):
    tid = rig.task()
    result = _heartbeat(tid)
    first = rig.apply(tid, result, meta={"ingest": "first.json"})
    before = rig.ts.get(tid)
    second = rig.apply(tid, result, meta={"ingest": "second.json"})
    after = rig.ts.get(tid)
    assert first == second == "IN_PROGRESS"
    assert rig.artifacts(tid) == ["file://x"]
    assert after["outputs"] == before["outputs"] == [_out("file://x")]
    assert after["notes_json"]["meta"] == {"ingest": "first.json"}  # the replay wrote no notes
    assert after["updated_at"] == before["updated_at"]
    assert len(rig.keys(tid)) == 1
    assert rig.deduplicated() == [{"task_id": tid, "mode": "ingest", "attempt": 1, "digest": rig.keys(tid)[0]["digest"],
                                   "reply": "IN_PROGRESS"}]


def test_in_review_replay_after_a_cycle_back_does_not_double_transition_artifacts_or_events(rig):
    tid = rig.task()
    result = _review(tid)
    assert rig.apply(tid, result) == "IN_REVIEW"
    _rework(rig, tid)
    history = rig.states(tid)
    # The state machine would accept IN_PROGRESS → IN_REVIEW again, so only the key stops the second application.
    assert rig.apply(tid, result) == "IN_REVIEW"  # the first reply, although the task is IN_PROGRESS again
    assert rig.ts.get(tid)["state"] == "IN_PROGRESS"
    assert rig.states(tid) == history
    assert rig.artifacts(tid) == ["file://x"]
    assert len(rig.deduplicated()) == 1


def test_gate_feedback_is_not_appended_twice_by_a_replay(rig):
    from swarm.gates import make_verdict

    targets = ("T-a", "T-b")
    for t in targets:
        rig.task(t, upto="CREATED")
    gate = "G-1"
    rig.ts.create(task_id=gate, correlation_id="c", capability="gate.review", notes={"gate": "review", "gate_for": list(targets)})
    for s in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"):
        rig.ts.transition(gate, s)
    for t in targets:
        rig.ts.record_verdict(t, make_verdict(gate="review", task_id=t, agent_id="A09@test", verdict="fail",
                                              findings=[{"severity": "major", "kind": "k", "summary": "s"}],
                                              correlation_id="c", extra={"gate_task": gate}))
    result = {"task_id": gate, "state": "IN_REVIEW", "outputs": [_out("file://rev")],
              "verdicts": {t: {"verdict": "request_changes", "findings": []} for t in targets}}
    assert rig.apply(gate, result) == "IN_REVIEW"
    feedback = {t: rig.ts.get(t)["notes_json"]["feedback"] for t in targets}
    assert all(len(v) == 1 for v in feedback.values())
    _rework(rig, gate)
    history = rig.states(gate)
    assert rig.apply(gate, result) == "IN_REVIEW"
    assert {t: rig.ts.get(t)["notes_json"]["feedback"] for t in targets} == feedback
    assert rig.states(gate) == history
    assert rig.artifacts(gate) == ["file://rev"]


def test_a_replay_the_state_machine_refuses_is_still_refused_and_records_nothing(rig):
    """IN_REVIEW → IN_REVIEW is illegal: the replay raises exactly as before (tests/test_results.py pins the CLI form)."""
    tid = rig.task()
    result = _review(tid)
    rig.apply(tid, result)
    keys, history = rig.keys(tid), rig.states(tid)
    with pytest.raises(rig.SwarmError, match="illegal transition IN_REVIEW → IN_REVIEW"):
        rig.apply(tid, result)
    assert rig.keys(tid) == keys and rig.states(tid) == history
    assert rig.deduplicated() == []


# ---- what is a different key -----------------------------------------------------------------------------------------

def test_the_same_result_under_another_attempt_is_applied(rig):
    tid = rig.task()
    result = _heartbeat(tid)
    rig.apply(tid, result)
    for s in ("RETRY", "CLAIMED", "IN_PROGRESS"):
        rig.ts.transition(tid, s)
    assert rig.ts.get(tid)["attempt"] == 2
    assert rig.apply(tid, result) == "IN_PROGRESS"
    assert rig.artifacts(tid) == ["file://x", "file://x"]
    assert [k["attempt"] for k in rig.keys(tid)] == [1, 2]
    assert rig.keys(tid)[0]["digest"] == rig.keys(tid)[1]["digest"]
    assert rig.deduplicated() == []


def test_a_different_result_for_the_same_attempt_is_applied(rig):
    tid = rig.task()
    rig.apply(tid, _heartbeat(tid, "file://one"))
    rig.apply(tid, _heartbeat(tid, "file://two"))
    rig.apply(tid, _heartbeat(tid, "file://one", summary_md="changed"))
    assert rig.artifacts(tid) == ["file://one", "file://two", "file://one"]
    assert len({k["digest"] for k in rig.keys(tid)}) == 3
    assert rig.deduplicated() == []


def test_the_key_is_per_task(rig):
    a, b = rig.task("A-1"), rig.task("A-2")
    rig.apply(a, _heartbeat(a))
    rig.apply(b, _heartbeat(b))  # same attempt, different task_id inside the result, so a different digest as well
    assert rig.artifacts(a) == rig.artifacts(b) == ["file://x"]
    assert rig.deduplicated() == []
    # a key entry copied onto another task never matches it
    rig.ts.set_notes(b, ingest_keys=[{**rig.keys(a)[0]}])
    rig.apply(b, _heartbeat(b))
    assert rig.artifacts(b) == ["file://x", "file://x"]


# ---- the bound -------------------------------------------------------------------------------------------------------

def test_the_33rd_distinct_key_evicts_the_oldest_and_its_replay_is_applied_again(rig):
    bound = rig.results.INGEST_KEYS_BOUND
    assert bound == 32
    tid = rig.task()
    results = [_heartbeat(tid, f"file://{i}") for i in range(bound + 1)]
    for r in results:
        rig.apply(tid, r)
    digests = [k["digest"] for k in rig.keys(tid)]
    assert len(digests) == bound
    assert rig.results.result_digest(results[0]) not in digests  # the oldest went
    assert digests == [rig.results.result_digest(r) for r in results[1:]]  # the newest 32, oldest first
    rows = len(rig.artifacts(tid))
    rig.apply(tid, results[-1])  # inside the bound: skipped
    assert len(rig.artifacts(tid)) == rows
    rig.apply(tid, results[0])  # evicted: applied again, the documented limit of the bound
    assert rig.artifacts(tid)[-1] == "file://0" and len(rig.artifacts(tid)) == rows + 1
    assert len(rig.keys(tid)) == bound and rig.results.result_digest(results[0]) == rig.keys(tid)[-1]["digest"]


# ---- nothing applied, nothing recorded -------------------------------------------------------------------------------

def _refusal_cases(rig: Rig) -> list[tuple[str, dict[str, Any]]]:
    claimed = rig.task("C-1", upto="CLAIMED")
    blocked = rig.task("B-1")
    rig.ts.transition(blocked, "BLOCKED")
    waiting = rig.task("W-1", upto="PLANNED", depends_on=["C-1"])
    rig.task("T-a", upto="CREATED")
    rig.ts.create(task_id="G-0", correlation_id="c", capability="gate.review", notes={"gate": "review", "gate_for": ["T-a"]})
    for s in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"):
        rig.ts.transition("G-0", s)
    return [
        (claimed, _heartbeat(claimed)),   # a heartbeat is legal only while IN_PROGRESS
        (blocked, _heartbeat(blocked)),   # A01 releases BLOCKED, not an ingest
        (waiting, _heartbeat(waiting)),   # dependencies unmet
        ("G-0", _review("G-0")),          # the gate script recorded no verdict
    ]


@pytest.mark.parametrize("index", range(4), ids=["claimed-heartbeat", "blocked", "unmet-deps", "gate-script-not-run"])
def test_a_refused_result_leaves_no_key_and_no_write(rig, index):
    tid, result = _refusal_cases(rig)[index]
    history, row = rig.states(tid), rig.ts.get(tid)
    with pytest.raises(rig.SwarmError):
        rig.apply(tid, result)
    after = rig.ts.get(tid)
    assert rig.keys(tid) == []
    assert rig.states(tid) == history
    assert after["notes_json"] == row["notes_json"] and after["outputs"] == row["outputs"]
    assert rig.artifacts(tid) == []
    assert rig.deduplicated() == []


def test_an_invalid_result_never_reaches_apply_result_and_leaves_no_key(rig):
    tid = rig.task()
    with pytest.raises(rig.SwarmError):
        rig.results.validate_result({"task_id": tid}, task_id=tid)
    assert rig.keys(tid) == []


def test_headless_applications_are_not_keyed(rig):
    tid = rig.task()
    result = _review(tid)
    rig.apply(tid, result, mode="headless")
    _rework(rig, tid)
    assert rig.apply(tid, result, mode="headless") == "IN_REVIEW"  # same bytes, same attempt: a new session's result
    assert rig.keys(tid) == []
    assert rig.artifacts(tid) == ["file://x", "file://x"]
    assert rig.deduplicated() == []


# ---- crash windows ---------------------------------------------------------------------------------------------------

def _fail(name: str, when: Any = lambda *a, **k: True) -> Any:
    def _patch(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        real = getattr(rig.ts, name)

        def boom(*a: Any, **k: Any) -> Any:
            if when(*a, **k):
                raise RuntimeError(f"injected crash in {name}")
            return real(*a, **k)
        monkeypatch.setattr(rig.ts, name, boom)
    return _patch


FAULTS = {
    "second-artifact": _fail("add_artifact", lambda *a, **k: k.get("uri") == "file://two"),
    "result-notes": _fail("set_notes", lambda *a, **k: "result" in k),
    "key-write": _fail("set_notes", lambda *a, **k: "ingest_keys" in k),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
@pytest.mark.parametrize("state", ["IN_PROGRESS", "IN_REVIEW"])
def test_a_crash_before_the_key_leaves_no_effect_and_no_key_and_the_retry_applies(rig, monkeypatch, fault, state):
    """Before COMMIT nothing is applied and no key exists, wherever the crash falls: the retry is applied in full."""
    tid = rig.task()
    result = {"task_id": tid, "state": state, "outputs": [_out("file://one"), _out("file://two")]}
    history, row = rig.states(tid), rig.ts.get(tid)
    with monkeypatch.context() as m:
        FAULTS[fault](rig, m)
        with pytest.raises(RuntimeError, match="injected crash"):
            rig.apply(tid, result)
    after = rig.ts.get(tid)
    assert after["state"] == "IN_PROGRESS" and rig.states(tid) == history
    assert after["notes_json"] == row["notes_json"] and after["outputs"] == []
    assert rig.artifacts(tid) == [] and rig.keys(tid) == []
    assert rig.apply(tid, result) == state
    assert rig.artifacts(tid) == ["file://one", "file://two"] and len(rig.keys(tid)) == 1
    assert rig.deduplicated() == []


def test_a_crash_between_the_feedback_and_the_key_leaves_neither(rig, monkeypatch):
    from swarm.gates import make_verdict

    rig.task("T-a", upto="CREATED")
    rig.ts.create(task_id="G-1", correlation_id="c", capability="gate.review", notes={"gate": "review", "gate_for": ["T-a"]})
    for s in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"):
        rig.ts.transition("G-1", s)
    rig.ts.record_verdict("T-a", make_verdict(gate="review", task_id="T-a", agent_id="A09@test", verdict="fail",
                                              findings=[{"severity": "major", "kind": "k", "summary": "s"}],
                                              correlation_id="c", extra={"gate_task": "G-1"}))
    result = {"task_id": "G-1", "state": "IN_REVIEW", "verdicts": {"T-a": {"verdict": "request_changes", "findings": []}}}
    with monkeypatch.context() as m:
        _fail("set_notes", lambda *a, **k: "ingest_keys" in k)(rig, m)
        with pytest.raises(RuntimeError, match="injected crash in set_notes"):
            rig.apply("G-1", result)
    assert rig.ts.get("G-1")["state"] == "IN_PROGRESS" and rig.keys("G-1") == []
    assert rig.ts.get("T-a")["notes_json"].get("feedback", []) == []
    rig.apply("G-1", result)
    assert len(rig.ts.get("T-a")["notes_json"]["feedback"]) == 1


@pytest.mark.parametrize("state", ["IN_PROGRESS", "IN_REVIEW"])
def test_the_key_is_the_last_write_of_the_applying_transaction(rig, monkeypatch, state):
    tid = rig.task()
    calls: list[str] = []
    for name in ("transition", "set_notes", "add_artifact", "append_feedback"):
        real = getattr(rig.ts, name)

        def spy(*a: Any, _real: Any = real, _name: str = name, **k: Any) -> Any:
            calls.append(f"{_name}:ingest_keys" if "ingest_keys" in k else _name)
            return _real(*a, **k)
        monkeypatch.setattr(rig.ts, name, spy)
    rig.apply(tid, {"task_id": tid, "state": state, "outputs": [_out("file://x")]})
    assert calls[-1] == "set_notes:ingest_keys"
    assert calls.count("set_notes:ingest_keys") == 1
    assert "add_artifact" in calls[:-1]


def test_a_crash_after_the_commit_leaves_the_effects_and_the_key_so_the_retry_is_skipped(rig):
    """The window after COMMIT is the lost reply itself: the caller never saw it, the retry finds the key."""
    tid = rig.task()
    result = _review(tid)
    rig.apply(tid, result)  # the reply is "lost"
    _rework(rig, tid)
    rows, history = rig.artifacts(tid), rig.states(tid)
    assert rig.apply(tid, result) == "IN_REVIEW"
    assert rig.artifacts(tid) == rows and rig.states(tid) == history


# ---- the digest ------------------------------------------------------------------------------------------------------

def test_the_digest_is_sha256_of_the_sorted_compact_utf8_json(rig):
    digest = rig.results.result_digest
    expected = hashlib.sha256('{"a":[1,{"c":2,"d":3}],"b":"é"}'.encode()).hexdigest()
    assert digest({"b": "é", "a": [1, {"d": 3, "c": 2}]}) == expected
    assert len({digest({"a": 1}), digest({"a": 1, "sig": "x"}), digest({"a": 1, "sig": "y"})}) == 3  # `sig` is not dropped
    assert digest({"a": 1}) != digest({"a": 1.0})  # JSON writes 1.0 differently, so a changed number is a changed result


# ---- through the CLI -------------------------------------------------------------------------------------------------

def test_orch_status_ingest_replay_is_one_application_with_one_reply(tmp_path, swarm_dir):
    plan = {"tasks": [{"id": "one", "capability": "code.backend", "agent": "A05", "title": "one", "depends_on": [], "gates": []}]}
    env = {"SWARM_DIR": str(swarm_dir)}
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    assert run_script("orch_plan.py", "--plan", str(tmp_path / "plan.json"), "--prefix", "T", "--json", env=env).returncode == 0
    f = tmp_path / "r.json"
    f.write_text(json.dumps(_heartbeat("T-one", "file://hb")))
    first = run_script("orch_status.py", "--ingest", str(f), "--json", env=env)
    second = run_script("orch_status.py", "--ingest", str(f), "--json", env=env)  # a PLANNED task was claimed by the first
    assert first.returncode == second.returncode == 0, first.stdout + second.stdout
    assert json.loads(first.stdout)["summary"] == json.loads(second.stdout)["summary"]
    from swarm.taskstore import TaskStore

    store = TaskStore()
    t = store.get("T-one")
    assert [r[0] for r in store.conn.execute("SELECT uri FROM artifacts WHERE task_id='T-one'")] == ["file://hb"]
    assert t["attempt"] == 1 and len(t["notes_json"]["ingest_keys"]) == 1
    assert [h["to_state"] for h in store.history("T-one")].count("CLAIMED") == 1
