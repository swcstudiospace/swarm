"""CORE-05: safe re-planning — prefix reuse/reject, derived prefixes, atomic plans, runner ambiguity."""
import json
import os
import re
import sqlite3
import subprocess
import sys

import pytest

from conftest import ROOT, run_script


def _env(tmp_path):
    return {"SWARM_DIR": str(tmp_path / ".swarm")}


def _plan(env, *args):
    return run_script("orch_plan.py", "--json", *args, env=env)


def _counts(tmp_path):
    con = sqlite3.connect(tmp_path / ".swarm" / "tasks.db")
    try:
        return tuple(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("tasks", "transitions"))
    finally:
        con.close()


def _corrs(tmp_path, like):
    con = sqlite3.connect(tmp_path / ".swarm" / "tasks.db")
    try:
        return {r[0] for r in con.execute("SELECT correlation_id FROM tasks WHERE task_id LIKE ?", (like,))}
    finally:
        con.close()


def test_same_prefix_same_brief_reuses(tmp_path):
    env = _env(tmp_path)
    first = _plan(env, "--brief-text", "billing", "--prefix", "R")
    assert first.returncode == 0, first.stdout + first.stderr
    corr = json.loads(first.stdout)["correlation_id"]
    before = _counts(tmp_path)
    latest = (tmp_path / ".swarm" / "latest_correlation").stat().st_mtime_ns
    again = _plan(env, "--brief-text", "billing", "--prefix", "R")
    assert again.returncode == 0, again.stdout + again.stderr
    out = json.loads(again.stdout)
    assert out["correlation_id"] == corr and out["reused"] is True
    assert len(out["tasks"]) == 13
    assert _counts(tmp_path) == before
    assert (tmp_path / ".swarm" / "latest_correlation").stat().st_mtime_ns == latest


def test_same_prefix_other_brief_exit2(tmp_path):
    env = _env(tmp_path)
    assert _plan(env, "--brief-text", "billing", "--prefix", "R").returncode == 0
    before = _counts(tmp_path)
    r = _plan(env, "--brief-text", "something else", "--prefix", "R")
    assert r.returncode == 2
    assert json.loads(r.stdout)["error"]["code"] == "E-CONTRACT"
    assert "--prefix" in r.stdout
    assert "IntegrityError" not in r.stdout + r.stderr
    assert _counts(tmp_path) == before


@pytest.mark.parametrize("change", [["--risk-class", "high"], ["--priority", "P0"], ["--acceptance", "p99 < 200ms"]])
def test_same_prefix_other_task_inputs_exit2(tmp_path, change):
    """WR-13: a re-plan asking for another risk class/priority/acceptance must not reuse the old (lower-gated) plan."""
    env = _env(tmp_path)
    base = ["--brief-text", "billing", "--prefix", "R", "--risk-class", "low"]
    assert _plan(env, *base).returncode == 0
    before = _counts(tmp_path)
    same = _plan(env, *base)
    assert same.returncode == 0 and json.loads(same.stdout)["reused"] is True, same.stdout + same.stderr
    r = _plan(env, *base, *change)
    assert r.returncode == 2, r.stdout + r.stderr
    assert json.loads(r.stdout)["error"]["code"] == "E-CONTRACT"
    assert _counts(tmp_path) == before


def test_same_prefix_other_pattern_exit2(tmp_path):
    env = _env(tmp_path)
    assert _plan(env, "--brief-text", "billing", "--prefix", "R", "--pattern", "feature").returncode == 0
    r = _plan(env, "--brief-text", "billing", "--prefix", "R", "--pattern", "hotfix")
    assert r.returncode == 2
    assert json.loads(r.stdout)["error"]["code"] == "E-CONTRACT"


def test_default_prefix_unique(tmp_path):
    env = _env(tmp_path)
    outs = []
    for _ in range(2):
        r = _plan(env, "--brief-text", "billing")
        assert r.returncode == 0, r.stdout + r.stderr
        outs.append(json.loads(r.stdout))
    assert outs[0]["correlation_id"] != outs[1]["correlation_id"]
    for o in outs:
        prefix = "T" + o["correlation_id"].replace("-", "")[:4]
        assert all(re.match(r"^T[0-9a-f]{4}-", t["task_id"]) for t in o["tasks"])
        assert all(t["task_id"].startswith(prefix + "-") for t in o["tasks"])
    assert _counts(tmp_path)[0] == 26


