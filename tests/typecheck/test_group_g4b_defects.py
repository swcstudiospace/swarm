"""Regression tests for runtime defects fixed while typing the G4b modules.

``SubagentLifecycleManager.stream_all_status`` passed a ``status=`` keyword through ``_drive_gsd_state`` into
``emit_gsd_omp``, whose first positional parameter is also named ``status``. Python raised ``TypeError: got multiple
values for argument 'status'``, which ``_drive_gsd_state`` swallowed, so the STREAMING record was never emitted.
"""
from __future__ import annotations

from pathlib import Path

from swarm import subagent_lifecycle as lifecycle
from swarm.runlog import read_events
from swarm.state_store import get_swarm_state_store


def test_register_omp_emits_status_without_keyword_collision(tmp_path: Path) -> None:
    mgr = lifecycle.SubagentLifecycleManager(2, root=tmp_path, dry_run=True)
    sid = mgr.new_subagent_id("g4b-task", root=tmp_path)

    summary = mgr.register_omp()

    assert summary["count"] == 1
    assert summary["active"] == [sid]


def test_stream_all_status_emits_consolidated_without_keyword_collision(tmp_path: Path) -> None:
    mgr = lifecycle.SubagentLifecycleManager(2, root=tmp_path, dry_run=True)
    sid = mgr.new_subagent_id("g4b-task", root=tmp_path)

    mgr.stream_all_status("active")

    streamed = [e["payload"] for e in read_events(root=tmp_path, event_type="gsd.autonomous.omp")
                if e["payload"].get("state") == "STREAMING"]
    assert len(streamed) == 1
    assert streamed[0]["omp_status"] == "active"
    assert get_swarm_state_store(root=tmp_path).get()["omp_status"] == "active"
    assert get_swarm_state_store(root=tmp_path).get()["swarm_state"] == "RUNNING"
    per_subagent = [e["payload"]["status"] for e in read_events(root=tmp_path, event_type="subagent.status.stream")
                    if e["payload"].get("subagent_id") == sid]
    assert "active" in per_subagent
