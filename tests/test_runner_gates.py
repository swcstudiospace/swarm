"""WR-12 / WR-08: supported headless sessions get no signing values or private references,
and the trusted runner records authoritative signed gates before a gate task can finish."""
import importlib.util
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import run_script

ROOT = Path(__file__).resolve().parent.parent
KEY_VARS = ("SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY")
# inherited vars that would change what the runner or its gate scripts do
_ENV_NOISE = (*KEY_VARS, "SWARM_AGENT_SESSION", "SWARM_CHILD", "SWARM_TASK_ID", "SWARM_CORRELATION_ID",
              "SWARM_DRYRUN_FAIL", "SWARM_RUNTIME", "ANTHROPIC_API_KEY")


def _clean_env(**extra) -> dict:
    return {**{k: v for k, v in os.environ.items() if k not in _ENV_NOISE and not k.startswith("SUBSTRATE_")}, **extra}


def _load_swarm_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    spec = importlib.util.spec_from_file_location("swarm_run_under_test", ROOT / "scripts" / "swarm_run.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))
    spec.loader.exec_module(mod)
    return mod


def _plan(env, prefix="X"):
    r = run_script("orch_plan.py", "--brief-text", "x", "--pattern", "feature", "--prefix", prefix, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def _lease(ts, tid, **notes):
    for s in ("CLAIMED", "IN_PROGRESS"):
        ts.transition(tid, s)
    if notes:
        ts.set_notes(tid, **notes)


def _rows(swarm, task_id=None):
    con = sqlite3.connect(swarm / "tasks.db")
    con.row_factory = sqlite3.Row
    q, a = ("SELECT * FROM verdicts WHERE task_id=? ORDER BY id", (task_id,)) if task_id else ("SELECT * FROM verdicts ORDER BY id", ())
    rows = [dict(r) for r in con.execute(q, a)]
    con.close()
    return rows


def _events(swarm, etype):
    p = swarm / "events.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if f'"{etype}"' in ln] if p.exists() else []


# ---------------------------------------------------------------- WR-12: no key material in agent sessions
@pytest.mark.parametrize("runtime", ["claude", "omp"])
def test_headless_child_env_has_no_keys(tmp_path, monkeypatch, runtime):
    monkeypatch.setenv("SWARM_SIGNING_KEY", "s")
    monkeypatch.setenv("SWARM_ED25519_KEY", "11" * 32)
    monkeypatch.setenv("SWARM_REQUIRE_KEY", "1")
    monkeypatch.setenv("SWARM_ED25519_KEY_FILE", str(tmp_path / "private-test-reference.hex"))
    monkeypatch.setenv("SWARM_TASK_ID", "T-parent")
    monkeypatch.setenv("SWARM_CORRELATION_ID", "corr-parent")
    mod = _load_swarm_run(tmp_path, monkeypatch)
    seen = {}
    # Each supported runtime's own stdout shape: omp streams JSONL, Claude prints one JSON object.
    stdout = (ROOT / "tests" / "fixtures" / "omp_agent_end.jsonl").read_text() if runtime == "omp" else '{"result":"ok"}'
    # ChildGroup patches this module's Popen. The dead-pid spawn has to use the real one, or it re-enters the double.
    real_popen = subprocess.Popen

    class FakePopen:
        """The runner reads the session's pipes while it is alive, so a double has to be a pipe, not `communicate`."""

        def __init__(self, cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw["env"]
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(stdout.encode())
            self.stderr = io.BytesIO()
            self.returncode = 0
            dead = real_popen([sys.executable, "-c", "pass"])
            dead.wait()
            self.pid = dead.pid  # already exited: never pgid 0, which is the test run's own group

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
    args = SimpleNamespace(runtime=runtime, claude_bin="claude", grok_bin="grok", omp_bin="omp",
                           permission_mode="acceptEdits", max_turns=5, model="", allowed_tools="", task_timeout=10)
    mod.run_agent_headless({"slug": "a09-reviewer", "id": "A09"}, "review the thing", tmp_path, args)
    env = seen["env"]
    assert seen["cmd"][0] == runtime
    assert not set(KEY_VARS) & set(env)
    assert str(tmp_path / "private-test-reference.hex") not in " ".join(seen["cmd"])
    assert os.environ["SWARM_ED25519_KEY_FILE"] == str(tmp_path / "private-test-reference.hex")
    assert "SWARM_TASK_ID" not in env and "SWARM_CORRELATION_ID" not in env  # T-06-19: the parent's ids stay with the runner
    assert os.environ["SWARM_TASK_ID"] == "T-parent"
    assert env["SWARM_AGENT_SESSION"] == "1"
    assert env["SWARM_AGENT"] == "a09-reviewer"  # the session identity swarm_gate binds the gate to
    assert env["SWARM_CHILD"] == "1"
    assert os.environ["SWARM_SIGNING_KEY"] == "s"  # the runner keeps its own key
    if runtime == "omp":  # the gate agent's hidden swarm_gate tool is only active when named
        assert {"swarm_gate", "yield"} <= set(seen["cmd"][seen["cmd"].index("--tools") + 1].split(","))


_STUB = r'''#!@PY@
import json, os, re, sys
if sys.argv[1:3] == ["auth", "status"]:
    print('{"loggedIn": true}')
    sys.exit(0)
prompt = sys.stdin.read()
tid = re.search(r'"task_id": "([^"]+)"', prompt).group(1)
with open(@DUMP@, "a") as fh:
    fh.write(json.dumps({"task_id": tid, "has_hmac": "SWARM_SIGNING_KEY" in os.environ,
                         "has_ed": "SWARM_ED25519_KEY" in os.environ,
                         "has_ed_file": "SWARM_ED25519_KEY_FILE" in os.environ,
                         "agent_session": os.environ.get("SWARM_AGENT_SESSION")}) + "\n")
res = {"task_id": tid, "state": "IN_REVIEW", "summary_md": "stub session: ran no script"}
if '"gate": "quality"' in prompt:
    res.update(gate="quality", verdicts={"T-be": {"verdict": "pass", "findings": []}})
print(json.dumps({"result": "done\n```json\n" + json.dumps(res) + "\n```"}))
'''


def test_runner_records_gate_after_session(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "tests").mkdir(parents=True)
    (work / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    dump = tmp_path / "env-dump.jsonl"
    stub = tmp_path / "claude-stub"
    stub.write_text(_STUB.replace("@PY@", sys.executable).replace("@DUMP@", repr(str(dump))))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"tasks": [
        {"id": "be", "capability": "code.backend", "agent": "A05", "gates": ["quality"]},
        {"id": "qa", "capability": "gate.quality", "agent": "A08", "depends_on": ["be"],
         "gates": {"gate": "quality", "for": ["be"]}},
    ]}))
    swarm = tmp_path / ".swarm"
    env = _clean_env(SWARM_DIR=str(swarm), SWARM_SIGNING_KEY="runner-secret",
                     SWARM_ED25519_KEY_FILE=str(tmp_path / "private-test-reference.hex"))
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "orch_plan.py"), "--plan", str(plan), "--prefix", "T",
                        "--repo", str(work), "--json"], capture_output=True, text=True, env=env, cwd=ROOT)
    assert p.returncode == 0, p.stdout + p.stderr
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "swarm_run.py"), "--claude-bin", str(stub),
                        "--runtime", "claude", "--repo", str(work), "--json"],
                       capture_output=True, text=True, env=env, cwd=ROOT, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-1500:]
    assert json.loads(r.stdout)["complete"] is True
    sessions = [json.loads(ln) for ln in dump.read_text().splitlines()]
    assert {s["task_id"] for s in sessions} == {"T-be", "T-qa"}
    assert all(not s["has_hmac"] and not s["has_ed"] and not s["has_ed_file"] and s["agent_session"] == "1" for s in sessions)
    rows = [x for x in _rows(swarm, "T-be") if x["gate"] == "quality"]
    assert rows, "the runner recorded no quality verdict on T-be"
    row = rows[-1]
    assert row["agent_id"] == "A08@local"
    envelope = json.loads(row["envelope_json"])
    assert envelope["payload"]["gate_task"] == "T-qa"
    for k in KEY_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SWARM_SIGNING_KEY", "runner-secret")
    from swarm.gates import validate_verdict
    assert validate_verdict(envelope)["verdict"] == "pass"


