"""Continuous scheduler (DESIGN v2.2 D5, 4.4-4.6, 7): overlap, caps, refill, cancel, isolation, backoff, backpressure,
claims, heartbeat/stall telemetry, handover intake. Sessions are a fake `omp` executable whose behaviour each test
scripts per task; timings are tenths of a second, ordering is asserted from timestamps the fake records itself."""
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import ROOT, _scrubbed_base_env, run_script

from swarm.schema import load_schema, validate
from swarm.taskstore import TaskStore

_FAKE = r'''#!@PY@
import json, os, re, subprocess, sys, time
cfg = json.load(open(@CFG@))
prompt = sys.stdin.read()
tid = re.search(r'"task_id": "([^"]+)"', prompt).group(1)
corr = re.search(r'"correlation_id": "([^"]+)"', prompt).group(1)
c = cfg.get(tid, cfg.get("*", {}))
LOG = @LOG@


def rec(ev, **kw):
    with open(LOG, "a") as fh:
        fh.write(json.dumps({"ev": ev, "tid": tid, "t": time.time(), "pid": os.getpid(), **kw}) + "\n")


def emit(ev):
    print(json.dumps(ev), flush=True)


starts = sum(1 for ln in open(LOG) if json.loads(ln)["ev"] == "start" and json.loads(ln)["tid"] == tid) if os.path.exists(LOG) else 0
rec("start", n=starts + 1)
barrier = c.get("barrier", 0)  # wait until N distinct sessions of this run have started: they are then alive together, on any machine
def started():
    tids = set()
    for ln in open(LOG):
        try:
            entry = json.loads(ln)
        except ValueError:  # a peer is still appending this line
            continue
        if entry["ev"] == "start":
            tids.add(entry["tid"])
    return tids
if barrier:
    limit = time.time() + 30
    while time.time() < limit:
        if len(started()) >= barrier:
            break
        time.sleep(0.02)
emit({"type": "session", "version": 3, "id": "fake-" + tid, "cwd": os.getcwd()})
for tool in c.get("tools", []):
    emit({"type": "tool_execution_start", "toolName": tool["name"], "args": tool.get("args", {})})
    time.sleep(tool.get("sleep", 0.05))
if c.get("report"):  # the agent's own process calls the real shared_status_report handler
    sys.path.insert(0, @ROOT@)
    from swarm.tools.shared_runtime import status_report
    from swarm.tools.support import caller_scope
    with caller_scope(c.get("agent", "A05")):
        rec("reported", out=status_report({"task_id": tid, **c["report"]}))
if c.get("child"):
    ch = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rec("child", child=ch.pid)
flag = os.path.join(os.environ["SWARM_DIR"], "cancel", corr)
deadline = time.time() + c.get("sleep", 0.0)
while time.time() < deadline:
    if os.path.exists(flag):
        rec("cancel_seen", flag=json.load(open(flag)))
        if c.get("leave_on_cancel"):
            rec("left")
            sys.exit(0)
        flag = os.path.join(os.environ["SWARM_DIR"], "cancel", "never")
    time.sleep(0.02)
fail_first = c.get("fail_first", 0)
if c.get("fail") or starts < fail_first:
    print(c.get("stderr", "boom"), file=sys.stderr, flush=True)
    rec("end", ok=False)
    sys.exit(1)
res = {"task_id": tid, "state": "IN_REVIEW", "outputs": [{"kind": "code.backend", "uri": "file://x", "version": "1",
                                                           "digest": ""}], "metrics": {}, "summary_md": "ok"}
asst = {"role": "assistant", "content": [{"type": "text", "text": "done"},
                                         {"type": "toolCall", "id": "t1", "name": "yield", "arguments": {"data": res}}],
        "stopReason": "toolUse", "usage": {"cost": {"total": 0.01}}}
tres = {"role": "toolResult", "toolCallId": "t1", "toolName": "yield", "isError": False,
        "details": {"data": res, "status": "success"}}
emit({"type": "turn_end", "message": asst, "toolResults": [tres]})
emit({"type": "agent_end", "messages": [{"role": "user", "content": prompt}, asst, tres], "isTerminal": True,
      "yielded": True})
rec("end", ok=True)
'''


def _task(tid, agent="A05", cap="code.backend", deps=()):
    return {"id": tid, "capability": cap, "agent": agent, "title": tid, "depends_on": list(deps), "gates": []}


class Rig:
    """A planned workspace, a fake omp whose per-task behaviour is `cfg`, and the files the tests read back."""

    def __init__(self, tmp_path, tasks, cfg, **env):
        self.tmp = tmp_path
        self.swarm = tmp_path / ".swarm"
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.env = {"SWARM_DIR": str(self.swarm), **env}
        self.log_file = tmp_path / "fake.log"
        cfg_file = tmp_path / "fake.json"
        cfg_file.write_text(json.dumps(cfg))
        self.bin = tmp_path / "omp-bin" / "omp"
        self.bin.parent.mkdir()
        self.bin.write_text(_FAKE.replace("@PY@", sys.executable).replace("@CFG@", repr(str(cfg_file)))
                            .replace("@LOG@", repr(str(self.log_file))).replace("@ROOT@", repr(str(ROOT))))
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        spec = tmp_path / "plan.json"
        spec.write_text(json.dumps({"tasks": tasks}))
        r = run_script("orch_plan.py", "--plan", str(spec), "--prefix", "T", "--repo", str(self.work),
                       "--risk-class", "low", "--json", env=self.env)
        assert r.returncode == 0, r.stdout + r.stderr
        self.corr = json.loads(r.stdout)["correlation_id"]

    def store(self) -> TaskStore:
        return TaskStore(self.swarm / "tasks.db")

    def set_priority(self, tid, priority):
        self.store().update(tid, priority=priority)

    def set_notes(self, tid, **notes):
        self.store().set_notes(tid, **notes)

    def command(self, *extra):
        return [sys.executable, str(ROOT / "scripts" / "swarm_run.py"), "--runtime", "omp", "--omp-bin", str(self.bin),
                "--repo", str(self.work), "--json", *extra]

    def run(self, *extra, timeout=120):
        r = subprocess.run(self.command(*extra), capture_output=True, text=True, timeout=timeout, cwd=ROOT,
                           env={**_scrubbed_base_env(), **self.env})
        return r

    def spawn(self, *extra):
        return subprocess.Popen(self.command(*extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=ROOT, env={**_scrubbed_base_env(), **self.env}, start_new_session=True)

    def fake_log(self):
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]

    def spans(self):
        """{task_id: [(start, end)]} of every fake session, from the timestamps the fake recorded itself."""
        spans, open_ = {}, {}
        for rec in self.fake_log():
            if rec["ev"] == "start":
                open_[rec["tid"]] = rec["t"]
            elif rec["ev"] in ("end", "left"):
                spans.setdefault(rec["tid"], []).append((open_.pop(rec["tid"]), rec["t"]))
        return spans

    def events(self, *types):
        path = self.swarm / "events.jsonl"
        recs = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [r for r in recs if not types or r["type"] in types]

    def states(self):
        return {t["task_id"]: t["state"] for t in self.store().list(correlation_id=self.corr)}

    def reasons(self, tid):
        return [h["reason"] for h in self.store().history(tid) if h["to_state"] == "FAILED"]


def _max_concurrency(spans):
    edges = sorted([(s, 1) for pairs in spans.values() for s, _ in pairs]
                   + [(e, -1) for pairs in spans.values() for _, e in pairs], key=lambda p: (p[0], p[1]))
    live = peak = 0
    for _, step in edges:
        live += step
        peak = max(peak, live)
    return peak


