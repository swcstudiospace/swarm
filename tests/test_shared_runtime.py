"""swarm/tools/shared_runtime.py: the runtime half of the shared baseline (DESIGN 8)."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from swarm import acp, substrate_handoff  # noqa: E402
from swarm.clock import FakeClock  # noqa: E402
from swarm.paths import swarm_dir  # noqa: E402
from swarm.schema import load_schema, validate  # noqa: E402
from swarm.taskstore import TaskStore  # noqa: E402
from swarm.tools import shared_runtime as sr  # noqa: E402
from swarm.tools.support import caller_scope  # noqa: E402

RUNTIME_TOOLS = ("shared_status_report", "shared_scratchpad_put", "shared_scratchpad_get", "shared_escalate",
                 "shared_cancel_check", "shared_log_emit", "shared_route_check", "shared_escalation_owner")
SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.delenv("SWARM_CANCEL_GRACE_S", raising=False)
    clock = FakeClock()
    monkeypatch.setattr(sr, "CLOCK", clock)
    return clock


@pytest.fixture()
def clock(state):
    return state


@pytest.fixture()
def store(tmp_path):
    s = TaskStore(tmp_path / ".swarm" / "tasks.db")
    s.create(task_id="T-be", correlation_id="C1", capability="code.backend", agent_id="A05", title="backend")
    s.create(task_id="T-qa", correlation_id="C1", capability="qa.gate", agent_id="A08", title="qa")
    return s


def call(fn, caller, payload):
    with caller_scope(caller):
        return fn(payload)


def events(kind: str | None = None) -> list[dict]:
    path = swarm_dir() / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if kind is None or r["type"] == kind]


# ---- shared_status_report ----------------------------------------------------------------------------------------
def report(store, caller="A05", **kw):
    payload = {"task_id": "T-be", "kind": "progress", "step": "reading", **kw}
    return call(sr.status_report, caller, payload)


def test_status_report_rejects_an_agent_that_is_not_assigned(store):
    out = report(store, caller="A07")
    assert out["state"] == "UNAUTHORIZED" and "A05" in out["reason"]
    assert not (swarm_dir() / "status" / "T-be.json").exists() and events("swarm.run.activity") == []
    assert report(store, caller=None)["state"] == "UNAUTHORIZED"


def test_status_report_needs_an_existing_task_and_never_creates_the_store(tmp_path):
    out = call(sr.status_report, "A05", {"task_id": "T-none", "kind": "done", "step": "x"})
    assert out["state"] == "INVALID_INPUT" and out["field"] == "task_id"
    assert not (tmp_path / ".swarm" / "tasks.db").exists()


@pytest.mark.parametrize("field,bad", [("kind", "paused"), ("percent", 101), ("percent", -1), ("percent", True),
                                       ("step", " "), ("task_id", "../x")])
def test_status_report_validates_every_field(store, field, bad):
    out = report(store, **{field: bad})
    assert out["state"] == "INVALID_INPUT" and out["field"] == field


def test_status_report_records_latest_status_and_emits_a_schema_valid_activity_event(store, clock):
    out = report(store, percent=40, note="halfway")
    assert out == {"state": "SUCCESS", "task_id": "T-be", "kind": "progress", "step": "reading",
                   "recorded": True, "emitted": True, "seq": 1}
    doc = json.loads((swarm_dir() / "status" / "T-be.json").read_text())
    assert (doc["agent"], doc["percent"], doc["note"], doc["correlation_id"], doc["updated_wall"]) == ("A05", 40, "halfway", "C1", clock.wall())
    [event] = events("swarm.run.activity")
    assert event["task_id"] == "T-be" and event["correlation_id"] == "C1" and event["payload"]["run_id"] == "T-be.a1"
    assert event["payload"]["agent"] == "A05" and "reading" in event["payload"]["text"] and "40%" in event["payload"]["text"]
    assert validate(event, load_schema("swarm.event.v1")) == []


def test_status_report_is_idempotent_and_throttles_progress_bursts(store, clock):
    assert report(store)["emitted"] is True
    again = report(store)  # identical content: nothing written, nothing emitted
    assert (again["recorded"], again["emitted"], again["seq"]) == (False, False, 1)
    clock.advance(0.4)
    burst = report(store, step="writing")  # new content inside the 1 s window: recorded, no event
    assert (burst["recorded"], burst["emitted"], burst["seq"]) == (True, False, 2)
    blocked = report(store, kind="blocked", step="waiting for the design")  # a state change is always announced
    assert (blocked["recorded"], blocked["emitted"]) == (True, True)
    clock.advance(1.5)
    assert report(store, step="resumed")["emitted"] is True
    assert len(events("swarm.run.activity")) == 3
    assert json.loads((swarm_dir() / "status" / "T-be.json").read_text())["seq"] == 4


def test_status_report_does_not_throttle_after_a_wall_clock_step_back(store, clock):
    report(store)
    clock.jump_wall(-3600)
    assert report(store, step="later")["emitted"] is True


def test_status_report_clips_and_redacts_text(store):
    out = report(store, step="x" * 400, note=f"token {SECRET} and Authorization: Bearer abcdefghijklmnopqrstuv0123")
    assert len(out["step"]) == 240 and out["step"].endswith("…")
    doc = json.loads((swarm_dir() / "status" / "T-be.json").read_text())
    blob = json.dumps(doc) + (swarm_dir() / "events.jsonl").read_text()
    assert SECRET not in blob and "abcdefghijklmnopqrstuv0123" not in blob and "[REDACTED]" in doc["note"]


def test_status_report_keeps_the_tool_call_counter_the_live_snapshot_shows(store):
    live = {"schema": "swarm.live.v1", "correlation_id": "C1", "seq": 1, "updated_wall": 1.0,
            "swarm": {"state": "running", "elapsed_s": 1.0, "max_parallel": 4, "effective_parallel": 4, "running": 1, "queued": 0,
                      "waiting": 0, "completed": 0, "failed": 0, "cancelled": 0, "timed_out": 0, "stalled": 0, "degraded": None},
            "runs": [{"run_id": "T-be.a1", "task_id": "T-be", "agent": "A05", "state": "running", "attempt": 1, "elapsed_s": 1.0,
                      "state_elapsed_s": 1.0, "since_activity_s": 0.0, "stalled": False, "activity": "x", "tool_calls": 12,
                      "last_tool": "edit", "parent": None, "handover_in": None, "handover_out": None, "error": None}]}
    assert sr._carried_calls("T-be.a1") == 0  # no live.json yet
    (swarm_dir() / "live.json").write_text(json.dumps(live))
    assert sr._carried_calls("T-be.a1") == 12 and sr._carried_calls("T-other.a1") == 0


def test_status_report_reaches_the_live_snapshot_a_scheduler_producer_owns(store, clock):
    """Greptile #6: the report only reached the event log; the producer's own snapshot never showed the step."""
    from swarm.telemetry import Telemetry, read_live

    producer = Telemetry(correlation_id="C1", clock=clock, tee=False)
    producer.run_queued("T-be.a1", "a05-backend")
    producer.run_started("T-be.a1", sid="s1", runtime="omp")
    clock.advance(2)  # past the one-write-per-second flush window
    assert report(store, percent=40)["emitted"] is True
    [row] = read_live()["runs"]
    assert read_live()["producer"] == "scheduler"
    assert row["run_id"] == "T-be.a1" and row["activity"] == "progress: reading (40%)"
    clock.advance(2)
    report(store, kind="blocked", step="waiting for the design")
    assert read_live()["runs"][0]["activity"] == "blocked: waiting for the design"


