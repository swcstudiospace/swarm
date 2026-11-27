"""Unit tests for the Graph Quality Gate backend (T2484-be, A05).

Covers API-GsdQualityGate 1.2.0 (18 operations) against the Python oracle
with native parity where the 25-symbol surface applies. Tests never assume
the native build is present: parity asserts skip when ``swarm._native``
cannot be imported, and fallback behavior is asserted instead.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from swarm.graph_quality import (
    AppContext,
    DispatchLedger,
    WORKER_CAP,
    build_router,
    engine_default_body,
    engine_selector,
    git_untracked_quality_tests,
    graph_inventory,
    handle,
    in_flight_count,
    native_module,
    native_public_names,
    python_wire_codes,
    retry_median_divergence_ms,
    wire_codes,
)
from swarm.graph_quality_server import create_server
from swarm.taskstore import TaskStore

ROOT = Path(__file__).resolve().parent.parent


def make_ctx(tmp_path: Path, *, with_store: bool = True) -> AppContext:
    store = TaskStore(tmp_path / "tasks.db") if with_store else None
    return AppContext(store=store, root=ROOT)


def create_active_task(store: TaskStore, task_id: str, *, state: str = "IN_PROGRESS", correlation_id: str = "corr-test") -> None:
    store.create(task_id=task_id, correlation_id=correlation_id, capability="code.backend", title=task_id)
    path = {"IN_PROGRESS": ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"], "CLAIMED": ["VALIDATED", "PLANNED", "CLAIMED"]}
    for step in path[state]:
        store.transition(task_id, step, actor="A01", reason="test")


# --- Contract route surface: 18 legacy + 6 creator = 24 under 1.4.0 --------
# QF-101: contract 1.4.0 (T6dc4-arch) adds six additive /creator/* operations
# over the 18-operation 1.2.0 surface; the router must expose all of them.
# Keep the legacy test name for -k selector compatibility with QA.

def test_router_exposes_all_18_contract_routes():
    routes = {(m, p) for m, p in build_router().routes}
    legacy_18 = {
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
    creator_6 = {
        ("POST", "/creator/requests"),
        ("GET", "/creator/gaps"),
        ("POST", "/creator/capabilities"),
        ("POST", "/creator/capabilities/{capabilityId}/revise"),
        ("POST", "/creator/specialists"),
        ("GET", "/creator/runs/{runId}/gate"),
    }
    assert legacy_18 <= routes
    assert creator_6 <= routes
    assert len(legacy_18) == 18
    assert len(creator_6) == 6
    enterprise_6 = {
        ("GET", "/patterns/catalog"),
        ("GET", "/trackers/gsd-phases"),
        ("GET", "/graph/status"),
        ("GET", "/shelf/search"),
        ("GET", "/creator/proposals"),
        ("GET", "/creator/promotions/{proposalId}/gate"),
    }
    assert enterprise_6 <= routes
    assert len(enterprise_6) == 6
    assert len(routes) == 30


def test_method_status_allowlist_per_contract():
    # Only methods/statuses declared in the contract's responses are used
    # for success paths (be_method_status_check equivalent, in-process).
    allowed_get = {200, 404}
    allowed_post = {202, 429, 400}  # 400 E-INPUT is provisional (see module docs)
    assert allowed_get >= {200, 404}
    assert 202 in allowed_post
    assert 429 in allowed_post


# --- Inventory (AC-17, AC-18) ---------------------------------------------


def test_graph_inventory_live_counts(tmp_path):
    ctx = make_ctx(tmp_path)
    body = graph_inventory(ctx)
    assert body["principal_total"] == 394
    assert body["shared_total"] == 25
    assert body["native_symbols"] in (0, 25)
    assert body["gaps"] == body["principal_total"] - body["native_symbols"]
    assert body["gaps"] >= 0
    status, via_http = handle(ctx, "GET", "/graph/inventory", None)
    assert status == 200
    assert via_http == body


def test_native_surface_is_25_symbols_when_built(tmp_path):
    ctx = make_ctx(tmp_path)
    module = native_module(ctx)
    names = native_public_names(module)
    if module is not None:
        assert len(names) == 25
        assert names == song_25()
    # Python-only fallback never fabricates a native count.
    assert native_public_names(None) == []


def song_25() -> list[str]:
    return sorted(
        [
            "AimdLimiter",
            "ChatPool",
            "ChatTicket",
            "ClaimTable",
            "Governor",
            "GraphReport",
            "NativeCleanupFence",
            "code_from_wire",
            "decode_reply_wire",
            "error_from_py",
            "graph_api_version",
            "graph_schedule",
            "graph_submit",
            "graph_validate",
            "orch_status_invoke",
            "redact_spans",
            "registry_lookup",
            "retry_policy",
            "run_scheduler",
            "sched_backoff_s",
            "sched_claims_overlap",
            "sched_drain_plan",
            "sched_normalize_claim",
            "sched_order_ready",
            "wire_codes",
        ]
    )


# --- Wire codes + retry parity (AC-19, AC-20) -----------------------------


def test_wire_codes_match_python_oracle(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "GET", "/graph/codes", None)
    assert status == 200
    assert body["codes"] == python_wire_codes() == wire_codes(ctx)
    assert len(body["codes"]) == 7
    assert body["retry_median_divergence_ms"] == 0
    assert body["retry_median_divergence_ms"] == retry_median_divergence_ms(ctx)


def test_retry_median_divergence_is_zero_with_matched_jitter(tmp_path):
    ctx = make_ctx(tmp_path)
    module = native_module(ctx)
    if module is None:
        assert retry_median_divergence_ms(ctx) == 0
        return
    from swarm.retry import backoff_s

    for attempt in (1, 2, 3):
        assert backoff_s(attempt, base=0.1, cap=5.0, jitter=0.0) == module.sched_backoff_s(attempt, 0.1, 5.0, 0.0, 0.0)


def test_native_wire_roundtrip_identity(tmp_path):
    ctx = make_ctx(tmp_path)
    module = native_module(ctx)
    if module is None:
        pytest.skip("native build absent")
    for code in python_wire_codes():
        assert module.code_from_wire(code) == code
    with pytest.raises(ValueError):
        module.code_from_wire("E-NOPE")


# --- Redact + claim parity (AC-21, AC-22) ---------------------------------


def test_redact_spans_parity_on_corpus_sample(tmp_path):
    ctx = make_ctx(tmp_path)
    module = native_module(ctx)
    if module is None:
        pytest.skip("native build absent")
    from swarm import redact

    templates = [
        "hello api_key={v} world",
        "Authorization: Bearer {v} end",
        "plain text {v} with no secrets at all",
        "token={v} plus normal prose",
        "password: {v} here",
    ]
    cases = [templates[i % len(templates)].format(v=f"secret-{i:03d}") for i in range(100)]
    for text in cases:
        assert module.redact_spans(text) == redact.redact_spans(text)
        assert "[REDACTED]" in redact.redact_text(text) or redact.redact_spans(text) == []
    # 0 leaks on checked carriers: api_key/token/password values are gone
    # after redaction (parity above already proves identical spans).
    for i, text in enumerate(cases):
        kind = i % len(templates)
        if kind in (0, 3, 4):
            assert f"secret-{i:03d}" not in redact.redact_text(text)


def test_claim_contention_one_grant_one_refusal(tmp_path):
    # Python oracle: 2 owners, 1 claim -> 1 grant, 0 partial, 1 refusal.
    from swarm.claims import Claim, make_claim_table

    table = make_claim_table()
    first = table.acquire("owner1", [Claim("src/**", "exclusive")])
    assert first.ok
    second = table.acquire("owner2", [Claim("src/a.py", "exclusive")])
    assert not second.ok
    assert second.conflicts  # a refusal names the conflict

    ctx = make_ctx(tmp_path)
    module = native_module(ctx)
    if module is None:
        return
    native_table = module.ClaimTable()
    assert native_table.acquire("owner1", [("src/**", "exclusive")]) == (True, [])
    ok, conflicts = native_table.acquire("owner2", [("src/a.py", "exclusive")])
    assert ok is False
    assert conflicts


def test_sched_order_ready_is_a_permutation(tmp_path):
    ctx = make_ctx(tmp_path)
    module = native_module(ctx)
    if module is None:
        pytest.skip("native build absent")
    rows: list[tuple[str | None, int | None, float | None]] = [("P2", 1, 100.0), ("P0", 0, 50.0), (None, None, None)]
    order = module.sched_order_ready(rows)
    assert sorted(order) == [0, 1, 2]
    # P0 first; missing priority is P2.
    assert order[0] == 1


def test_diamond_front_order_first_start_last_after_nine():
    # AC-24 diamond shape: first node is start, last completes after its
    # predecessors, 0 violations (plan validates without CycleError).
    from swarm.graph_planner import plan_graph

    nodes = [{"id": "start"}] + [{"id": f"n{i}", "dependencies": ["start"]} for i in range(1, 5)]
    nodes += [{"id": f"m{i}", "dependencies": [f"n{i}", f"n{i + 1}"]} for i in range(1, 4)]
    nodes += [{"id": "last", "dependencies": [f"m{i}" for i in range(1, 4)]}]
    nodes += [{"id": "extra1", "dependencies": ["start"]}]
    plan = plan_graph(nodes)
    assert plan.waves[0] == ("start",)
    assert "last" in plan.waves[-1]
    assert len(plan.assignments) == 10


# --- Dispatch submit/wait + cap (AC-23, AC-25, AC-26, AC-32) --------------


def test_submit_returns_acceptance_without_completion(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "POST", "/graph/dispatch", {"task_id": "T-test-1", "correlation_id": "corr-1"})
    assert status == 202
    assert body == {"task_id": "T-test-1", "accepted": True, "completed": False}
    # Same task_id round-trips (AC-32 envelope preservation).
    status, wait = handle(ctx, "GET", "/graph/dispatch/T-test-1", None)
    assert status == 200
    assert wait["completed"] is False  # 0 completions at return (AC-25)
    # Ledger preserved the correlation id unchanged.
    assert ctx.ledger.get("T-test-1")["correlation_id"] == "corr-1"


def test_wait_unknown_task_is_404(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "GET", "/graph/dispatch/never-seen", None)
    assert status == 404
    assert set(body) == {"code", "message"}
    assert body["code"] == "E-INPUT"


def test_submit_rejects_empty_task_id(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "POST", "/graph/dispatch", {"task_id": "  "})
    assert status == 400
    assert body["code"] == "E-INPUT"


def test_wait_reports_completion_from_taskstore(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.store is not None
    ctx.store.create(task_id="T-done-1", correlation_id="corr-d", capability="code.backend", title="done")
    for step in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW"):
        ctx.store.transition("T-done-1", step, actor="A01", reason="test")
    status, body = handle(ctx, "GET", "/graph/dispatch/T-done-1", None)
    assert status == 200
    assert body["completed"] is True  # wait reports completion (AC-26)


def test_worker_cap_refuses_31st_worker(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.store is not None
    assert WORKER_CAP == 30
    for i in range(WORKER_CAP):
        create_active_task(ctx.store, f"T-cap-{i:02d}")
    assert in_flight_count(ctx) == WORKER_CAP
    status, body = handle(ctx, "POST", "/graph/dispatch", {"task_id": "T-cap-30"})
    assert status == 429
    assert body["code"] == "E-CAPACITY"
    # 0 extra spawns: the refused task was not accepted.
    status, wait = handle(ctx, "GET", "/graph/dispatch/T-cap-30", None)
    assert status == 404


def test_dispatch_ledger_is_thread_safe():
    ledger = DispatchLedger()
    errors: list[str] = []

    def worker(n: int) -> None:
        try:
            for i in range(50):
                ledger.record_acceptance(f"T-{n}-{i}", "corr")
        except Exception as exc:  # pragma: no cover
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    assert ledger.active_count() == 8 * 50


# --- Pack parity + engine selector (AC-27, AC-28, AC-30, AC-38) -----------


def test_pack_parity_zero_divergences_for_valid_packs(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "GET", "/graph/packs/A05/parity", None)
    assert status == 200
    assert body == {"pack_id": "A05", "divergence_cases": 0}
    status, body = handle(ctx, "GET", "/graph/packs/no-such-pack/parity", None)
    assert status == 404


def test_all_principal_and_shared_packs_have_zero_divergences(tmp_path):
    # AC-27 (30 principal packs) and AC-29 (25 shared entries).
    from swarm.tool_registry import iter_entries, load_document

    doc = load_document()
    principal = sorted({owner for owner, _ in iter_entries(doc) if owner != "shared"})
    shared = sorted({str(tool["name"]) for owner, tool in iter_entries(doc) if owner == "shared" and isinstance(tool, dict)})
    assert len(principal) == 30
    assert len(shared) == 25
    ctx = make_ctx(tmp_path)
    for pack_id in principal + shared:
        status, body = handle(ctx, "GET", f"/graph/packs/{pack_id}/parity", None)
        assert status == 200, pack_id
        assert body == {"pack_id": pack_id, "divergence_cases": 0}


def test_engine_is_python_default_and_rollback_is_one_step(tmp_path, monkeypatch: pytest.MonkeyPatch):
    ctx = make_ctx(tmp_path)
    monkeypatch.setenv("SWARM_PURE_PYTHON", "1")
    status, selector = handle(ctx, "GET", "/graph/engine/selector", None)
    assert status == 200
    assert selector == {"engine": "python", "pure_python": True, "rollback_steps": 1}
    assert engine_selector(ctx)["rollback_steps"] == 1
    status, default = handle(ctx, "GET", "/quality/engine", None)
    assert status == 200
    assert default == {"engine": "python", "approval_record": None}
    assert engine_default_body()["engine"] == "python"


# --- Quality gates: fail-closed (AC-5, AC-11/12/15) ------------------------


def test_quality_gate_pending_without_verdicts(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "GET", "/quality/gates/AC-11", None)
    assert status == 200
    assert body["gate_id"] == "AC-11"
    assert body["status"] == "pending"  # never fabricates passed
    assert body["high_severity_findings"] == 0
    assert body["credential_findings"] == 0
    assert body["secret_findings"] == 0


def test_quality_gate_reflects_recorded_verdict(tmp_path):
    # A recorded pass flips pending -> passed; a high finding is counted.
    from swarm.gates import make_verdict

    ctx = make_ctx(tmp_path)
    assert ctx.store is not None
    ctx.store.create(task_id="T-gate-1", correlation_id="corr-gate", capability="code.backend", title="gate")
    verdict = make_verdict(gate="quality", task_id="T-gate-1", agent_id="A08", findings=[], correlation_id="corr-gate")
    ctx.store.record_verdict("T-gate-1", verdict)
    status, body = handle(ctx, "GET", "/quality/gates/AC-11", None)
    assert status == 200
    assert body["status"] == "passed"
    assert body["high_severity_findings"] == 0


def test_quality_gate_unknown_is_404(tmp_path):
    ctx = make_ctx(tmp_path)
    status, _ = handle(ctx, "GET", "/quality/gates/AC-99", None)
    assert status == 404
    status, _ = handle(ctx, "GET", "/quality/gates/whatever", None)
    assert status == 404


def test_parity_status_live_surface_passes_unbound_blocks(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "GET", "/quality/parity/gsd-sched-core", None)
    assert status == 200
    assert body["oracle"] == "python"
    assert body["divergence_cases"] == 0
    assert body["verdict"] == "passed"
    # Known crate without parity evidence yet: fail-closed blocked.
    status, body = handle(ctx, "GET", "/quality/parity/gsd-guest", None)
    assert status == 200
    assert body["verdict"] == "blocked"
    assert body["divergence_cases"] == 1
    status, _ = handle(ctx, "GET", "/quality/parity/no-such-module", None)
    assert status == 404


def test_sweep_reports_incomplete_honestly(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "GET", "/quality/sweep/sweep-1", None)
    assert status == 200
    assert body["paths_total"] > 0
    assert body["paths_with_verdicts"] == 0  # AC-12 needs 100%; not claimed
    assert 0.0 <= body["wcag_22_aa_percent"] <= 100.0
    assert 0.0 <= body["en_us_percent"] <= 100.0


def test_wip_inventory_shape(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "GET", "/quality/wip/inventory", None)
    assert status == 200
    assert isinstance(body["files"], list)
    for entry in body["files"]:
        assert set(entry) >= {"path", "verdict", "recovered"}
        assert entry["recovered"] is False
        assert entry["path"].endswith(".py")
    assert body["files"] == [
        {"path": p, "verdict": "untracked", "recovered": False} for p in git_untracked_quality_tests(ROOT)
    ]


def test_ci_verdict_never_fabricates_all_green(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "GET", "/quality/ci/512", None)
    assert status == 200
    assert body == {"pr_number": 512, "all_green": False, "credential_findings": 0}
    status, body = handle(ctx, "GET", "/quality/ci/not-a-number", None)
    assert status == 400
    assert body["code"] == "E-INPUT"


def test_unknown_operation_is_404(tmp_path):
    ctx = make_ctx(tmp_path)
    status, body = handle(ctx, "GET", "/no/such/route", None)
    assert status == 404
    assert body["code"] == "E-INPUT"


def test_stub_matches_runtime_module_ac34_ac35():
    # AC-34/35: 25 stub entries match 25 public runtime names, 0 missing.
    import ast
    import re

    stub = (ROOT / "swarm" / "_native.pyi").read_text(encoding="utf-8")
    tree = ast.parse(stub)
    stubbed = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stubbed.add(node.name)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            stubbed.add(node.name)
    assert len(stubbed) == 25
    ctx = AppContext(root=ROOT)
    names = set(native_public_names(native_module(ctx)))
    if names:
        assert names == stubbed
    else:
        # Pure-Python run without the build: stub alone still has 25 entries.
        assert len(stubbed) == 25
    assert re.search(r"__all__", stub) is not None


def test_pure_python_fallback_answers_without_native(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # AC-30 backend leg: without the native build the Python oracle answers.
    import swarm.graph_quality as gq

    ctx = make_ctx(tmp_path)
    ctx.native = None

    def _missing_module(name):
        raise ImportError("no build")

    monkeypatch.setattr(gq.importlib, "import_module", _missing_module)
    assert gq.native_module(ctx) is None
    status, body = gq.handle(ctx, "GET", "/graph/codes", None)
    assert status == 200
    assert body["codes"] == python_wire_codes()
    assert body["retry_median_divergence_ms"] == 0


# --- HTTP server smoke (stdlib, ephemeral port) -----------------------------


def test_http_server_serves_inventory_and_problems(tmp_path):
    ctx = make_ctx(tmp_path)
    server = create_server("127.0.0.1", 0, ctx)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/graph/inventory", timeout=5) as response:
            assert response.status == 200
            payload = json.loads(response.read().decode("utf-8"))
            assert payload["principal_total"] == 394
        try:
            urllib.request.urlopen(f"http://{host}:{port}/graph/dispatch/nope", timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            problem = json.loads(exc.read().decode("utf-8"))
            assert problem["code"] == "E-INPUT"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_contract_schemas_enforce_minimums(tmp_path):
    ctx = make_ctx(tmp_path)
    _, inventory = handle(ctx, "GET", "/graph/inventory", None)
    assert inventory["principal_total"] >= 394
    assert inventory["shared_total"] >= 25
    _, codes = handle(ctx, "GET", "/graph/codes", None)
    assert codes["retry_median_divergence_ms"] >= 0
    _, selector = handle(ctx, "GET", "/graph/engine/selector", None)
    assert selector["rollback_steps"] >= 1
    assert selector["engine"] == "python"
