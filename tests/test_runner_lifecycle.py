"""T-06-26 / T-06-27: a killed runner does not leave tasks IN_PROGRESS forever, and an omp extension that
fails to load does not get to run — or to be retried — unguarded."""
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path

from conftest import ROOT, omp_calls, run_script, stub_omp

from swarm.taskstore import TaskState, TaskStore

RESULT = {"state": "IN_REVIEW", "outputs": [{"kind": "code.backend", "uri": "file://x", "version": "1", "digest": ""}],
          "metrics": {}, "summary_md": "ok"}
ONE = {"tasks": [{"id": "one", "capability": "code.backend", "agent": "A05", "title": "one", "depends_on": [], "gates": []}]}
TWO = {"tasks": [
    {"id": "a", "capability": "code.backend", "agent": "A05", "title": "a", "depends_on": [], "gates": []},
    {"id": "b", "capability": "code.backend", "agent": "A05", "title": "b", "depends_on": [], "gates": []},
]}


def _runner():
    spec = importlib.util.spec_from_file_location("swarm_run_lifecycle_under_test", ROOT / "scripts" / "swarm_run.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _env(swarm, **extra) -> dict:
    # Test-owned values only. run_script's scrubbed base already supplies the platform vars
    # (PATH, HOME, ...) and the ephemeral-signing-key fixture supplies the key, so neither is
    # copied here; anything else the child needs rides in explicitly via extra. Host vars
    # (SUBSTRATE_* tokens, proxies, SWARM_* session vars) never pass.
    return {"SWARM_DIR": str(swarm), **extra}


def _plan(tmp_path, env, plan=ONE) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    spec = tmp_path / "plan.json"
    spec.write_text(json.dumps(plan))
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


def _store_with(db: Path, tid: str = "T-one", corr: str = "c1") -> TaskStore:
    store = TaskStore(db)
    store.create(task_id=tid, correlation_id=corr, capability="code.backend", agent_id="A05", title=tid)
    for state in (TaskState.VALIDATED, TaskState.PLANNED, TaskState.CLAIMED, TaskState.IN_PROGRESS):
        store.transition(tid, state, reason="setup")
    return store


def test_stderr_scan_spans_chunks_and_ignores_other_lines():
    mod = _runner()
    scan = mod.StderrScan()
    assert scan.feed("noise\n\x1b[31mFail") is None
    found = scan.feed("ed to load extension pkg/src/index.ts: boom\x1b[0m\n")
    assert found is not None and found.startswith("Failed to load extension")
    assert scan.feed("Failed to load extension again\n") == found  # the first match stands

    quiet = mod.StderrScan()
    assert quiet.feed("warning: failed to load something else\n") is None
    assert quiet.feed("") is None


def test_recover_stranded_only_past_the_deadline(tmp_path):
    mod = _runner()
    store = _store_with(tmp_path / "tasks.db")
    store.set_notes("T-one", running=100.0, running_deadline=1_000.0)
    events: list[tuple] = []
    assert mod.recover_stranded(store, "c1", now=999.0, emit=lambda *a: events.append(a)) == []
    assert store.get("T-one")["state"] == "IN_PROGRESS"
    assert events == []

    got = mod.recover_stranded(store, "c1", now=1_000.0, emit=lambda *a: events.append(a))
    assert got == ["T-one"]
    task = store.get("T-one")
    assert task["state"] == "FAILED"
    assert task["notes_json"]["running"] is None and task["notes_json"]["running_deadline"] is None
    assert events[0][0] == "task.stranded"
    reasons = [h["reason"] for h in store.history("T-one") if h["to_state"] == "FAILED"]
    assert reasons == ["E-TIMEOUT: stranded, the runner left it IN_PROGRESS past its deadline"]


def test_recover_stranded_legacy_mark_waits_six_hours(tmp_path):
    mod = _runner()
    store = _store_with(tmp_path / "tasks.db")
    store.set_notes("T-one", running=1_000.0)  # no deadline: a runner from before this field
    stale = mod.STRANDED_STALE_S
    assert mod.recover_stranded(store, "c1", now=1_000.0 + stale - 1) == []
    assert store.get("T-one")["state"] == "IN_PROGRESS"
    assert mod.recover_stranded(store, "c1", now=1_000.0 + stale) == ["T-one"]
    assert store.get("T-one")["state"] == "FAILED"


def test_recover_stranded_skips_live_rework_other_plans_and_bools(tmp_path):
    mod = _runner()
    store = _store_with(tmp_path / "tasks.db")
    store.set_notes("T-one", running=None)  # rework sits IN_PROGRESS with no running mark
    other = TaskStore(tmp_path / "tasks.db")
    other.create(task_id="T-two", correlation_id="c2", capability="code.backend", agent_id="A05", title="two")
    for state in (TaskState.VALIDATED, TaskState.PLANNED, TaskState.CLAIMED, TaskState.IN_PROGRESS):
        other.transition("T-two", state, reason="setup")
    other.set_notes("T-two", running=1.0, running_deadline=2.0)
    flagged = TaskStore(tmp_path / "tasks.db")
    flagged.create(task_id="T-three", correlation_id="c1", capability="code.backend", agent_id="A05", title="three")
    for state in (TaskState.VALIDATED, TaskState.PLANNED, TaskState.CLAIMED, TaskState.IN_PROGRESS):
        flagged.transition("T-three", state, reason="setup")
    flagged.set_notes("T-three", running=True, running_deadline=True)
    assert mod.recover_stranded(store, "c1", now=10_000.0) == []
    assert store.get("T-one")["state"] == "IN_PROGRESS"
    assert other.get("T-two")["state"] == "IN_PROGRESS"  # a different correlation
    assert flagged.get("T-three")["state"] == "IN_PROGRESS"


def _alive(pid: int) -> bool:
    """False once the pid is gone or is a zombie. `os.kill(pid, 0)` still succeeds for state Z."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def test_gate_dispatch_deadline_covers_session_and_script(tmp_path, monkeypatch):
    """A gate task records running_deadline as 2 * task_timeout + the 30s grace: the session, then the gate script."""
    mod = _runner()
    db = tmp_path / "tasks.db"
    store = _store_with(db, tid="T-rev")
    store.set_notes("T-rev", gate="review", gate_for=["T-be"])
    task = store.get("T-rev")
    seen = {}

    def headless(agent, prompt, repo, args, on_session=None):
        notes = TaskStore(db).get("T-rev")["notes_json"]
        seen["span"] = notes["running_deadline"] - notes["running"]
        raise mod.AgentTimeout(["omp"], args.task_timeout, "", {"timed_out": True})

    monkeypatch.setattr(mod, "assignment_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(mod, "run_agent_headless", headless)

    class Ctx:
        def emit(self, *args, **kwargs):
            return None

    mod._execute_one(db, task, {"id": "A09", "slug": "a09-reviewer"},
                     type("A", (), {"task_timeout": 90, "dry_run": False})(), Ctx(), tmp_path, None, None)
    assert abs(seen["span"] - (2 * 90 + 30)) < 1e-6


def test_recover_stranded_leaves_a_restarted_attempt(tmp_path, monkeypatch):
    """The attempt can change after the list and before the write. The fresh IN_PROGRESS row keeps its deadline."""
    mod = _runner()
    store = _store_with(tmp_path / "tasks.db")
    store.set_notes("T-one", running=100.0, running_deadline=1_000.0)
    listed = store.get("T-one")["attempt"]
    fresh_deadline = 50_000.0
    original = store.get

    def get_once(task_id):
        store.get = original
        row = original(task_id)
        notes = dict(row["notes_json"])
        notes["running"] = 40_000.0
        notes["running_deadline"] = fresh_deadline
        store.conn.execute(
            "UPDATE tasks SET attempt=?, notes=? WHERE task_id=?",
            (row["attempt"] + 1, json.dumps(notes), task_id),
        )
        return original(task_id)

    monkeypatch.setattr(store, "get", get_once)
    assert mod.recover_stranded(store, "c1", now=1_000.0) == []
    fresh = original("T-one")
    assert fresh["state"] == "IN_PROGRESS"
    assert fresh["attempt"] == listed + 1
    assert fresh["notes_json"]["running"] == 40_000.0
    assert fresh["notes_json"]["running_deadline"] == fresh_deadline


def test_exited_leader_with_a_live_tool_is_a_timeout(tmp_path, monkeypatch):
    """The leader exits immediately; a child keeps stdout open. That is a task timeout, and the child is reaped."""
    mod = _runner()
    pidfile = tmp_path / "tool.pid"
    script = tmp_path / "leader.py"
    script.write_text(
        "import os, signal, sys, time\n"
        "r, w = os.pipe()\n"
        "if os.fork() == 0:\n"
        "    os.close(r)\n"
        "    signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "    os.write(w, b'1')\n"
        "    os.close(w)\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "os.close(w)\n"
        "os.read(r, 1)\n"
        "os._exit(0)\n"
    )
    monkeypatch.setattr(
        mod, "headless_command",
        lambda runtime, agent, repo, sdir, args: ([sys.executable, str(script)], dict(os.environ), repo),
    )
    args = type("A", (), {"runtime": "claude", "task_timeout": 1})()
    pid = 0
    try:
        try:
            mod.run_agent_headless({"slug": "a05-backend", "id": "A05"}, "go", tmp_path, args)
        except mod.AgentTimeout:
            pass
        else:
            raise AssertionError("a tool holding stdout after the leader exited returned a result")
        assert pidfile.is_file()
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.05)
        assert not _alive(pid)
        assert not mod._SESSIONS
    finally:
        if pid and _alive(pid):
            os.kill(pid, 9)


def test_dispatch_records_a_deadline_and_clears_it(tmp_path):
    swarm = tmp_path / ".swarm"
    env = _env(swarm)
    work = _plan(tmp_path, env)
    real = stub_omp(tmp_path, RESULT)
    snap = tmp_path / "notes.json"
    wrapper = _executable(tmp_path / "omp-bin" / "omp-snap", f"""#!{sys.executable}
import json, os, sqlite3, sys
con = sqlite3.connect(os.environ["SWARM_DIR"] + "/tasks.db")
rows = [{{"task_id": tid, "notes": notes}} for tid, notes in con.execute("SELECT task_id, notes FROM tasks")]
con.close()
open({str(snap)!r}, "w").write(json.dumps(rows))
os.execv({str(real)!r}, sys.argv)
""")
    r = run_script("swarm_run.py", "--runtime", "omp", "--omp-bin", str(wrapper), "--repo", str(work),
                   "--once", "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = json.loads(snap.read_text())
    notes = json.loads(seen[0]["notes"])
    assert abs((notes["running_deadline"] - notes["running"]) - (1800 + 30)) < 1e-6
    finished = _notes(swarm, "T-one")
    assert finished.get("running") is None and finished.get("running_deadline") is None
    assert _states(swarm)["T-one"] == "DONE"


def _strand(swarm: Path, tid: str, *, running: float, deadline):
    store = TaskStore(swarm / "tasks.db")
    store.transition(tid, TaskState.CLAIMED, reason="setup")
    store.transition(tid, TaskState.IN_PROGRESS, reason="setup")
    store.set_notes(tid, running=running, running_deadline=deadline)
    store.conn.close()


def test_runner_recovers_a_stranded_task_and_finishes_it(tmp_path):
    swarm = tmp_path / ".swarm"
    env = _env(swarm)
    work = _plan(tmp_path, env)
    _strand(swarm, "T-one", running=time.time() - 100, deadline=time.time() - 1)
    stub = stub_omp(tmp_path, RESULT)
    r = run_script("swarm_run.py", "--runtime", "omp", "--omp-bin", str(stub), "--repo", str(work), "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _states(swarm)["T-one"] == "DONE"
    assert any(reason.startswith("E-TIMEOUT: stranded") for reason in _failed(swarm, "T-one"))
    assert len(omp_calls(stub)) == 1


def test_runner_leaves_a_live_deadline_and_a_dry_run_alone(tmp_path):
    swarm = tmp_path / ".swarm"
    env = _env(swarm)
    work = _plan(tmp_path, env)
    deadline = time.time() + 3600
    _strand(swarm, "T-one", running=time.time(), deadline=deadline)
    stub = stub_omp(tmp_path, RESULT)
    r = run_script("swarm_run.py", "--runtime", "omp", "--omp-bin", str(stub), "--repo", str(work),
                   "--once", "--json", env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _states(swarm)["T-one"] == "IN_PROGRESS"
    assert _notes(swarm, "T-one")["running_deadline"] > time.time()
    assert omp_calls(stub) == []

    dry = run_script("swarm_run.py", "--runtime", "omp", "--omp-bin", str(stub), "--repo", str(work),
                     "--dry-run", "--once", "--json", env=env)
    assert dry.returncode in (0, 1), dry.stdout + dry.stderr
    assert _states(swarm)["T-one"] == "IN_PROGRESS"
    assert omp_calls(stub) == []


def _slow_load_error(path: Path, launches: Path, marker: Path) -> None:
    _executable(path, f"""#!{sys.executable}
import sys, time
open({str(launches)!r}, "a").write("1\\n")
print("Failed to load extension /pkg/src/index.ts: boom", file=sys.stderr, flush=True)
time.sleep(8)
open({str(marker)!r}, "w").close()
""")


def test_extension_load_failure_stops_before_any_tool(tmp_path):
    """The stderr line is the signal. The session's process group is stopped before the sleep that stands in
    for a tool call, and the task fails E-POLICY once."""
    swarm = tmp_path / ".swarm"
    env = _env(swarm)
    work = _plan(tmp_path, env)
    launches, marker = tmp_path / "launches", tmp_path / "tool-ran"
    wrapper = tmp_path / "omp-bin" / "omp-slow"
    _slow_load_error(wrapper, launches, marker)
    started = time.monotonic()
    r = run_script("swarm_run.py", "--runtime", "omp", "--omp-bin", str(wrapper), "--repo", str(work),
                   "--max-parallel", "1", "--json", env=env)
    elapsed = time.monotonic() - started
    assert r.returncode == 1, r.stdout + r.stderr
    assert elapsed < 4, elapsed
    assert not marker.exists()
    assert launches.read_text().count("1") == 1
    assert _states(swarm)["T-one"] == "FAILED"
    reasons = _failed(swarm, "T-one")
    assert len(reasons) == 1 and reasons[0].startswith("E-POLICY") and "Failed to load extension" in reasons[0]


def test_extension_load_failure_does_not_start_the_next_task(tmp_path):
    """One broken load opens the circuit for the rest of the run. A later run, with the extension loading, finishes
    both tasks — the failure is not a permanent wedge."""
    swarm = tmp_path / ".swarm"
    env = _env(swarm)
    work = _plan(tmp_path, env, TWO)
    launches, marker = tmp_path / "launches", tmp_path / "tool-ran"
    wrapper = tmp_path / "omp-bin" / "omp-slow"
    _slow_load_error(wrapper, launches, marker)
    started = time.monotonic()
    r = run_script("swarm_run.py", "--runtime", "omp", "--omp-bin", str(wrapper), "--repo", str(work),
                   "--max-parallel", "1", "--json", env=env)
    elapsed = time.monotonic() - started
    assert r.returncode == 1, r.stdout + r.stderr
    assert elapsed < 4, elapsed
    assert not marker.exists()
    assert launches.read_text().count("1") == 1
    states = _states(swarm)
    assert sorted(states.values()) == ["FAILED", "PLANNED"]
    assert "no further sessions this run" in r.stdout

    stub = stub_omp(tmp_path, RESULT)
    again = run_script("swarm_run.py", "--runtime", "omp", "--omp-bin", str(stub), "--repo", str(work), "--json", env=env)
    assert again.returncode == 0, again.stdout + again.stderr
    assert _states(swarm) == {"T-a": "DONE", "T-b": "DONE"}
    assert len(omp_calls(stub)) == 2
