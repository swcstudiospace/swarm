"""Parity of `orch_assign` refusals and a simple signed assignment (phase 14.5, unit U3.7a).

Envelope ids, timestamps and signatures are not compared. A task with dependencies stays Python.
Unset `GSD_ENGINE_BIN` skips.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "orch_assign.py"
SEED = "11" * 32


def _binary() -> str:
    value = os.environ.get("GSD_ENGINE_BIN", "")
    if not value:
        pytest.skip("GSD_ENGINE_BIN is not set")
    if not os.access(value, os.X_OK):
        pytest.fail(f"GSD_ENGINE_BIN is not executable: {value}")
    return value


def _run(root: Path, engine: str, binary: str, extra: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SWARM_DIR"] = str(root / ".swarm")
    env["SWARM_GSD_STATE_FILE"] = ""
    env["SWARM_CLAUDE_STATE_DIR"] = str(root / "claude")
    env["SWARM_ED25519_KEY"] = SEED
    env.pop("SWARM_SIGNING_KEY", None)
    env.pop("SWARM_ED25519_KEY_FILE", None)
    env.pop("SWARM_ALLOW_INSECURE_DEV_KEY", None)
    env.pop("SUBSTRATE_URL", None)
    env.pop("SWARM_CHILD", None)
    env.pop("SWARM_AGENT_SESSION", None)
    env.pop("SWARM_AGENT", None)
    if engine == "rust":
        env["GSD_ENGINE"] = "rust"
        env["GSD_ENGINE_BIN"] = binary
        env["GSD_RUST_BACKEND"] = "host"
    else:
        env["GSD_ENGINE"] = "python"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "--root", str(root), *extra],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )


def _error(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout)["error"]


def test_refusals_match_python(tmp_path: Path) -> None:
    binary = _binary()
    cases = [
        ["--dry-run", "--task-id", "T1", "--correlation-id", "c1"],
        ["--task-id", "../secret", "--correlation-id", "c1"],
        ["--correlation-id", "c1"],
        ["--task-id", "T1", "--correlation-id", "c1"],
    ]
    for extra in cases:
        py_root, rs_root = tmp_path / "py", tmp_path / "rs"
        py_root.mkdir()
        rs_root.mkdir()
        py = _run(py_root, "python", binary, extra)
        rs = _run(rs_root, "rust", binary, extra)
        assert py.returncode == rs.returncode == 2
        assert _error(py) == _error(rs)
        py_root.rmdir() if not any(py_root.iterdir()) else None
        for child in (py_root, rs_root):
            if child.exists():
                subprocess.run(["rm", "-rf", str(child)], check=True)


def test_a_leased_task_matches_python(tmp_path: Path) -> None:
    binary = _binary()
    sys.path.insert(0, str(ROOT))
    os.environ.pop("SWARM_DIR", None)
    from swarm.taskstore import TaskStore

    def seed(root: Path) -> None:
        root.mkdir()
        store = TaskStore(root=root)
        store.create(task_id="T1", correlation_id="c1", agent_id="A02", capability="req.spec",
                     title="Stored specialist assignment", notes={"brief_excerpt": "Resource data"})
        for state in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"):
            store.transition("T1", state)
        store.conn.close()

    py_root, rs_root = tmp_path / "py", tmp_path / "rs"
    seed(py_root)
    seed(rs_root)
    py = _run(py_root, "python", binary, ["--task-id", "T1", "--correlation-id", "c1"])
    rs = _run(rs_root, "rust", binary, ["--task-id", "T1", "--correlation-id", "c1"])
    assert py.returncode == rs.returncode == 0, rs.stderr + rs.stdout
    py_body, rs_body = json.loads(py.stdout), json.loads(rs.stdout)
    assert rs_body["status"] == py_body["status"] == "ok"
    assert rs_body["agent_slug"] == py_body["agent_slug"]
    assert rs_body["assignment_verified"] is True
    assert rs_body["assignment"]["payload"] == py_body["assignment"]["payload"]
    fence = re.compile(r"```json\n.*?\n```", re.S)
    py_prompt = fence.sub("FENCE", py_body["prompt"]).replace(str(py_root), "REPO")
    rs_prompt = fence.sub("FENCE", rs_body["prompt"]).replace(str(rs_root), "REPO")
    assert rs_prompt == py_prompt