_REV_STUB = r'''#!@PY@
import json, re, sys
if sys.argv[1:3] == ["auth", "status"]:
    print('{"loggedIn": true}')
    sys.exit(0)
prompt = sys.stdin.read()
tid = re.search(r'"task_id": "([^"]+)"', prompt).group(1)
if '"gate": "review"' not in prompt:
    res = {"task_id": tid, "state": "IN_REVIEW", "summary_md": "built"}
elif @MODE@ == "crash":
    print(json.dumps({"is_error": True, "result": "API Error: overloaded"}))
    sys.exit(1)
else:
    res = {"task_id": tid, "state": "BLOCKED", "needs": "human-approval: diff touches prod IAM policy"}
print(json.dumps({"result": "done\n```json\n" + json.dumps(res) + "\n```"}))
'''


def _run_stub_swarm(tmp_path, stub_src: str, tasks: list[dict]) -> Path:
    """Plan `tasks` (prefix S, low risk) on an empty repo and run the swarm with `stub_src` as `claude`; the state dir."""
    work = tmp_path / "work"
    work.mkdir()
    stub = tmp_path / "claude-stub"
    stub.write_text(stub_src.replace("@PY@", sys.executable))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"tasks": tasks}))
    swarm = tmp_path / ".swarm"
    env = _clean_env(SWARM_DIR=str(swarm), SWARM_SIGNING_KEY="runner-secret")
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "orch_plan.py"), "--plan", str(plan), "--prefix", "S",
                        "--risk-class", "low", "--repo", str(work), "--json"], capture_output=True, text=True, env=env, cwd=ROOT)
    assert p.returncode == 0, p.stdout + p.stderr
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "swarm_run.py"), "--claude-bin", str(stub),
                        "--runtime", "claude", "--repo", str(work), "--json"],
                       capture_output=True, text=True, env=env, cwd=ROOT, timeout=600)
    assert r.returncode in (0, 1), r.stdout[-2000:] + r.stderr[-1500:]
    return swarm