def _trip(detail):
    """(from, to) of a consecutive-failure trip, read from 'effective_parallel F -> T' in its detail."""
    frm, to = detail.rsplit("effective_parallel ", 1)[1].split(" -> ")
    return int(frm), int(to)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def _runner():
    spec = importlib.util.spec_from_file_location("swarm_run_scheduler_under_test", ROOT / "scripts" / "swarm_run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------------------------------ overlap, caps, refill
def test_two_sessions_overlap(tmp_path):
    rig = Rig(tmp_path, [_task("a"), _task("b")], {"*": {"sleep": 0.6, "barrier": 2}})
    r = rig.run("--max-parallel", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    (a,), (b,) = rig.spans()["T-a"], rig.spans()["T-b"]
    assert max(a[0], b[0]) < min(a[1], b[1]), f"sessions ran one after the other: {a} {b}"
    assert rig.states() == {"T-a": "DONE", "T-b": "DONE"}


def test_global_cap_is_respected_and_overflow_is_queued(tmp_path):
    tasks = [_task(f"t{i}") for i in range(5)]
    # Two sessions start together before either ends, so the peak of 2 is forced rather than timed.
    rig = Rig(tmp_path, tasks, {"*": {"sleep": 0.3, "barrier": 2}})
    r = rig.run("--max-parallel", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _max_concurrency(rig.spans()) == 2
    assert set(rig.states().values()) == {"DONE"}
    ticks = [e["payload"] for e in rig.events("swarm.scheduler.tick")]
    first = ticks[0]
    assert first["max_parallel"] == 2 and first["running"] == 2 and first["queued"] == 3
    assert sorted(first["skipped"].values()) == ["no-free-slot"] * 3
    assert all(e["correlation_id"] == rig.corr for e in rig.events("swarm.scheduler.tick"))


def test_per_agent_ceiling_holds_with_free_global_slots(tmp_path):
    tasks = [_task(f"r{i}", agent="A12", cap="release.plan") for i in range(4)]  # A12's manifest max_parallel is 2
    # Two of A12's four sessions must be alive together before either ends, so the overlap is forced, not timed.
    rig = Rig(tmp_path, tasks, {"*": {"sleep": 0.3, "barrier": 2}})
    r = rig.run("--max-parallel", "4")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _max_concurrency(rig.spans()) == 2
    first = rig.events("swarm.scheduler.tick")[0]["payload"]
    assert first["per_agent"] == {"a12-release": {"running": 2, "limit": 2}}
    assert set(first["skipped"].values()) == {"class-ceiling:A12"}


def test_a_slow_task_does_not_delay_newly_ready_work(tmp_path):
    tasks = [_task("slow"), _task("fast"), _task("next", deps=["fast"])]
    # The slow session stays alive until T-next has started, so the freed slot is refilled while it runs. If it is not, the barrier times out and the assertions below fail.
    cfg = {"T-slow": {"sleep": 1.6, "barrier": 3}, "T-fast": {"sleep": 0.2}, "T-next": {"sleep": 0.2}}
    rig = Rig(tmp_path, tasks, cfg)
    r = rig.run("--max-parallel", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    spans = rig.spans()
    assert spans["T-next"][0][0] < spans["T-slow"][0][1], "the freed slot was not refilled while the slow task ran"
    assert spans["T-next"][0][1] < spans["T-slow"][0][1]


def test_rounds_fallback_keeps_the_barrier(tmp_path):
    """SWARM_SCHEDULER=rounds: the second round only starts once the slowest session of the first has ended."""
    tasks = [_task("slow"), _task("fast"), _task("next", deps=["fast"])]
    cfg = {"T-slow": {"sleep": 1.0}, "T-fast": {"sleep": 0.2}, "T-next": {"sleep": 0.2}}
    rig = Rig(tmp_path, tasks, cfg, SWARM_SCHEDULER="rounds")
    r = rig.run("--max-parallel", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    spans = rig.spans()
    assert spans["T-next"][0][0] >= spans["T-slow"][0][1]
    assert json.loads(r.stdout)["scheduler"] == "rounds"
    assert rig.states() == {"T-slow": "DONE", "T-fast": "DONE", "T-next": "DONE"}


@pytest.mark.parametrize("scheduler,dispatched", [("continuous", 2), ("rounds", 2)])
def test_max_rounds_bounds_the_dispatches(tmp_path, scheduler, dispatched):
    """One round was a batch of at most --max-parallel tasks; the continuous scheduler keeps that meaning as a budget."""
    rig = Rig(tmp_path, [_task(f"t{i}") for i in range(5)], {"*": {"sleep": 0.1}}, SWARM_SCHEDULER=scheduler)
    r = rig.run("--max-rounds", "1", "--max-parallel", "2")
    assert r.returncode == 1, r.stdout + r.stderr
    assert len([rec for rec in rig.fake_log() if rec["ev"] == "start"]) == dispatched
    assert sorted(rig.states().values()).count("DONE") == dispatched


def test_one_pass_never_starts_more_than_the_remaining_budget(tmp_path):
    """Greptile #9: a root finishes and unlocks four children while one dispatch is spent; with a budget of four
    (--max-rounds 1 --max-parallel 4) that pass may start three of them, never four (five dispatches in total)."""
    tasks = [_task("root")] + [_task(f"c{i}", deps=["root"]) for i in range(4)]
    rig = Rig(tmp_path, tasks, {"*": {"sleep": 0.1}})
    r = rig.run("--max-rounds", "1", "--max-parallel", "4")
    assert r.returncode == 1, r.stdout + r.stderr
    starts = [rec["tid"] for rec in rig.fake_log() if rec["ev"] == "start"]
    assert len(starts) == 4 and starts[0] == "T-root", starts
    assert sorted(rig.states().values()).count("DONE") == 4


def test_an_agent_status_report_reaches_the_schedulers_live_snapshot(tmp_path):
    """Greptile #6: shared_status_report runs in the agent's process; the derived refresh it triggers refuses to replace
    the scheduler's fresh live.json, so the scheduler (the owning producer) has to read the report itself."""
    cfg = {"*": {"sleep": 4.0, "report": {"kind": "progress", "step": "indexing the repo", "percent": 40}}}
    rig = Rig(tmp_path, [_task("a")], cfg, SWARM_HEARTBEAT_S="1")
    proc = rig.spawn("--max-parallel", "1")
    try:
        deadline, row, live = time.time() + 30, None, None
        while time.time() < deadline and row is None and proc.poll() is None:
            try:
                live = json.loads((rig.swarm / "live.json").read_text())
            except (OSError, ValueError):
                live = None
            row = next((r for r in (live or {}).get("runs", []) if r["task_id"] == "T-a"
                        and "indexing the repo" in (r["activity"] or "")), None)
            time.sleep(0.1)
        assert row is not None, f"the report never reached live.json: {live}"
        assert live["producer"] == "scheduler"  # the scheduler's own file, not a derived replacement
        assert row["activity"] == "progress: indexing the repo (40%)" and row["state"] == "running"
        assert [r for r in rig.fake_log() if r["ev"] == "reported"][0]["out"]["recorded"] is True
    finally:
        proc.communicate(timeout=60)
    assert rig.states() == {"T-a": "DONE"}


def test_an_unknown_scheduler_is_refused(tmp_path):
    rig = Rig(tmp_path, [_task("a")], {}, SWARM_SCHEDULER="turbo")
    r = rig.run()
    assert r.returncode == 2 and "SWARM_SCHEDULER" in r.stdout


def test_priority_then_depth_then_age_orders_dispatch(tmp_path):
    rig = Rig(tmp_path, [_task("low"), _task("mid"), _task("top")], {"*": {"sleep": 0.1}})
    rig.set_priority("T-low", "P3")
    rig.set_priority("T-mid", "P2")
    rig.set_priority("T-top", "P0")
    r = rig.run("--max-parallel", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    order = [rec["tid"] for rec in rig.fake_log() if rec["ev"] == "start"]
    assert order == ["T-top", "T-mid", "T-low"]


def test_order_ready_is_stable_for_ties():
    mod = _runner()
    rows = [{"task_id": "x", "priority": "P2", "dag_depth": 1, "created_at": 5},
            {"task_id": "y", "priority": "P2", "dag_depth": 0, "created_at": 9},
            {"task_id": "z", "priority": "P1", "dag_depth": 3, "created_at": 1},
            {"task_id": "w", "priority": "P2", "dag_depth": 0, "created_at": 9}]
    assert [t["task_id"] for t in mod.order_ready(rows)] == ["z", "y", "w", "x"]


# -------------------------------------------------------------------------------- isolation, timeout, internal fault
def test_failing_and_timed_out_runs_do_not_stop_siblings(tmp_path):
    tasks = [_task("good"), _task("bad"), _task("hang")]
    cfg = {"T-good": {"sleep": 0.5}, "T-bad": {"fail": True}, "T-hang": {"sleep": 30}}
    rig = Rig(tmp_path, tasks, cfg)
    r = rig.run("--max-parallel", "3", "--once", "--task-timeout", "3")
    out = json.loads(r.stdout)
    assert r.returncode == 1 and out["status"] == "fail", r.stdout + r.stderr
    assert rig.states() == {"T-good": "DONE", "T-bad": "FAILED", "T-hang": "FAILED"}
    assert rig.reasons("T-bad")[0].startswith("E-CONTRACT")
    assert rig.reasons("T-hang") == ["E-TIMEOUT: task_timeout exceeded"]
    finished = {e["task_id"]: e["payload"] for e in rig.events("swarm.run.finished")}
    assert {t: p["state"] for t, p in finished.items()} == {"T-good": "completed", "T-bad": "failed", "T-hang": "timed_out"}
    assert finished["T-bad"]["error_code"] == "E-CONTRACT" and finished["T-hang"]["error_code"] == "E-TIMEOUT"
    assert all(e["correlation_id"] == rig.corr for e in rig.events("swarm.run.finished"))


def test_a_worker_exception_is_that_tasks_structured_failure(tmp_path, monkeypatch):
    rig = Rig(tmp_path, [_task("ok"), _task("boom")], {"*": {"sleep": 0.1}})
    mod = _runner()
    real = mod.execute_one

    def flaky(store_path, task, agent, args, ctx, repo, leases=None, handoffs=None):
        if task["task_id"] == "T-boom":
            raise RuntimeError("disk on fire")
        return real(store_path, task, agent, args, ctx, repo, leases, handoffs)

    monkeypatch.setattr(mod, "execute_one", flaky)
    monkeypatch.setenv("SWARM_DIR", str(rig.swarm))
    monkeypatch.setenv("SWARM_ALLOW_INSECURE_DEV_KEY", "1")
    args = SimpleNamespace(repo=str(rig.work), dry_run=True, runtime="claude", claude_bin="claude", grok_bin="grok",
                           omp_bin="omp", max_parallel=2, max_rounds=10, once=False, max_turns=1, task_timeout=30,
                           permission_mode="default", model=None, allowed_tools="")
    ctx = SimpleNamespace(correlation_id=rig.corr, emit=lambda t, p, **k: None, root=rig.work)
    out = mod.run(args, ctx)
    assert out["status"] == "fail"
    assert rig.states() == {"T-ok": "DONE", "T-boom": "ESCALATED"}  # three attempts, each an E-INTERNAL failure
    reasons = rig.reasons("T-boom")
    assert len(reasons) == 3 and all(r.startswith("E-INTERNAL: RuntimeError: disk on fire") for r in reasons)
    assert [e["payload"]["state"] for e in rig.events("swarm.run.finished") if e["task_id"] == "T-boom"] == ["failed"] * 3


# --------------------------------------------------------------------------------------------- cancel and timeout
def test_swarm_timeout_writes_the_flag_waits_the_grace_then_kills_without_orphans(tmp_path):
    rig = Rig(tmp_path, [_task("stuck")], {"T-stuck": {"sleep": 60, "child": True}}, SWARM_CANCEL_GRACE_S="1")
    started = time.monotonic()
    r = rig.run("--swarm-timeout", "8", "--max-parallel", "1", "--task-timeout", "100")
    elapsed = time.monotonic() - started
    out = json.loads(r.stdout)
    assert r.returncode == 1 and out["cancelled"] == "swarm-timeout", r.stdout + r.stderr
    assert elapsed < 25, elapsed
    log = rig.fake_log()
    seen = next(rec for rec in log if rec["ev"] == "cancel_seen")["flag"]
    assert seen["reason"] == "swarm-timeout" and seen["grace_s"] == 1.0 and seen["requested_wall"] > 0
    pids = [rec["pid"] for rec in log if rec["ev"] == "start"] + [rec["child"] for rec in log if rec["ev"] == "child"]
    assert len(pids) == 2 and not any(_alive(p) for p in pids), "the session or its tool process outlived the runner"
    assert rig.reasons("T-stuck") == ["E-TIMEOUT: swarm timeout"]
    finished = rig.events("swarm.run.finished")
    assert [e["payload"]["state"] for e in finished] == ["timed_out"]
    assert any(e["payload"]["mode"] == "cancelling" for e in rig.events("swarm.degraded"))
    assert not (rig.swarm / "cancel" / rig.corr).exists(), "the flag outlived the run"


def test_a_session_that_leaves_within_the_grace_is_not_killed(tmp_path):
    rig = Rig(tmp_path, [_task("polite")], {"T-polite": {"sleep": 60, "leave_on_cancel": True}},
              SWARM_CANCEL_GRACE_S="20")
    started = time.monotonic()
    r = rig.run("--swarm-timeout", "8", "--max-parallel", "1", "--task-timeout", "100")
    assert time.monotonic() - started < 20  # the 8 s timeout is inside this bound; a full 20 s grace wait is not
    assert json.loads(r.stdout)["cancelled"] == "swarm-timeout"
    assert [rec["ev"] for rec in rig.fake_log() if rec["tid"] == "T-polite"] == ["start", "cancel_seen", "left"]
    assert not any("swarm timeout" in x or "runner shutdown" in x for x in rig.reasons("T-polite")), "it was killed"
    assert [e["payload"]["state"] for e in rig.events("swarm.run.finished")] == ["timed_out"]


def test_sigint_cancels_cooperatively_and_exits_with_the_signal_status(tmp_path):
    rig = Rig(tmp_path, [_task("stuck")], {"T-stuck": {"sleep": 60, "child": True}}, SWARM_CANCEL_GRACE_S="1")
    runner = rig.spawn("--max-parallel", "1", "--task-timeout", "100")
    try:
        deadline = time.monotonic() + 30
        while not any(rec["ev"] == "child" for rec in rig.fake_log()) and time.monotonic() < deadline:
            time.sleep(0.1)
        os.kill(runner.pid, signal.SIGINT)
        runner.communicate(timeout=30)
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
    assert runner.returncode == 128 + signal.SIGINT
    log = rig.fake_log()
    assert next(rec for rec in log if rec["ev"] == "cancel_seen")["flag"]["reason"] == "signal SIGINT"
    assert not any(_alive(rec["pid"]) for rec in log if rec["ev"] == "start")
    assert not any(_alive(rec["child"]) for rec in log if rec["ev"] == "child")
    assert [e["payload"]["state"] for e in rig.events("swarm.run.finished")] == ["cancelled"]
    assert rig.states() == {"T-stuck": "FAILED"}


def test_every_run_ends_with_a_terminal_record_even_when_never_dispatched(tmp_path):
    """--once leaves overflow tasks queued: each still gets one swarm.run.finished (cancelled) so no row is left open."""
    rig = Rig(tmp_path, [_task("a"), _task("b"), _task("c")], {"*": {"sleep": 0.1}})
    r = rig.run("--once", "--max-parallel", "1")
    assert r.returncode == 1, r.stdout + r.stderr
    done = {e["task_id"]: e["payload"]["state"] for e in rig.events("swarm.run.finished")}
    assert sorted(done.values()) == ["cancelled", "cancelled", "completed"]
    assert len(rig.events("swarm.run.finished")) == 3


# ------------------------------------------------------------------------------------- retry, backoff, backpressure
def test_a_transient_failure_waits_its_backoff_before_the_retry(tmp_path):
    rig = Rig(tmp_path, [_task("flaky")], {"T-flaky": {"fail_first": 1, "stderr": "429 Too Many Requests"}},
              SWARM_RETRY_BASE_S="1.0", SWARM_RATE_COOLDOWN_S="0")
    r = rig.run("--max-parallel", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    first, second = rig.spans()["T-flaky"]
    assert second[0] - first[1] >= 0.7, "the retry did not wait for the backoff (base 1 s, jitter 25 %)"
    assert rig.reasons("T-flaky")[0].startswith("E-CAPACITY")
    assert any("transient failure, re-dispatch in" in line for line in json.loads(r.stdout)["log"])
    assert rig.states() == {"T-flaky": "DONE"}
    assert rig.store().get("T-flaky")["notes_json"]["retry_after"] is None


def test_a_rate_limit_halves_parallelism_and_successes_restore_it(tmp_path):
    tasks = [_task("bad")] + [_task(f"s{i}") for i in range(3)] + [_task(f"e{i}") for i in range(4)]
    cfg = {"T-bad": {"fail_first": 1, "stderr": "HTTP 429 rate limit exceeded"}, "*": {"sleep": 0.4}}
    rig = Rig(tmp_path, tasks, cfg, SWARM_RATE_COOLDOWN_S="0", SWARM_RETRY_BASE_S="0.2")
    r = rig.run("--max-parallel", "4")
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(rig.states().values()) == {"DONE"}
    degraded = [e["payload"] for e in rig.events("swarm.degraded")]
    assert [d["reason"] for d in degraded] == ["rate_limited", "recovered"]  # the second one clears the marker
    assert degraded[0]["detail"] == "effective_parallel 4 -> 2" and degraded[1]["mode"] == "recovered"
    effective = [e["payload"]["effective_parallel"] for e in rig.events("swarm.scheduler.tick")]
    assert 2 in effective and max(effective[effective.index(2):]) > 2, effective
    assert degraded[0]["mode"] == "reduced_parallelism"  # the failure circuit stays out of a rate-limit run


# ------------------------------------------------------------------------- failure isolation and the failure circuit
def _effective(rig):
    return [e["payload"]["effective_parallel"] for e in rig.events("swarm.scheduler.tick")]


def test_one_failing_task_keeps_the_parallelism_and_follows_the_retry_ladder(tmp_path):
    """FIX-11: one failing task among 8 independent ones never lowers the effective parallelism (it used to latch
    sequential for the rest of the run); the failing task still goes through its retry ladder to ESCALATED."""
    tasks = [_task("f")] + [_task(f"s{i}") for i in range(7)]
    rig = Rig(tmp_path, tasks, {"T-f": {"fail": True, "sleep": 0.1}, "*": {"sleep": 0.8}})
    for t in tasks:
        rig.set_priority("T-" + t["id"], "P0" if t["id"] == "f" else "P2")
    r = rig.run("--max-parallel", "4")
    assert r.returncode == 1, r.stdout + r.stderr
    states = rig.states()
    assert states.pop("T-f") == "ESCALATED" and set(states.values()) == {"DONE"}, states
    assert len(rig.reasons("T-f")) == 3 and all(x.startswith("E-CONTRACT") for x in rig.reasons("T-f"))  # unchanged ladder
    assert rig.events("swarm.degraded") == []
    assert set(_effective(rig)) == {4}, _effective(rig)
    # Event-ordered, not timed: the scheduler's own ticks report how many sessions it had running after the failure,
    # and at least four other tasks finished after it.
    events = rig.events("swarm.run.finished", "swarm.scheduler.tick")
    failed_at = next(i for i, e in enumerate(events) if e["type"] == "swarm.run.finished" and e["task_id"] == "T-f")
    after = events[failed_at + 1:]
    finished_after = {e["task_id"] for e in after if e["type"] == "swarm.run.finished"} - {"T-f"}
    running_after = [e["payload"]["running"] for e in after if e["type"] == "swarm.scheduler.tick"]
    assert len(finished_after) >= 4 and running_after and max(running_after) >= 3, (sorted(finished_after), running_after)


def test_three_failures_in_a_row_halve_the_parallelism_and_successes_restore_it(tmp_path):
    bads = [_task(f"b{i}") for i in range(3)]
    oks = [_task(f"s{i}") for i in range(5)]
    permanent = {"fail": True}  # a plain failure, retried as the scenario was written: near-immediately
    cfg = {**{f"T-b{i}": permanent for i in range(3)}, "*": {"sleep": 2.5}}
    rig = Rig(tmp_path, bads + oks, cfg, SWARM_RATE_COOLDOWN_S="0", SWARM_RETRY_BASE_S="0.001", SWARM_RETRY_CAP_S="0.01")
    for t in bads:
        rig.set_priority("T-" + t["id"], "P0")
    for t in oks:
        rig.set_priority("T-" + t["id"], "P2")
    r = rig.run("--max-parallel", "4")
    assert r.returncode == 1, r.stdout + r.stderr
    states = rig.states()
    assert [states[f"T-b{i}"] for i in range(3)] == ["ESCALATED"] * 3  # each still ran its own retry ladder
    assert {states[f"T-s{i}"] for i in range(5)} == {"DONE"}
    degraded = [e["payload"] for e in rig.events("swarm.degraded")]
    reasons = [d["reason"] for d in degraded]
    assert reasons[0] == "consecutive_failures" and reasons[-1] == "recovered", degraded
    assert set(reasons) == {"consecutive_failures", "recovered"}, degraded
    assert all(d["mode"] == "reduced_parallelism" for d in degraded if d["reason"] == "consecutive_failures")
    assert degraded[-1]["mode"] == "recovered" and degraded[-1]["detail"] == "reduced_parallelism"
    # The circuit latches until a success, so a second trip needs a success in between, and a success raises the limit
    # by one. Each trip halves the limit it finds, so no trip starts from the level the previous trip left.
    trips = [_trip(d["detail"]) for d in degraded if d["reason"] == "consecutive_failures"]
    assert trips[0] == (4, 2), trips
    assert all(to == max(1, frm // 2) for frm, to in trips), trips
    assert all(prev_to < frm for (_, prev_to), (frm, _) in zip(trips, trips[1:])), trips
    effective = _effective(rig)
    assert 2 in effective and max(effective[effective.index(2):]) == 4, effective
    assert json.loads((rig.swarm / "live.json").read_text())["swarm"]["degraded"] is None


@pytest.mark.parametrize("order,trips", [
    (("b0", "g0", "b1", "g1"), False),  # a success between the failures resets the streak
    (("b0", "b1", "g0", "g1"), True),
])
def test_a_success_between_failures_resets_the_streak(tmp_path, order, trips):
    tasks = [_task(n) for n in ("b0", "b1", "g0", "g1")]
    permanent = {"fail": True}  # a plain failure, retried as the scenario was written: near-immediately
    cfg = {"T-b0": permanent, "T-b1": permanent, "*": {"sleep": 0.1}}
    rig = Rig(tmp_path, tasks, cfg, SWARM_FAIL_STREAK="2", SWARM_RATE_COOLDOWN_S="0", SWARM_RETRY_BASE_S="0.001", SWARM_RETRY_CAP_S="0.01")
    for name, prio in zip(order, ("P0", "P1", "P2", "P3")):
        rig.set_priority("T-" + name, prio)
        rig.set_notes("T-" + name, claims=["src/**"])  # one at a time, in priority order
    r = rig.run("--max-parallel", "4")
    assert r.returncode == 1, r.stdout + r.stderr
    degraded = [e["payload"] for e in rig.events("swarm.degraded")]
    if trips:
        assert [d["reason"] for d in degraded] == ["consecutive_failures", "recovered"], degraded
        assert degraded[0]["detail"] == "2 failures in a row; effective_parallel 4 -> 2"
    else:
        assert degraded == []


def test_fail_streak_zero_disables_the_circuit(tmp_path):
    bads = [_task(f"b{i}") for i in range(4)]
    oks = [_task(f"s{i}") for i in range(2)]
    cfg = {**{f"T-b{i}": {"fail": True} for i in range(4)}, "*": {"sleep": 0.5}}
    rig = Rig(tmp_path, bads + oks, cfg, SWARM_FAIL_STREAK="0")
    r = rig.run("--max-parallel", "6")
    assert r.returncode == 1, r.stdout + r.stderr
    assert rig.events("swarm.degraded") == [] and set(_effective(rig)) == {6}, _effective(rig)


def test_a_malformed_fail_streak_is_refused_before_anything_is_dispatched(tmp_path):
    for value in ("abc", "-1", "1.5"):
        sub = tmp_path / value.replace(".", "_")
        sub.mkdir()
        rig = Rig(sub, [_task("a")], {"*": {"sleep": 0.1}}, SWARM_FAIL_STREAK=value)
        r = rig.run()
        assert r.returncode == 2 and "SWARM_FAIL_STREAK" in r.stdout and rig.fake_log() == [], (value, r.stdout + r.stderr)


def test_error_handler_keeps_its_public_api_and_the_runner_never_latches_it():
    from swarm.errors import ErrorCode, ErrorHandler
    eh = ErrorHandler()
    assert eh.detect_parallel(3, 4) is True and eh.effective_parallel(4) == 4
    assert eh.should_degrade(code=ErrorCode.E_INTERNAL) is True and eh.effective_parallel(4) == 1  # callers may still latch
    assert ErrorHandler().force_degrade("x") is True
    text = " ".join(ErrorHandler().spec()["degrade_paths"])
    assert "does NOT call force_degrade" in text and "SWARM_FAIL_STREAK" in text


def test_a_clean_session_that_mentions_rate_limits_is_not_a_rate_limit(tmp_path):
    rig = Rig(tmp_path, [_task("a")], {"T-a": {"tools": [{"name": "edit", "args": {"path": "rate limit 429 overloaded"}}]}})
    r = rig.run("--max-parallel", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert rig.events("swarm.degraded") == []


# ----------------------------------------------------------------------------------------------------------- claims
@pytest.mark.parametrize("claims_b,serial", [(["src/**"], True), (["docs/**"], False)])
def test_overlapping_claims_serialize_tasks(tmp_path, claims_b, serial):
    rig = Rig(tmp_path, [_task("a"), _task("b")], {"*": {"sleep": 0.5}})
    rig.set_notes("T-a", claims=["src/**"])
    rig.set_notes("T-b", claims=claims_b)
    r = rig.run("--max-parallel", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    (a,), (b,) = rig.spans()["T-a"], rig.spans()["T-b"]
    overlapped = max(a[0], b[0]) < min(a[1], b[1])
    assert overlapped is not serial
    skipped = rig.events("swarm.scheduler.tick")[0]["payload"]["skipped"]
    assert (skipped == {"T-b": "claim-conflict:T-a"}) is serial


def test_a_malformed_claim_blocks_the_task_instead_of_running_it_unclaimed(tmp_path):
    rig = Rig(tmp_path, [_task("a")], {"*": {"sleep": 0.1}})
    rig.set_notes("T-a", claims=[{"mode": "exclusive"}])
    r = rig.run("--max-parallel", "1")
    assert r.returncode == 1, r.stdout + r.stderr
    assert rig.states() == {"T-a": "BLOCKED"} and rig.fake_log() == []


def test_select_batch_reports_a_reason_for_every_skipped_task(tmp_path):
    mod = _runner()
    store = TaskStore(tmp_path / "tasks.db")
    for tid, agent, cap in (("T-1", "A12", "release.plan"), ("T-2", "A12", "release.plan"), ("T-3", "A12", "release.plan"),
                            ("T-4", "A05", "code.backend"), ("T-5", "A05", "code.backend"), ("T-6", "A06", "code.frontend")):
        store.create(task_id=tid, correlation_id="c", capability=cap, agent_id=agent, title=tid)
    store.set_notes("T-4", claims=["a/**"])
    store.set_notes("T-5", claims=["a/b.py"])
    tasks = [store.get(f"T-{i}") for i in range(1, 7)]
    from swarm.claims import ClaimTable
    skipped: dict = {}
    batch, waiting = mod.select_batch(store, tasks, 4, None, running={"A12": 1}, claims=ClaimTable(), skipped=skipped)
    assert [t["task_id"] for t, _ in batch] == ["T-1", "T-4", "T-6"] and waiting == []
    # T-2 is A12's second replica with one already running; T-5 overlaps T-4's claim; nothing is left for a 4th slot
    assert skipped == {"T-2": "class-ceiling:A12", "T-3": "class-ceiling:A12", "T-5": "claim-conflict:T-4"}
    skipped = {}
    batch, _ = mod.select_batch(store, tasks[3:], 1, None, skipped=skipped)
    assert [t["task_id"] for t, _ in batch] == ["T-4"] and skipped == {"T-5": "no-free-slot", "T-6": "no-free-slot"}


# ------------------------------------------------------------------------------------ telemetry, heartbeat and stall
def test_heartbeat_and_stall_events_carry_correlation_ids_and_no_secrets(tmp_path):
    secret = "Bearer abcdefghijklmnopqrstuvwx"
    cfg = {"T-a": {"tools": [{"name": "bash", "args": {"command": f"curl -H 'Authorization: {secret}' https://x"}},
                             {"name": "edit", "args": {"path": "swarm/x.py"}, "sleep": 1.6}]}}
    rig = Rig(tmp_path, [_task("a")], cfg, SWARM_HEARTBEAT_S="0.2", SWARM_STALL_AFTER_S="0.6")
    r = rig.run("--max-parallel", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    beats = rig.events("swarm.run.heartbeat")
    assert len(beats) >= 3 and all(e["correlation_id"] == rig.corr and e["task_id"] == "T-a" for e in beats)
    assert any(e["payload"]["stalled"] for e in beats)
    stalled = rig.events("swarm.run.stalled")
    assert len(stalled) == 1 and stalled[0]["correlation_id"] == rig.corr and stalled[0]["payload"]["threshold_s"] == 0.6
    assert [e["type"] for e in rig.events() if e["task_id"] == "T-a" and e["type"] in (
        "swarm.run.queued", "swarm.run.started", "swarm.run.finished")] == [
        "swarm.run.queued", "swarm.run.started", "swarm.run.finished"]
    assert rig.events("swarm.run.activity")  # throttled to one per second per run; the counters are in live.json below
    for text in ((rig.swarm / "events.jsonl").read_text(), (rig.swarm / "live.json").read_text()):
        assert "abcdefghijklmnopqrstuvwx" not in text
    live = json.loads((rig.swarm / "live.json").read_text())
    assert validate(live, load_schema("swarm.live.v1")) == [] and live["correlation_id"] == rig.corr
    assert live["swarm"]["completed"] == 1
    assert live["runs"][0]["tool_calls"] == 2 and live["runs"][0]["last_tool"] == "edit"


def test_a_runtime_that_streams_nothing_is_not_called_stalled(tmp_path):
    """Only omp reports tool activity; a claude session is silent until it ends, so it reports elapsed time only."""
    rig = Rig(tmp_path, [_task("a")], {}, SWARM_HEARTBEAT_S="0.2", SWARM_STALL_AFTER_S="0.6")
    result = {"task_id": "T-a", "state": "IN_REVIEW", "outputs": [], "metrics": {}, "summary_md": "ok"}
    slow = tmp_path / "claude-slow"
    slow.write_text(f"#!{sys.executable}\nimport sys, time\nif sys.argv[1:3] == ['auth', 'status']:\n"
                    f"    print('{{\"loggedIn\": true}}')\nelif '-p' in sys.argv:\n    sys.stdin.read()\n    time.sleep(1.5)\n"
                    f"    print({json.dumps(json.dumps({'result': '```json' + chr(10) + json.dumps(result) + chr(10) + '```'}))})\n")
    slow.chmod(0o755)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "swarm_run.py"), "--runtime", "claude", "--claude-bin",
                        str(slow), "--repo", str(rig.work), "--json", "--max-parallel", "1"],
                       capture_output=True, text=True, timeout=120, cwd=ROOT, env={**_scrubbed_base_env(), **rig.env})
    assert r.returncode == 0, r.stdout + r.stderr
    assert rig.events("swarm.run.stalled") == []
    beats = rig.events("swarm.run.heartbeat")
    assert beats and all(b["payload"]["tool_calls"] == 0 for b in beats) and beats[-1]["payload"]["elapsed_s"] >= 1.0


def test_dry_run_smoke_writes_live_json(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    swarm = tmp_path / ".swarm"
    env = {"SWARM_DIR": str(swarm)}
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    plan = run_script("orch_plan.py", "--repo", str(work), "--brief-text", "smoke", "--pattern", "hotfix", "--json", env=env)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    r = run_script("swarm_run.py", "--repo", str(work), "--dry-run", "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["complete"] is True and out["scheduler"] == "continuous"
    live = json.loads((swarm / "live.json").read_text())
    assert validate(live, load_schema("swarm.live.v1")) == []
    assert live["swarm"]["running"] == 0 and live["swarm"]["queued"] == 0 and live["swarm"]["completed"] >= 1


# -------------------------------------------------------------------------------- caps, manager, stream, prompt
def test_default_cap_ceiling_and_env(tmp_path, monkeypatch):
    from swarm.errors import ErrorCode, SwarmError
    from swarm.subagent_lifecycle import SubagentLifecycleManager, resolve_max_parallel
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    for name in ("SWARM_MAX_PARALLEL", "SWARM_MAX_PARALLEL_CEILING"):
        monkeypatch.delenv(name, raising=False)
    assert resolve_max_parallel() == 4 and resolve_max_parallel(8) == 8 and resolve_max_parallel(0) == 1
    assert resolve_max_parallel(500) == 32
    monkeypatch.setenv("SWARM_MAX_PARALLEL", "6")
    assert resolve_max_parallel() == 6
    monkeypatch.setenv("SWARM_MAX_PARALLEL_CEILING", "5")
    assert resolve_max_parallel() == 5 and resolve_max_parallel(20) == 5
    monkeypatch.setenv("SWARM_MAX_PARALLEL", "many")
    with pytest.raises(SwarmError) as err:
        resolve_max_parallel()
    assert err.value.code is ErrorCode.E_INPUT and "SWARM_MAX_PARALLEL" in str(err.value)
    monkeypatch.delenv("SWARM_MAX_PARALLEL_CEILING")
    monkeypatch.delenv("SWARM_MAX_PARALLEL")
    assert SubagentLifecycleManager(8, root=tmp_path, dry_run=True).max_concurrent == 8  # the old hard cap of 4 is gone


def test_a_full_manager_refuses_instead_of_waiting_forever(tmp_path, monkeypatch):
    from swarm.errors import ErrorCode, SwarmError
    from swarm.subagent_lifecycle import SubagentLifecycleManager
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    mgr = SubagentLifecycleManager(1, root=tmp_path, dry_run=True)
    first, second = mgr.new_subagent_id("T-1"), mgr.new_subagent_id("T-2")
    mgr.acquire(first)
    with pytest.raises(SwarmError) as err:
        mgr.acquire(second, timeout=0.05)
    assert err.value.code is ErrorCode.E_CAPACITY
    mgr.release(first)
    mgr.release(second)
    assert mgr.active_ids() == []


def test_progress_events_carry_correlation_and_task_ids_and_complete_once(tmp_path, monkeypatch):
    from swarm.runlog import read_events
    from swarm.subagent_lifecycle import SubagentLifecycleManager
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    mgr = SubagentLifecycleManager(2, root=tmp_path, correlation_id="corr-1")
    sid = mgr.new_subagent_id("T-7")
    mgr.acquire(sid)
    mgr.release(sid)
    assert mgr.release(sid) is None  # a repeat reports nothing
    mine = [e for e in read_events(root=tmp_path)
            if e["type"] in ("subagent.status.stream", "gsd.autonomous.omp") and e["payload"].get("subagent_id") == sid]
    assert mine and all(e["correlation_id"] == "corr-1" and e["task_id"] == "T-7" for e in mine)
    assert sum(1 for e in mine if e["payload"].get("state") == "COMPLETE") == 1
    assert sum(1 for e in read_events(root=tmp_path, event_type="subagent.complete.immediate")
               if e["payload"]["subagent_id"] == sid) == 1


def test_omp_activity_reads_the_stream_incrementally():
    mod = _runner()
    seen = []
    tap = mod.OmpActivity(lambda **kw: seen.append(kw))
    line = json.dumps({"type": "tool_execution_start", "toolName": "edit", "args": {"path": "swarm/x.py"}})
    tap.feed("omp: banner\n" + line[:20])
    assert seen == []  # half a line is not an event
    tap.feed(line[20:] + "\n" + json.dumps({"type": "turn_end", "message": {"usage": {"totalTokens": 120}}}) + "\n")
    assert seen == [{"tool": "edit", "text": "edit swarm/x.py", "tool_calls": 1}, {"turns": 1, "tokens": 120}]
    tap.feed("{not json}\n[1]\n")
    assert len(seen) == 2


def test_assignment_prompt_adds_a_compact_handover_per_dependency(tmp_path, monkeypatch):
    mod = _runner()
    store = TaskStore(tmp_path / "tasks.db")
    store.create(task_id="T-up", correlation_id="c", capability="design.blueprint", agent_id="A03", title="up")
    store.create(task_id="T-old", correlation_id="c", capability="design.blueprint", agent_id="A03", title="old")
    store.create(task_id="T-down", correlation_id="c", capability="code.backend", agent_id="A05", title="down",
                 depends_on=["T-up", "T-old"])
    doc = {"from_agent": "A03", "to_agent": "A05", "goal": "ship the contract " + "x" * 5000,
           "remaining": [f"step {i}" for i in range(9)], "blockers": ["waiting on schema"],
           "artifacts": [{"kind": "design.contract", "uri": "file://c.md", "digest": "sha256:" + "a" * 64}],
           "verification": {"status": "partial", "confidence": 0.5}}

    def inspect(tid, root=None, to_agent=None, **_kw):  # the real call: (status, document) for ONE file
        status = {"status": "ok" if tid == "T-up" else "stale"}
        return status, (doc if status["status"] == "ok" else None)

    fake = SimpleNamespace(inspect_handover=inspect)
    monkeypatch.setitem(sys.modules, "swarm.handover", fake)
    task = store.get("T-down")
    prompt = mod.assignment_prompt(store, task, {"id": "A05", "slug": "a05-backend"}, tmp_path)
    section = prompt.split("## Handover\n", 1)[1].split("\n\nExecute the task", 1)[0]
    assert section.startswith("### T-up (A03 → A05)") and "### T-old" not in section
    assert "step 4" in section and "step 5" not in section  # five items per list
    assert "waiting on schema" in section and "design.contract: file://c.md sha256:" in section
    assert "verification: partial (confidence 0.5)" in section
    assert len(section) <= mod.HANDOVER_DEP_CHARS + 2  # the same budget as the upstream summary


def test_assignment_prompt_without_a_handover_module_is_unchanged(tmp_path, monkeypatch):
    mod = _runner()
    store = TaskStore(tmp_path / "tasks.db")
    store.create(task_id="T-up", correlation_id="c", capability="design.blueprint", agent_id="A03", title="up")
    store.create(task_id="T-down", correlation_id="c", capability="code.backend", agent_id="A05", title="down",
                 depends_on=["T-up"])
    task = store.get("T-down")
    monkeypatch.setitem(sys.modules, "swarm.handover", None)  # not importable
    def body(prompt):  # the signed envelope above carries a fresh message id and trace id every time
        return prompt.split("## Brief", 1)[1]

    without = body(mod.assignment_prompt(store, task, {"id": "A05", "slug": "a05-backend"}, tmp_path))
    assert "## Handover" not in without
    empty = SimpleNamespace(inspect_handover=lambda tid, root=None, to_agent=None, **_kw: ({"status": "missing"}, None))
    monkeypatch.setitem(sys.modules, "swarm.handover", empty)
    assert body(mod.assignment_prompt(store, task, {"id": "A05", "slug": "a05-backend"}, tmp_path)) == without


# ------------------------------------------------------------------------------- in-session transitions (orch_status)
def _transition(rig, tid, state):
    r = run_script("orch_status.py", "--root", str(rig.work), "--correlation-id", rig.corr, "--transition", tid, state,
                   "--reason", "by hand", "--json", env=rig.env)
    assert r.returncode == 0, r.stdout + r.stderr


def test_in_session_transitions_emit_the_matching_run_events(tmp_path):
    rig = Rig(tmp_path, [_task("a")], {})
    for state in ("CLAIMED", "IN_PROGRESS", "FAILED"):
        _transition(rig, "T-a", state)
    runs = [e for e in rig.events("swarm.run.state", "swarm.run.finished") if e["task_id"] == "T-a"]
    assert [(e["type"], e["payload"].get("to") or e["payload"]["state"]) for e in runs] == [
        ("swarm.run.state", "starting"), ("swarm.run.state", "running"), ("swarm.run.state", "failed"),
        ("swarm.run.finished", "failed")]
    assert all(e["correlation_id"] == rig.corr and e["payload"]["run_id"] == "T-a.a1" for e in runs)
    assert runs[-1]["payload"]["error"] == "by hand" and runs[-1]["payload"]["duration_s"] >= 0
    event_schema = load_schema("swarm.event.v1")
    assert all(validate(e, event_schema) == [] for e in runs)


def test_a_transition_of_a_task_a_runner_is_working_is_left_to_that_runner(tmp_path):
    rig = Rig(tmp_path, [_task("a")], {})
    for state in ("CLAIMED", "IN_PROGRESS"):
        _transition(rig, "T-a", state)
    rig.set_notes("T-a", running=time.time())
    before = len(rig.events("swarm.run.state", "swarm.run.finished"))
    _transition(rig, "T-a", "FAILED")
    assert len(rig.events("swarm.run.state", "swarm.run.finished")) == before


# ------------------------------------------------------------------- w3-fix-2: shutdown budget, AIMD burst, release, pool
def _ignoring_wrapper(rig):
    """An `omp` that ignores SIGTERM and then execs the rig's fake (which keeps a tool process of its own)."""
    wrapper = rig.tmp / "omp-wrap" / "omp"
    wrapper.parent.mkdir()
    wrapper.write_text(f"#!{sys.executable}\nimport os, signal, sys\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                       f"os.execv({str(rig.bin)!r}, [{str(rig.bin)!r}] + sys.argv[1:])\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


def _hook():
    spec = importlib.util.spec_from_file_location("autonomous_run_for_scheduler_tests", ROOT / "hooks" / "autonomous_run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _everything_ended(rig, expect_pids=2):
    log = rig.fake_log()
    pids = [rec["pid"] for rec in log if rec["ev"] == "start"] + [rec["child"] for rec in log if rec["ev"] == "child"]
    assert len(pids) == expect_pids, f"the session never got going: {log}"
    time.sleep(0.5)
    alive = [p for p in pids if _alive(p)]
    for p in alive:
        os.kill(p, signal.SIGKILL)
    assert not alive, f"orphans: {alive}"


def test_sigterm_from_the_hook_ends_a_session_that_ignores_sigterm_inside_the_supervisors_grace(tmp_path):
    """RV-A P1 repro: a wrapper sets SIGTERM to SIG_IGN and execs the fake omp (sleep 120 + a grandchild), then
    `autonomous_run.run_capped(rig.command(), timeout=3, grace=15)`. HEAD 13263d7: rc 143 in 8.1 s, nothing alive.
    Before this fix: rc -9 after 18 s (the runner waited 10 s, then 5 s, and was SIGKILLed at 15 s), the session and its
    grandchild alive, T-a IN_PROGRESS with notes.running set."""
    rig = Rig(tmp_path, [_task("a")], {"*": {"sleep": 120, "child": True}})
    rig.bin = _ignoring_wrapper(rig)
    started = time.monotonic()
    cp = _hook().run_capped(rig.command(), cwd=str(ROOT), env={**_scrubbed_base_env(), **rig.env}, timeout=3, grace=15)
    took = time.monotonic() - started
    assert cp.returncode == 128 + signal.SIGTERM, (cp.returncode, cp.stderr[-500:])  # the runner's own exit, not -9
    assert took - 3 < 15 - 1, f"the shutdown took {took - 3:.1f}s of the supervisor's 15 s grace"
    _everything_ended(rig)
    task = rig.store().get("T-a")
    assert task["state"] == "FAILED" and task["notes_json"].get("running") is None, task
    assert rig.reasons("T-a") == ["E-TIMEOUT: runner shutdown"]


def test_the_shutdown_budget_shortens_the_cooperative_wait_and_the_kill_fits_in_it(tmp_path):
    """SWARM_SHUTDOWN_BUDGET_S (the hook exports its grace) bounds the cooperative wait + kill: with a 6 s budget the
    10 s SWARM_CANCEL_GRACE_S becomes 0 s, SIGKILL lands after 3 s and the runner is gone well inside the budget."""
    rig = Rig(tmp_path, [_task("a")], {"*": {"sleep": 120, "child": True}},
              SWARM_SHUTDOWN_BUDGET_S="6", SWARM_CANCEL_GRACE_S="10")
    rig.bin = _ignoring_wrapper(rig)
    runner = rig.spawn("--max-parallel", "1", "--task-timeout", "100")
    try:
        deadline = time.monotonic() + 30
        while not any(rec["ev"] == "child" for rec in rig.fake_log()) and time.monotonic() < deadline:
            time.sleep(0.05)
        signalled = time.monotonic()
        os.kill(runner.pid, signal.SIGTERM)
        runner.communicate(timeout=30)
        took = time.monotonic() - signalled
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
    assert runner.returncode == 128 + signal.SIGTERM and took < 6, (runner.returncode, took)
    _everything_ended(rig)
    cancelling = [e["payload"] for e in rig.events("swarm.degraded") if e["payload"]["mode"] == "cancelling"]
    assert cancelling and cancelling[0]["detail"] == "grace 0s", cancelling


def test_a_malformed_cancel_grace_is_refused_before_anything_is_dispatched(tmp_path):
    """RV-A P3 repro: SWARM_CANCEL_GRACE_S=abc with --swarm-timeout 1. The run dispatched normally and only then exited
    E-INPUT (rc 2) when the cancel happened, instead of a clean timeout result."""
    rig = Rig(tmp_path, [_task("a")], {"*": {"sleep": 30}}, SWARM_CANCEL_GRACE_S="abc")
    started = time.monotonic()
    r = rig.run("--swarm-timeout", "1")
    assert r.returncode == 2 and "SWARM_CANCEL_GRACE_S" in r.stdout, r.stdout + r.stderr
    assert rig.fake_log() == [] and time.monotonic() - started < 15, "a session was started before the input check"
    (tmp_path / "b").mkdir()
    rig2 = Rig(tmp_path / "b", [_task("a")], {"*": {"sleep": 30}}, SWARM_SHUTDOWN_BUDGET_S="-1")
    r = rig2.run("--swarm-timeout", "1")
    assert r.returncode == 2 and "SWARM_SHUTDOWN_BUDGET_S" in r.stdout and rig2.fake_log() == []


def test_a_rate_limit_burst_halves_parallelism_once_and_recovery_clears_degraded(tmp_path):
    """RV-A P2 repro: 8 tasks, --max-parallel 8, each fails once with '429 rate limit'. The log showed 8→4→2→1 from that
    one burst (on_rate_limit ran for every completion). Now one halving; once successes restore the cap the degraded
    marker is cleared (live.json and a `recovered` swarm.degraded event)."""
    tasks = [_task(f"t{i}", agent="A05" if i % 2 else "A06", cap="code.backend" if i % 2 else "code.frontend")
             for i in range(8)]
    rig = Rig(tmp_path, tasks, {"*": {"fail_first": 1, "sleep": 0.5, "barrier": 8, "stderr": "HTTP 429 rate limit exceeded"}},
              SWARM_RATE_COOLDOWN_S="0", SWARM_RETRY_BASE_S="0.2")
    r = rig.run("--max-parallel", "8")
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(rig.states().values()) == {"DONE"}
    first_wave = sorted((s, e) for p in rig.spans().values() for s, e in p)[:8]
    # all 8 sessions were alive at the same instant (the limit hit while they were all running); a start-time window would
    # depend on machine load, the overlap does not
    assert max(s for s, _ in first_wave) < min(e for _, e in first_wave), "the 8 sessions were not running together when the limit hit"
    degraded = [e["payload"] for e in rig.events("swarm.degraded")]
    limited = [d for d in degraded if d["reason"] == "rate_limited"]
    assert [d["detail"] for d in limited] == ["effective_parallel 8 -> 4"], degraded
    log = json.loads(r.stdout)["log"]
    assert not any("→ 1" in line for line in log) and any("same burst" in line for line in log)
    assert any(d["mode"] == "recovered" and d["detail"] == "reduced_parallelism" for d in degraded), degraded
    assert json.loads((rig.swarm / "live.json").read_text())["swarm"]["degraded"] is None


def test_a_rate_limit_at_parallelism_one_sets_no_degraded_marker(tmp_path):
    """RV-G P3: at --max-parallel 1 a rate limit reduces nothing, so no swarm.degraded marker is raised (the clear only
    fires when the cap rises, which it never could)."""
    tasks = [_task(f"t{i}") for i in range(3)]
    rig = Rig(tmp_path, tasks, {"T-t0": {"fail_first": 1, "stderr": "HTTP 429 rate limit exceeded"}},
              SWARM_RATE_COOLDOWN_S="0", SWARM_RETRY_BASE_S="0.2")
    r = rig.run("--max-parallel", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(rig.states().values()) == {"DONE"}
    assert rig.events("swarm.degraded") == []
    assert json.loads((rig.swarm / "live.json").read_text())["swarm"]["degraded"] is None


def test_rounds_mode_still_cancels_on_the_swarm_timeout_without_orphans(tmp_path):
    rig = Rig(tmp_path, [_task("stuck")], {"T-stuck": {"sleep": 60, "child": True}}, SWARM_CANCEL_GRACE_S="1",
              SWARM_SCHEDULER="rounds")
    # A margin, not a race: a loaded host can take seconds to start the stuck session, which must start before the swarm timeout.
    r = rig.run("--swarm-timeout", "8", "--max-parallel", "1", "--task-timeout", "100")
    out = json.loads(r.stdout)
    assert r.returncode == 1 and out["cancelled"] == "swarm-timeout" and out["scheduler"] == "rounds"
    _everything_ended(rig)


def _bare_scheduler(mod, tmp_path, mgr):
    sched = mod.Scheduler.__new__(mod.Scheduler)
    sched.cancel, sched.leases, sched.mgr = mod.CancelRequest(), None, mgr
    sched.args, sched.ctx, sched.repo, sched.store_path, sched.handoffs = None, None, tmp_path, tmp_path / "t.db", None
    sched._internal_failure = lambda task, agent, exc: mod.Done(task["task_id"], agent["id"], "FAILED", str(exc))
    return sched


def test_the_lifecycle_slot_is_released_on_every_worker_exit_path(tmp_path, monkeypatch):
    """RV-A P3: `_worker` returned SKIPPED (and failed `acquire`) without `mgr.release(sid)`, so `_active` kept the sid
    and active_count() / register_omp() over-reported."""
    mod = _runner()
    from swarm.subagent_lifecycle import SubagentLifecycleManager
    mgr = SubagentLifecycleManager(2, root=tmp_path, dry_run=True)
    sched = _bare_scheduler(mod, tmp_path, mgr)
    task, agent = {"task_id": "T-x"}, {"id": "A05"}
    run = SimpleNamespace(slot_acquired=lambda: None)

    sid = mgr.new_subagent_id("T-x", root=tmp_path, correlation_id="c")
    sched.cancel.request("test")  # cancelled before the pool picked the task up
    assert sched._worker(task, agent, sid, run).outcome == "SKIPPED"
    assert mgr.active_count() == 0

    sched.cancel = mod.CancelRequest()
    sid = mgr.new_subagent_id("T-x", root=tmp_path, correlation_id="c")
    monkeypatch.setattr(mgr, "acquire", lambda s, **k: (_ for _ in ()).throw(mod.SwarmError(mod.ErrorCode.E_CAPACITY, "full")))
    assert sched._worker(task, agent, sid, run).outcome == "FAILED"
    assert mgr.active_count() == 0


def test_worker_pool_runs_calls_on_at_most_max_workers_daemon_threads():
    mod = _runner()
    import threading
    pool = mod.WorkerPool(2, "t")
    seen, gate = set(), threading.Event()

    def call(n):
        seen.add(threading.current_thread())
        gate.wait(5)
        if n == 3:
            raise ValueError("boom")
        return n

    futures = [pool.submit(call, n) for n in range(4)]
    gate.set()
    assert [f.result(5) for f in futures[:3]] == [0, 1, 2]
    with pytest.raises(ValueError):
        futures[3].result(5)
    assert len(seen) <= 2 and all(t.daemon for t in seen)
    pool.shutdown(wait=True)
    with pytest.raises(RuntimeError):
        pool.submit(call, 9)


def test_a_worker_stuck_after_the_kill_does_not_keep_the_finished_runner_alive(tmp_path):
    """RV-A P3: 'abandoned after kill' did not let the runner exit, because ThreadPoolExecutor threads are joined at
    interpreter exit. The scheduler's pool threads are daemons."""
    code = (f"import importlib.util, sys, time\n"
            f"spec = importlib.util.spec_from_file_location('swarm_run_x', {str(ROOT / 'scripts' / 'swarm_run.py')!r})\n"
            f"m = importlib.util.module_from_spec(spec); sys.modules['swarm_run_x'] = m; spec.loader.exec_module(m)\n"
            f"pool = m.WorkerPool(1, 'stuck')\n"
            f"pool.submit(time.sleep, 120)\n"
            f"time.sleep(0.2)\n"
            f"pool.shutdown(wait=False)\n"
            f"print('exiting', flush=True)\n")
    started = time.monotonic()
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT,
                       env={**_scrubbed_base_env(), "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0 and "exiting" in r.stdout, r.stderr
    assert time.monotonic() - started < 20
