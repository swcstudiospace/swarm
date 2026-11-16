"""P4-07: the prompt hook must reach swarm.signal_detector from its own repo, whatever the cwd, and must say so when it cannot."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "user_prompt_submit.py"

# Runs the hook as __main__ so the probe can see whether the classifier module was loaded. It reports on stderr only.
_PROBE = r"""
import runpy, sys
try:
    runpy.run_path(sys.argv[1], run_name="__main__")
except SystemExit:
    pass
print("SD_LOADED=" + str("swarm.signal_detector" in sys.modules and sys.modules["swarm.signal_detector"] is not None), file=sys.stderr)
"""


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "SWARM_CHILD")}
    env["AIO_SWARM"] = "0"
    return env


def test_classifier_loads_from_repo_when_cwd_is_outside_it(tmp_path):
    p = subprocess.run([sys.executable, "-c", _PROBE, str(HOOK)], input=json.dumps({"prompt": "build x"}),
                       capture_output=True, text=True, cwd=tmp_path, env=_env())
    assert p.returncode == 0, p.stderr
    assert "SD_LOADED=True" in p.stderr, p.stderr
    assert "unavailable" not in p.stderr
    assert "agent-swarm-orchestrate" in json.loads(p.stdout).get("additionalContext", "")


def test_unavailable_classifier_falls_back_and_reports_the_reason(tmp_path):
    probe = "import sys\nsys.modules['swarm.signal_detector'] = None\n" + _PROBE
    p = subprocess.run([sys.executable, "-c", probe, str(HOOK)], input=json.dumps({"prompt": "build x"}),
                       capture_output=True, text=True, cwd=tmp_path, env=_env())
    assert p.returncode == 0, p.stderr
    assert "swarm.signal_detector unavailable" in p.stderr
    assert "agent-swarm-orchestrate" in json.loads(p.stdout).get("additionalContext", "")
