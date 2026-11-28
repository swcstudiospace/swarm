"""T-06-27: an omp guard extension that fails to load stops its session's process group while the session is still
running. The attempt is FAILED as E-POLICY, and it is not re-dispatched, so the unguarded session never runs again."""
import json
import sqlite3
import stat
import sys
import time
from pathlib import Path

from conftest import run_script

ONE = {"tasks": [{"id": "one", "capability": "code.backend", "agent": "A05", "title": "one", "depends_on": [], "gates": []}]}


def _env(swarm, **extra) -> dict:
    return {"SWARM_DIR": str(swarm), **extra}


def _plan(tmp_path, env) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    spec = tmp_path / "plan.json"
    spec.write_text(json.dumps(ONE))
    r = run_script("orch_plan.py", "--plan", str(spec), "--prefix", "T", "--repo", str(work),
                   "--risk-class", "low", "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return work


def _states(swarm) -> dict:
    con = sqlite3.connect(swarm / "tasks.db")
    states = dict(con.execute("SELECT task_id, state FROM tasks"))
    con.close()
    return states


def _notes(swarm, tid) -> dict:
    con = sqlite3.connect(swarm / "tasks.db")
    row = con.execute("SELECT notes FROM tasks WHERE task_id = ?", (tid,)).fetchone()
    con.close()
    return json.loads((row[0] if row else None) or "{}")


def _failed(swarm, tid) -> list[str]:
    con = sqlite3.connect(swarm / "tasks.db")
    rows = [reason for (reason,) in con.execute(
        "SELECT reason FROM transitions WHERE task_id = ? AND to_state = 'FAILED' ORDER BY id", (tid,))]
    con.close()
    return rows


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_extension_load_failure_stops_the_session_and_is_not_redispatched(tmp_path):
    swarm = tmp_path / ".swarm"
    env = _env(swarm)
    work = _plan(tmp_path, env)
    launches = tmp_path / "launches"
    wrapper = _executable(tmp_path / "omp-bin" / "omp-guardless", f"""#!{sys.executable}
import sys, time
open({str(launches)!r}, "a").write("1\\n")
sys.stderr.write("Failed to load extension /pkg/src/index.ts: boom\\n"); sys.stderr.flush()
time.sleep(60)
""")
    started = time.monotonic()
    r = run_script("swarm_run.py", "--runtime", "omp", "--omp-bin", str(wrapper), "--repo", str(work),
                   "--max-parallel", "1", "--json", env=env)
    elapsed = time.monotonic() - started
    assert r.returncode == 1, r.stdout + r.stderr
    assert elapsed < 20, elapsed  # the 60 s sleep is never waited out: the group is stopped on the stderr line
    assert _states(swarm)["T-one"] == "FAILED"
    reasons = _failed(swarm, "T-one")
    assert len(reasons) == 1, reasons
    assert reasons[0].startswith("E-POLICY: guard extension failed to load; the session was stopped before it could act")
    assert "Failed to load extension" in reasons[0]
    assert _notes(swarm, "T-one").get("retry_after") is None  # a permanent failure is not scheduled for a re-dispatch
    assert launches.read_text().count("1") == 1
