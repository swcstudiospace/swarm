"""Parity of `rel_plan` on Python and on the host engine (phase 14.5, unit U3.6c).

The first cell is the no-task path: no store reads, one finding, a plan file. The plan file must match
except the clock field. No-task signed verdict files must report skipped runs, not vacuous passes. Direct probes
must be served by the binary; keyed selectors pin its digest at fixture time. Unset `GSD_ENGINE_BIN` skips.
"""
from __future__ import annotations

import hashlib
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


@pytest.fixture
def engine_binary(monkeypatch: pytest.MonkeyPatch) -> str:
    binary = str(Path(_binary()).resolve())
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", hashlib.sha256(Path(binary).read_bytes()).hexdigest())
    return binary


def _env(root: Path, engine: str, binary: str) -> dict[str, str]:
    env = os.environ.copy()
    env["SWARM_DIR"] = str(root / ".swarm")
    env["SWARM_GSD_STATE_FILE"] = ""
    env["SWARM_CLAUDE_STATE_DIR"] = str(root / "claude")
    env.pop("SWARM_SIGNING_KEY", None)
    env["SWARM_SIGNING_KEY"] = "ab" * 32
    env.pop("SWARM_ED25519_KEY", None)
    for key in ("SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SWARM_AGENT_SESSION", "SWARM_DRYRUN_FAIL"):
        env.pop(key, None)
    if engine == "rust":
        env["GSD_ENGINE"] = "rust"
        env["GSD_ENGINE_BIN"] = binary
        env["GSD_RUST_BACKEND"] = "host"
    else:
        env["GSD_ENGINE"] = "python"
    return env


def _run(root: Path, engine: str, binary: str, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    env = _env(root, engine, binary)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "--root", str(root), *(extra or [])],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )


def _probe(root: Path, binary: str, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    env = _env(root, "python", binary)
    request = {
        "agent": "A12",
        "script": "rel_plan",
        "argv": ["--json", "--root", str(root), *(extra or [])],
        "root": str(root),
        "env": {key: value for key, value in env.items() if key.startswith(("SWARM_", "GSD_")) and value},
    }
    done = subprocess.run([binary], input=json.dumps(request), capture_output=True, text=True,
                          env=env, cwd=ROOT, timeout=60)
    assert done.returncode in (0, 1), f"the binary declined a served release cell: {done.stdout!r}"
    assert json.loads(done.stdout).get("script") == "rel_plan"
    return done


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


def test_no_task_plan_matches_python(tmp_path: Path, engine_binary: str, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = engine_binary
    py_root, rs_root = tmp_path / "py", tmp_path / "rs"
    py_root.mkdir()
    rs_root.mkdir()
    native_root = tmp_path / "native"
    native_root.mkdir()
    native = _probe(native_root, binary)
    py = _run(py_root, "python", binary)
    rs = _run(rs_root, "rust", binary)
    assert py.returncode == rs.returncode == 1
    assert "no rust route" not in rs.stdout
    assert _comparable(json.loads(py.stdout)) == _comparable(json.loads(rs.stdout))
    assert native.returncode == py.returncode
    assert _comparable(json.loads(native.stdout)) == _comparable(json.loads(py.stdout))
    sys.path.insert(0, str(ROOT))
    from swarm.envelope import verify_envelope

    with monkeypatch.context() as signing:
        signing.setenv("SWARM_SIGNING_KEY", "ab" * 32)
        for root in (py_root, rs_root, native_root):
            envelope = json.loads((root / ".swarm" / "verdicts" / "T-unassigned.release.json").read_text(encoding="utf-8"))
            assert verify_envelope(envelope)
            assert envelope["payload"]["verdict"] == "fail"
            runs = envelope["payload"]["runs"]
            assert runs == {"review": "skipped:no-task", "freeze": "none"}
    py_plan = (py_root / ".swarm" / "releases" / "REL-T-unassigned.plan.json").read_text(encoding="utf-8")
    rs_plan = (rs_root / ".swarm" / "releases" / "REL-T-unassigned.plan.json").read_text(encoding="utf-8")
    clock = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
    assert clock.sub("CLOCK", py_plan) == clock.sub("CLOCK", rs_plan)


@pytest.mark.parametrize("task_id", ["T1", "T-unknown"], ids=["known", "unknown"])
def test_a_task_with_no_verdict_rows_matches_python(tmp_path: Path, engine_binary: str, task_id: str) -> None:
    binary = engine_binary
    sys.path.insert(0, str(ROOT))
    from swarm.taskstore import TaskStore

    os.environ.pop("SWARM_DIR", None)
    py_root, rs_root, native_root = tmp_path / "py", tmp_path / "rs", tmp_path / "native"
    for root in (py_root, rs_root, native_root):
        root.mkdir()
        store = TaskStore(root=root)
        store.create(task_id="T1", correlation_id="c1", capability="code.backend", risk_class="low")
    extra = ["--task-id" if task_id == "T1" else "--task-ids", task_id]
    native = _probe(native_root, binary, extra)
    py = _run(py_root, "python", binary, extra)
    rs = _run(rs_root, "rust", binary, extra)
    assert py.returncode == rs.returncode
    assert "no rust route" not in rs.stdout
    py_body, rs_body = json.loads(py.stdout), json.loads(rs.stdout)
    assert _comparable(py_body) == _comparable(rs_body)
    assert native.returncode == py.returncode
    assert _comparable(json.loads(native.stdout)) == _comparable(py_body)
    # An identified but unknown task is not the no-task path: its absent required gates remain vacuous passes.
    envelope_task = task_id if task_id == "T1" else "T-unassigned"
    for root in (py_root, rs_root, native_root):
        envelope = json.loads((root / ".swarm" / "verdicts" / f"{envelope_task}.release.json").read_text(encoding="utf-8"))
        expected = "fail" if task_id == "T1" else "pass"
        assert envelope["payload"]["runs"]["review"] == expected
    py_plan = (py_root / ".swarm" / "releases").glob("*.plan.json")
    rs_plan = (rs_root / ".swarm" / "releases").glob("*.plan.json")
    clock = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
    assert clock.sub("CLOCK", next(py_plan).read_text(encoding="utf-8")) == clock.sub("CLOCK", next(rs_plan).read_text(encoding="utf-8"))
