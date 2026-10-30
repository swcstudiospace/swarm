"""Parity of `rel_plan` on Python and on the host engine (phase 14.5, unit U3.6c).

The first cell is the no-task path: no store reads, one finding, a plan file. The plan file must match
except the clock field. Envelope signatures and timestamps are not compared. Unset `GSD_ENGINE_BIN` skips.
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
SCRIPT = ROOT / "scripts" / "rel_plan.py"


def _binary() -> str:
    value = os.environ.get("GSD_ENGINE_BIN", "")
    if not value:
        pytest.skip("GSD_ENGINE_BIN is not set")
    if not os.access(value, os.X_OK):
        pytest.fail(f"GSD_ENGINE_BIN is not executable: {value}")
    return value


def _run(root: Path, engine: str, binary: str, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SWARM_DIR"] = str(root / ".swarm")
    env["SWARM_GSD_STATE_FILE"] = ""
    env["SWARM_CLAUDE_STATE_DIR"] = str(root / "claude")
    env.pop("SWARM_SIGNING_KEY", None)
    env["SWARM_SIGNING_KEY"] = "ab" * 32
    env.pop("SWARM_ED25519_KEY", None)
    if engine == "rust":
        env["GSD_ENGINE"] = "rust"
        env["GSD_ENGINE_BIN"] = binary
        env["GSD_RUST_BACKEND"] = "host"
    else:
        env["GSD_ENGINE"] = "python"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "--root", str(root), *(extra or [])],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )


def _comparable(body: dict) -> dict:
    return {
        "status": body.get("status"),
        "verdict": body.get("verdict"),
        "summary": body.get("summary"),
        "release_id": body.get("release_id"),
        "frozen": body.get("frozen"),
        "recorded": body.get("recorded"),
        "findings": [
            {key: item.get(key) for key in ("id", "severity", "kind", "summary", "owner_suggestion")}
            for item in body.get("findings") or []
        ],
    }


def test_no_task_plan_matches_python(tmp_path: Path) -> None:
    binary = _binary()
    py_root, rs_root = tmp_path / "py", tmp_path / "rs"
    py_root.mkdir()
    rs_root.mkdir()
    py = _run(py_root, "python", binary)
    rs = _run(rs_root, "rust", binary)
    assert py.returncode == rs.returncode == 1
    assert "no rust route" not in rs.stdout
    assert _comparable(json.loads(py.stdout)) == _comparable(json.loads(rs.stdout))
    py_plan = (py_root / ".swarm" / "releases" / "REL-T-unassigned.plan.json").read_text(encoding="utf-8")
    rs_plan = (rs_root / ".swarm" / "releases" / "REL-T-unassigned.plan.json").read_text(encoding="utf-8")
    clock = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
    assert clock.sub("CLOCK", py_plan) == clock.sub("CLOCK", rs_plan)


def test_a_task_with_no_verdict_rows_matches_python(tmp_path: Path) -> None:
    binary = _binary()
    sys.path.insert(0, str(ROOT))
    from swarm.taskstore import TaskStore

    os.environ.pop("SWARM_DIR", None)
    py_root, rs_root = tmp_path / "py", tmp_path / "rs"
    for root in (py_root, rs_root):
        root.mkdir()
        store = TaskStore(root=root)
        store.create(task_id="T1", correlation_id="c1", capability="code.backend", risk_class="low")
    py = _run(py_root, "python", binary, ["--task-id", "T1"])
    rs = _run(rs_root, "rust", binary, ["--task-id", "T1"])
    assert py.returncode == rs.returncode
    assert "no rust route" not in rs.stdout
    py_body, rs_body = json.loads(py.stdout), json.loads(rs.stdout)
    assert _comparable(py_body) == _comparable(rs_body)
    py_plan = (py_root / ".swarm" / "releases").glob("*.plan.json")
    rs_plan = (rs_root / ".swarm" / "releases").glob("*.plan.json")
    clock = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
    assert clock.sub("CLOCK", next(py_plan).read_text(encoding="utf-8")) == clock.sub("CLOCK", next(rs_plan).read_text(encoding="utf-8"))
