"""Grok runtime: the assignment travels in a bridge-owned --prompt-file, its output is normalised from grok's own JSON
shape, and the SWARM_DISABLED=1 kill switch turns the prompt hook off."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "user_prompt_submit.py"


@pytest.fixture
def sr(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    spec = importlib.util.spec_from_file_location("swarm_run_grok_under_test", ROOT / "scripts" / "swarm_run.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_grok_output_is_normalised_from_its_own_json_shape(sr):
    grok_json = json.dumps({"text": "PONG", "stopReason": "end_turn", "sessionId": "s-1",
                            "total_cost_usd": 0.1, "num_turns": 1})
    text, meta = sr.session_output("grok", grok_json, "", 0)
    assert text == "PONG"
    assert meta["is_error"] is False and meta["session_id"] == "s-1" and meta["num_turns"] == 1


@pytest.mark.parametrize("payload", [
    {"text": "partial", "stopReason": "max_turns", "sessionId": "s-2"},
    {"type": "error", "message": "Couldn't create session: Invalid params"},
])
def test_grok_failures_are_reported_as_errors(sr, payload):
    text, meta = sr.session_output("grok", json.dumps(payload), "", 0)
    assert meta["is_error"] is True
    assert text == payload.get("message", payload.get("text"))


def test_claude_output_keeps_its_result_shape(sr):
    claude_json = json.dumps({"result": "done", "is_error": False, "session_id": "c-1", "num_turns": 3})
    text, meta = sr.session_output("claude", claude_json, "", 0)
    assert text == "done" and meta["is_error"] is False and meta["session_id"] == "c-1"


def test_prompt_hook_is_silent_under_the_kill_switch():
    prompt = "Plan and build a new feature for the swarm"
    base = {**os.environ, "AIO_SWARM": "0"}
    base.pop("SWARM_DISABLED", None)
    off = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"prompt": prompt}), capture_output=True,
                         text=True, env={**base, "SWARM_DISABLED": "1"})
    assert off.returncode == 0 and json.loads(off.stdout or "{}") == {}


def test_autonomous_runner_exits_under_the_kill_switch(tmp_path):
    swarm = tmp_path / ".swarm"
    env = {**os.environ, "SWARM_DIR": str(swarm), "SWARM_DISABLED": "1", "AIO_SWARM": "0"}
    p = subprocess.run([sys.executable, str(ROOT / "hooks" / "autonomous_run.py"), "--cwd", str(tmp_path),
                        "--brief", "Plan and build a new feature for the swarm", "--dry-run"],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    assert not (swarm / "tasks.db").exists() and not (swarm / "autonomous.log").exists()
