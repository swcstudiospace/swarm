"""Dual-surface parity contract for the local Swarm server (14.7 W0-01, AC-14.7-05).

The OMP explicit surface (scripts/orch_status.py --correlation-id <id> --json) and the stdio MCP client (tests/fixtures/mcp_stdio_client.py)
must report the same canonical task and attempt outcomes for the same authorized root and correlation, the same bounded pages, and the
same state labels. Both surfaces read one temporary SWARM_DIR seeded through the real TaskStore. Expected outcomes are read back from
TaskStore itself, never from a surface's own answer.

Persisted fixture data is test data: every task is titled with TEST_DATA_PREFIX and carries FIXTURE_LABEL in its notes. No model runs.

Placeholders, not accepted API: TOOL, the paging argument names, the next-cursor field and the provenance keys are W0 placeholders
(DA-09 is unresolved). The assertions pin parity and page boundaries, not final tool names.

Pages: the OMP surface has no paging argument. Its answer is the complete ordered row set, and its page boundaries are that answer
sliced by PAGE_SIZE. The MCP paged calls must produce exactly those boundaries.

State labels: no surface reports a label field today. _state_label derives one from the canonical row projection that both surfaces
must share, so identical rows yield an identical label. The labels are:
  empty        no task rows for the correlation (explicit; never a fallback to another correlation)
  interrupted  a task is RETRY: its attempt stopped without a result and awaits re-dispatch
  stale        a task was reworked and its gate evidence predates the rework, so the gate is uncovered
  partial      a task is IN_REVIEW and a required gate has no current verdict
  current      none of the above

Tests that need the W1 MCP server carry strict xfail, so this file is a red contract, not a skip. The OMP-only checks run green.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_CLIENT_PATH = ROOT / "tests" / "fixtures" / "mcp_stdio_client.py"
_client_spec = importlib.util.spec_from_file_location("mcp_stdio_client", _CLIENT_PATH)
_client = importlib.util.module_from_spec(_client_spec)
_client_spec.loader.exec_module(_client)
McpStdioClient = _client.McpStdioClient
call_tool = _client.call_tool
initialize_session = _client.initialize_session
server_argv = _client.server_argv

ORCH_STATUS = ROOT / "scripts" / "orch_status.py"
PROTO = "2025-06-18"  # revision with structuredContent
TOOL = "swarm_status"  # placeholder name (DA-09)
PAGE_SIZE_ARG = "limit"  # placeholder paging argument (DA-09)
CURSOR_ARG = "cursor"  # placeholder paging argument (DA-09)
NEXT_CURSOR_FIELD = "next_cursor"  # placeholder: absent or null on the last page
PAGE_SIZE = 2
SUBPROCESS_TIMEOUT_S = 120

TEST_DATA_PREFIX = "[TEST DATA] "
FIXTURE_LABEL = "test-data:w0-dual-adapter-parity"

CORR = "corr-w0-parity"  # mixed lifecycle states; the canonical-outcome and paging fixture
CORR_EMPTY = "corr-w0-parity-empty"  # no tasks; present only so an ambient fallback would be visible
CORR_PARTIAL = "corr-w0-parity-partial"
CORR_STALE = "corr-w0-parity-stale"
CORR_INTERRUPTED = "corr-w0-parity-interrupted"

TASK_IDS = ("T-w0p-alpha", "T-w0p-beta", "T-w0p-gamma", "T-w0p-delta", "T-w0p-epsilon")
# (task_id, agent_id, lifecycle states applied after CREATED). dag_depth is the index, so the store's order is fixed.
MAIN_SPECS = (
    (TASK_IDS[0], "A05", ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS")),
    (TASK_IDS[1], "A06", ("VALIDATED", "PLANNED")),
    (TASK_IDS[2], "A05", ("VALIDATED", "PLANNED", "CLAIMED")),
    (TASK_IDS[3], "A06", ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "FAILED")),
    (TASK_IDS[4], "A05", ()),
)
IN_PROGRESS_PATH = ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS")

LABEL_EMPTY = "empty"
LABEL_INTERRUPTED = "interrupted"
LABEL_STALE = "stale"
LABEL_PARTIAL = "partial"
LABEL_CURRENT = "current"

CORE_KEYS = ("task_id", "agent", "state", "attempt", "rework", "depth")
CANONICAL_KEYS = CORE_KEYS + ("ready", "gates_required", "verdicts", "missing_gates")

SERVER_NOT_BUILT = pytest.mark.xfail(strict=True, reason="14.7 W1 server not built")


def _project(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in keys} for row in rows]


def _uncovered(row: dict[str, Any]) -> list[str]:
    return [gate for gate in row["gates_required"] if gate not in row["verdicts"]]


def _state_label(rows: list[dict[str, Any]]) -> str:
    """The label of a correlation, derived from canonical rows only. Precedence follows the module docstring."""
    if not rows:
        return LABEL_EMPTY
    if any(row["state"] == "RETRY" for row in rows):
        return LABEL_INTERRUPTED
    if any(row["rework"] > 0 and _uncovered(row) for row in rows):
        return LABEL_STALE
    if any(row["state"] == "IN_REVIEW" and row["missing_gates"] for row in rows):
        return LABEL_PARTIAL
    return LABEL_CURRENT


def _pages(items: list[str], size: int) -> list[list[str]]:
    return [items[start:start + size] for start in range(0, len(items), size)]


def _tool_result(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, f"tools/call answered with a protocol error, not a result: {response}"
    assert "result" in response, response
    return response["result"]


@pytest.fixture()
def server_env(swarm_dir: Path) -> dict[str, str]:
    """A minimal child environment: the temporary SWARM_DIR, the pure-Python authority, and no signing material."""
    home = swarm_dir.parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8", "HOME": str(home),
            "SWARM_DIR": str(swarm_dir), "SWARM_PURE_PYTHON": "1", "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}


@pytest.fixture()
def mcp(server_env: dict[str, str]):
    client = McpStdioClient(server_argv(), env=server_env)
    yield client
    client.close()


def _create(store: Any, corr: str, task_id: str, depth: int, agent_id: str) -> None:
    store.create(task_id=task_id, correlation_id=corr, capability="code", title=f"{TEST_DATA_PREFIX}{task_id}",
                 agent_id=agent_id, dag_depth=depth, notes={"fixture": FIXTURE_LABEL})


def _move(store: Any, task_id: str, states: tuple[str, ...]) -> None:
    for state in states:
        store.transition(task_id, state, actor="A01", reason=FIXTURE_LABEL)


def _record_superseded_verdict(store: Any, task_id: str) -> None:
    """A review row with a NULL envelope, the shape _migrate_verdicts leaves on legacy rows. It is written before the rework
    below, so the rework's cut-off makes it stale and latest_verdicts no longer reports it."""
    now = time.time()
    store.conn.execute(
        "INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts, envelope_json, sig)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, "review", "pass", "A09", "[]", now + 3600, now, None, None))
    store.conn.commit()


@pytest.fixture()
def parity_root(swarm_dir: Path) -> Path:
    """Seeds one authorized root with the main correlation and one correlation per named state. CORR_EMPTY is never created."""
    from swarm.taskstore import TaskStore  # imported after the swarm_dir fixture has reset the swarm modules

    with TaskStore() as store:
        for depth, (task_id, agent_id, states) in enumerate(MAIN_SPECS):
            _create(store, CORR, task_id, depth, agent_id)
            _move(store, task_id, states)

        _create(store, CORR_PARTIAL, "T-w0p-partial", 0, "A05")
        _move(store, "T-w0p-partial", (*IN_PROGRESS_PATH, "IN_REVIEW"))

        _create(store, CORR_INTERRUPTED, "T-w0p-interrupted", 0, "A06")
        _move(store, "T-w0p-interrupted", (*IN_PROGRESS_PATH, "RETRY"))

        _create(store, CORR_STALE, "T-w0p-stale", 0, "A05")
        _move(store, "T-w0p-stale", IN_PROGRESS_PATH)
        _record_superseded_verdict(store, "T-w0p-stale")
        _move(store, "T-w0p-stale", ("IN_REVIEW", "CHANGES_REQUESTED", "IN_PROGRESS"))
    return swarm_dir


def _store_core(corr: str) -> list[dict[str, Any]]:
    """Canonical outcomes read straight from TaskStore, in TaskStore's own order."""
    from swarm.taskstore import TaskStore

    with TaskStore() as store:
        return [{"task_id": t["task_id"], "agent": t["agent_id"], "state": t["state"], "attempt": t["attempt"],
                 "rework": t["rework_loops"], "depth": t["dag_depth"]} for t in store.list(correlation_id=corr)]


def _omp_payload(env: dict[str, str], corr: str) -> dict[str, Any]:
    completed = subprocess.run([sys.executable, str(ORCH_STATUS), "--correlation-id", corr, "--json"],
                               env=env, cwd=ROOT, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["correlation_id"] == corr, payload  # an explicit correlation, never an ambient latest one
    return payload


def _omp_rows(env: dict[str, str], corr: str) -> list[dict[str, Any]]:
    return _omp_payload(env, corr)["tasks"]


def _mcp_page(mcp: Any, corr: str, *, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
    arguments: dict[str, Any] = {"correlation_id": corr}
    if limit is not None:
        arguments[PAGE_SIZE_ARG] = limit
    if cursor is not None:
        arguments[CURSOR_ARG] = cursor
    result = _tool_result(call_tool(mcp, TOOL, arguments))
    assert not result.get("isError"), result
    assert "structuredContent" in result, result
    return result["structuredContent"]


def _assert_label_parity(env: dict[str, str], mcp: Any, corr: str, expected: str) -> None:
    omp_rows = _project(_omp_rows(env, corr), CANONICAL_KEYS)
    mcp_rows = _project(_mcp_page(mcp, corr)["tasks"], CANONICAL_KEYS)
    assert mcp_rows == omp_rows, f"canonical rows differ for {corr}:\nOMP {omp_rows}\nMCP {mcp_rows}"
    omp_label, mcp_label = _state_label(omp_rows), _state_label(mcp_rows)
    assert omp_label == mcp_label == expected, f"{corr}: OMP {omp_label!r}, MCP {mcp_label!r}, expected {expected!r}"


# ---- OMP explicit surface: green -------------------------------------------------------------------------------------


def test_omp_surface_reports_store_outcomes_for_every_task(server_env, parity_root):
    rows = _omp_rows(server_env, CORR)
    assert [row["task_id"] for row in rows] == list(TASK_IDS)
    assert _project(rows, CORE_KEYS) == _store_core(CORR)
    assert _state_label(_project(rows, CANONICAL_KEYS)) == LABEL_CURRENT


def test_omp_surface_is_untruncated_and_its_page_boundaries_are_stable(server_env, parity_root):
    first = [row["task_id"] for row in _omp_rows(server_env, CORR)]
    second = [row["task_id"] for row in _omp_rows(server_env, CORR)]
    assert first == second == list(TASK_IDS), "the OMP answer must be complete and identical on every read"
    assert [len(page) for page in _pages(first, PAGE_SIZE)] == [2, 2, 1]


def test_omp_surface_provenance_names_the_seeded_store(server_env, parity_root):
    payload = _omp_payload(server_env, CORR)
    assert Path(payload["db"]).name == "tasks.db"
    assert Path(payload["swarm_dir"]).resolve() == parity_root.resolve()


def test_empty_state_omp_surface_is_explicit_and_never_falls_back(server_env, parity_root):
    payload = _omp_payload(server_env, CORR_EMPTY)
    assert payload["tasks"] == []
    assert _state_label(payload["tasks"]) == LABEL_EMPTY


def test_partial_state_omp_surface_names_the_missing_review_gate(server_env, parity_root):
    rows = _project(_omp_rows(server_env, CORR_PARTIAL), CANONICAL_KEYS)
    assert [row["state"] for row in rows] == ["IN_REVIEW"]
    assert rows[0]["missing_gates"] == ["review"]
    assert _state_label(rows) == LABEL_PARTIAL


def test_stale_state_omp_surface_hides_the_superseded_verdict(server_env, parity_root):
    rows = _project(_omp_rows(server_env, CORR_STALE), CANONICAL_KEYS)
    assert [row["state"] for row in rows] == ["IN_PROGRESS"]
    assert rows[0]["rework"] == 1
    assert rows[0]["gates_required"] == ["review"]
    assert rows[0]["verdicts"] == {}, "a verdict older than the rework cut-off must not be reported as current"
    assert _state_label(rows) == LABEL_STALE


def test_interrupted_state_omp_surface_reports_the_retry_attempt(server_env, parity_root):
    rows = _project(_omp_rows(server_env, CORR_INTERRUPTED), CANONICAL_KEYS)
    assert [row["state"] for row in rows] == ["RETRY"]
    assert rows[0]["attempt"] == 1
    assert _state_label(rows) == LABEL_INTERRUPTED


# ---- MCP client against the W1 server: strict xfail ------------------------------------------------------------------


@SERVER_NOT_BUILT
def test_mcp_and_omp_report_identical_canonical_outcomes(server_env, mcp, parity_root):
    initialize_session(mcp, PROTO)
    omp = _omp_payload(server_env, CORR)
    page = _mcp_page(mcp, CORR)
    assert _project(page["tasks"], CANONICAL_KEYS) == _project(omp["tasks"], CANONICAL_KEYS)
    assert _project(page["tasks"], CORE_KEYS) == _store_core(CORR)
    assert page["swarm_dir"] == omp["swarm_dir"], "both surfaces must name the same store"


@SERVER_NOT_BUILT
def test_mcp_pages_have_the_same_boundaries_as_the_omp_answer(server_env, mcp, parity_root):
    initialize_session(mcp, PROTO)
    expected = _pages([row["task_id"] for row in _omp_rows(server_env, CORR)], PAGE_SIZE)
    pages: list[list[str]] = []
    cursor: str | None = None
    for _ in range(len(expected) + 1):
        page = _mcp_page(mcp, CORR, limit=PAGE_SIZE, cursor=cursor)
        pages.append([row["task_id"] for row in page["tasks"]])
        cursor = page.get(NEXT_CURSOR_FIELD)
        if cursor is None:
            break
    assert cursor is None, f"MCP pagination did not end within {len(expected)} pages"
    assert pages == expected
    first_again = [row["task_id"] for row in _mcp_page(mcp, CORR, limit=PAGE_SIZE)["tasks"]]
    assert first_again == expected[0], "the first page must not move between reads"


@SERVER_NOT_BUILT
def test_empty_state_mcp_reports_explicit_empty_like_omp(server_env, mcp, parity_root):
    initialize_session(mcp, PROTO)
    _assert_label_parity(server_env, mcp, CORR_EMPTY, LABEL_EMPTY)


@SERVER_NOT_BUILT
def test_partial_state_mcp_reports_the_same_label_as_omp(server_env, mcp, parity_root):
    initialize_session(mcp, PROTO)
    _assert_label_parity(server_env, mcp, CORR_PARTIAL, LABEL_PARTIAL)


@SERVER_NOT_BUILT
def test_stale_state_mcp_reports_the_same_label_as_omp(server_env, mcp, parity_root):
    initialize_session(mcp, PROTO)
    _assert_label_parity(server_env, mcp, CORR_STALE, LABEL_STALE)


@SERVER_NOT_BUILT
def test_interrupted_state_mcp_reports_the_same_label_as_omp(server_env, mcp, parity_root):
    initialize_session(mcp, PROTO)
    _assert_label_parity(server_env, mcp, CORR_INTERRUPTED, LABEL_INTERRUPTED)
