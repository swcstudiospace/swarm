"""U3.5f: an invalid `--ingest` result writes nothing.

The binary fault cases (a lost reply, a replay, a race of two) live in
`crates/gsd-adapter/tests/ingest_faults.rs`. This file is the selector-level check that a result the
route cannot parse leaves the store file unchanged, on Python and, when the binary is named, on the
selector too.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "orch_status.py"


def _seed(root: Path) -> Path:
    swarm = root / ".swarm"
    swarm.mkdir()
    db = swarm / "tasks.db"
    code = """
from swarm.taskstore import TaskStore
import sys
store = TaskStore(sys.argv[1])
store.create(task_id="H-1", correlation_id="c", capability="code.backend", notes={"gates": []})
for state in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"):
    store.transition("H-1", state)
store.conn.close()
"""
    env = os.environ.copy()
    env["SWARM_DIR"] = str(swarm)
    env["SWARM_GSD_STATE_FILE"] = ""
    env["SWARM_CLAUDE_STATE_DIR"] = str(root / "claude")
    done = subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True, text=True, env=env, cwd=ROOT)
    assert done.returncode == 0, done.stderr
    return db


def _ingest(root: Path, result: Path, engine: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SWARM_DIR"] = str(root / ".swarm")
    env["SWARM_GSD_STATE_FILE"] = ""
    env["SWARM_CLAUDE_STATE_DIR"] = str(root / "claude")
    env.pop("SWARM_SIGNING_KEY", None)
    if engine == "rust":
        env["GSD_ENGINE"] = "rust"
        env["GSD_ENGINE_BIN"] = os.environ["GSD_ENGINE_BIN"]
        env["GSD_RUST_BACKEND"] = "host"
    else:
        env["GSD_ENGINE"] = "python"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--ingest", str(result), "--json", "--root", str(root)],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )


def test_an_invalid_result_leaves_the_store_file_unchanged(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    before = db.read_bytes()
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    done = _ingest(tmp_path, bad, "python")
    assert done.returncode == 2, done.stdout + done.stderr
    assert db.read_bytes() == before


@pytest.mark.skipif(not os.environ.get("GSD_ENGINE_BIN"), reason="GSD_ENGINE_BIN is not set")
def test_the_selector_also_leaves_the_store_unchanged_for_an_invalid_result(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    before = db.read_bytes()
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    done = _ingest(tmp_path, bad, "rust")
    assert done.returncode == 2, done.stdout + done.stderr
    assert db.read_bytes() == before
