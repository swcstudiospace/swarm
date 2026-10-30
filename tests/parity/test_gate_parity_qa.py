"""Parity of `qa_gate` on Python and on the host engine (phase 14.5, unit U3.6b).

The cells are a repository with no runner and one whose `npm test` is a PATH shim. Status, verdict, runs, finding
identity and the executed counts must match. Envelope signatures and clock fields are not compared. Unset
`GSD_ENGINE_BIN` skips.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qa_gate.py"


def _binary() -> str:
    value = os.environ.get("GSD_ENGINE_BIN", "")
    if not value:
        pytest.skip("GSD_ENGINE_BIN is not set")
    if not os.access(value, os.X_OK):
        pytest.fail(f"GSD_ENGINE_BIN is not executable: {value}")
    return value


def _run(root: Path, engine: str, binary: str, extra: list[str], path: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SWARM_DIR"] = str(root / ".swarm")
    env["SWARM_GSD_STATE_FILE"] = ""
    env["SWARM_CLAUDE_STATE_DIR"] = str(root / "claude")
    env["PATH"] = path
    env.pop("SWARM_SIGNING_KEY", None)
    env.pop("SWARM_ED25519_KEY", None)
    if engine == "rust":
        env["GSD_ENGINE"] = "rust"
        env["GSD_ENGINE_BIN"] = binary
        env["GSD_RUST_BACKEND"] = "host"
    else:
        env["GSD_ENGINE"] = "python"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "--root", str(root), "--risk-class", "low", "--timeout", "10", *extra],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )


def _comparable(body: dict) -> dict:
    return {
        "status": body.get("status"),
        "verdict": body.get("verdict"),
        "summary": body.get("summary"),
        "risk_class": body.get("risk_class"),
        "runs": body.get("runs"),
        "findings": [
            {key: item.get(key) for key in ("id", "severity", "kind", "summary")}
            for item in body.get("findings") or []
        ],
        "executed": [
            {key: item.get(key) for key in ("runner", "returncode", "passed", "failed")}
            for item in body.get("executed") or []
        ],
    }


def test_no_runner_and_an_npm_shim_match_python(tmp_path: Path) -> None:
    binary = _binary()
    shim = tmp_path / "bin"
    shim.mkdir()
    npm = shim / "npm"
    npm.write_text("#!/bin/sh\necho 'Tests: 1 passed'\n", encoding="utf-8")
    npm.chmod(npm.stat().st_mode | stat.S_IEXEC)
    path = f"{shim}{os.pathsep}{os.environ.get('PATH', '')}"
    empty = tmp_path / "empty"
    empty.mkdir()
    with_npm = tmp_path / "npm-repo"
    with_npm.mkdir()
    (with_npm / "package.json").write_text(json.dumps({"scripts": {"test": "echo ok"}}), encoding="utf-8")
    for root in (empty, with_npm):
        py = _run(root, "python", binary, [], path)
        rs = _run(root, "rust", binary, [], path)
        assert py.returncode == rs.returncode, f"{root.name}\n{py.stdout}\n{rs.stdout}\n{rs.stderr}"
        py_body, rs_body = json.loads(py.stdout), json.loads(rs.stdout)
        assert _comparable(py_body) == _comparable(rs_body), f"{root.name}\n{py.stdout}\n{rs.stdout}"
        assert "no rust route" not in rs.stdout