def test_status_report_reaches_the_derived_snapshot_when_no_scheduler_runs(store):
    from swarm.telemetry import read_live

    assert report(store, step="indexing")["emitted"] is True
    doc = read_live()
    assert doc["producer"] == "derived"
    assert [r["activity"] for r in doc["runs"] if r["task_id"] == "T-be"] == ["progress: indexing"]


# ---- shared_scratchpad_put / get ---------------------------------------------------------------------------------
def put(caller="A05", key="plan", value=None, corr="C1", **kw):
    return call(sr.scratchpad_put, caller, {"correlation_id": corr, "key": key, "value": {"n": 1} if value is None else value, **kw})


def get(caller="A05", corr="C1", **kw):
    return call(sr.scratchpad_get, caller, {"correlation_id": corr, **kw})


def test_scratchpad_round_trip_with_metadata(clock):
    out = put(value={"steps": ["a", "b"]})
    assert (out["state"], out["owner"], out["version"], out["shared"], out["updated"], out["updated_by"]) == ("SUCCESS", "A05", 1, False, clock.wall(), "A05")
    got = get("A08", key="plan")
    assert got["found"] is True and got["value"] == {"steps": ["a", "b"]} and got["meta"]["owner"] == "A05"
    assert got["meta"]["bytes"] == len(json.dumps({"steps": ["a", "b"]}, separators=(",", ":")))
    assert get(key="missing") == {"state": "SUCCESS", "correlation_id": "C1", "found": False}
    assert get(corr="C-empty")["keys"] == []