def _states(swarm) -> dict:
    con = sqlite3.connect(swarm / "tasks.db")
    states = dict(con.execute("SELECT task_id, state FROM tasks"))
    con.close()
    return states


@pytest.mark.parametrize("mode", ["crash", "blocked"])
def test_failed_gate_session_records_no_verdict(tmp_path, mode):
    """CR-04: a review session that crashed or reported BLOCKED must not satisfy the target's review gate."""
    swarm = _run_stub_swarm(tmp_path, _REV_STUB.replace("@MODE@", repr(mode)), [
        {"id": "be", "capability": "code.backend", "agent": "A05", "gates": ["review"]},
        {"id": "rev", "capability": "gate.review", "agent": "A09", "depends_on": ["be"],
         "gates": {"gate": "review", "for": ["be"]}},
    ])
    assert [x for x in _rows(swarm, "S-be") if x["gate"] == "review"] == []
    states = _states(swarm)
    assert states["S-be"] == "IN_REVIEW"
    assert states["S-rev"] == ("ESCALATED" if mode == "crash" else "BLOCKED")


_VERDICTS_STUB = r'''#!@PY@
import json, re, sys
if sys.argv[1:3] == ["auth", "status"]:
    print('{"loggedIn": true}')
    sys.exit(0)
prompt = sys.stdin.read()
tid = re.search(r'"task_id": "([^"]+)"', prompt).group(1)
res = {"task_id": tid, "state": "IN_REVIEW", "summary_md": "done"}
if '"gate": "review"' in prompt:
    res.update(gate="review", verdicts=@VERDICTS@)
print(json.dumps({"result": "done\n```json\n" + json.dumps(res) + "\n```"}))
'''
_BE_FE_REVIEW = [
    {"id": "be", "capability": "code.backend", "agent": "A05", "gates": ["review"]},
    {"id": "fe", "capability": "code.frontend", "agent": "A06", "gates": ["review"]},
    {"id": "rev", "capability": "gate.review", "agent": "A09", "depends_on": ["be", "fe"],
     "gates": {"gate": "review", "for": ["be", "fe"]}},
]


def _review_rows(swarm, tid):
    return [x for x in _rows(swarm, tid) if x["gate"] == "review"]


