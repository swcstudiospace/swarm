"""Regression tests for the observability projection (D2) and the error-aggregate write result (D14)."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from swarm import observability
from swarm.observability import build_swarm_state, emit_error_agg
from swarm.runlog import emit, read_events
from swarm.taskstore import TaskStore

CORR = "obs-review-corr"
SID = "subagent-t1-0123456789ab-1700000000000"


def _seed(root: Path, states: list[str]) -> None:
    """Create the Task Store under root with one row per state, all in correlation CORR."""
    ts = TaskStore(root=root)
    try:
        for i, state in enumerate(states):
            ts.create(task_id=f"t{i}", correlation_id=CORR, capability="cap")
            if state != "CREATED":
                ts.update(f"t{i}", state=state)
    finally:
        ts.conn.close()


def _unreadable(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
    raise sqlite3.OperationalError("unable to open database file")


def test_build_swarm_state_empty_store_reports_empty(tmp_path: Path) -> None:
    _seed(tmp_path, [])

    data = build_swarm_state(corr=CORR, root=tmp_path)

    assert data["swarm_state"] == "EMPTY"
    assert data["progress"] == {"done": 0, "total": 0, "active": 0}


def test_build_swarm_state_one_done_task_reports_done_and_completed(tmp_path: Path) -> None:
    _seed(tmp_path, ["DONE"])

    data = build_swarm_state(corr=CORR, root=tmp_path)

    assert data["swarm_state"] == "COMPLETED"
    assert data["progress"] == {"done": 1, "total": 1, "active": 0}


def test_build_swarm_state_unreadable_store_reports_error_not_event_state(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    # A done event in the log must not make an unreadable Task Store look COMPLETED.
    emit("task.done", {"task_id": "t0"}, source="test", correlation_id=CORR, root=tmp_path, tee=False)
    # Patch the class build_swarm_state imports at call time. The swarm_dir fixture reloads the
    # swarm modules, so the TaskStore bound at this file's import can be a stale class object.
    from swarm import taskstore as current_taskstore

    monkeypatch.setattr(current_taskstore.TaskStore, "list", _unreadable)

    data = build_swarm_state(corr=CORR, root=tmp_path)

    assert data["swarm_state"] == "ERROR"
    assert data["progress"] == {"done": 0, "total": 0, "active": 0}


def test_emit_error_agg_failed_write_reports_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    def _failing_emit(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise OSError("disk full")

    monkeypatch.setattr(observability, "emit", _failing_emit)
    agg = {"errors": 1}

    with caplog.at_level(logging.WARNING, logger="swarm.observability"):
        result = emit_error_agg(SID, agg, task_id="t1", corr=CORR, root=tmp_path, dry_run=True)

    assert result == {"subagent_id": SID, "error_agg": agg, "state": "ERROR", "written": False}
    assert any(r.levelno == logging.WARNING and r.name == "swarm.observability" for r in caplog.records)


def test_emit_error_agg_successful_write_reports_written(tmp_path: Path) -> None:
    result = emit_error_agg(SID, {"errors": 1}, task_id="t1", corr=CORR, root=tmp_path, dry_run=True)

    assert result["written"] is True
    assert result["type"] == "error.agg"
    records = read_events(root=tmp_path, event_type="error.agg")
    assert [r["msg_id"] for r in records] == [result["msg_id"]]


def test_emit_gsd_omp_reserved_keys_win(tmp_path: Path) -> None:
    # A caller extra named like a reserved payload key must not overwrite the builder's own value.
    result = observability.emit_gsd_omp("updated", subagent_id=SID, corr=CORR, root=tmp_path, dry_run=True, ts=1)

    assert result["payload"]["ts"] != 1
    assert abs(result["payload"]["ts"] - time.time()) < 60


def test_stream_subagent_status_reserved_keys_win(tmp_path: Path) -> None:
    result = observability.stream_subagent_status(SID, "RUNNING", corr=CORR, root=tmp_path, dry_run=True, ts=1)

    assert result["payload"]["ts"] != 1
    assert abs(result["payload"]["ts"] - time.time()) < 60
