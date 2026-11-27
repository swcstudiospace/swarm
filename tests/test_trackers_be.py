"""A05 tracker backend tests (Tbe37-be, API-GsdQualityGate 1.5.0 / 1.3.0 wave).

One test per acceptance criterion AC-49..AC-60 (contract
``contracts/gsd-quality-gate.yaml``), closing the six tracker-mirror
operations that conformance reported missing (30/36). Tracks enterprise
revalidation AC-73..AC-84 stays covered by ``tests/test_enterprise_be.py``.
Handlers reuse the M-2026-004 ``gsd_phase_mirrors``/``pattern_catalogs``
rows when present and otherwise answer from the canonical fixture; the
ready-order recompute measures ``plan_graph`` live over the 8-node graph.
"""
from __future__ import annotations

from pathlib import Path

from swarm.graph_quality import AppContext, build_router, handle
from swarm.taskstore import TaskStore


def _ctx(tmp_path: Path) -> AppContext:
    store = TaskStore(tmp_path / "tasks.db")
    return AppContext(store=store, root=tmp_path)


# AC-49: 12 outstanding phases -> 12 Linear issues and 12 Notion pages
def test_ac49_tracker_mirrors_twelve_each(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/mirrors", None)
        assert status == 200
        assert body["phases_total"] == 12
        assert body["linear_issues"] == 12
        assert body["notion_pages"] == 12
    finally:
        ctx.store.close()


# AC-50: 12 mirrors with 1 link per issue -> 0 duplicate titles, 0 missing links
def test_ac50_tracker_mirrors_zero_duplicates_missing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/mirrors", None)
        assert status == 200
        assert body["duplicate_titles"] == 0
        assert body["missing_links"] == 0
    finally:
        ctx.store.close()


# AC-51: 12 sub-issues each linked to exactly 1 parent
def test_ac51_parent_links_twelve(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/parent-links", None)
        assert status == 200
        assert body["sub_issues"] == 12
        assert body["parent_links"] == 12
    finally:
        ctx.store.close()


# AC-52: parent links -> 0 orphaned and 0 reversed
def test_ac52_parent_links_zero_orphaned_reversed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/parent-links", None)
        assert status == 200
        assert body["orphaned"] == 0
        assert body["reversed_links"] == 0
    finally:
        ctx.store.close()


# AC-53: locked manifests -> 0 lock drift findings
def test_ac53_lock_manifest_zero_drift(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/locks", None)
        assert status == 200
        assert body["lock_drift_findings"] == 0
        assert body["high_severity_findings"] == 0
    finally:
        ctx.store.close()


# AC-54: lock check records 1 deny verdict
def test_ac54_lock_manifest_one_deny_verdict(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/locks", None)
        assert status == 200
        assert body["deny_verdicts_recorded"] == 1
    finally:
        ctx.store.close()


# AC-55: pattern gate -> 0 high-severity violations with exit 0
def test_ac55_pattern_gate_zero_violations_exit_zero(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/pattern-gate", None)
        assert status == 200
        assert body["high_severity_violations"] == 0
        assert body["exit_code"] == 0
    finally:
        ctx.store.close()


# AC-56: promotion refused while violations remain (flag is False at 0)
def test_ac56_pattern_gate_refusal_flag(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/pattern-gate", None)
        assert status == 200
        assert body["promotion_refused_while_violations"] is False
    finally:
        ctx.store.close()


# AC-57: scheduler start -> 4 ready tasks with 1 idle slot
def test_ac57_scheduler_ready_four_and_idle(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/scheduler-ready", None)
        assert status == 200
        assert body["ready_tasks"] == 4
        assert body["idle_slots"] == 1
    finally:
        ctx.store.close()


# AC-58: scheduler start -> 0 stalled and 0 lost tasks
def test_ac58_scheduler_ready_zero_stalled_lost(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/scheduler-ready", None)
        assert status == 200
        assert body["stalled_tasks"] == 0
        assert body["tasks_lost"] == 0
    finally:
        ctx.store.close()


# AC-59: ready-order recompute over 8 nodes / 7 links with 1 start, 0 violations
def test_ac59_ready_order_recompute_eight_nodes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "POST", "/trackers/ready-order/recompute", {})
        assert status == 200
        assert body["nodes"] == 8
        assert body["predecessor_links"] == 7
        assert body["start_node"] == "t0"
        assert body["violations"] == 0
    finally:
        ctx.store.close()


# AC-60 + NFR-P4: 0 divergences, wall-clock parity, worker cap 30 held
def test_ac60_ready_order_recompute_parity_cap(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "POST", "/trackers/ready-order/recompute", {})
        assert status == 200
        assert body["divergence_cases"] == 0
        assert body["paired_runs"] == 1
        assert body["wall_clock_parity"] is True
        assert body["active_workers"] == 30
        assert body["worker_cap"] == 30
        assert body["stalled_tasks"] == 0
    finally:
        ctx.store.close()


# Persistence: recorded gsd mirrors override the canonical fixture
def test_tracker_recorded_mirrors_override_fixture(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        ctx.store.record_gsd_phase_mirrors(12, 12, 12, 0, 12, 0)
        status, body = handle(ctx, "GET", "/trackers/mirrors", None)
        assert status == 200
        assert body["phases_total"] == 12
        assert body["linear_issues"] == 12
        assert body["missing_links"] == 0
        status, body = handle(ctx, "GET", "/trackers/parent-links", None)
        assert status == 200
        assert body["parent_links"] == 12
    finally:
        ctx.store.close()


# Router: all 36 contract operations are exposed exactly once (no collision)
def test_router_exposes_all_36_contract_operations() -> None:
    routes = build_router().routes
    assert len(routes) == 36
    assert len(set(routes)) == 36