@pytest.mark.parametrize("severity", ["major", "high"])
def test_review_findings_stay_on_their_target(tmp_path, severity):
    """WR-14: the agent's finding on S-be fails only S-be; an unknown severity counts as major instead of vanishing."""
    bad = {"severity": severity, "kind": "security", "summary": "SQL built by string concat", "location": "be/db.py:3"}
    verdicts = {"S-be": {"verdict": "fail", "findings": [bad]}, "S-fe": {"verdict": "pass", "findings": []}}
    swarm = _run_stub_swarm(tmp_path, _VERDICTS_STUB.replace("@VERDICTS@", repr(verdicts)), _BE_FE_REVIEW)
    be, fe = _review_rows(swarm, "S-be"), _review_rows(swarm, "S-fe")
    assert be and be[0]["verdict"] == "fail"
    assert "SQL built by string concat" in be[0]["findings"]
    assert fe and all(x["verdict"] == "pass" for x in fe), [x["findings"] for x in fe]
    assert _states(swarm)["S-fe"] == "DONE"
    coerced = _events(swarm, "gate.findings.coerced")
    assert [e["payload"]["coerced"] for e in coerced][:1] == ([] if severity == "major" else [[{"from": "high", "to": "major"}]])


def _feedback(swarm, tid) -> list[dict]:
    con = sqlite3.connect(swarm / "tasks.db")
    (notes,) = con.execute("SELECT notes FROM tasks WHERE task_id=?", (tid,)).fetchone()
    con.close()
    return json.loads(notes).get("feedback") or []


@pytest.mark.parametrize("agent_verdict", ["fail", "FAIL", " Failed "])
def test_review_finding_without_severity_fails_closed(tmp_path, agent_verdict):
    """WR-16/WR-17: a finding with no severity under the agent's failing entry (any spelling other than "pass")
    counts as major (not minor), is reported, and becomes agent feedback."""
    verdicts = {"S-be": {"verdict": agent_verdict, "findings": [{"kind": "security", "summary": "migration drops users.email"}]},
                "S-fe": {"verdict": "PASS", "findings": [{"kind": "style", "summary": "naming nit", "severity": None}]}}
    swarm = _run_stub_swarm(tmp_path, _VERDICTS_STUB.replace("@VERDICTS@", repr(verdicts)), _BE_FE_REVIEW)
    be, fe = _review_rows(swarm, "S-be"), _review_rows(swarm, "S-fe")
    assert be and be[0]["verdict"] == "fail", [x["findings"] for x in be]
    assert [f["severity"] for f in json.loads(be[0]["findings"])] == ["major"]
    assert fe and fe[0]["verdict"] == "pass" and [f["severity"] for f in json.loads(fe[0]["findings"])] == ["minor"]
    states = _states(swarm)
    assert states["S-be"] != "DONE" and states["S-fe"] == "DONE"
    assert _events(swarm, "gate.findings.coerced")[0]["payload"]["coerced"] == [
        {"from": "missing", "to": "major"}, {"from": "missing", "to": "minor"}]
    agent_fb = [e for e in _feedback(swarm, "S-be") if e["source"] == "agent"]
    assert agent_fb and agent_fb[0]["findings"][0]["summary"] == "migration drops users.email"
    assert [e for e in _feedback(swarm, "S-fe") if e["source"] == "agent"] == []


@pytest.mark.parametrize("entry", [{"verdict": "fail", "findings": []}, {"verdict": "Rejected"}, {"findings": []},
                                   {"verdict": "request_changes", "findings": []}, {"verdict": "block", "findings": []},
                                   {"verdict": "waive", "findings": []}],
                         ids=["fail-empty", "unknown-no-findings", "missing-verdict", "request_changes", "block", "waive"])
def test_failing_agent_verdict_without_findings_fails_target(tmp_path, entry):
    """IN-15/WR-18: an agent entry outside the pass set (A09's legacy request_changes/block and the human-only waive
    included) that carries no findings gets one synthesized major finding on its target (gate.findings.synthesized)
    instead of recording a silent PASS; an explicit pass is untouched."""
    verdicts = {"S-be": entry, "S-fe": {"verdict": "pass", "findings": []}}
    swarm = _run_stub_swarm(tmp_path, _VERDICTS_STUB.replace("@VERDICTS@", repr(verdicts)), _BE_FE_REVIEW)
    be, fe = _review_rows(swarm, "S-be"), _review_rows(swarm, "S-fe")
    assert be and be[0]["verdict"] == "fail", [x["findings"] for x in be]
    synth = json.loads(be[0]["findings"])
    assert [(f["severity"], f["kind"]) for f in synth] == [("major", "agent-verdict")]
    assert fe and fe[0]["verdict"] == "pass" and fe[0]["findings"] == "[]"
    states = _states(swarm)
    assert states["S-be"] != "DONE" and states["S-fe"] == "DONE"
    ev = _events(swarm, "gate.findings.synthesized")
    assert [(s["key"], s["applied_to"]) for s in ev[0]["payload"]["synthesized"]] == [("S-be", ["S-be"])]
    agent_fb = [e for e in _feedback(swarm, "S-be") if e["source"] == "agent"]
    assert agent_fb and agent_fb[0]["findings"][0]["summary"] == synth[0]["summary"]


