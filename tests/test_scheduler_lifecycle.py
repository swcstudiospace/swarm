"""Original-controller lifecycle helpers; no native facade or selector is involved.

The synthetic import deliberately differs from the canonical runner module. Assertions
observe real store histories, lifecycle manager slots and telemetry, not source text.
"""
import importlib.util
import json
import logging
import signal
import subprocess
import sys
import threading
import time
import types
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from conftest import ROOT
from swarm.subagent_lifecycle import SubagentLifecycleManager
from swarm.taskstore import TaskStore, TaskState as S
from swarm.telemetry import Telemetry


@pytest.fixture()
def lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.setenv("SUBSTRATE_DISABLED", "1")
    monkeypatch.setenv("SWARM_PURE_PYTHON", "1")
    monkeypatch.setenv("SWARM_SCHEDULER", "continuous")
    monkeypatch.delenv("SWARM_AGENT_SESSION", raising=False)
    monkeypatch.delenv("SWARM_CHILD", raising=False)
    name = "swarm_run_lifecycle_under_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "swarm_run.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    store = TaskStore(tmp_path / ".swarm" / "tasks.db")
    corr = "helper-lifecycle"
    store.create(task_id="T-helper", correlation_id=corr, capability="code.backend", agent_id="A05",
                 budget={"max_wall_s": 30}, notes={"gates": ["qa"]})
    store.transition("T-helper", S.VALIDATED)
    store.transition("T-helper", S.PLANNED)
    task = store.get("T-helper")
    agent = mod.resolve_agent(task)
    args = SimpleNamespace(repo=str(tmp_path), dry_run=True, runtime="claude", claude_bin="claude",
                           grok_bin="grok", omp_bin="omp", max_parallel=1, max_rounds=1, once=True,
                           max_turns=1, task_timeout=30, permission_mode="default", model=None, allowed_tools="")
    emitted = []
    ctx = SimpleNamespace(root=tmp_path, correlation_id=corr,
                          emit=lambda kind, payload, **kw: emitted.append((kind, payload)))
    mgr = SubagentLifecycleManager(1, root=tmp_path, correlation_id=corr, dry_run=True)
    tel = Telemetry(root=tmp_path, correlation_id=corr, tee=False, live=False)
    sched = mod.Scheduler(args, ctx, store=store, store_path=store.path, repo=tmp_path, corr=corr,
                          mgr=mgr, tel=tel, leases=None, handoffs=None, cancel=mod.CancelRequest(),
                          barrier=False, sdir=store.path.parent)
    rig = SimpleNamespace(mod=mod, sched=sched, store=store, task=task, agent=agent,
                          mgr=mgr, tel=tel, args=args, ctx=ctx, emitted=emitted)
    yield rig
    sched.close()
    store.conn.close()


def _events(rig, kind):
    path = rig.store.path.parent / "events.jsonl"
    return [event for line in path.read_text().splitlines()
            if (event := json.loads(line))["type"] == kind and event.get("task_id") == rig.task["task_id"]]


def _history(rig):
    return [row["to_state"] for row in rig.store.history(rig.task["task_id"])]


def _terminal_once(rig, state, *, has_sid=True):
    events = _events(rig, "swarm.run.finished")
    assert len(events) == 1 and events[0]["payload"]["state"] == state
    assert events[0]["correlation_id"] == rig.ctx.correlation_id
    assert len(_events(rig, "swarm.run.queued")) == 1
    assert len(_events(rig, "subagent.complete.immediate")) == int(has_sid)
    assert rig.mgr.active_ids() == []
    assert rig.sched.book.unsettled() == []


def test_monitor_helper_starts_only_original_controllers_monitor(lifecycle, monkeypatch):
    rig = lifecycle
    entered = threading.Event()
    seen = []

    def watch(controller):
        seen.append(controller)
        entered.set()
        controller._monitor_stop.wait(5)

    def no_pool(*args, **kwargs):
        pytest.fail("monitor-only helper created a worker pool")

    monkeypatch.setattr(rig.mod.Scheduler, "_watch", watch)
    monkeypatch.setattr(rig.mod, "WorkerPool", no_pool)
    assert rig.sched._start_monitor() is None
    try:
        assert entered.wait(5)
        assert seen == [rig.sched]
        assert rig.sched.pool is None
        assert rig.sched._monitor.daemon
    finally:
        rig.sched.close()
    assert not rig.sched._monitor.is_alive()