def test_scratchpad_get_lists_keys_with_metadata_sorted():
    put(key="b.key"), put(caller="A07", key="a.key")
    keys = get()["keys"]
    assert [(k["key"], k["owner"], k["version"]) for k in keys] == [("a.key", "A07", 1), ("b.key", "A05", 1)]


def test_scratchpad_overwrite_is_owner_only_unless_the_key_is_shared():
    put(key="mine")
    denied = put(caller="A07", key="mine", value={"n": 2})
    assert denied["state"] == "UNAUTHORIZED" and "A05" in denied["reason"] and get(key="mine")["value"] == {"n": 1}
    owned = put(key="mine", value={"n": 3})
    assert owned["version"] == 2
    put(key="open", shared=True)
    by_other = put(caller="A07", key="open", value={"n": 9})
    assert (by_other["state"], by_other["owner"], by_other["updated_by"], by_other["version"]) == ("SUCCESS", "A05", "A07", 2)
    assert by_other["shared"] is True
    # a non-owner cannot flip the flag, the owner can
    assert put(caller="A07", key="open", shared=False)["shared"] is True
    assert put(key="open", shared=False)["shared"] is False
    assert put(caller="A07", key="open")["state"] == "UNAUTHORIZED"


def test_scratchpad_compare_and_swap():
    assert put(key="k", if_version=0)["version"] == 1  # create-only
    stale = put(key="k", if_version=0)
    assert (stale["state"], stale["field"], stale["current_version"]) == ("INVALID_INPUT", "if_version", 1)
    assert put(key="k", value={"n": 2}, if_version=1)["version"] == 2
    lost = put(key="k", value={"n": 3}, if_version=1)
    assert lost["state"] == "INVALID_INPUT" and lost["current_version"] == 2 and get(key="k")["value"] == {"n": 2}
    assert put(key="nope", if_version=3)["state"] == "INVALID_INPUT"  # a missing key is at version 0


def test_scratchpad_ownership_is_checked_before_the_version():
    put(key="k")
    assert put(caller="A07", key="k", if_version=99)["state"] == "UNAUTHORIZED"


@pytest.mark.parametrize("key", ["", "Upper", "has space", "a/b", "x" * 65, "ünï"])
def test_scratchpad_rejects_bad_keys(key):
    assert put(key=key)["field"] == "key"
    assert get(key=key)["field"] == "key"


@pytest.mark.parametrize("corr", ["", "..", "a/b", ".", "x" * 129])
def test_scratchpad_rejects_bad_correlation_ids(corr):
    assert put(corr=corr)["field"] == "correlation_id"


def test_scratchpad_value_size_limit_is_8_kib():
    fits = {"v": "x" * (8 * 1024 - len('{"v":""}'))}
    assert put(key="fit", value=fits)["bytes"] == 8 * 1024
    out = put(key="big", value={"v": "x" * (8 * 1024 - len('{"v":""}') + 1)})
    assert out["state"] == "INVALID_INPUT" and out["field"] == "value" and get(key="big")["found"] is False
    assert put(key="notobj", value=["a"])["field"] == "value"
    assert put(key="notjson", value={"v": {1, 2}})["field"] == "value"


def test_scratchpad_refuses_a_non_finite_number_inside_the_value():
    """SW-02: json.dumps writes NaN and Infinity as bare tokens, which are not JSON. The whole value is refused, the
    refusal names the value field, and nothing is stored under the key."""
    for key, bad in (("nan", float("nan")), ("inf", float("inf"))):
        out = put(key=key, value={"x": bad})
        assert out["state"] == "INVALID_INPUT" and out["field"] == "value", key
        assert get(key=key)["found"] is False, key


