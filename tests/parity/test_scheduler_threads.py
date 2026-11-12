"""Native loop thread, output and timing behaviour, driven through ``swarm.rust_scheduler.RustScheduler``.

The facade runs the real ``swarm._native.run_scheduler`` loop and its real worker pool. The doubles are the controller (the
Scheduler attributes the facade reads, as in tests/test_scheduler_ports.py) and the runner-local helpers. Worker callables run
on native pool threads, so the worker path is the production one. No debug API is used.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

native = pytest.importorskip("swarm._native")
if not hasattr(native, "run_scheduler"):  # pragma: no cover - the extension always exports it
    pytest.skip("swarm._native does not export run_scheduler", allow_module_level=True)

from swarm import rust_scheduler  # noqa: E402
from swarm.rust_scheduler import RunnerHelpers, RunSettings, RustScheduler  # noqa: E402

WORKER_FAILED = "E-INTERNAL: scheduler worker callback failed"
CANARY = "INERT_THREADS_CANARY_5C2E"
HOLD_BOUND_S = 3.0
SETTINGS = RunSettings(
    cooldown_s=0.0,
    retry_cap_s=120.0,
    backoff_base_s=0.1,
    backoff_cap_s=5.0,
    wait_tick_s=0.05,
    internal_failure_limit=3,
    kill_grace_s=0.5,
    kill_settle_s=0.5,
)


@pytest.fixture(autouse=True)
def no_store_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rust_scheduler, "reconcile", lambda *args, **kwargs: [])
    monkeypatch.setattr(rust_scheduler, "log_phase", lambda *args, **kwargs: {})


def make_task(task_id: str) -> dict[str, Any]:
    # attempt == max_attempts: a failed task is never retried, so the run has no backoff sleep to reason about.
    return {
        "task_id": task_id,
        "state": "PLANNED",
        "priority": "P2",
        "dag_depth": 0,
        "created_at": 1.0,
        "attempt": 1,
        "max_attempts": 1,
        "notes_json": {},
        "agent_id": "a1",
        "capability": "docs",
    }


class Store:
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self.tasks = {task["task_id"]: task for task in tasks}

    def list(self, *, correlation_id: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        return list(self.tasks.values())

    def get(self, task_id: str) -> dict[str, Any]:
        return self.tasks[task_id]

    def set_notes(self, task_id: str, **values: object) -> None:
        self.tasks[task_id]["notes_json"].update(values)


class Controller:
    """The Scheduler attributes and methods the facade reads. The worker and observer hooks belong to one test each."""

    def __init__(self, root: Path, tasks: list[dict[str, Any]], *, width: int, once: bool, budget: int) -> None:
        self.corr = "corr-threads"
        self.repo = root
        self.sdir = root / "swarm"
        self.store = Store(tasks)
        self.ctx = SimpleNamespace(emit=lambda *args, **kwargs: None)
        self.mgr = SimpleNamespace(max_concurrent=width, release=lambda sid: None)
        self.tel = SimpleNamespace(degraded=lambda *args, **kwargs: None, clear_degraded=lambda *args, **kwargs: None)
        self.book = SimpleNamespace(seen=lambda task, *, waiting, queue_depth: "run")
        self.leases = None
        self.handoffs = None
        self.claims = SimpleNamespace(release=lambda owner: 0)
        self.cancel = SimpleNamespace(event=threading.Event(), force=threading.Event(), reason=None, at=None)
        self.barrier = False
        self.once = once
        self.max_rounds = 4
        self.budget = budget
        self.fail_streak = 3
        self.cancel_grace = 0.0
        self.shutdown_budget = 2.0
        self.kill_reason = ""
        self.rounds = 0
        self.dispatched = 0
        self.closed = False
        self.extension_down = False
        self.t0 = 0.0
        self.log: list[str] = []
        self.inflight: dict[str, SimpleNamespace] = {}
        self.ticks: list[tuple[int, int, int, int, dict[str, str]]] = []
        self.completions: list[tuple[str, str, str | None]] = []
        self.threads: list[int] = []
        self.kills: list[dict[str, Any]] = []
        self.flags: list[tuple[str, str, float]] = []
        self.work: Callable[[str], object] = lambda task_id: None
        self.on_tick: Callable[[], None] | None = None
        self.on_finish: Callable[[], None] | None = None

    def _start_monitor(self) -> None:
        return None

    def _open_run(self, task: dict[str, Any], agent: dict[str, Any]) -> SimpleNamespace:
        tid = task["task_id"]
        item = SimpleNamespace(task=task, agent=agent, sid=f"sid-{tid}", run=SimpleNamespace(rate_limited=None))
        self.inflight[tid] = item
        self.dispatched += 1
        return SimpleNamespace(item=item, failure=None)

    def _worker(self, task: dict[str, Any], agent: dict[str, Any], sid: str, run: object) -> SimpleNamespace:
        # Runs on a native pool thread. Records the thread, then the test's work; an exception here is a worker fault.
        self.threads.append(threading.get_ident())
        self.work(task["task_id"])
        return SimpleNamespace(task_id=task["task_id"], agent_id=agent["id"], outcome="DONE", error=None)

    def _internal_failure(self, task: dict[str, Any], agent: dict[str, Any], exc: Exception) -> SimpleNamespace:
        return SimpleNamespace(task_id=task["task_id"], agent_id=agent["id"], outcome="FAILED", error=str(exc))

    def _finish_run(self, item: object, done: Any) -> SimpleNamespace:
        if self.on_finish is not None:
            self.on_finish()
        self.completions.append((done.task_id, done.outcome, done.error))
        self.store.tasks[done.task_id]["state"] = "DONE" if done.outcome == "DONE" else "FAILED"
        failed = done.error is not None or done.outcome == "FAILED"
        return SimpleNamespace(failed=failed, reason=done.error or "")

    def _tick(self, ready: int, submitted: int, eff: int, free: int, skipped: dict[str, str]) -> None:
        self.ticks.append((ready, submitted, eff, free, dict(skipped)))
        if self.on_tick is not None:
            self.on_tick()


def make_helpers(controller: Controller, *, kill_delay: float = 0.0) -> RunnerHelpers:
    def dispatchable(store: Store, corr: str, emit: object, handoffs: object = None, **kwargs: Any) -> list[dict[str, Any]]:
        skip = kwargs.get("skip", frozenset())
        return [task for task in store.list() if task["state"] == "PLANNED" and task["task_id"] not in skip]

    def kill_sessions(grace: float = 0.0, reason: str = "") -> None:
        started = time.monotonic()
        if kill_delay:
            time.sleep(kill_delay)
        controller.kills.append({"grace": grace, "reason": reason, "took": time.monotonic() - started})

    def write_cancel_flag(sdir: Path, corr: str, reason: str, grace_s: float) -> Path:
        controller.flags.append((corr, reason, grace_s))
        return sdir

    return RunnerHelpers(
        dispatchable=dispatchable,
        resolve_agent=lambda task: {"id": "a1", "slug": "agent-one", "max_parallel": controller.mgr.max_concurrent},
        extension_failed=lambda store, tid: False,
        kill_sessions=kill_sessions,
        write_cancel_flag=write_cancel_flag,
        safe=lambda fn, *args, **kwargs: fn(*args, **kwargs),
    )


def run_facade(controller: Controller, helpers: RunnerHelpers) -> tuple[dict[str, object], Any]:
    fence = native.NativeCleanupFence()
    facade = RustScheduler(
        controller,
        helpers=helpers,
        settings=SETTINGS,
        run_native=native.run_scheduler,
        fence_factory=lambda: fence,
    )
    return facade.run(), fence


def request_cancel(controller: Controller, reason: str) -> None:
    controller.cancel.reason = reason
    controller.cancel.at = time.monotonic()
    controller.cancel.event.set()


def native_thread_count() -> int:
    return len(os.listdir("/proc/self/task"))


def wait_for(predicate: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def test_overbudget_kill_callback_is_reported_as_abandoned_and_the_fence_stays_open(tmp_path: Path) -> None:
    controller = Controller(tmp_path, [make_task("slow")], width=1, once=True, budget=1)
    controller.shutdown_budget = 0.3
    release = threading.Event()
    controller.work = lambda task_id: release.wait(HOLD_BOUND_S)

    def cancel_on_first_pass() -> None:
        if not controller.cancel.event.is_set():
            request_cancel(controller, "operator")

    controller.on_tick = cancel_on_first_pass
    try:
        summary, fence = run_facade(controller, make_helpers(controller, kill_delay=0.6))
        assert summary["stopped"] == "cancelled"
        assert summary["abandoned"] == ["slow"]
        assert summary["dispatched"] == 1
        assert len(controller.kills) == 1
        kill = controller.kills[0]
        assert kill["reason"] == "E-TIMEOUT: runner shutdown"
        assert 0.0 <= kill["grace"] <= 0.3
        assert kill["took"] > 0.3, "the kill callback must overrun the shutdown budget for this test to mean anything"
        assert controller.completions == []
        assert "abandoned after kill: ['slow']" in controller.log
        assert fence.wait(0.0) is False, "the abandoned worker still holds native references"
    finally:
        release.set()
    assert fence.wait(2.0) is True


def test_cancel_observed_in_the_final_drain_stops_the_run_cancelled(tmp_path: Path) -> None:
    controller = Controller(tmp_path, [make_task("last")], width=1, once=True, budget=1)
    controller.on_finish = lambda: request_cancel(controller, "operator")

    summary, fence = run_facade(controller, make_helpers(controller))

    assert summary["stopped"] == "cancelled"
    assert summary["closed"] is True
    assert summary["dispatched"] == 1
    assert summary["abandoned"] == []
    assert controller.completions == [("last", "DONE", None)]
    assert controller.kills == []
    assert [reason for _, reason, _ in controller.flags] == ["operator"]
    assert any(line.startswith("cancelling: operator; 0 session(s)") for line in controller.log)
    assert fence.wait(2.0) is True


def test_idle_final_drain_without_a_cancel_stops_closed(tmp_path: Path) -> None:
    controller = Controller(tmp_path, [make_task("last")], width=1, once=True, budget=1)

    summary, fence = run_facade(controller, make_helpers(controller))

    assert summary["stopped"] == "closed"
    assert summary["closed"] is True
    assert controller.completions == [("last", "DONE", None)]
    assert controller.flags == []
    assert controller.kills == []
    assert fence.wait(2.0) is True


@pytest.mark.parametrize("width", [8, 32])
def test_every_worker_completes_once_on_its_own_thread_and_none_is_left_behind(tmp_path: Path, width: int) -> None:
    if not os.path.isdir("/proc/self/task"):
        pytest.skip("the native thread census reads /proc/self/task")
    tids = [f"w{index:02d}" for index in range(width)]
    controller = Controller(tmp_path, [make_task(tid) for tid in tids], width=width, once=True, budget=width)
    # Every worker must be alive at once, so the barrier also proves that all `width` ran concurrently.
    gate = threading.Barrier(width)
    controller.work = lambda task_id: gate.wait(HOLD_BOUND_S)

    py_before = {thread.ident for thread in threading.enumerate()}
    py_count_before = len(threading.enumerate())
    native_before = native_thread_count()

    summary, fence = run_facade(controller, make_helpers(controller))

    assert summary["dispatched"] == width
    assert summary["stopped"] == "closed"
    assert summary["abandoned"] == []
    assert len(controller.completions) == width
    assert sorted(tid for tid, _, _ in controller.completions) == tids
    assert {outcome for _, outcome, _ in controller.completions} == {"DONE"}
    assert len(set(controller.threads)) == width
    assert threading.get_ident() not in controller.threads
    assert fence.wait(2.0) is True
    assert {thread.ident for thread in threading.enumerate()} - py_before == set()
    assert len(threading.enumerate()) <= py_count_before
    assert wait_for(lambda: native_thread_count() <= native_before), "a native worker thread outlived the run"


def test_workers_that_release_the_interpreter_all_complete_with_their_results(tmp_path: Path) -> None:
    tids = [f"g{index}" for index in range(8)]
    controller = Controller(tmp_path, [make_task(tid) for tid in tids], width=3, once=False, budget=8)
    results: dict[str, int] = {}
    active = 0
    peak = 0
    lock = threading.Lock()

    def work(task_id: str) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)  # the interpreter is released here and must be reacquired to finish the worker
        total = sum(range(1000))
        with lock:
            results[task_id] = total
            active -= 1

    controller.work = work

    summary, fence = run_facade(controller, make_helpers(controller))

    assert summary["stopped"] == "closed"
    assert summary["dispatched"] == 8
    assert sum(tick[1] for tick in controller.ticks) == 8, "every submission must be visible to the pass observer"
    assert sorted(tid for tid, _, _ in controller.completions) == sorted(tids)
    assert {outcome for _, outcome, _ in controller.completions} == {"DONE"}
    assert results == {tid: 499500 for tid in tids}
    assert peak <= 3
    assert fence.wait(2.0) is True


def test_worker_fault_stderr_canary_is_not_persisted_in_observer_records(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    controller = Controller(tmp_path, [make_task("fault")], width=1, once=True, budget=1)

    def faulting(task_id: str) -> None:
        # Rust panic injection is cfg(test)-only. A worker port exception takes the same native fault route.
        os.write(2, f"{CANARY} worker stderr\n".encode())
        raise RuntimeError(f"worker panic {CANARY}")

    controller.work = faulting

    summary, fence = run_facade(controller, make_helpers(controller))

    assert CANARY in capfd.readouterr().err, "the fault must really emit the canary on stderr"
    assert summary["stopped"] == "closed"
    assert summary["dispatched"] == 1
    assert controller.completions == [("fault", "FAILED", WORKER_FAILED)]
    observer = repr((summary, controller.completions, controller.ticks, controller.log, controller.flags, controller.kills))
    assert CANARY not in observer
    assert fence.wait(2.0) is True