def test_python_start_creates_pool_before_calling_bound_monitor(lifecycle, monkeypatch):
    rig = lifecycle
    seen = []
    monkeypatch.setattr(rig.sched, "_start_monitor", lambda: seen.append(rig.sched.pool))
    rig.sched.start()
    assert len(seen) == 1 and seen[0] is rig.sched.pool
    assert isinstance(seen[0], rig.mod.WorkerPool)
    assert rig.sched._monitor is None


def test_open_run_preserves_identity_without_submitting_or_acquiring(lifecycle):
    rig = lifecycle
    opened = rig.sched._open_run(rig.task, rig.agent)
    item = opened.item
    assert isinstance(opened, rig.mod.OpenRun) and opened.failure is None
    assert rig.sched.inflight[rig.task["task_id"]] is item
    assert item.task is rig.task and item.agent is rig.agent
    assert isinstance(item.run, rig.mod.RunHandle) and item.run.tel is rig.tel
    assert item.sid == item.run.sid and rig.mgr.active_ids() == [item.sid]
    assert rig.mgr._active[item.sid]["holds_slot"] is False
    assert isinstance(item.future, Future) and not item.future.done() and not item.future.running()
    assert rig.sched.pool is None and rig.sched.dispatched == 1
    assert rig.sched.book.count("open") == 1
    assert _history(rig) == ["CREATED", "VALIDATED", "PLANNED"]
    assert len(_events(rig, "subagent.spawn.immediate")) == 1
    assert _events(rig, "swarm.run.started") == []
    rig.sched.close()
    assert rig.mgr.release(item.sid) is None
    rig.sched.close()
    _terminal_once(rig, "cancelled")


@pytest.mark.parametrize("stage", ["before-sid", "after-sid"])
def test_open_setup_failure_is_one_registered_failed_future(lifecycle, monkeypatch, stage):
    rig = lifecycle
    real_status = rig.mod.stream_subagent_status

    def fail(*args, **kwargs):
        raise RuntimeError(stage)

    if stage == "before-sid":
        monkeypatch.setattr(rig.mgr, "new_subagent_id", fail)
    else:
        monkeypatch.setattr(rig.mod, "stream_subagent_status", fail)
    opened = rig.sched._open_run(rig.task, rig.agent)
    assert isinstance(opened.failure, rig.mod.Done)
    assert opened.failure.outcome == "FAILED" and stage in opened.failure.error
    assert opened.item.future.result() is opened.failure
    assert rig.sched.inflight[rig.task["task_id"]] is opened.item
    assert bool(opened.item.sid) == (stage == "after-sid")
    assert rig.sched.dispatched == 1 and rig.sched._internal == {rig.task["task_id"]: 1}
    assert rig.mgr.active_ids() == []
    assert _history(rig) == ["CREATED", "VALIDATED", "PLANNED", "CLAIMED", "FAILED"]
    assert [kind for kind, _ in rig.emitted] == ["task.result.rejected"]
    # Restore only the injected runner status fault, keeping fixture isolation.
    monkeypatch.setattr(rig.mod, "stream_subagent_status", real_status)
    rig.sched._complete(opened.item.future)
    assert rig.sched.inflight == {}
    rig.sched.close()
    rig.sched.close()
    _terminal_once(rig, "failed", has_sid=stage == "after-sid")


def test_python_submit_skips_pool_on_setup_failure(lifecycle, monkeypatch):
    rig = lifecycle
    calls = []
    rig.sched.pool = SimpleNamespace(submit=lambda *a: calls.append(a), shutdown=lambda **kw: None)

    def fail(*args, **kwargs):
        raise RuntimeError("no SID")

    monkeypatch.setattr(rig.mgr, "new_subagent_id", fail)
    rig.sched._submit(rig.task, rig.agent)
    assert calls == [] and rig.sched.dispatched == 1
    future = rig.sched.inflight[rig.task["task_id"]].future
    assert future.done() and future.result().outcome == "FAILED"
    rig.sched._complete(future)
    _terminal_once(rig, "failed", has_sid=False)


