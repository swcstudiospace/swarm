"""orch_plan custom plans: the plan file is read as UTF-8 whatever the locale, and effects-lost reports go through logging."""
import importlib.util
import json
import logging
import os
import subprocess
import sys

from conftest import ROOT

TITLE = "Café brief — 検証"
PLAN = {"tasks": [{"id": "T-UTF8-1", "capability": "code.backend", "agent": "A05", "title": TITLE}]}

_LOAD_SNIPPET = (
    "import importlib.util, json, sys\n"
    "from pathlib import Path\n"
    "spec = importlib.util.spec_from_file_location('orch_plan_ascii', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(mod)\n"
    "rows = mod.load_custom(Path(sys.argv[2]))\n"
    "print(json.dumps([r[3] for r in rows]))\n"
)


def _load_orch_plan():
    spec = importlib.util.spec_from_file_location("orch_plan_under_test", ROOT / "scripts" / "orch_plan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_custom_utf8(tmp_path):
    # Run under an ASCII locale with UTF-8 mode and locale coercion off, so the decode depends on the explicit encoding.
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    done = subprocess.run(
        [sys.executable, "-c", _LOAD_SNIPPET, str(ROOT / "scripts" / "orch_plan.py"), str(plan)],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=ROOT, check=False, timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [TITLE]


def test_plan_effects_lost_reaches_logging(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    orch_plan = _load_orch_plan()
    with caplog.at_level(logging.WARNING):
        orch_plan._plan_effects_lost(["--root", str(tmp_path)], "manifest missing")
    messages = [record.getMessage() for record in caplog.records]
    assert "orch_plan: effects lost: manifest missing" in messages