def test_scratchpad_key_count_limit_is_64_per_correlation_and_per_correlation_only():
    for i in range(64):
        assert put(key=f"k{i}")["state"] == "SUCCESS"
    full = put(key="one-more")
    assert full["state"] == "INVALID_INPUT" and full["field"] == "key"
    assert put(key="k0", value={"n": 2})["version"] == 2  # overwriting an existing key never needs a free slot
    assert put(key="one-more", corr="C2")["state"] == "SUCCESS"
    assert len(get()["keys"]) == 64


def test_scratchpad_requires_a_known_caller():
    assert put(caller=None)["state"] == "UNAUTHORIZED"


def test_scratchpad_redacts_values_on_write():
    put(key="creds", value={"password": "hunter2", "note": f"use {SECRET}", "n": 3})
    stored = (swarm_dir() / "scratch" / "C1" / "creds.json").read_text()
    assert "hunter2" not in stored and SECRET not in stored
    assert get(key="creds")["value"] == {"password": "[REDACTED]", "note": "use [REDACTED]", "n": 3}


def test_scratchpad_concurrent_puts_from_threads_lose_no_update():
    put(key="counter", shared=True, value={"n": 0})
    errors = []

    def worker(caller):
        for i in range(15):
            out = put(caller=caller, key="counter", value={"n": i})
            if out["state"] != "SUCCESS":
                errors.append(out)

    threads = [threading.Thread(target=worker, args=(f"A0{n}",)) for n in range(1, 9)]
    [t.start() for t in threads], [t.join() for t in threads]
    assert errors == [] and get(key="counter")["meta"]["version"] == 1 + 8 * 15


def test_scratchpad_key_limit_holds_under_concurrent_creates():
    results = []

    def worker(n):
        for i in range(10):
            results.append(put(caller=f"A0{n}", key=f"t{n}-{i}")["state"])

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 9)]
    [t.start() for t in threads], [t.join() for t in threads]
    assert results.count("SUCCESS") == 64 and results.count("INVALID_INPUT") == 16 and len(get()["keys"]) == 64


