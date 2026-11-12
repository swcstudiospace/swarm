"""CORE-04 / WR-01: the documented plan → run → status flow shares one Task Store (D-09/D-10)."""
import json
import os
import subprocess
import sys

from conftest import ROOT


def _env():
    # no SWARM_DIR (state must follow --repo) and no other ambient swarm settings.
    # A dry-run signs task.assign. Pass this test's throwaway HMAC key; do not opt into the dev key.
    key = os.environ["SWARM_SIGNING_KEY"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    env["SWARM_SIGNING_KEY"] = key
    env["SWARM_PURE_PYTHON"] = "1"  # the oracle switch is a test setting, not an operator one: the scripts import the pure modules
    return env


def _run(script, *args, cwd):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args],
                          capture_output=True, text=True, cwd=cwd, env=_env())


def _layout(tmp_path):
    app, other = tmp_path / "app", tmp_path / "other"
    app.mkdir()
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    return app, other


def _plan(app, other):
    p = _run("orch_plan.py", "--repo", str(app), "--brief-text", "x", "--pattern", "hotfix", "--json", cwd=other)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(p.stdout)["correlation_id"]


def test_documented_flow_one_state_dir(tmp_path):
    app, other = _layout(tmp_path)
    corr = _plan(app, other)

    r = _run("swarm_run.py", "--repo", str(app), "--dry-run", "--json", cwd=other)
    assert r.returncode == 0, r.stdout + r.stderr
    run = json.loads(r.stdout)
    assert run["complete"] is True and run["counts"] == {"DONE": 8}

    s = _run("orch_status.py", "--repo", str(app), "--json", cwd=other)
    assert s.returncode == 0, s.stdout + s.stderr
    status = json.loads(s.stdout)
    assert status["correlation_id"] == corr and len(status["tasks"]) == 8

    sdir = app.resolve() / ".swarm"
    assert (sdir / "tasks.db").exists()
    assert not (other / ".swarm").exists()
    assert [p.resolve() for p in tmp_path.rglob("events.jsonl")] == [sdir / "events.jsonl"]


def test_run_unknown_correlation_exit2(tmp_path):
    app, other = _layout(tmp_path)
    _plan(app, other)

    r = _run("swarm_run.py", "--repo", str(app), "--dry-run", "--correlation-id", "nope", "--json", cwd=other)
    assert r.returncode == 2, r.stdout + r.stderr
    err = json.loads(r.stdout)["error"]
    assert err["code"] == "E-INPUT" and "nope" in err["message"]