def test_python_pool_submission_failure_retains_structured_completion(lifecycle):
    rig = lifecycle
    seen = []

    def fail(worker, task, agent, sid, run):
        seen.append((worker, task, agent, sid, run))
        raise RuntimeError("pool closed")

    rig.sched.pool = SimpleNamespace(submit=fail, shutdown=lambda **kw: None)
    rig.sched._submit(rig.task, rig.agent)
    item = rig.sched.inflight[rig.task["task_id"]]
    assert seen == [(rig.sched._worker, rig.task, rig.agent, item.sid, item.run)]
    assert item.future.result().error == "E-INTERNAL: RuntimeError: pool closed"
    assert rig.sched.dispatched == 1 and rig.mgr.active_ids() == []
    rig.sched._complete(item.future)
    assert _history(rig)[-2:] == ["CLAIMED", "FAILED"]
    _terminal_once(rig, "failed")


@pytest.mark.parametrize("exit_path", ["success", "acquire-refusal", "pre-pickup-cancel"])
def test_worker_clears_run_and_slot_and_completion_records_one_terminal_state(lifecycle, monkeypatch, exit_path):
    rig = lifecycle
    opened = rig.sched._open_run(rig.task, rig.agent)
    item = opened.item
    executed, settled = [], []
    rig.sched.leases = SimpleNamespace(settle=settled.append)

    def execute(store_path, task, agent, args, ctx, repo, leases, handoffs):
        executed.append((rig.mod.current_run(), rig.mod._RUN_LOCAL, rig.mod._SESSIONS,
                         store_path, task, agent, ctx))
        assert rig.mod.current_run() is item.run
        assert rig.mgr.active_ids() == [item.sid]
        store = TaskStore(store_path)
        try:
            store.transition(task["task_id"], S.CLAIMED)
            store.transition(task["task_id"], S.IN_PROGRESS)
            store.transition(task["task_id"], S.IN_REVIEW)
        finally:
            store.conn.close()
        return task["task_id"], agent["id"], "IN_REVIEW"

    monkeypatch.setattr(rig.mod, "execute_one", execute)
    if exit_path == "acquire-refusal":
        def refuse(sid):
            raise rig.mod.SwarmError(rig.mod.ErrorCode.E_CAPACITY, "full")
        monkeypatch.setattr(rig.mgr, "acquire", refuse)
    elif exit_path == "pre-pickup-cancel":
        rig.sched.cancel.request("test")
    done = rig.sched._worker(rig.task, rig.agent, item.sid, item.run)
    assert rig.mod.current_run() is None
    assert rig.mgr.active_ids() == [] and rig.mgr.release(item.sid) is None
    item.future.set_result(done)
    rig.sched._complete(item.future)
    assert rig.sched.inflight == {} and rig.sched.dispatched == 1
    if exit_path == "success":
        assert executed == [(item.run, rig.mod._RUN_LOCAL, rig.mod._SESSIONS, rig.store.path,
                             rig.task, rig.agent, rig.ctx)]
        assert _history(rig)[-3:] == ["CLAIMED", "IN_PROGRESS", "IN_REVIEW"]
        state = "completed"
    elif exit_path == "acquire-refusal":
        assert executed == [] and settled == [rig.task["task_id"]]
        assert _history(rig)[-2:] == ["CLAIMED", "FAILED"]
        state = "failed"
    else:
        assert executed == [] and settled == [rig.task["task_id"]]
        assert done.outcome == "SKIPPED"
        assert _history(rig) == ["CREATED", "VALIDATED", "PLANNED"]
        state = "cancelled"
    rig.sched.close()
    rig.sched.close()
    _terminal_once(rig, state)


def test_finish_run_returns_metadata_without_backoff_or_retry(lifecycle, monkeypatch):
    rig = lifecycle
    item = rig.sched._open_run(rig.task, rig.agent).item
    calls = []
    monkeypatch.setattr(rig.sched, "_backpressure", lambda *args: calls.append("aimd"))
    monkeypatch.setattr(rig.sched, "_schedule_retry", lambda *args: calls.append("retry"))
    # The engine owns these steps, not _finish_run (the Future remains passive).
    del rig.sched.inflight[rig.task["task_id"]]
    rig.mgr.release(item.sid)
    result = rig.sched._finish_run(item, rig.mod.Done(rig.task["task_id"], rig.agent["id"], "FAILED", "failure"))
    assert isinstance(result, rig.mod.FinishedRun)
    assert result.failed is True and result.reason == "failure"
    assert calls == [] and not item.future.done()
    assert rig.sched.extension_down is False and rig.sched.closed is False
    assert rig.sched.dispatched == 1 and rig.sched._internal == {}
    _terminal_once(rig, "failed")


