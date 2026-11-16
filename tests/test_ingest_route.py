"""The host `--ingest` route (U3.5e part 2): the grammar the selector and the binary share, and one heartbeat
whose end state matches Python when `GSD_ENGINE=rust`. The default stays Python.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from conftest import ROOT, run_script

BINARY = ROOT / "target" / "debug" / "gsd_engine"


def _script():
    spec = importlib.util.spec_from_file_location("orch_status_ingest_route", ROOT / "scripts" / "orch_status.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_ingest_grammar_matches_the_cases_the_binary_serves() -> None:
    mod = _script()
    assert mod.rust_serves_ingest(["--ingest", "result.json", "--json"])
    assert mod.rust_serves_ingest(["--json", "--ingest=dir/result.json", "--advisory", "--task-id", "T-1"])
    assert not mod.rust_serves_ingest(["--ingest", "result.json"])
    assert not mod.rust_serves_ingest(["--json", "--ingest", "result.json", "--dry-run"])
    assert not mod.rust_serves_ingest(["--json", "--transition", "T-1", "DONE", "--ingest", "result.json"])
    assert not mod.rust_serves_write(["--ingest", "result.json", "--json"])
    assert mod.DEFAULT_ENGINE_INGEST == "python"


def test_an_unset_engine_does_not_start_the_binary_for_an_ingest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _script()
    monkeypatch.delenv("GSD_ENGINE", raising=False)
    monkeypatch.setenv("GSD_ENGINE_BIN", str(tmp_path / "missing"))
    assert mod.select_engine(["--ingest", "result.json", "--json"]) is None


def _states(db: Path) -> dict[str, str]:
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT task_id, state FROM tasks ORDER BY task_id"))
    finally:
        conn.close()


def _event_types(swarm: Path) -> list[str]:
    path = swarm / "events.jsonl"
    if not path.exists():
        return []
    types = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        body = json.loads(line)
        types.append(body.get("type") or body.get("event_type") or body.get("event"))
    return types


@pytest.mark.skipif(not BINARY.is_file(), reason="debug gsd_engine is not built")
def test_a_heartbeat_ingest_on_the_rust_engine_ends_where_python_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_GSD_STATE_FILE", "")
    monkeypatch.setenv("SWARM_CLAUDE_STATE_DIR", str(tmp_path / "claude"))
    from swarm.taskstore import TaskStore

    def seed(root: Path) -> None:
        monkeypatch.setenv("SWARM_DIR", str(root / ".swarm"))
        store = TaskStore(root=root)
        store.create(task_id="H-1", correlation_id="c", capability="code.backend", notes={"gates": []})
        for state in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"):
            store.transition("H-1", state)

    py_root, rs_root = tmp_path / "py", tmp_path / "rs"
    seed(py_root)
    shutil.copytree(py_root / ".swarm", rs_root / ".swarm")
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"task_id": "H-1", "state": "IN_PROGRESS", "outputs": [
        {"kind": "code", "uri": "file://once", "version": "1", "digest": "d"}]}), encoding="utf-8")
    py = run_script("orch_status.py", "--ingest", str(result), "--json", "--root", str(py_root),
                    env={"SWARM_DIR": str(py_root / ".swarm"), "GSD_ENGINE": "python"})
    rs = run_script("orch_status.py", "--ingest", str(result), "--json", "--root", str(rs_root),
                    env={"SWARM_DIR": str(rs_root / ".swarm"), "GSD_ENGINE": "rust", "GSD_ENGINE_BIN": str(BINARY),
                         "GSD_RUST_BACKEND": "host"})
    assert py.returncode == 0, py.stdout + py.stderr
    assert rs.returncode == 0, rs.stdout + rs.stderr
    assert _states(py_root / ".swarm" / "tasks.db") == _states(rs_root / ".swarm" / "tasks.db") == {"H-1": "IN_PROGRESS"}
    py_body, rs_body = json.loads(py.stdout), json.loads(rs.stdout)
    assert py_body["summary"] == rs_body["summary"]
    assert rs_body["task"]["state"] == "IN_PROGRESS"
    assert _event_types(py_root / ".swarm") == _event_types(rs_root / ".swarm")
