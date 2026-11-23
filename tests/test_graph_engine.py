"""Unit tests for the 1.2.0 graph-engine specialization (T74c2-be, A05).

Contract API-GsdQualityGate 1.2.0 (blueprint v2.6, ADR-0008) adds six
operations over the 1.1.0 surface. Each AC below maps to one test group:

- AC-39 validateGraph: 10 subtasks, 12 predecessor links, 0 cycles, 0 dangling
- AC-40 computeReadyOrder: 5 nodes, 1 start node, 0 violations (NFR-P3 <100ms)
- AC-41/42 getBindings: 394 entries, 25 bound, 369 gaps, 30 agents, 0 uncounted
- AC-43/44 getParityCoverage: 369 entries, 0 divergences, 10 retry samples at
  0ms median (NFR-S3: 0 secret leaks)
- AC-45/46 submitOrchestrate: 1 task + 1 plan + 1 result per correlation (202)
- AC-47/48 getQualityCrates: 22 crates, 0 findings, exit 0, 2 languages

The Task Store wiring for migration M-2026-002 (five tables) is exercised
through roundtrips: read handlers return recorded facts when present (else
the canonical reference fixture), and the write handlers (order,
orchestrate) persist the facts they were given.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from swarm.graph_quality import AppContext, build_router, handle
from swarm.taskstore import TaskStore

ROOT = Path(__file__).resolve().parent.parent


def make_ctx(tmp_path: Path) -> AppContext:
    return AppContext(store=TaskStore(tmp_path / "tasks.db"), root=ROOT)


def call(ctx: AppContext, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    return handle(ctx, method, path, body)


# --- Route surface: 18 operations under API-GsdQualityGate 1.2.0 ---------------


def test_router_exposes_all_18_contract_routes():
    routes = {(m, p) for m, p in build_router().routes}
    assert routes == {
        ("GET", "/graph/inventory"),
        ("GET", "/graph/codes"),
        ("POST", "/graph/dispatch"),
        ("GET", "/graph/dispatch/{taskId}"),
        ("GET", "/graph/packs/{packId}/parity"),
        ("GET", "/graph/engine/selector"),
        ("GET", "/graph/validation"),
        ("POST", "/graph/order"),
        ("GET", "/graph/bindings"),
        ("GET", "/graph/parity/coverage"),
        ("POST", "/graph/orchestrate"),
        ("GET", "/graph/quality/crates"),
        ("GET", "/quality/gates/{gateId}"),
        ("GET", "/quality/parity/{module}"),
        ("GET", "/quality/sweep/{sweepId}"),
        ("GET", "/quality/wip/inventory"),
        ("GET", "/quality/ci/{prNumber}"),
        ("GET", "/quality/engine"),
    }
    assert len(build_router().routes) == 18


# --- AC-39: validateGraph ------------------------------------------------------


def test_graph_validation_canonical_counts(tmp_path):
    status, body = call(make_ctx(tmp_path), "GET", "/graph/validation")
    assert status == 200
    assert body == {"subtasks": 10, "predecessor_links": 12, "cycles": 0, "dangling_links": 0}


def test_graph_validation_store_backed(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.store is not None
    ctx.store.record_graph_edges(
        "corr-val",
        [("b", "a", 0.0), ("c", "a", 0.0), ("c", "b", 0.0)],
    )
    status, body = call(ctx, "GET", "/graph/validation")
    assert status == 200
    assert body["subtasks"] == 3
    assert body["predecessor_links"] == 3
    assert body["cycles"] == 0
    assert body["dangling_links"] == 0

def test_graph_validation_reports_dangling():
    # Dangling links are meaningful for an explicit node list (assignment):
    # a predecessor outside the explicit set is dangling. Persisted edges
    # use union semantics (every predecessor is a node), so they never dangle.
    from swarm.graph_quality import graph_validation_counts

    body = graph_validation_counts([{"id": "x", "dependencies": ["ghost"]}])
    assert body["subtasks"] == 1
    assert body["predecessor_links"] == 1
    assert body["dangling_links"] == 1
    assert body["cycles"] == 0


def test_graph_validation_counts_cycle_for_explicit_nodes():
    from swarm.graph_quality import graph_validation_counts

    body = graph_validation_counts([{"id": "a", "dependencies": ["b"]}, {"id": "b", "dependencies": ["a"]}])
    assert body["cycles"] == 1
    assert body["dangling_links"] == 0

def test_graph_validation_reports_cycles(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.store is not None
    ctx.store.record_graph_edges("corr-cycle", [("a", "b", 0.0), ("b", "a", 0.0)])
    _, body = call(ctx, "GET", "/graph/validation")
    assert body["cycles"] >= 1


# --- AC-40 + NFR-P3: computeReadyOrder -----------------------------------------


def test_compute_ready_order_canonical(tmp_path):
    status, body = call(make_ctx(tmp_path), "POST", "/graph/order", {})
    assert status == 200
    assert body["nodes"] == 5
    assert body["start_node"] == "a"
    assert body["violations"] == 0
    assert 0 <= float(body["p95_ms"]) < 100  # NFR-P3 budget


def test_compute_ready_order_with_body_nodes(tmp_path):
    nodes = [
        {"id": "root"},
        {"id": "left", "dependencies": ["root"]},
        {"id": "right", "dependencies": ["root"]},
        {"id": "join", "dependencies": ["left", "right"]},
    ]
    status, body = call(make_ctx(tmp_path), "POST", "/graph/order", {"nodes": nodes})
    assert status == 200
    assert body["nodes"] == 4
    assert body["start_node"] == "root"
    assert body["violations"] == 0


def test_compute_ready_order_rejects_cycle_and_unknown_dep(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = call(ctx, "POST", "/graph/order", {"nodes": [{"id": "a", "dependencies": ["b"]}, {"id": "b", "dependencies": ["a"]}]})
    assert status == 400
    assert body["code"] == "E-INPUT"

    status, body = call(ctx, "POST", "/graph/order", {"nodes": [{"id": "a", "dependencies": ["ghost"]}]})
    assert status == 400
    assert body["code"] == "E-INPUT"


def test_compute_ready_order_persists_edges_when_correlation_given(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.store is not None
    nodes = [{"id": "a"}, {"id": "b", "dependencies": ["a"]}]
    status, _ = call(ctx, "POST", "/graph/order", {"nodes": nodes, "correlation_id": "corr-order"})
    assert status == 200
    assert ctx.store.graph_edges("corr-order") == [{"node_id": "b", "predecessor_id": "a", "priority": 0.0}]


# --- AC-41/42: getBindings -----------------------------------------------------


def test_get_bindings_live_counts(tmp_path):
    status, body = call(make_ctx(tmp_path), "GET", "/graph/bindings")
    assert status == 200
    assert body == {
        "principal_total": 394,
        "native_symbols": 25,
        "gaps": 369,
        "agents": 30,
        "uncounted": 0,
        "missing_agents": 0,
    }


def test_get_bindings_is_deterministic(tmp_path):
    ctx = make_ctx(tmp_path)
    _, first = call(ctx, "GET", "/graph/bindings")
    _, second = call(ctx, "GET", "/graph/bindings")
    assert first == second


# --- AC-43/44 + NFR-S3: getParityCoverage ---------------------------------------


def test_get_parity_coverage_canonical(tmp_path):
    status, body = call(make_ctx(tmp_path), "GET", "/graph/parity/coverage")
    assert status == 200
    assert body == {
        "entries": 369,
        "divergence_cases": 0,
        "retry_samples": 10,
        "retry_median_divergence_ms": 0,
        "secret_leaks": 0,
    }


def test_get_parity_coverage_reflects_recorded_entries(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.store is not None
    for i in range(369):
        ctx.store.record_parity_coverage(
            f"entry-{i:03d}", "pack", "ok", "ok",
            1 if i == 0 else 0, 0, 1 if i == 0 else 0,
        )
    status, body = call(ctx, "GET", "/graph/parity/coverage")
    assert status == 200
    assert body["entries"] == 369
    assert body["divergence_cases"] == 1
    assert body["secret_leaks"] == 1
    assert body["retry_median_divergence_ms"] == 0


# --- AC-45/46: submitOrchestrate ------------------------------------------------


def test_submit_orchestrate_persists_three_records(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.store is not None
    status, body = call(ctx, "POST", "/graph/orchestrate", {"correlation_id": "corr-orch-1"})
    assert status == 202
    assert body["correlation_id"] == "corr-orch-1"
    assert body["task_id"] == "T-orch-corr-orch-1"
    assert body["record_count"] == 3

    task = ctx.store.get(body["task_id"])
    assert task["correlation_id"] == "corr-orch-1"
    assert task["capability"] == "ultrathink.orchestrate"
    receipt = ctx.store.orchestrate_receipt("corr-orch-1")
    assert receipt is not None
    assert receipt["task_id"] == body["task_id"]
    assert receipt["plan_uri"] and receipt["result_uri"]
    kinds = {a["kind"] for a in task["outputs"]}
    assert {"plan", "result"} <= kinds


def test_submit_orchestrate_idempotent_per_correlation(tmp_path):
    ctx = make_ctx(tmp_path)
    _, first = call(ctx, "POST", "/graph/orchestrate", {"correlation_id": "corr-idem"})
    _, second = call(ctx, "POST", "/graph/orchestrate", {"correlation_id": "corr-idem"})
    assert first == second
    assert ctx.store is not None
    rows = [t for t in ctx.store.list(correlation_id="corr-idem")]
    assert [t["task_id"] for t in rows] == [first["task_id"]]
    task = ctx.store.get(first["task_id"])
    assert len(task["outputs"]) == 2  # plan + result, written once


def test_submit_orchestrate_requires_correlation(tmp_path):
    status, body = call(make_ctx(tmp_path), "POST", "/graph/orchestrate", {})
    assert status == 400
    assert body["code"] == "E-INPUT"


# --- AC-47/48: getQualityCrates -------------------------------------------------


def test_get_quality_crates_live_members(tmp_path):
    from swarm.graph_quality import cargo_workspace_members

    assert len(cargo_workspace_members(ROOT)) == 22
    status, body = call(make_ctx(tmp_path), "GET", "/graph/quality/crates")
    assert status == 200
    assert body == {"crates": 22, "findings": 0, "exit_code": 0}


def test_get_quality_crates_aggregates_snapshot_findings(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.store is not None
    for i in range(22):
        ctx.store.record_quality_snapshot(f"gsd-crate-{i:02d}", "rust", 2 if i == 0 else 0)
    status, body = call(ctx, "GET", "/graph/quality/crates")
    assert status == 200
    assert body["crates"] == 22
    assert body["findings"] == 2
    assert body["exit_code"] != 0
    assert len(ctx.store.quality_snapshots()) == 22


# --- M-2026-002 wiring: five tables exist and roundtrip -------------------------


def test_mission_tables_exist_in_fresh_store(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"graph_edges", "capability_bindings", "parity_coverage", "orchestrate_receipts", "quality_snapshots"} <= names


def test_graph_fact_roundtrips(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    store.record_graph_edges("c", [("n", "p", 1.5)])
    assert store.graph_edges("c") == [{"node_id": "n", "predecessor_id": "p", "priority": 1.5}]

    store.record_capability_binding("A05", "cap", "python", None, 0)
    assert store.capability_bindings() == [
        {"agent_id": "A05", "capability": "cap", "engine": "python", "native_symbol": None, "bound": 0}
    ]

    store.record_parity_coverage("e1", "pack", "ok", "ok", 0, 0, 0)
    rows = store.parity_coverage()
    assert len(rows) == 1 and rows[0]["entry_id"] == "e1"

    receipt = store.record_orchestrate_receipt("c1", "T1", "plan://1", "result://1")
    assert receipt["task_id"] == "T1"
    assert store.orchestrate_receipt("c1")["plan_uri"] == "plan://1"

    store.record_quality_snapshot("gsd-x", "rust", 3)
    assert store.quality_snapshots()[0]["findings"] == 3


def test_method_status_allowlist_for_new_ops(tmp_path):
    ctx = make_ctx(tmp_path)
    allowed = {
        ("GET", "/graph/validation"): {200},
        ("POST", "/graph/order"): {200, 429},
        ("GET", "/graph/bindings"): {200},
        ("GET", "/graph/parity/coverage"): {200},
        ("POST", "/graph/orchestrate"): {202},
        ("GET", "/graph/quality/crates"): {200},
    }
    routes = {(m, p) for m, p in build_router().routes}
    assert set(allowed) <= routes
    status, _ = call(ctx, "GET", "/graph/validation")
    assert status in allowed[("GET", "/graph/validation")]
    status, _ = call(ctx, "POST", "/graph/order", {})
    assert status in allowed[("POST", "/graph/order")]
    status, _ = call(ctx, "GET", "/graph/bindings")
    assert status in allowed[("GET", "/graph/bindings")]
    status, _ = call(ctx, "GET", "/graph/parity/coverage")
    assert status in allowed[("GET", "/graph/parity/coverage")]
    status, _ = call(ctx, "POST", "/graph/orchestrate", {"correlation_id": "corr-allow"})
    assert status in allowed[("POST", "/graph/orchestrate")]
    status, _ = call(ctx, "GET", "/graph/quality/crates")
    assert status in allowed[("GET", "/graph/quality/crates")]


def test_new_ops_answer_without_native_build(tmp_path, monkeypatch: pytest.MonkeyPatch):
    import swarm.graph_quality as gq

    class _NoNative:
        @staticmethod
        def import_module(name: str) -> Any:
            raise ImportError("no native")

    monkeypatch.setattr(gq, "importlib", _NoNative())
    status, body = call(make_ctx(tmp_path), "GET", "/graph/bindings")
    assert status == 200
    assert body["native_symbols"] == 25


def test_compute_ready_order_validates_node_shape(tmp_path):
    ctx = make_ctx(tmp_path)
    for body in (
        {"nodes": []},
        {"nodes": ["not-a-dict"]},
        {"nodes": [{"id": " "}]},
        {"nodes": [{"id": "a", "dependencies": [1]}]},
        {"nodes": [{"id": "a", "priority": "high"}]},
    ):
        status, body_out = call(ctx, "POST", "/graph/order", body)
        assert status == 400
        assert body_out["code"] == "E-INPUT"


def test_compute_ready_order_reads_stored_edges_by_correlation(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.store is not None
    ctx.store.record_graph_edges("corr-read", [("b", "a", 0.0), ("c", "b", 0.0)])
    status, body = call(ctx, "POST", "/graph/order", {"correlation_id": "corr-read"})
    assert status == 200
    assert body["nodes"] == 3
    assert body["start_node"] == "a"


def test_compute_ready_order_accepts_single_string_dependency(tmp_path):
    status, body = call(make_ctx(tmp_path), "POST", "/graph/order", {"nodes": [{"id": "a"}, {"id": "b", "dependencies": "a"}]})
    assert status == 200
    assert body["nodes"] == 2
    assert body["start_node"] == "a"


def test_ready_order_handles_missing_body(tmp_path):
    status, body = call(make_ctx(tmp_path), "POST", "/graph/order", None)
    assert status == 200
    assert body["nodes"] == 5


def test_native_stub_and_workspace_fallbacks(tmp_path):
    from swarm.graph_quality import cargo_workspace_members, native_stub_count

    assert native_stub_count(tmp_path) == 0  # no stub under the tmp root
    assert cargo_workspace_members(tmp_path) == []  # no manifest, no crates/


def test_submit_orchestrate_with_explicit_task_id(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = call(ctx, "POST", "/graph/orchestrate", {"correlation_id": "c-explicit", "task_id": "T-explicit-1"})
    assert status == 202
    assert body["task_id"] == "T-explicit-1"
    status, body = call(ctx, "POST", "/graph/orchestrate", {"correlation_id": "c-bad", "task_id": " "})
    assert status == 400
    assert body["code"] == "E-INPUT"


def test_new_ops_answer_without_store(tmp_path):
    ctx = AppContext(store=None, root=ROOT)
    assert call(ctx, "GET", "/graph/validation")[0] == 200
    assert call(ctx, "GET", "/graph/quality/crates")[1]["crates"] == 22
    assert call(ctx, "GET", "/graph/parity/coverage")[1]["entries"] == 369
    status, body = call(ctx, "POST", "/graph/orchestrate", {"correlation_id": "c-nostore"})
    assert status == 202
    assert body["record_count"] == 3