def test_python_completion_keeps_cleanup_and_decisions_in_original_order(lifecycle, monkeypatch):
    rig = lifecycle
    item = rig.sched._open_run(rig.task, rig.agent).item
    order = []
    real_release = rig.mgr.release
    real_finish = rig.sched._finish_run

    def release(sid):
        assert rig.sched.inflight == {}
        order.append("sid")
        return real_release(sid)

    def failure(task, agent, exc):
        order.append("failure")
        return rig.mod.Done(task["task_id"], agent["id"], "FAILED", "E-INTERNAL: test")

    def finish(i, done):
        assert i is item
        order.append("finish")
        return real_finish(i, done)

    def extension(store, tid):
        order.append("extension")
        return True

    monkeypatch.setattr(rig.mgr, "release", release)
    rig.sched.claims = SimpleNamespace(release=lambda tid: order.append("claims"))
    monkeypatch.setattr(rig.sched, "_internal_failure", failure)
    monkeypatch.setattr(rig.sched, "_finish_run", finish)
    monkeypatch.setattr(rig.sched, "_backpressure", lambda *args: order.append("aimd"))
    monkeypatch.setattr(rig.sched, "_schedule_retry", lambda *args: order.append("retry"))
    monkeypatch.setattr(rig.mod, "_extension_failed", extension)
    item.future.set_exception(KeyboardInterrupt("escaped worker"))
    rig.sched._complete(item.future)
    assert order == ["sid", "claims", "failure", "finish", "aimd", "retry", "extension"]
    assert rig.sched.extension_down is True and rig.sched.closed is True
    assert rig.sched.log[-1] == "stopped: omp extension failed to load; no further sessions this run"
    _terminal_once(rig, "failed")


def test_run_uses_active_synthetic_module_helpers_and_execute_one(lifecycle, monkeypatch):
    rig = lifecycle
    controllers, opened, finished, workers = [], [], [], []
    real_init = rig.mod.Scheduler.__init__
    real_open = rig.mod.Scheduler._open_run
    real_finish = rig.mod.Scheduler._finish_run
    real_execute = rig.mod.execute_one

    def init(controller, *args, **kwargs):
        controllers.append(controller)
        return real_init(controller, *args, **kwargs)

    def open_run(controller, task, agent):
        result = real_open(controller, task, agent)
        opened.append((controller, result.item))
        return result

    def finish_run(controller, item, done):
        finished.append((controller, item, done))
        return real_finish(controller, item, done)

    def execute(*args, **kwargs):
        controller = controllers[0]
        workers.append((rig.mod.current_run(), controller.mgr, rig.mod._RUN_LOCAL, rig.mod._SESSIONS))
        assert controller.mgr.active_ids() == [rig.mod.current_run().sid]
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(rig.mod.Scheduler, "__init__", init)
    monkeypatch.setattr(rig.mod.Scheduler, "_open_run", open_run)
    monkeypatch.setattr(rig.mod.Scheduler, "_finish_run", finish_run)
    monkeypatch.setattr(rig.mod, "execute_one", execute)
    out = rig.mod.run(rig.args, rig.ctx)
    assert len(controllers) == len(opened) == len(finished) == len(workers) == 1
    controller, item = opened[0]
    assert controller is controllers[0] and finished[0][0] is controller
    assert finished[0][1].run is item.run
    assert workers == [(item.run, controller.mgr, rig.mod._RUN_LOCAL, rig.mod._SESSIONS)]
    assert controller.book.tel is controller.tel and item.run.tel is controller.tel
    assert controller.mgr.active_ids() == [] and controller.inflight == {}
    assert controller.dispatched == 1 and out["scheduler"] == "continuous"
    assert out["counts"] == {"IN_REVIEW": 1}
    assert _history(rig) == ["CREATED", "VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW"]
    controller.close()
    controller.close()
    events = _events(rig, "swarm.run.finished")
    assert len(events) == 1 and events[0]["payload"]["state"] == "completed"
    assert len(_events(rig, "subagent.complete.immediate")) == 1
    assert controller.book.unsettled() == []


