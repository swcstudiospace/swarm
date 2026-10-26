"""orch_status GSD_ENGINE=rust: a wedged binary gets E-TIMEOUT (exit 2), never the Python body's answer."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "orch_status.py"


def test_wedged_rust_binary_reports_e_timeout_and_never_falls_back(tmp_path: Path) -> None:
    # `exec` replaces the shell, so the timeout kill reaches the sleeping process and its stdout pipe closes.
    fake = tmp_path / "gsd_engine"
    fake.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    fake.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GSD_") and not key.startswith("SWARM_")}
    env.update({"GSD_ENGINE": "rust", "GSD_ENGINE_BIN": str(fake), "GSD_RUST_TIMEOUT_S": "1"})

    proc = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(tmp_path)], env=env,
                          capture_output=True, text=True, timeout=120, check=False)

    assert proc.returncode == 2
    reply = json.loads(proc.stdout)
    assert reply["status"] == "error"
    assert reply["error"]["code"] == "E-TIMEOUT"
    assert reply["error"]["message"] == "the gsd_engine binary did not answer within the call deadline"
    # the Python body's answer carries a correlation/tasks summary; a fallback would print it instead
    assert "tasks" not in reply
    assert "correlation_id" not in reply