@pytest.mark.parametrize("entry, severities", [
    ({"verdict": "approve", "findings": []}, []),
    ({"verdict": " Approve ", "findings": [{"kind": "style", "summary": "naming nit"}]}, ["minor"]),
], ids=["approve-empty", "approve-nit-without-severity"])
def test_approving_agent_verdict_is_a_pass(tmp_path, entry, severities):
    """WR-18: A09's legacy `approve` is a pass like `pass`: a clean approval reaches DONE with no synthesized finding,
    and a severity-less nit under it stays minor."""
    verdicts = {"S-be": entry, "S-fe": {"verdict": "pass", "findings": []}}
    swarm = _run_stub_swarm(tmp_path, _VERDICTS_STUB.replace("@VERDICTS@", repr(verdicts)), _BE_FE_REVIEW)
    be = _review_rows(swarm, "S-be")
    assert be and be[0]["verdict"] == "pass", [x["findings"] for x in be]
    assert [f["severity"] for f in json.loads(be[0]["findings"])] == severities
    assert _states(swarm)["S-be"] == "DONE"
    assert _events(swarm, "gate.findings.synthesized") == []
    assert [e for e in _feedback(swarm, "S-be") if e["source"] == "agent"] == []


def test_review_finding_under_unknown_key_fails_every_target(tmp_path):
    """WR-16: findings filed under a key that is not a gate_for id ("be" for S-be) apply to every target instead of
    vanishing, and the unattributed key is reported."""
    verdicts = {"be": {"verdict": "fail", "findings": [{"severity": "critical", "kind": "security", "summary": "SQL concat"}]},
                "S-fe": {"verdict": "pass", "findings": []}}
    swarm = _run_stub_swarm(tmp_path, _VERDICTS_STUB.replace("@VERDICTS@", repr(verdicts)), _BE_FE_REVIEW)
    for tid in ("S-be", "S-fe"):
        rows = _review_rows(swarm, tid)
        assert rows and rows[0]["verdict"] == "fail" and "SQL concat" in rows[0]["findings"], (tid, rows)
    states = _states(swarm)
    assert states["S-be"] != "DONE" and states["S-fe"] != "DONE"
    ev = _events(swarm, "gate.findings.unattributed")
    assert ev and ev[0]["payload"]["keys"] == ["be"] and ev[0]["payload"]["applied_to"] == ["S-be", "S-fe"]