def test_scratchpad_concurrent_puts_from_a_second_process(tmp_path):
    put(key="counter", shared=True, value={"n": 0})
    code = ("import sys; sys.path.insert(0, sys.argv[1])\n"
            "from swarm.tools import shared_runtime as sr\n"
            "from swarm.tools.support import caller_scope\n"
            "bad = 0\n"
            "for i in range(25):\n"
            "    with caller_scope('A07'):\n"
            "        out = sr.scratchpad_put({'correlation_id': 'C1', 'key': 'counter', 'value': {'n': i}})\n"
            "    bad += out['state'] != 'SUCCESS'\n"
            "sys.exit(bad)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(ROOT)], env={**os.environ, "SWARM_DIR": str(tmp_path / ".swarm")},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    mine = [put(caller="A05", key="counter", value={"n": i})["state"] for i in range(25)]
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err.decode()
    assert mine == ["SUCCESS"] * 25 and get(key="counter")["meta"]["version"] == 1 + 25 + 25


def test_scratchpad_reports_a_busy_lock_as_unavailable(monkeypatch):
    monkeypatch.setattr(sr, "SCRATCH_LOCK_WAIT_S", 0.05)
    import fcntl

    directory = swarm_dir() / "scratch" / "C1"
    directory.mkdir(parents=True)
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(OSError, match="busy"):  # the registry maps OSError to DEPENDENCY_UNAVAILABLE
            put()
    finally:
        os.close(fd)


# ---- shared_cancel_check -----------------------------------------------------------------------------------------
def write_flag(corr="C1", body=None):
    path = swarm_dir() / "cancel" / corr
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


def cancel(task="T-be"):
    return call(sr.cancel_check, "A05", {"task_id": task})


def test_cancel_check_without_a_flag_or_deadline(store):
    assert cancel() == {"state": "SUCCESS", "task_id": "T-be", "cancel_requested": False, "reason": "", "grace_remaining_s": 0.0,
                        "has_deadline": False, "deadline_remaining_s": 0.0, "deadline_exceeded": False}


def test_cancel_check_reads_the_flag_and_counts_the_grace_down(store, clock):
    write_flag(body={"requested_wall": clock.wall(), "reason": "operator stop", "grace_s": 10})
    out = cancel()
    assert (out["cancel_requested"], out["reason"], out["grace_remaining_s"]) == (True, "operator stop", 10.0)
    clock.advance(4.25)
    assert cancel()["grace_remaining_s"] == 5.75
    clock.advance(60)
    assert cancel()["grace_remaining_s"] == 0.0 and cancel()["cancel_requested"] is True


def test_cancel_check_flag_is_per_correlation(store, clock):
    write_flag("C-other", {"requested_wall": clock.wall(), "reason": "x", "grace_s": 5})
    assert cancel()["cancel_requested"] is False


def test_cancel_check_grace_never_exceeds_grace_s_after_a_wall_step_back(store, clock):
    write_flag(body={"requested_wall": clock.wall(), "reason": "r", "grace_s": 10})
    clock.jump_wall(-500)
    assert cancel()["grace_remaining_s"] == 10.0


def test_cancel_check_accepts_an_unparseable_flag_using_mtime_and_env_grace(store, clock, monkeypatch):
    monkeypatch.setenv("SWARM_CANCEL_GRACE_S", "20")
    path = write_flag(body="")
    os.utime(path, (clock.wall() - 5, clock.wall() - 5))
    out = cancel()
    assert out["cancel_requested"] is True and out["reason"] == "" and out["grace_remaining_s"] == pytest.approx(15, abs=0.01)
    write_flag(body={"requested_wall": "soon", "grace_s": -3, "reason": 7})  # bad types fall back, never raise
    assert cancel()["cancel_requested"] is True


def test_cancel_check_deadline_math(store, clock):
    store.set_notes("T-be", running_deadline=clock.wall() + 30)
    out = cancel()
    assert (out["has_deadline"], out["deadline_remaining_s"], out["deadline_exceeded"]) == (True, 30.0, False)
    clock.advance(12.5)
    assert cancel()["deadline_remaining_s"] == 17.5
    clock.advance(17.5)
    out = cancel()
    assert (out["deadline_remaining_s"], out["deadline_exceeded"]) == (0.0, True)
    clock.advance(100)
    assert cancel()["deadline_remaining_s"] == 0.0
    store.set_notes("T-be", running_deadline="never")  # a non-number is no deadline
    assert cancel()["has_deadline"] is False


def test_cancel_check_unknown_task(store):
    assert cancel("T-none")["state"] == "INVALID_INPUT"


# ---- shared_escalate / shared_escalation_owner -------------------------------------------------------------------
def escalate(caller="A05", **kw):
    return call(sr.escalate, caller, {"task_id": "T-be", "reason_code": "blocked", "summary": "no input", **kw})


def test_escalate_returns_a_valid_blocked_result_patch_and_records_the_event(store):
    out = escalate(needs=["the signed design", "an API key"])
    assert out["state"] == "SUCCESS" and out["owner"] == "A01" and out["reason_code"] == "blocked"
    patch = out["result_patch"]
    assert validate(patch, load_schema("task.result.v1")) == []
    assert patch["state"] == "BLOCKED" and patch["task_id"] == "T-be" and "the signed design" in patch["needs"]
    assert patch["needs"].startswith("blocked: no input")
    [event] = events("escalation.request")
    assert event["task_id"] == "T-be" and event["correlation_id"] == "C1" and event["msg_id"] == out["escalation_id"]
    body = event["payload"]
    assert body["reason_code"] == "blocked" and body["owner"] == "A01" and body["from_agent"] == "A05"
    assert body["evidence"] == ["no input", "the signed design", "an API key"] and body["options"]


@pytest.mark.parametrize("reason", ["blocked", "ambiguous-input", "stale-handover", "budget", "policy", "conflict", "other"])
def test_escalate_accepts_every_reason_code(store, reason):
    assert validate(escalate(reason_code=reason)["result_patch"], load_schema("task.result.v1")) == []


@pytest.mark.parametrize("kw", [{"reason_code": "E-INPUT"}, {"summary": ""}, {"needs": "one"}, {"needs": [""]},
                                {"needs": ["x"] * 21}, {"task_id": "a/b"}])
def test_escalate_validates_input(store, kw):
    out = escalate(**kw)
    assert out["state"] == "INVALID_INPUT" and out["field"] in kw and events("escalation.request") == []


def test_escalate_is_for_the_assigned_agent_only(store):
    assert escalate(caller="A07")["state"] == "UNAUTHORIZED" and events("escalation.request") == []
    assert call(sr.escalate, "A05", {"task_id": "T-none", "reason_code": "other", "summary": "x"})["state"] == "INVALID_INPUT"


def test_escalate_redacts_and_clips_before_the_record_and_the_patch(store):
    out = escalate(summary=f"leaked {SECRET} " + "y" * 600)
    text = json.dumps(out) + (swarm_dir() / "events.jsonl").read_text()
    assert SECRET not in text and len(events("escalation.request")[0]["payload"]["summary"]) <= 500
    assert len(out["result_patch"]["needs"]) <= 500


def test_escalation_owner_matches_the_substrate_logic(store, tmp_path):
    store.create(task_id="T-maint", correlation_id="C1", capability="maint.patch", agent_id="A14")
    store.create(task_id="T-fix", correlation_id="C1", capability="code.backend", agent_id="A05", depends_on=["T-maint"])
    store.create(task_id="T-mixed", correlation_id="C1", capability="code.backend", agent_id="A05", depends_on=["T-gone", "T-qa", "T-maint"])
    store.create(task_id="T-plain", correlation_id="C1", capability="code.backend", agent_id="A05", depends_on=["T-qa"])
    for task_id, expected in (("T-fix", "A14"), ("T-mixed", "A14"), ("T-plain", "A01"), ("T-be", "A01"), ("T-maint", "A01")):
        got = call(sr.escalation_owner, "A02", {"task_id": task_id})
        assert got["state"] == "SUCCESS" and got["owner"] == expected == substrate_handoff.escalation_owner(store, store.get(task_id))
    assert escalate_owner_of("T-fix") == "A14"
    assert call(sr.escalation_owner, "A02", {"task_id": "T-none"})["state"] == "INVALID_INPUT"


def escalate_owner_of(task_id):
    """The owner that shared_escalate puts in its record for a task the caller is assigned to."""
    out = call(sr.escalate, "A05", {"task_id": task_id, "reason_code": "other", "summary": "s"})
    return out["owner"]


# ---- shared_log_emit ---------------------------------------------------------------------------------------------
def log(caller="A05", **kw):
    return call(sr.log_emit, caller, {"level": "info", "message": "hello", **kw})


def test_log_emit_writes_one_agent_log_event_with_ids(store):
    out = log(task_id="T-be", fields={"files": 3, "ok": True, "name": "x", "ratio": 0.5, "none": None})
    assert out["state"] == "SUCCESS" and out["correlation_id"] == "C1" and out["task_id"] == "T-be"
    [event] = events("agent.log")
    assert (event["task_id"], event["correlation_id"], event["msg_id"]) == ("T-be", "C1", out["msg_id"])
    assert event["payload"] == {"level": "info", "message": "hello", "agent": "A05",
                                "fields": {"files": 3, "ok": True, "name": "x", "ratio": 0.5, "none": None}}


def test_log_emit_correlation_precedence(store, tmp_path):
    (tmp_path / ".swarm" / "latest_correlation").write_text("C-latest\n")
    assert log()["correlation_id"] == "C-latest"
    assert log(task_id="T-be")["correlation_id"] == "C1"
    assert log(task_id="T-be", correlation_id="C-explicit")["correlation_id"] == "C-explicit"
    assert log(task_id="T-unknown")["correlation_id"] == "C-latest"  # a log line never fails on an unknown task


@pytest.mark.parametrize("kw", [{"level": "loud"}, {"message": ""}, {"message": "x" * 501}, {"fields": {f"f{i}": i for i in range(21)}},
                                {"fields": {"nested": {"a": 1}}}, {"fields": {"list": [1]}}, {"fields": []}, {"task_id": "../x"}])
def test_log_emit_validates_input(kw):
    out = log(**kw)
    assert out["state"] == "INVALID_INPUT" and out["field"] in kw and events("agent.log") == []


def test_log_emit_accepts_the_limits():
    assert log(message="x" * 500, fields={f"f{i}": i for i in range(20)})["state"] == "SUCCESS"


def test_log_emit_redacts_message_and_fields_and_requires_a_caller():
    log(message=f"token {SECRET}", fields={"api_token": "plain-value", "note": "Bearer abcdefghijklmnopqrstuv0123", "n": 1})
    [event] = events("agent.log")
    payload = json.dumps(event)
    assert SECRET not in payload and "plain-value" not in payload and "abcdefghijklmnopqrstuv0123" not in payload
    assert event["payload"]["fields"]["n"] == 1
    assert log(caller=None)["state"] == "UNAUTHORIZED"


# ---- shared_route_check ------------------------------------------------------------------------------------------
def test_route_check_agrees_with_acp_message_allowed_for_allowed_and_denied_pairs():
    data = acp.load_agents()
    table = acp.routing_table(data)
    seen = {True: 0, False: 0}
    for sender in ("A01", "A05", "A08", "A14"):
        types = sorted(set(table[sender]["produces"]) | {"no.such.type"})
        for recipient in table:
            for message_type in types:
                got = call(sr.route_check, "A02", {"sender": sender, "recipient": recipient, "message_type": message_type})
                expected = acp.message_allowed(data, sender, recipient, message_type)
                assert got["state"] == "SUCCESS" and got["allowed"] is expected
                assert (got["reason"] == "") is expected  # a denial always says why
                seen[expected] += 1
    assert seen[True] > 0 and seen[False] > 0


def test_route_check_explains_denials():
    table = acp.routing_table()
    produced = sorted(table["A01"]["produces"])[0]
    cases = [(("A99", "A01", produced), "unknown sender"), (("A01", "A99", produced), "unknown recipient"),
             (("A01", "A01", produced), "itself"), (("A01", "A02", "no.such.type"), "does not produce")]
    for (sender, recipient, message_type), word in cases:
        got = call(sr.route_check, "A03", {"sender": sender, "recipient": recipient, "message_type": message_type})
        assert got["allowed"] is False and word in got["reason"]
    unaccepted = next(t for t in table["A01"]["produces"] if t not in table["A02"]["accepts"])
    assert "does not accept" in call(sr.route_check, "A03", {"sender": "A01", "recipient": "A02", "message_type": unaccepted})["reason"]


def test_route_check_reads_the_live_registry_file(tmp_path, monkeypatch):
    # acp.load_agents imports swarm.tool_registry lazily: patch the module object it will actually see (conftest's
    # swarm_dir fixture evicts swarm* modules, so the module-level `tool_registry` may be a stale instance).
    live_registry = importlib.import_module("swarm.tool_registry")
    data = json.loads((ROOT / "agents.json").read_text())
    for agent in data["agents"]:
        if agent["id"] == "A02":
            agent["consumes"] = ["probe.only"]
        if agent["id"] == "A01":
            agent["produces"] = ["probe.only"]
    changed = tmp_path / "agents.json"
    changed.write_text(json.dumps(data))
    monkeypatch.setattr(live_registry, "AGENTS_FILE", changed)
    assert call(sr.route_check, "A09", {"sender": "A01", "recipient": "A02", "message_type": "probe.only"})["allowed"] is True


@pytest.mark.parametrize("field", ["sender", "recipient", "message_type"])
def test_route_check_validates_input(field):
    payload = {"sender": "A01", "recipient": "A02", "message_type": "x", field: " "}
    assert call(sr.route_check, "A01", payload)["field"] == field


# ---- registry contract -------------------------------------------------------------------------------------------
@pytest.fixture()
def registry():
    """agents.json (the eight runtime tools are merged into the registry)."""
    return _live_registry().load_document()


def _live_registry():
    """The swarm.tool_registry module that `call_tool` handlers resolve against right now.

    conftest's swarm_dir fixture purges every swarm* module between tests, so the module-level `tool_registry` import can
    bind a different `swarm.tools.support` ContextVar than the handlers loaded later; resolve it lazily instead."""
    return importlib.import_module("swarm.tool_registry")


def test_registry_entries_satisfy_the_contract(registry):
    live = _live_registry()
    agent_ids = {a["id"] for a in registry["agents"]}
    for name in RUNTIME_TOOLS:
        tool = live.find_tool(registry, name)
        assert tool is not None, name
        errors, ok = live._check_tool(tool, "shared", agent_ids, ROOT)
        assert errors == [] and ok, errors
        assert sorted(tool["permitted_callers"]) == sorted(agent_ids) and len(agent_ids) == 30
        expected_tier = "WRITE" if name in ("shared_status_report", "shared_scratchpad_put", "shared_escalate", "shared_log_emit") else "READ"
        assert live.tool_tier(tool) == expected_tier


def test_registry_call_path_round_trip_and_authorization(registry):
    call_tool = _live_registry().call_tool
    put_out = call_tool("shared_scratchpad_put", "A05", {"correlation_id": "C9", "key": "k", "value": {"a": 1}}, data=registry)
    assert put_out["state"] == "SUCCESS" and put_out["owner"] == "A05"  # the registry binds the caller for the handler
    got = call_tool("shared_scratchpad_get", "A12", {"correlation_id": "C9"}, data=registry)
    assert got["state"] == "SUCCESS" and [k["key"] for k in got["keys"]] == ["k"]
    assert call_tool("shared_scratchpad_get", "ghost-agent", {"correlation_id": "C9"}, data=registry)["state"] == "UNAUTHORIZED"
    bad = call_tool("shared_scratchpad_put", "A05", {"correlation_id": "C9", "key": "k"}, data=registry)
    assert bad["state"] == "INVALID_INPUT" and bad["field"] == "value"
    denied = call_tool("shared_scratchpad_put", "A07", {"correlation_id": "C9", "key": "k", "value": {}}, data=registry)
    assert denied["state"] == "UNAUTHORIZED" and denied["target"] == "k"


def test_registry_tools_return_outputs_that_match_their_schemas(registry, store):
    call_tool = _live_registry().call_tool
    cases = [("shared_status_report", "A05", {"task_id": "T-be", "kind": "done", "step": "finished"}),
             ("shared_escalate", "A05", {"task_id": "T-be", "reason_code": "budget", "summary": "out of budget", "needs": ["more"]}),
             ("shared_cancel_check", "A12", {"task_id": "T-be"}),
             ("shared_log_emit", "A05", {"level": "warn", "message": "m", "fields": {"a": 1}}),
             ("shared_route_check", "A05", {"sender": "A01", "recipient": "A02", "message_type": "x"}),
             ("shared_escalation_owner", "A05", {"task_id": "T-be"}),
             ("shared_scratchpad_get", "A05", {"correlation_id": "C1", "key": "absent"})]
    for name, caller, payload in cases:
        out = call_tool(name, caller, payload, data=registry)
        assert out["state"] == "SUCCESS", (name, out)  # _finish validates SUCCESS payloads against output_schema


def test_cancel_check_finds_the_flag_the_runner_names_for_an_unusual_correlation_id(tmp_path, clock):
    """RV-B #7: scripts/swarm_run.py writes `cancel/<re.sub('[^A-Za-z0-9._-]', '_', corr)>` while the reader opened the raw
    correlation id, so a correlation id such as 'run/odd id' was never seen as cancelled. Both sides use `cancel_flag_path`."""
    s = TaskStore(tmp_path / ".swarm" / "tasks.db")
    s.create(task_id="T-odd", correlation_id="run/odd id", capability="code.backend", agent_id="A05", title="odd")
    runner_path = swarm_dir() / "cancel" / "run_odd_id"  # the runner's spelling, written out literally
    runner_path.parent.mkdir(parents=True, exist_ok=True)
    runner_path.write_text(json.dumps({"requested_wall": clock.wall(), "reason": "operator stop", "grace_s": 7}))
    out = cancel("T-odd")
    assert (out["cancel_requested"], out["reason"], out["grace_remaining_s"]) == (True, "operator stop", 7.0)
    assert sr.cancel_flag_path(swarm_dir(), "run/odd id") == runner_path


def test_cancel_flag_path_never_names_a_directory(tmp_path):
    """A correlation id made only of dots would resolve to `cancel/..` (the swarm dir itself): it is spelled with underscores."""
    base = tmp_path / ".swarm"
    assert sr.cancel_flag_path(base, "C1") == base / "cancel" / "C1"
    assert sr.cancel_flag_path(base, "..") == base / "cancel" / "__"
    assert sr.cancel_flag_path(base, ".") == base / "cancel" / "_"
    assert sr.cancel_flag_path(base, "") == base / "cancel" / "_"
    assert sr.cancel_flag_path(base, "../x") == base / "cancel" / ".._x"