SAME_CORR = "aaaa1111-0000-4000-8000-000000000000"


def _task_count(tmp_path, corr):
    con = sqlite3.connect(tmp_path / ".swarm" / "tasks.db")
    try:
        return con.execute("SELECT COUNT(*) FROM tasks WHERE correlation_id=?", (corr,)).fetchone()[0]
    finally:
        con.close()


def test_same_correlation_rerun_reuses(tmp_path):
    env = _env(tmp_path)
    outs = []
    for _ in range(3):
        r = _plan(env, "--brief-text", "billing", "--pattern", "feature", "--correlation-id", SAME_CORR)
        assert r.returncode == 0, r.stdout + r.stderr
        outs.append(json.loads(r.stdout))
    for o in outs:
        assert o["correlation_id"] == SAME_CORR
        assert all(t["task_id"].startswith("Taaaa-") for t in o["tasks"]), [t["task_id"] for t in o["tasks"]]
    assert [o.get("reused") is True for o in outs] == [False, True, True]
    assert _task_count(tmp_path, SAME_CORR) == 13


def test_same_correlation_other_brief_exit2(tmp_path):
    env = _env(tmp_path)
    assert _plan(env, "--brief-text", "billing", "--correlation-id", SAME_CORR).returncode == 0
    before = _counts(tmp_path)
    r = _plan(env, "--brief-text", "other", "--correlation-id", SAME_CORR)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "E-CONTRACT" in r.stdout
    assert _counts(tmp_path) == before
    assert _task_count(tmp_path, SAME_CORR) == 13


def test_plan_atomic_rollback(tmp_path):
    env = _env(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"tasks": [
        {"id": "a", "capability": "req.spec", "agent": "A02"},
        {"id": "b", "capability": "code.backend", "agent": "A05", "depends_on": ["a"]},
        {"id": "a", "capability": "req.spec", "agent": "A02"},
    ]}))
    corr = "11111111-2222-3333-4444-555555555555"
    r = _plan(env, "--plan", str(plan), "--pattern", "custom", "--prefix", "C", "--correlation-id", corr)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "IntegrityError" not in r.stdout + r.stderr
    assert _corrs(tmp_path, "%") == set()
    assert not (tmp_path / ".swarm" / "plans" / f"{corr}.json").exists()
    assert not (tmp_path / ".swarm" / "latest_correlation").exists()


def test_reuse_tolerates_truncated_plan_snapshot(tmp_path):
    """A re-plan must not traceback when the snapshot exists but the writer has not finished it."""
    env = _env(tmp_path)
    first = _plan(env, "--brief-text", "billing", "--prefix", "R")
    assert first.returncode == 0, first.stdout + first.stderr
    corr = json.loads(first.stdout)["correlation_id"]
    snap = tmp_path / ".swarm" / "plans" / f"{corr}.json"
    snap.write_text("")
    again = _plan(env, "--brief-text", "billing", "--prefix", "R")
    assert again.returncode == 0, again.stdout + again.stderr
    out = json.loads(again.stdout)
    assert out["reused"] is True and out["plan_file"] is None
    assert len(out["tasks"]) == 13
    assert "IntegrityError" not in again.stdout + again.stderr and "Traceback" not in again.stdout + again.stderr


def test_concurrent_same_prefix_no_integrityerror(tmp_path):
    env = {**os.environ, **_env(tmp_path)}
    # initialise the DB schema first so both racers contend only on plan rows
    assert _plan(_env(tmp_path), "--brief-text", "warmup", "--prefix", "W").returncode == 0
    cmd = [sys.executable, str(ROOT / "scripts" / "orch_plan.py"), "--json", "--brief-text", "race", "--prefix", "P"]
    procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT)
             for _ in range(2)]
    results = [(p.wait(timeout=60), *p.communicate()) for p in procs]
    for rc, out, err in results:
        assert rc in (0, 2), out + err
        assert "IntegrityError" not in out + err and "Traceback" not in out + err
    assert len(_corrs(tmp_path, "P-%")) == 1
    assert _counts(tmp_path)[0] == 26