def test_agent_session_gate_script_records_nothing(swarm_dir):
    from swarm.taskstore import TaskStore
    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    _lease(TaskStore(), "X-qa", dry_run=True)  # leased and flagged exactly as a runner dry-run would
    r = run_script("qa_gate.py", "--dry-run", "--task-id", "X-qa", "--json", env={**env, "SWARM_AGENT_SESSION": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (swarm_dir / "verdicts" / "X-qa.quality.json").exists()
    assert _rows(swarm_dir) == []
    assert any("agent session" in e["payload"]["reason"] for e in _events(swarm_dir, "gate.verdict.unrecorded"))


def test_agent_session_preview_emits_no_dev_key_event(swarm_dir):
    """WR-15: an agent session's key-less gate preview must not raise the security.dev_key misconfiguration signal;
    a gate script signing with the dev key outside a session still does."""
    from swarm.taskstore import TaskStore
    env = _clean_env(SWARM_DIR=str(swarm_dir))
    _plan(env)
    _lease(TaskStore(), "X-qa", dry_run=True)
    gate = [sys.executable, str(ROOT / "scripts" / "qa_gate.py"), "--dry-run", "--task-id", "X-qa", "--json"]
    r = subprocess.run(gate, capture_output=True, text=True, env={**env, "SWARM_AGENT_SESSION": "1"}, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _events(swarm_dir, "gate.verdict.unrecorded")
    assert _events(swarm_dir, "security.dev_key") == []
    r = subprocess.run(gate, capture_output=True, text=True,
                       env={**env, "SWARM_ALLOW_INSECURE_DEV_KEY": "1"}, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert {e["payload"]["source"] for e in _events(swarm_dir, "security.dev_key")} == {"A08@dry"}


# ---------------------------------------------------------------- WR-08: a gate task cannot finish without its script
_GATE_RESULT = {"gate": "quality", "state": "IN_REVIEW"}


def _gate_pair(ts, prefix, *, lease=True):
    """Target <prefix>-be plus quality gate task <prefix>-qa (gate_for [<prefix>-be]), the gate task leased by A01."""
    ts.create(task_id=f"{prefix}-be", correlation_id="c", capability="code.backend", notes={"gates": ["quality"]})
    ts.create(task_id=f"{prefix}-qa", correlation_id="c", capability="gate.quality",
              notes={"gate": "quality", "gate_for": [f"{prefix}-be"], "gates": []})
    if lease:
        for s in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"):
            ts.transition(f"{prefix}-qa", s)
    return {**_GATE_RESULT, "task_id": f"{prefix}-qa", "verdicts": {f"{prefix}-be": {"verdict": "pass", "findings": []}}}


def test_ingest_gate_result_without_script_rejected(tmp_path, swarm_dir):
    from swarm.taskstore import TaskStore
    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    ts = TaskStore()
    _lease(ts, "X-qa")
    f = tmp_path / "r.json"
    f.write_text(json.dumps({**_GATE_RESULT, "task_id": "X-qa", "verdicts": {"X-be": {"verdict": "pass", "findings": []}}}))
    r = run_script("orch_status.py", "--ingest", str(f), "--json", env=env)
    assert r.returncode == 2 and "gate script not run" in r.stdout, r.stdout + r.stderr
    assert ts.get("X-qa")["state"] == "IN_PROGRESS"
    ts.set_notes("X-qa", dry_run=True)
    g = run_script("qa_gate.py", "--dry-run", "--task-id", "X-qa", "--json", env=env)
    assert g.returncode == 0, g.stdout + g.stderr
    r = run_script("orch_status.py", "--ingest", str(f), "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert ts.get("X-qa")["state"] in ("IN_REVIEW", "DONE")


def test_headless_gate_without_rows_fails_attempt(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.results import apply_result
    ts = TaskStore()
    res = _gate_pair(ts, "H")
    events = []
    out = apply_result(ts, ts.get("H-qa"), agent_id="A08", result=res, meta={}, mode="headless",
                       emit=lambda t, p: events.append((t, p)))
    assert out == "FAILED"
    assert ts.history("H-qa")[-1]["reason"].startswith("E-CONTRACT: gate script not run")
    assert [p["mode"] for t, p in events if t == "task.result.rejected"] == ["headless"]


def test_gate_task_without_claimed_row_rejected(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.results import apply_result
    from swarm.verdicts import record_gate_verdicts
    from swarm.errors import SwarmError, ErrorCode
    ts = TaskStore()
    res = _gate_pair(ts, "N", lease=False)
    ts.conn.execute("UPDATE tasks SET state='IN_PROGRESS' WHERE task_id='N-qa'")  # no CLAIMED row in its history
    ts.conn.commit()
    record_gate_verdicts(ts, gate_task_id="N-qa", gate="quality", agent_id="A08@local", findings=[], runs={},
                         correlation_id="c", expires_s=60, emit=lambda *a, **k: None)
    assert ts.latest_verdicts("N-be", verified_only=True)["quality"]["verdict"] == "pass"
    with pytest.raises(SwarmError) as e:
        apply_result(ts, ts.get("N-qa"), agent_id="A08", result=res, meta={}, mode="ingest", emit=lambda *a: None)
    assert e.value.code is ErrorCode.E_CONTRACT and "gate script not run" in str(e.value)
    assert ts.get("N-qa")["state"] == "IN_PROGRESS"


def test_reconcile_escalates_stalled_gate(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.results import reconcile
    ts = TaskStore()
    steps = ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW")
    ts.create(task_id="S-be", correlation_id="c", capability="code.backend", notes={"gates": ["review"]})
    for s in steps:
        ts.transition("S-be", s)
    events = []

    def emit(t, p):
        events.append((t, p))

    reconcile(ts, "c", emit)
    reconcile(ts, "c", emit)
    esc = [p for t, p in events if t == "escalation.request"]
    assert len(esc) == 1
    assert esc[0]["task_id"] == "S-be" and esc[0]["evidence"] == ["review:absent"]
    assert ts.get("S-be")["state"] == "IN_REVIEW"  # A01 or a human decides
    # a live gate task that will issue the gate: waiting, not a stall
    ts.create(task_id="D-be", correlation_id="d", capability="code.backend", notes={"gates": ["review"]})
    for s in steps:
        ts.transition("D-be", s)
    ts.create(task_id="D-rev", correlation_id="d", capability="gate.review",
              notes={"gate": "review", "gate_for": ["D-be"], "gates": []})
    for s in ("VALIDATED", "PLANNED"):
        ts.transition("D-rev", s)
    events.clear()
    reconcile(ts, "d", emit)
    assert [p for t, p in events if t == "escalation.request"] == []


# The controlled dotnet CLI writes actual TRX; executor behavior is not replaced
# with mocked success. Reuse it while exercising the real gate/TaskStore boundary.
from test_qa_gate_dotnet import dotnet_repo  # noqa: E402,F401


@pytest.mark.parametrize("scenario,expected", [
    ({}, "pass"),
    ({"reports": [{"storage_module": "auth.tests", "codebase_module": "Auth.Tests",
                   "outcomes": ["Passed"]}]}, "pass"),
    ({"build_exit": 19}, "fail"),
    ({"test_exit": 9}, "fail"),
    ({"test_exit": 9, "reports": [{"module": "Auth.Tests", "outcomes": ["Passed", "Failed"],
                                  "summary": "Failed"}]}, "fail"),
    ({"reports": []}, "fail"),
    ({"reports": [{"module": "Auth.Tests", "outcomes": ["NotExecuted"]}]}, "fail"),
    ({"reports": [{"module": "Other.Tests", "outcomes": ["Passed"]}]}, "fail"),
])
def test_dotnet_execution_reaches_canonical_quality_authority(
        tmp_path, swarm_dir, monkeypatch, dotnet_repo, scenario, expected):
    """Execution evidence reaches ordinary signed gate handling, never a worker-written verdict."""
    from test_qa_gate_dotnet import configure, load_gate
    from swarm.script_base import Ctx
    from swarm.taskstore import TaskStore

    # conftest supplies an isolated ephemeral signing fixture; no private key,
    # insecure signer, or production TaskStore is used by this behavior test.
    monkeypatch.delenv("SWARM_AGENT_SESSION", raising=False)
    configure(dotnet_repo, **scenario)
    ts = TaskStore()
    _gate_pair(ts, "DN")
    gate = load_gate()
    args = gate.AgentScript("A08", "qa_gate", gate.run, add_args=gate.add_args).parser().parse_args([
        "--task-id", "DN-qa", "--correlation-id", "c", "--root", str(dotnet_repo),
        "--risk-class", "medium", "--dotnet-target", "App.slnf",
        "--dotnet-required-test-module", "Auth.Tests", "--timeout", "10"])
    result = gate.run(args, Ctx("A08", "qa_gate", "DN-qa", "c", False, dotnet_repo))
    assert result["verdict"] == expected
    assert result["runs"]["unit"] == result["runs"]["integration"] == expected
    assert result["executed"][0]["runner"] == "dotnet"
    rows = _rows(swarm_dir, "DN-be")
    assert rows and rows[-1]["verdict"] == expected
    if expected == "fail":
        assert any(finding["severity"] == "major" for finding in result["findings"])
    else:
        assert result["executed"][0]["modules"]["auth.tests"]["executed"] == 1
    if scenario.get("test_exit"):
        execution = result["executed"][0]
        assert execution["returncode"] == scenario["test_exit"]
        assert execution["executed"] > 0 and execution["modules"]["auth.tests"]["executed"] > 0
        assert execution["stages"][-1]["status"] == "fail"
