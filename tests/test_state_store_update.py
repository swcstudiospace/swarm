"""Regression tests for SwarmStateStore.update: reserved state keys win over caller extras, and the dead pending-timeout stub is gone."""
from __future__ import annotations

import json
from pathlib import Path

from swarm.state_store import SwarmStateStore


def test_check_pending_timeout_removed() -> None:
    # The method was a no-op stub with no callers; clean cutover deletes it rather than keeping a shim.
    assert not hasattr(SwarmStateStore, "check_pending_timeout")


def test_extra_cannot_override_swarm_state(tmp_path: Path) -> None:
    store = SwarmStateStore(root=tmp_path)

    data = store.update(
        "RUNNING",
        corr="corr-real",
        progress={"done": 1, "total": 3, "active": 2},
        extra={
            "swarm_state": "COMPLETE",
            "correlation_id": "corr-forged",
            "updated_at": "1970-01-01T00:00:00Z",
            "progress": {"done": 9, "total": 9, "active": 0},
            "subagent_id": "sub-1",
        },
    )

    assert data["swarm_state"] == "RUNNING"
    assert data["correlation_id"] == "corr-real"
    assert data["updated_at"] != "1970-01-01T00:00:00Z"
    assert data["progress"] == {"done": 1, "total": 3, "active": 2}
    assert data["subagent_id"] == "sub-1"

    on_disk = json.loads(store.swarm_path.read_text())
    assert on_disk["swarm_state"] == "RUNNING"
    assert on_disk["correlation_id"] == "corr-real"