def test_insert_integrityerror_mapped(tmp_path):
    env = _env(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"tasks": [{"id": "be", "capability": "code.backend", "agent": "A05"}]}))
    assert _plan(env, "--plan", str(plan), "--pattern", "custom", "--prefix", "Q").returncode == 0
    r = _plan(env, "--brief-text", "billing", "--prefix", "Q")
    assert r.returncode == 2
    assert json.loads(r.stdout)["error"]["code"] == "E-CONTRACT"
    assert "IntegrityError" not in r.stdout + r.stderr
    assert _counts(tmp_path)[0] == 1


def _write_plan(path, capability, agent):
    path.write_text(json.dumps({"tasks": [{"id": "a", "capability": capability, "agent": agent}]}))


def _task_row(tmp_path, task_id):
    con = sqlite3.connect(tmp_path / ".swarm" / "tasks.db")
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
    finally:
        con.close()


def test_edited_custom_plan_rejected(tmp_path):
    env = _env(tmp_path)
    plan = tmp_path / "plan.json"
    _write_plan(plan, "code.backend", "A05")
    first = _plan(env, "--plan", str(plan), "--prefix", "C")
    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads(first.stdout)["pattern"] == "custom"
    again = _plan(env, "--plan", str(plan), "--prefix", "C")
    assert again.returncode == 0, again.stdout + again.stderr
    assert json.loads(again.stdout)["reused"] is True
    before = _counts(tmp_path)
    _write_plan(plan, "code.frontend", "A06")
    r = _plan(env, "--plan", str(plan), "--prefix", "C")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "E-CONTRACT" in r.stdout
    assert _counts(tmp_path) == before
    row = _task_row(tmp_path, "C-a")
    assert (row["agent_id"], row["capability"]) == ("A05", "code.backend")


def test_plan_records_custom_pattern(tmp_path):
    env = _env(tmp_path)
    plan = tmp_path / "plan.json"
    _write_plan(plan, "code.backend", "A05")
    assert _plan(env, "--plan", str(plan), "--prefix", "C").returncode == 0
    notes = json.loads(_task_row(tmp_path, "C-a")["notes"])
    assert notes["pattern"] == "custom"
    assert re.fullmatch(r"[0-9a-f]{64}", notes.get("plan_sha256", ""))


def test_run_ambiguous_correlation_exit2(tmp_path):
    env = _env(tmp_path)
    corrs = [json.loads(_plan(env, "--brief-text", "billing", "--prefix", p).stdout)["correlation_id"] for p in ("A", "B")]
    r = run_script("swarm_run.py", "--dry-run", "--json", env=env)
    assert r.returncode == 2, r.stdout + r.stderr
    err = json.loads(r.stdout)["error"]
    assert err["code"] == "E-INPUT"
    assert all(c in err["message"] for c in corrs)
    r = run_script("swarm_run.py", "--dry-run", "--json", "--correlation-id", corrs[0], env=env)
    assert r.returncode in (0, 1), r.stdout + r.stderr
    assert json.loads(r.stdout)["correlation_id"] == corrs[0]
    con = sqlite3.connect(tmp_path / ".swarm" / "tasks.db")
    states = {s for (s,) in con.execute("SELECT DISTINCT state FROM tasks WHERE task_id LIKE 'B-%'")}
    con.close()
    assert states == {"PLANNED"}

def test_gate_paths_are_stored_and_absolute_paths_are_rejected(tmp_path):
    env = _env(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"tasks": [
        {"id": "be", "capability": "code.backend", "agent": "A05"},
        {"id": "rev", "capability": "gate.review", "agent": "A09", "depends_on": ["be"],
         "gates": {"gate": "review", "for": ["be"], "paths": ["docs/GOAL.md"]}},
    ]}))
    r = _plan(env, "--plan", str(plan), "--pattern", "custom", "--prefix", "P")
    assert r.returncode == 0, r.stdout + r.stderr
    con = sqlite3.connect(tmp_path / ".swarm" / "tasks.db")
    notes = json.loads(con.execute("SELECT notes FROM tasks WHERE task_id='P-rev'").fetchone()[0])
    con.close()
    assert notes["subject_paths"] == ["docs/GOAL.md"]
    assert notes["gates"] == []
    assert notes["gate_for"] == ["P-be"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tasks": [
        {"id": "rev", "capability": "gate.review", "agent": "A09",
         "gates": {"gate": "review", "for": ["be"], "paths": ["/etc/passwd"]}},
    ]}))
    refused = _plan(env, "--plan", str(bad), "--pattern", "custom", "--prefix", "Q")
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "gates.paths" in refused.stdout + refused.stderr

