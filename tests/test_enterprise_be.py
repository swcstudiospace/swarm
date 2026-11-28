"""A05 enterprise backend tests (T5059-be, API-GsdQualityGate 1.5.0).

Test-first: one test per acceptance criterion AC-73..AC-84 (see
docs/acceptance.md), plus validation/persistence checks. Each test names
its AC id in its docstring so coverage is auditable.
"""
from __future__ import annotations

from pathlib import Path

from swarm.graph_quality import AppContext, handle
from swarm.taskstore import TaskStore


def _ctx(tmp_path: Path) -> AppContext:
    store = TaskStore(tmp_path / "tasks.db")
    return AppContext(store=store, root=tmp_path)


# AC-73: 1 codebase -> 1 catalog entry with 1 owner and 1 review record
def test_ac73_pattern_catalog_one_entry(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/patterns/catalog", None)
        assert status == 200
        assert body["codebase"] == "agent-swarm"
        assert body["catalog_entries"] == 1
        assert body["owners"] == 1
        assert body["review_records"] == 1
    finally:
        ctx.store.close()


# AC-74: 1 catalog entry with 1 owner -> 0 high-severity violations across 1 scan
def test_ac74_pattern_catalog_zero_violations(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/patterns/catalog", None)
        assert status == 200
        assert body["high_severity_violations"] == 0
    finally:
        ctx.store.close()


# AC-75: 12 outstanding GSD phases -> 12 Linear issues with 0 duplicate titles
def test_ac75_gsd_mirrors_twelve_issues(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/gsd-phases", None)
        assert status == 200
        assert body["phases_total"] == 12
        assert body["linear_issues"] == 12
        assert body["notion_pages"] == 12
        assert body["duplicate_titles"] == 0
    finally:
        ctx.store.close()


# AC-76: 12 Linear issues with 1 Notion page each -> 12 sub-issues, 0 orphaned
def test_ac76_gsd_subissues_twelve_no_orphans(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/trackers/gsd-phases", None)
        assert status == 200
        assert body["sub_issues"] == 12
        assert body["orphaned_sub_issues"] == 0
    finally:
        ctx.store.close()


# AC-77: 1 graph with 10 nodes and 12 predecessor links -> 1 start, 0 violations
def test_ac77_graph_status_ten_nodes_one_start(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/graph/status", None)
        assert status == 200
        assert body["nodes"] == 10
        assert body["predecessor_links"] == 12
        assert body["start_node"] == "start"
        assert body["violations"] == 0
        assert body["stalled_nodes"] == 0
    finally:
        ctx.store.close()


# AC-78: 1 running assignment with 30 workers -> 0 stalled unreported, 30 active
def test_ac78_graph_status_thirty_workers_none_hidden(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/graph/status", None)
        assert status == 200
        assert body["stalled_unreported"] == 0
        assert body["active_workers"] == 30
        assert body["worker_cap"] == 30
        assert body["owners"] == 30
        assert 0 <= body["p95_ms"] < 100
    finally:
        ctx.store.close()


# AC-79: 30 agents with 1 shared shelf -> 1 catalog row, 0 duplicate rows
def test_ac79_shelf_search_one_row(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/shelf/search", {"capability": "graph.engine"})
        assert status == 200
        assert body["capability"] == "graph.engine"
        assert body["catalog_rows"] == 1
        assert body["duplicate_rows"] == 0
        assert body["agents"] == 30
    finally:
        ctx.store.close()


# AC-80: 1 capability with 1 approved tool -> 1 reuse record, 0 duplicate requests
def test_ac80_shelf_reuse_one_record(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/shelf/search", {"capability": "graph.engine"})
        assert status == 200
        assert body["reuse_records"] == 1
        assert body["duplicate_requests"] == 0
    finally:
        ctx.store.close()


# AC-81: 10 session records with 0 matching tools -> 10 proposals, 0 uncounted gaps
def test_ac81_proposals_ten_no_uncounted(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/creator/proposals", None)
        assert status == 200
        assert body["sessions_checked"] == 10
        assert body["proposals"] == 10
        assert body["uncounted_gaps"] == 0
    finally:
        ctx.store.close()


# AC-82: 1 proposal with 1 session link -> 1 proposal points to 1 session, 0 dangling
def test_ac82_proposals_link_no_dangling(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/creator/proposals", None)
        assert status == 200
        assert body["dangling_links"] == 0
    finally:
        ctx.store.close()


# AC-83: 1 proposed tool with 1 high-severity finding -> promotion refused, 1 refusal, 0 promotions
def test_ac83_promotion_refused_with_findings(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        ctx.store.record_promotion_gate("prop-83", 1, 0, 1, 0, 0, 0, 0)
        status, body = handle(ctx, "GET", "/creator/promotions/prop-83/gate", None)
        assert status == 200
        assert body["proposal_id"] == "prop-83"
        assert body["high_severity_findings"] == 1
        assert body["promotions"] == 0
        assert body["refusal_records"] == 1
    finally:
        ctx.store.close()


# AC-84: 1 proposed tool with 0 high-severity findings -> 1 approval record, 1 test record
def test_ac84_promotion_approved_with_test_record(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        ctx.store.record_promotion_gate("prop-84", 0, 0, 0, 1, 1, 0, 0)
        status, body = handle(ctx, "GET", "/creator/promotions/prop-84/gate", None)
        assert status == 200
        assert body["high_severity_findings"] == 0
        assert body["approval_records"] == 1
        assert body["test_records"] == 1
        assert body["credential_findings"] == 0
        assert body["secret_findings"] == 0
    finally:
        ctx.store.close()


# Validation: shelf search with a missing/blank capability is refused 400 (fail-closed)
def test_shelf_search_validation(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/shelf/search", {"capability": ""})
        assert status == 400
        assert body["code"] == "E-INPUT"
        status, _ = handle(ctx, "GET", "/shelf/search", None)
        assert status == 400
    finally:
        ctx.store.close()


# Validation: unknown promotion proposal answers 404 (never a fabricated record)
def test_promotion_unknown_proposal_404(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        status, body = handle(ctx, "GET", "/creator/promotions/ghost/gate", None)
        assert status == 404
        assert body["code"] == "E-INPUT"
    finally:
        ctx.store.close()


# Persistence: enterprise facts written by the store are readable back from the store
def test_enterprise_persistence_roundtrip(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        ctx.store.record_pattern_catalog("agent-swarm", 1, 1, 1, 0)
        assert ctx.store.pattern_catalogs()[0]["codebase"] == "agent-swarm"
        ctx.store.record_gsd_phase_mirrors(12, 12, 12, 0, 12, 0)
        assert ctx.store.gsd_phase_mirrors()[0]["linear_issues"] == 12
        ctx.store.record_graph_status(10, 12, "start", 0, 0, 0, 30, 30, 30, 1.5)
        assert ctx.store.graph_statuses()[0]["nodes"] == 10
        ctx.store.record_tool_shelf_row("cap-p", 1, 0, 1, 0, 30)
        status, body = handle(ctx, "GET", "/shelf/search", {"capability": "cap-p"})
        assert status == 200
        assert body["capability"] == "cap-p"
        ctx.store.record_improvement_proposals(10, 10, 0, 0)
        assert ctx.store.improvement_proposals()[0]["proposals"] == 10
        gate = ctx.store.promotion_gate("prop-83")
        assert gate is None
        ctx.store.record_promotion_gate("prop-83", 1, 0, 1, 0, 0, 0, 0)
        assert ctx.store.promotion_gate("prop-83")["refusal_records"] == 1
    finally:
        ctx.store.close()


# Persistence: recorded enterprise rows override the canonical fixture
def test_enterprise_recorded_rows_override_fixture(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    try:
        ctx.store.record_gsd_phase_mirrors(12, 12, 12, 1, 12, 0)
        status, body = handle(ctx, "GET", "/trackers/gsd-phases", None)
        assert status == 200
        assert body["duplicate_titles"] == 1
        ctx.store.record_improvement_proposals(4, 4, 0, 0)
        status, body = handle(ctx, "GET", "/creator/proposals", None)
        assert status == 200
        assert body["sessions_checked"] == 4
        assert body["proposals"] == 4
    finally:
        ctx.store.close()