def test_a_signal_reentering_request_forces_the_cancel_and_keeps_its_signum_without_deadlock(lifecycle, monkeypatch):
    rig = lifecycle
    cancel = rig.mod.CancelRequest()
    real_monotonic = time.monotonic
    nested = []

    def monotonic():
        if not nested:  # the first clock read is inside the outer request's critical section
            nested.append(cancel.request("signal SIGTERM", signal.SIGTERM))
        return real_monotonic()

    monkeypatch.setattr(rig.mod, "time", types.SimpleNamespace(monotonic=monotonic))
    outer = []
    worker = threading.Thread(target=lambda: outer.append(cancel.request("swarm timeout")), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert outer == [True] and nested == [False]
    assert cancel.event.is_set() and cancel.force.is_set()
    assert cancel.reason == "swarm timeout" and cancel.signum == signal.SIGTERM


def test_reap_group_abandons_a_child_the_kernel_will_not_reap(lifecycle, monkeypatch, caplog):
    rig = lifecycle
    signalled = []
    monkeypatch.setattr(rig.mod, "kill_group", lambda pgid, sig: signalled.append((pgid, sig)))

    class Unreapable:
        pid = 0
        killed = False

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("child", timeout)

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("child", timeout)

    fake = Unreapable()
    result = []
    worker = threading.Thread(target=lambda: result.append(rig.mod.reap_group(fake)), daemon=True)
    with caplog.at_level(logging.ERROR, logger="swarm.run"):
        worker.start()
        worker.join(5)
    assert not worker.is_alive()
    assert result == [("", "")] and fake.killed
    assert signalled == [(0, signal.SIGTERM), (0, signal.SIGKILL)]
    errors = [r for r in caplog.records if r.name == "swarm.run" and r.levelno == logging.ERROR]
    assert len(errors) == 1 and "pid 0" in errors[0].getMessage()


def _snapshot(rig):
    """Every counter and record a repeated release or close could touch, read back from its real owner."""
    path = rig.store.path.parent / "events.jsonl"
    return (rig.sched.dispatched, dict(rig.sched._internal), list(rig.sched.log), rig.sched.closed,
            rig.sched.extension_down, rig.mgr.active_ids(), _history(rig), list(rig.emitted), (path.read_text() if path.exists() else ""))


@pytest.mark.parametrize("exit_path", ["abandoned", "setup-failed-after-sid"])
def test_repeated_release_and_close_change_no_counter_or_event(lifecycle, monkeypatch, exit_path):
    rig = lifecycle
    real_status = rig.mod.stream_subagent_status
    if exit_path == "setup-failed-after-sid":
        def fail(*args, **kwargs):
            raise RuntimeError("after sid")
        monkeypatch.setattr(rig.mod, "stream_subagent_status", fail)
    item = rig.sched._open_run(rig.task, rig.agent).item
    monkeypatch.setattr(rig.mod, "stream_subagent_status", real_status)
    if exit_path == "setup-failed-after-sid":
        rig.sched._complete(item.future)
        state = "failed"
    else:
        state = "cancelled"
    rig.sched.close()
    assert rig.sched.dispatched == 1 and rig.mgr.active_ids() == []
    before = _snapshot(rig)
    assert rig.mgr.release(item.sid) is None
    rig.sched.close()
    assert _snapshot(rig) == before
    _terminal_once(rig, state)


def test_repeated_pool_shutdown_leaves_the_queue_empty(lifecycle):
    rig = lifecycle
    barrier = threading.Barrier(2, timeout=10)
    pool = rig.mod.WorkerPool(2, "shutdown-test")
    # Two calls that block on each other force two real worker threads into existence.
    futures = [pool.submit(barrier.wait) for _ in range(2)]
    for future in futures:
        future.result(timeout=10)
    threads = list(pool._threads)
    assert len(threads) == 2 and all(thread.is_alive() for thread in threads)
    pool.shutdown()
    pool.shutdown()
    assert pool._queue.qsize() == 0
    assert not any(thread.is_alive() for thread in threads)


def test_dot_and_dotdot_correlation_ids_are_refused_and_write_no_flag(lifecycle, tmp_path):
    rig = lifecycle
    sdir = tmp_path / ".swarm"
    for corr in ("..", "."):
        with pytest.raises(rig.mod.SwarmError) as refused:
            rig.mod.write_cancel_flag(sdir, corr, "operator stop", 1.0)
        assert refused.value.code == rig.mod.ErrorCode.E_INPUT
        with pytest.raises(rig.mod.SwarmError):
            rig.mod.clear_cancel_flag(sdir, corr)
    assert not (sdir / "cancel").exists()
