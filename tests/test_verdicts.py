"""CORE-06: gate scripts are the only verdict writers; targets come from the gate task's notes.gate_for."""
import json
import sqlite3

import pytest

from conftest import run_script


def _plan(env, prefix="X", pattern="feature"):
    r = run_script("orch_plan.py", "--brief-text", "x", "--pattern", pattern, "--prefix", prefix, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


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


def _notes(swarm, tid):
    con = sqlite3.connect(swarm / "tasks.db")
    (n,) = con.execute("SELECT notes FROM tasks WHERE task_id=?", (tid,)).fetchone()
    con.close()
    return json.loads(n or "{}")


def _store_task(swarm, tid, **notes):
    from swarm.taskstore import TaskStore
    ts = TaskStore()
    ts.create(task_id=tid, correlation_id="c", capability="qa.test", notes=notes)
    return ts


def _lease(ts, tid, **notes):
    """A01 lease (up to CLAIMED → IN_PROGRESS from CREATED or PLANNED), plus notes A01 writes at dispatch."""
    steps = ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS")
    state = ts.get(tid)["state"]
    for s in steps[steps.index(state) + 1 if state in steps else 0:]:
        ts.transition(tid, s)
    if notes:
        ts.set_notes(tid, **notes)


def test_gate_script_records_on_gate_for(swarm_dir):
    from swarm.taskstore import TaskStore
    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    targets = _notes(swarm_dir, "X-qa")["gate_for"]
    assert set(targets) == {"X-be", "X-fe", "X-data"}
    _lease(TaskStore(), "X-qa", dry_run=True)  # leased by the runner under --dry-run
    r = run_script("qa_gate.py", "--dry-run", "--task-id", "X-qa", "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    for t in targets:
        rows = _rows(swarm_dir, t)
        assert [(x["gate"], x["verdict"]) for x in rows] == [("quality", "pass")]
        assert json.loads(rows[0]["envelope_json"])["payload"]["dry_run"] is True
    assert _rows(swarm_dir, "X-qa") == []
    env_file = json.loads((swarm_dir / "verdicts" / "X-qa.quality.json").read_text())
    assert env_file["type"] == "gate.verdict" and env_file["payload"]["dry_run"] is True


def test_non_gate_task_id_records_nothing(swarm_dir):
    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    r = run_script("qa_gate.py", "--dry-run", "--task-id", "X-be", "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (swarm_dir / "verdicts" / "X-be.quality.json").exists()
    assert _rows(swarm_dir) == []
    assert _events(swarm_dir, "gate.verdict.unrecorded")


def test_gate_mismatch_e_policy(swarm_dir):
    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    r = run_script("rev_gate.py", "--dry-run", "--task-id", "X-qa", "--json", env=env)
    assert r.returncode == 2 and "E-POLICY" in r.stdout
    assert _rows(swarm_dir) == []


def test_dryrun_fail_inside_script(swarm_dir):
    from swarm.taskstore import TaskStore
    env = {"SWARM_DIR": str(swarm_dir), "SWARM_DRYRUN_FAIL": "X-be:quality"}
    _plan(env)
    _lease(TaskStore(), "X-qa", dry_run=True)
    run_script("qa_gate.py", "--dry-run", "--task-id", "X-qa", "--json", env=env)
    (row,) = _rows(swarm_dir, "X-be")
    assert row["verdict"] == "fail" and [f["id"] for f in json.loads(row["findings"])] == ["SIM-1"]
    assert _rows(swarm_dir, "X-fe")[0]["verdict"] == "pass"
    fb = _notes(swarm_dir, "X-be")["feedback"]
    assert fb[-1]["gate"] == "quality" and fb[-1]["source"] == "script"
    assert "feedback" not in _notes(swarm_dir, "X-fe")


def test_two_gates_same_target_separate_rows(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.verdicts import record_gate_verdicts
    ts = TaskStore()
    ts.create(task_id="T-be", correlation_id="c", capability="code.backend")
    ts.create(task_id="G-q", correlation_id="c", capability="qa.test", notes={"gate": "quality", "gate_for": ["T-be"], "gates": []})
    ts.create(task_id="G-r", correlation_id="c", capability="review.code", notes={"gate": "review", "gate_for": ["T-be"], "gates": []})
    kw = dict(findings=[], runs={}, correlation_id="c", expires_s=60, emit=lambda *a, **k: None)
    _lease(ts, "G-q")
    _lease(ts, "G-r")
    record_gate_verdicts(ts, gate_task_id="G-q", gate="quality", agent_id="A08@local", **kw)
    record_gate_verdicts(ts, gate_task_id="G-r", gate="review", agent_id="A09@local", **kw)
    latest = ts.latest_verdicts("T-be")
    assert set(latest) == {"quality", "review"}
    assert latest["quality"]["agent_id"] == "A08@local" and latest["review"]["agent_id"] == "A09@local"


def test_rerun_supersedes_by_id(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.gates import make_finding
    from swarm.verdicts import record_gate_verdicts
    ts = TaskStore()
    ts.create(task_id="T-be", correlation_id="c", capability="code.backend")
    ts.create(task_id="G-q", correlation_id="c", capability="qa.test", notes={"gate": "quality", "gate_for": ["T-be"], "gates": []})
    kw = dict(gate_task_id="G-q", gate="quality", agent_id="A08@local", runs={}, correlation_id="c", expires_s=60,
              emit=lambda *a, **k: None)
    _lease(ts, "G-q")
    record_gate_verdicts(ts, findings=[make_finding("F", "major", "functional", "x")], **kw)
    record_gate_verdicts(ts, findings=[], **kw)
    assert ts.latest_verdicts("T-be")["quality"]["verdict"] == "pass"


def test_empty_gate_for_records_nothing(swarm_dir):
    env = {"SWARM_DIR": str(swarm_dir)}
    _store_task(swarm_dir, "G-empty", gate="quality", gate_for=[], gates=[])
    r = run_script("qa_gate.py", "--dry-run", "--task-id", "G-empty", "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rows(swarm_dir) == []
    assert _events(swarm_dir, "gate.verdict.unrecorded")


def test_record_verdict_binds_envelope_task(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.gates import make_verdict
    from swarm.errors import SwarmError
    ts = TaskStore()
    ts.create(task_id="T-a", correlation_id="c", capability="code.backend")
    ts.create(task_id="T-b", correlation_id="c", capability="code.backend")
    env = make_verdict(gate="review", task_id="T-a", agent_id="A09@local", correlation_id="c")
    with pytest.raises(SwarmError) as e:
        ts.record_verdict("T-b", env)
    assert e.value.code.value == "E-CONTRACT"


def test_runner_inserts_no_verdicts(swarm_dir):
    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    r = run_script("swarm_run.py", "--dry-run", "--json", env=env)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-800:]
    con = sqlite3.connect(swarm_dir / "tasks.db")
    gates = {tid: json.loads(n) for tid, n in con.execute("SELECT task_id, notes FROM tasks")}
    con.close()
    gate_tasks = {t: n for t, n in gates.items() if n.get("gate")}
    rows = _rows(swarm_dir)
    assert rows and {x["agent_id"] for x in rows} <= {"A08@dry", "A09@dry", "A10@dry", "A12@dry"}
    assert len(rows) == sum(len(n["gate_for"]) for n in gate_tasks.values())
    for x in rows:
        assert any(x["task_id"] in n["gate_for"] and n["gate"] == x["gate"] for n in gate_tasks.values())
    for gid, n in gate_tasks.items():
        payload = json.loads((swarm_dir / "verdicts" / f"{gid}.{n['gate']}.json").read_text())["payload"]
        assert payload["task_id"] == gid


def test_ingest_gate_result_no_rows(tmp_path, swarm_dir):
    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    targets = _notes(swarm_dir, "X-qa")["gate_for"]
    from swarm.taskstore import TaskStore
    ts = TaskStore()
    _lease(ts, "X-qa", dry_run=True)  # A01 lease; ingest no longer auto-claims a task with unmet deps
    g = run_script("qa_gate.py", "--dry-run", "--task-id", "X-qa", "--json", env=env)  # the script ran in the lease
    assert g.returncode == 0, g.stdout + g.stderr
    fnd = [{"id": "Q-1", "severity": "major", "kind": "functional", "summary": "broken"}]
    res = {"task_id": "X-qa", "gate": "quality", "state": "IN_REVIEW",
           "verdicts": {targets[0]: {"verdict": "fail", "findings": fnd}, targets[1]: {"verdict": "pass", "findings": []},
                        "X-rel": {"verdict": "pass", "findings": []}}}
    f = tmp_path / "r.json"
    f.write_text(json.dumps(res))
    before = _rows(swarm_dir)
    r = run_script("orch_status.py", "--ingest", str(f), "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rows(swarm_dir) == before  # the agent's verdicts{} adds no rows
    assert _notes(swarm_dir, targets[0])["feedback"] == [{"gate": "quality", "source": "agent", "findings": fnd}]
    assert "feedback" not in _notes(swarm_dir, targets[1])
    assert _notes(swarm_dir, "X-qa")["result"]["verdicts"] == res["verdicts"]


# ---------------------------------------------------------------- CORE-07 / D-14: verified verdict rows
def _in_review(ts, tid, gates=("review",)):
    ts.create(task_id=tid, correlation_id="c", capability="code.backend", notes={"gates": list(gates)})
    for s in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW"):
        ts.transition(tid, s)


def _raw_row(ts, tid, gate, verdict, envelope_json=None):
    import time
    ts.conn.execute("INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts, envelope_json)"
                    " VALUES (?,?,?,?,?,?,?,?)", (tid, gate, verdict, "A09", "[]", time.time() + 999, time.time(), envelope_json))
    ts.conn.commit()


def test_migration_idempotent_legacy_rows_missing(swarm_dir):
    from swarm.taskstore import TaskStore
    swarm_dir.mkdir(parents=True, exist_ok=True)
    db = swarm_dir / "tasks.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE verdicts (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, gate TEXT, verdict TEXT,"
                " agent_id TEXT, findings TEXT, expires_at REAL, ts REAL)")
    con.commit()
    con.close()
    ts = TaskStore(db)
    ts.conn.close()
    ts = TaskStore(db)
    cols = [r[1] for r in ts.conn.execute("PRAGMA table_info(verdicts)")]
    assert "envelope_json" in cols and "sig" in cols
    assert not (swarm_dir / "tasks.db.pre-v2").exists()  # option-a: no backup
    _in_review(ts, "L-1")
    ts.conn.execute("INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts)"
                    " VALUES ('L-1','review','pass','A09','[]',9e12,9e9)")
    ts.conn.commit()
    assert ts.missing_gates("L-1") == ["review"]
    assert ts.missing_gate_reasons("L-1") == {"review": "unsigned"}


def test_forged_or_unsigned_verdict_never_approves(swarm_dir, monkeypatch):
    from swarm.taskstore import TaskStore
    from swarm.gates import make_verdict
    from swarm.errors import SwarmError
    ts = TaskStore()
    # (a) unsigned legacy-style row
    _in_review(ts, "F-a")
    _raw_row(ts, "F-a", "review", "pass")
    # (b) payload.verdict flipped fail→pass after signing, row edited to match
    _in_review(ts, "F-b")
    ts.record_verdict("F-b", make_verdict(gate="review", task_id="F-b", agent_id="A09", verdict="fail", correlation_id="c"))
    (eid, ej) = ts.conn.execute("SELECT id, envelope_json FROM verdicts WHERE task_id='F-b'").fetchone()
    env = json.loads(ej)
    env["payload"]["verdict"] = "pass"
    ts.conn.execute("UPDATE verdicts SET verdict='pass', envelope_json=? WHERE id=?", (json.dumps(env), eid))
    ts.conn.commit()
    # (c) signed with k1, verified with k2
    _in_review(ts, "F-c")
    monkeypatch.setenv("SWARM_SIGNING_KEY", "k1")
    ts.record_verdict("F-c", make_verdict(gate="review", task_id="F-c", agent_id="A09", correlation_id="c"))
    monkeypatch.setenv("SWARM_SIGNING_KEY", "k2")
    for tid in ("F-a", "F-b", "F-c"):
        with pytest.raises(SwarmError) as e:
            ts.transition(tid, "APPROVED")
        assert e.value.code.value == "E-POLICY"
    reasons = []
    for tid in ("F-a", "F-b", "F-c"):
        r = run_script("orch_status.py", "--history", tid, "--json",
                       env={"SWARM_DIR": str(swarm_dir), "SWARM_SIGNING_KEY": "k2"})
        assert r.returncode == 0, r.stdout + r.stderr
        reasons.append(json.loads(r.stdout)["missing_gate_reasons"]["review"])
    assert reasons == ["unsigned", "bad-sig", "bad-sig"]


def test_row_verdict_mismatch(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.gates import make_verdict
    ts = TaskStore()
    _in_review(ts, "M-1")
    ts.record_verdict("M-1", make_verdict(gate="review", task_id="M-1", agent_id="A09", verdict="fail", correlation_id="c"))
    ts.conn.execute("UPDATE verdicts SET verdict='pass' WHERE task_id='M-1'")
    ts.conn.commit()
    assert ts.missing_gate_reasons("M-1") == {"review": "mismatch"}


def test_expiry_boundary(swarm_dir, monkeypatch):
    import time
    from swarm.taskstore import TaskStore
    from swarm.gates import make_verdict
    ts = TaskStore()
    _in_review(ts, "E-1")
    env = make_verdict(gate="review", task_id="E-1", agent_id="A09", expires_s=10, correlation_id="c")
    ts.record_verdict("E-1", env)
    assert ts.missing_gate_reasons("E-1") == {}
    boundary = env["payload"]["issued_at"] + 10
    monkeypatch.setattr(time, "time", lambda: boundary)
    assert ts.missing_gate_reasons("E-1") == {"review": "expired"}


def test_absent_and_no_required_gates(swarm_dir):
    from swarm.taskstore import TaskStore
    ts = TaskStore()
    _in_review(ts, "N-1")
    assert ts.missing_gate_reasons("N-1") == {"review": "absent"}
    _in_review(ts, "N-2", gates=())
    assert ts.missing_gate_reasons("N-2") == {}


def test_forged_fail_does_not_force_rework(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.results import reconcile
    ts = TaskStore()
    _in_review(ts, "R-1")
    _raw_row(ts, "R-1", "review", "fail")
    reconcile(ts, "c", lambda *a, **k: None)
    assert ts.get("R-1")["state"] == "IN_REVIEW"


def test_dev_key_event_and_require_key(swarm_dir, monkeypatch):
    from swarm.taskstore import TaskStore
    from swarm.gates import make_verdict
    from swarm.errors import SwarmError
    monkeypatch.delenv("SWARM_SIGNING_KEY", raising=False)
    monkeypatch.delenv("SWARM_ED25519_KEY", raising=False)
    monkeypatch.setenv("SWARM_ALLOW_INSECURE_DEV_KEY", "1")
    ts = TaskStore()
    _in_review(ts, "K-1")
    ts.record_verdict("K-1", make_verdict(gate="review", task_id="K-1", agent_id="A09", correlation_id="c"))
    ev = _events(swarm_dir, "security.dev_key")
    assert ev and ev[-1]["payload"] == {"msg_type": "gate.verdict", "source": "A09"}
    monkeypatch.setenv("SWARM_REQUIRE_KEY", "1")
    with pytest.raises(SwarmError, match="SWARM_REQUIRE_KEY=1 but no signing key configured"):
        ts.transition("K-1", "APPROVED")
    monkeypatch.setenv("SWARM_SIGNING_KEY", "real")
    n = len(_events(swarm_dir, "security.dev_key"))
    ts.record_verdict("K-1", make_verdict(gate="review", task_id="K-1", agent_id="A09", correlation_id="c"))
    assert len(_events(swarm_dir, "security.dev_key")) == n
    assert ts.transition("K-1", "APPROVED")["state"] == "APPROVED"


# ---------------------------------------------------------------- WR-04 / D-14: verdicts bound to time and correlation
def test_pre_rework_verdict_replay_is_stale(swarm_dir):
    import time
    from swarm.taskstore import TaskStore
    from swarm.gates import make_verdict, make_finding
    from swarm.errors import SwarmError
    ts = TaskStore()
    _in_review(ts, "P-1", gates=("review", "quality"))
    env_r = make_verdict(gate="review", task_id="P-1", agent_id="A09", correlation_id="c")
    ts.record_verdict("P-1", env_r)
    ts.record_verdict("P-1", make_verdict(gate="quality", task_id="P-1", agent_id="A08", correlation_id="c",
                                          findings=[make_finding("Q-1", "major", "functional", "broken")]))
    for s in ("CHANGES_REQUESTED", "IN_PROGRESS", "IN_REVIEW"):
        ts.transition("P-1", s)
    time.sleep(0.01)  # verdicts_since is rework time + 1 ms; the fresh quality pass is issued after it
    ts.record_verdict("P-1", env_r)  # the pre-rework pass, re-inserted after the rework
    ts.record_verdict("P-1", make_verdict(gate="quality", task_id="P-1", agent_id="A08", correlation_id="c"))
    assert ts.missing_gate_reasons("P-1") == {"review": "stale"}
    with pytest.raises(SwarmError) as e:
        ts.transition("P-1", "APPROVED")
    assert e.value.code.value == "E-POLICY"


def test_cross_correlation_envelope_mismatch(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.gates import make_verdict
    ts = TaskStore()
    _in_review(ts, "Q-1")
    ts.record_verdict("Q-1", make_verdict(gate="review", task_id="Q-1", agent_id="A09", correlation_id="other"))
    assert ts.missing_gate_reasons("Q-1") == {"review": "mismatch"}


def test_gate_script_signs_target_correlation(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.verdicts import record_gate_verdicts
    _plan({"SWARM_DIR": str(swarm_dir)})
    ts = TaskStore()
    _lease(ts, "X-qa")
    corr = ts.get("X-be")["correlation_id"]
    recorded = record_gate_verdicts(ts, gate_task_id="X-qa", gate="quality", agent_id="A08@local", findings=[],
                                    runs={}, correlation_id=None, expires_s=60, emit=lambda *a, **k: None)
    assert set(recorded) == {"X-be", "X-fe", "X-data"}
    for t in recorded:
        (row,) = _rows(swarm_dir, t)
        assert json.loads(row["envelope_json"])["correlation_id"] == corr
    assert "quality" not in ts.missing_gate_reasons("X-be")


@pytest.mark.parametrize("source", ["flag", "env"])
def test_gate_script_foreign_correlation_e_policy(swarm_dir, source):
    from swarm.taskstore import TaskStore
    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    _lease(TaskStore(), "X-qa", dry_run=True)  # leased and runner-flagged: only the correlation can refuse it
    if source == "env":  # a stale export, no --correlation-id flag
        env["SWARM_CORRELATION_ID"] = "not-this-plan"
    flag = ["--correlation-id", "not-this-plan"] if source == "flag" else []
    r = run_script("qa_gate.py", "--dry-run", "--task-id", "X-qa", *flag, "--json", env=env)
    assert r.returncode == 2 and "E-POLICY" in r.stdout, r.stdout + r.stderr
    assert "SWARM_CORRELATION_ID" in r.stdout
    assert _rows(swarm_dir) == []


# ---------------------------------------------------------------- CR-02: dry-run verdicts count only inside a runner dry-run
def test_dry_run_gate_requires_leased_runner_task(swarm_dir):
    from swarm.taskstore import TaskStore
    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    cmd = ("qa_gate.py", "--dry-run", "--task-id", "X-qa", "--json")
    # (a) X-qa is PLANNED, not leased: refused before any write
    r = run_script(*cmd, env=env)
    assert r.returncode == 2 and "E-POLICY" in r.stdout, r.stdout + r.stderr
    assert _rows(swarm_dir) == []
    # (b) leased, but the runner did not flag it as a dry-run: envelope file only
    ts = TaskStore()
    _lease(ts, "X-qa")
    r = run_script(*cmd, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rows(swarm_dir) == []
    assert any("dry-run" in e["payload"]["reason"] for e in _events(swarm_dir, "gate.verdict.unrecorded"))
    # (c) runner-flagged gate task: rows are signed dry-run and do not count on a target outside the dry-run
    ts.set_notes("X-qa", dry_run=True)
    r = run_script(*cmd, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    for t in ("X-be", "X-fe", "X-data"):
        (row,) = _rows(swarm_dir, t)
        assert json.loads(row["envelope_json"])["payload"]["dry_run"] is True
    assert ts.missing_gate_reasons("X-be")["quality"] == "dry-run"
    # (d) the target is part of the runner dry-run too: the dry-run row counts
    ts.set_notes("X-be", dry_run=True)
    assert "quality" not in ts.missing_gate_reasons("X-be")


def test_release_dry_run_honours_freeze(swarm_dir):
    from swarm.taskstore import TaskStore
    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    freeze = swarm_dir / "release.freeze"
    freeze.write_text(json.dumps({"reason": "INC-1"}))
    _lease(TaskStore(), "X-rel", dry_run=True)
    r = run_script("rel_plan.py", "--dry-run", "--task-id", "X-rel", "--json", env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["verdict"] == "fail" and out["status"] == "fail" and out["frozen"] is True
    (row,) = _rows(swarm_dir, "X-be")
    assert row["verdict"] == "fail"
    assert [(f["kind"], f["severity"]) for f in json.loads(row["findings"])] == [("freeze", "blocker")]
    assert freeze.exists()


@pytest.mark.parametrize("script,gate", [("qa_gate.py", "quality"), ("rev_gate.py", "review"),
                                         ("sec_gate.py", "security"), ("rel_plan.py", "release")])
def test_path_task_id_rejected_without_writing(swarm_dir, tmp_path, script, gate):
    """CR-01: a task id is a file name under verdicts/, so a traversal or absolute id is E-INPUT and writes nothing."""
    (tmp_path / "out").mkdir()
    for bad, escaped in [("../../esc", tmp_path / f"esc.{gate}.json"),
                         (str(tmp_path / "out" / "evil"), tmp_path / "out" / f"evil.{gate}.json"),
                         ("a/b", swarm_dir / "verdicts" / "a" / f"b.{gate}.json"), ("-x", None), ("a\x01", None)]:
        r = run_script(script, f"--task-id={bad}", "--dry-run", "--json")
        assert r.returncode == 2, r.stdout + r.stderr
        assert json.loads(r.stdout)["error"]["code"] == "E-INPUT"
        assert escaped is None or not escaped.exists()
    assert not list((swarm_dir / "verdicts").glob("**/*")) if (swarm_dir / "verdicts").exists() else True
    assert not list(tmp_path.glob(f"**/*.{gate}.json"))


def test_release_id_is_not_a_path(swarm_dir, tmp_path):
    r = run_script("rel_plan.py", "--task-ids=T-x", "--release-id=../../esc", "--json")
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "E-INPUT"
    assert not list(tmp_path.glob("**/esc.plan.json"))


@pytest.mark.parametrize("script", ["orch_plan.py", "rev_gate.py", "rel_plan.py"])
def test_path_correlation_id_rejected_without_writing(swarm_dir, tmp_path, script):
    """WR-05: a correlation id names plans/<corr>.json, so a traversal or absolute id (flag or env) is E-INPUT."""
    (tmp_path / "out").mkdir()
    args = ["--brief-text=x", "--pattern=hotfix"] if script == "orch_plan.py" else ["--dry-run"]
    for bad in ["../../pwn", str(tmp_path / "out" / "pwn"), "a/b", "-x"]:
        for flag, env in [([f"--correlation-id={bad}"], None), ([], {"SWARM_CORRELATION_ID": bad})]:
            r = run_script(script, *args, *flag, "--json", env=env)
            assert r.returncode == 2, r.stdout + r.stderr
            assert json.loads(r.stdout)["error"]["code"] == "E-INPUT"
    assert not [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix == ".json"]


def test_generated_correlation_ids_pass_the_id_rule(swarm_dir):
    """uuid4 (orch_plan's default) and an explicit uuid round-trip: plan, then gate and status by that correlation."""
    import uuid
    from swarm.script_base import check_task_id
    for _ in range(50):
        check_task_id(str(uuid.uuid4()), "correlation id")
    r = run_script("orch_plan.py", "--brief-text=x", "--pattern=hotfix", "--prefix=U", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    corr = json.loads(r.stdout)["correlation_id"]
    assert (swarm_dir / "plans" / f"{corr}.json").exists()
    r = run_script("orch_plan.py", "--brief-text=x", "--pattern=hotfix", "--prefix=U", f"--correlation-id={corr}", "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["reused"] is True, r.stdout + r.stderr
    r = run_script("orch_status.py", f"--correlation-id={corr}", "--json")
    assert r.returncode == 0, r.stdout + r.stderr


def test_empty_id_env_means_unset_but_empty_flag_is_rejected(swarm_dir):
    """WR-06: an exported-but-empty SWARM_TASK_ID / SWARM_CORRELATION_ID behaves as unset."""
    _plan({}, prefix="E")
    r = run_script("orch_status.py", "--json", env={"SWARM_CORRELATION_ID": "", "SWARM_TASK_ID": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    assert {t["task_id"] for t in json.loads(r.stdout)["tasks"]} >= {"E-rev"}
    for flag in ("--correlation-id=", "--task-id="):
        r = run_script("orch_status.py", flag, "--json")
        assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "E-INPUT", r.stdout + r.stderr


# ---------------------------------------------------------------- P1-19: severities and verdicts are closed sets
def test_make_verdict_rejects_unknown_severity(monkeypatch):
    from swarm.errors import SwarmError
    from swarm.gates import make_verdict
    monkeypatch.setenv("SWARM_SIGNING_KEY", "k")
    bogus = {"id": "F-1", "severity": "catastrophic", "kind": "functional", "summary": "x", "evidence": "", "ac_ref": None,
             "owner_suggestion": None, "location": None}
    with pytest.raises(SwarmError) as e:
        make_verdict(gate="review", task_id="T-1", agent_id="A09", findings=[bogus], audit=False)
    assert e.value.code.value == "E-CONTRACT"


@pytest.mark.xfail(strict=True, reason="BLOCKED-PIN P1-19 validate half: verdict membership lives in the pinned Rust mirror crates/gsd-route/src/gate_facts.rs parse_envelope")
def test_validate_verdict_rejects_unknown_verdict(monkeypatch):
    from swarm.envelope import build_envelope, sign_envelope
    from swarm.errors import SwarmError
    from swarm.gates import validate_verdict
    monkeypatch.setenv("SWARM_SIGNING_KEY", "k")
    payload = {"gate": "review", "task_id": "T-1", "verdict": "maybe", "findings": [], "runs": {}, "expires_s": 86400,
               "issued_at": 1000.0}
    env = sign_envelope(build_envelope(source="A09", target="A01", msg_type="gate.verdict", payload=payload,
                                       correlation_id="C-1", priority="P1"), audit=False)
    with pytest.raises(SwarmError) as e:
        validate_verdict(env)
    assert e.value.code.value == "E-CONTRACT"
