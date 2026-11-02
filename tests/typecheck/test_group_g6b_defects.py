"""Regression test for a runtime defect fixed while typing the G6b modules.

``observability.build_swarm_state`` called ``TaskStore.list_tasks``, which does not exist; the method is
``TaskStore.list``. The ``AttributeError`` was swallowed by the surrounding ``except Exception``, so the swarm
state was always rebuilt from the event log and never from the Task Store rows.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from swarm import observability
from swarm.taskstore import TaskStore


def test_build_swarm_state_counts_done_tasks_from_task_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    store = TaskStore(tmp_path / ".swarm" / "tasks.db")
    store.create(task_id="T-g6b", correlation_id="C-g6b", capability="code.backend", agent_id="A05", title="g6b")
    store.update("T-g6b", state="DONE")

    data = observability.build_swarm_state(corr="C-g6b", root=tmp_path)

    assert data["progress"] == {"done": 1, "total": 1, "active": 0}


def test_explain_regression_check_refuses_empty_queries() -> None:
    from swarm.tools.a07_tools import data_explain_regression_check

    result = data_explain_regression_check({"queries": []})

    assert result["state"] == "INVALID_INPUT"
    assert result["field"] == "queries"
