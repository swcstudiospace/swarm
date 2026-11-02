"""Host side of the native scheduler loop, composed around the active Python controller.

``swarm._native.run_scheduler`` owns the decision loop, the claim table, AIMD, backoff and the worker pool. This module
implements the sixteen callbacks that loop reads. It never constructs a controller, never imports the runner, and never
starts a Python ``WorkerPool``, ``Scheduler.start()`` or ``Scheduler.loop()``. A dispatch is registered through the
controller's ``_open_run``, executed through its ``_worker`` and finished through its ``_finish_run``, so the Python
oracle's RunBook, RunHandle, SID, status and lease bookkeeping stays on one path.

``Scheduler._submit`` is deliberately not used: it submits to the Python pool, which the native pool replaces.
The runner-local helpers (``dispatchable``, ``resolve_agent``, ``extension_failed``, ``kill_sessions``,
``write_cancel_flag``, the telemetry guard) arrive in ``RunnerHelpers``; the environment-resolved settings arrive in
``RunSettings``. The controller owns every other value.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, NamedTuple, Protocol

from swarm.claims import parse_claims
from swarm.errors import ErrorCode, SwarmError
from swarm.limiter import AimdLimiter
from swarm.results import reconcile
from swarm.retry import is_transient
from swarm.runlog import log_phase
from swarm.taskstore import TaskState as S

if TYPE_CHECKING:
    from swarm.claims import ClaimStore
    from swarm.results import Emit
    from swarm.substrate_handoff import HandoffBridge
    from swarm.substrate_lease import LeaseBridge
    from swarm.taskstore import TaskRow, TaskStore
    from swarm.telemetry import Telemetry

__all__ = ["RunSettings", "RunnerHelpers", "RustScheduler", "SchedulerController"]

_LOG = logging.getLogger("swarm.run")

_SUMMARY_KEYS = frozenset(
    {"rounds", "dispatched", "effective_parallel", "closed", "extension_down", "abandoned", "stopped", "waiting"}
)
_STOP_REASONS = frozenset({"closed", "nothing-ready", "lease-wait", "stalled", "cancelled"})
_TERMINAL_STATES = frozenset({S.DONE.value, S.CANCELLED.value, S.ESCALATED.value})
_REDUCED_MODE = "reduced_parallelism"
_KILL_GRACE_FLOOR_S = 0.5  # drain.rs KILL_GRACE_FLOOR_S: the floor of the kill grace the native drain plans
_STOP_GRACE_S = 10.0  # drain.rs STOP_GRACE_S: the bound the native drain puts on the kill grace as well

_ReadyRow = tuple[str, str | None, int | None, float | None, float | None]
_DoneTuple = tuple[str, str, str, str | None]
_ClaimPair = tuple[str, str]


class _Done(NamedTuple):
    """The worker's result, with the field names the controller's Done carries."""

    task_id: str
    agent_id: str
    outcome: str
    error: str | None = None


def _done_tuple(done: _Done) -> _DoneTuple:
    return (done.task_id, done.agent_id, done.outcome, done.error)


def _kill_reason(reason: str) -> str:
    return "E-TIMEOUT: swarm timeout" if reason == "swarm-timeout" else "E-TIMEOUT: runner shutdown"


class _Cancel(Protocol):
    event: threading.Event
    force: threading.Event
    reason: str | None
    at: float | None


class _Manager(Protocol):
    max_concurrent: int

    def release(self, sid: str) -> object: ...


class _Context(Protocol):
    emit: Emit


class _Book(Protocol):
    def seen(self, task: TaskRow, *, waiting: bool, queue_depth: int) -> str: ...


class _RunHandle(Protocol):
    rate_limited: float | None
    submitted_at: float


class _Dispatch(Protocol):
    @property
    def task(self) -> TaskRow: ...
    @property
    def agent(self) -> TaskRow: ...
    @property
    def sid(self) -> str: ...
    @property
    def run(self) -> _RunHandle: ...


class _Opened(Protocol):
    @property
    def item(self) -> _Dispatch: ...
    @property
    def failure(self) -> _Done | None: ...


class _Finished(Protocol):
    @property
    def failed(self) -> bool: ...
    @property
    def reason(self) -> str: ...


class SchedulerController(Protocol):
    """The active Scheduler's state and bound methods that the facade reads or updates.

    The facade composes around this object. It never constructs one, and it reaches the controller only through these
    members.
    """

    corr: str
    repo: Path
    sdir: Path
    store: TaskStore
    ctx: _Context
    mgr: _Manager
    tel: Telemetry
    limiter: AimdLimiter
    book: _Book
    leases: LeaseBridge | None
    handoffs: HandoffBridge | None
    claims: ClaimStore
    cancel: _Cancel
    barrier: bool
    once: bool
    max_rounds: int
    budget: int
    fail_streak: int
    cancel_grace: float
    shutdown_budget: float
    kill_reason: str
    rounds: int
    dispatched: int
    closed: bool
    extension_down: bool
    t0: float
    log: list[str]
    inflight: dict[str, _Dispatch]

    def _start_monitor(self) -> None: ...
    def _open_run(self, task: TaskRow, agent: TaskRow) -> _Opened: ...
    def _worker(self, task: TaskRow, agent: TaskRow, sid: str, run: _RunHandle) -> _Done: ...
    def _internal_failure(self, task: TaskRow, agent: TaskRow, exc: Exception) -> _Done: ...
    def _finish_run(self, item: _Dispatch, done: _Done) -> _Finished: ...
    def _tick(self, ready: int, submitted: int, eff: int, free: int, skipped: dict[str, str]) -> None: ...


@dataclass(frozen=True)
class RunSettings:
    """The RunConfig values the controller does not own. The caller's Python helpers resolve them from the environment."""

    cooldown_s: float
    retry_cap_s: float
    backoff_base_s: float
    backoff_cap_s: float
    wait_tick_s: float
    internal_failure_limit: int
    kill_grace_s: float
    kill_settle_s: float


@dataclass(frozen=True)
class RunnerHelpers:
    """Runner-local functions the facade calls, bound by the caller. The facade never imports the runner."""

    dispatchable: Callable[..., list[TaskRow]]
    resolve_agent: Callable[[TaskRow], TaskRow]
    extension_failed: Callable[[TaskStore, str], bool]
    kill_sessions: Callable[..., None]
    write_cancel_flag: Callable[[Path, str, str, float], object]
    safe: Callable[..., object]


def _row(task: TaskRow) -> _ReadyRow:
    until: object = task["notes_json"].get("retry_after")
    retry_after: float | None = None
    if isinstance(until, (int, float)) and not isinstance(until, bool):
        retry_after = float(until)
    return (task["task_id"], task.get("priority"), task.get("dag_depth"), task.get("created_at"), retry_after)


def _effective_of(limiter: AimdLimiter, cap: int) -> int:
    """The controller's effective parallelism for a limiter: the cap lowered by it, at least 1 (Scheduler.effective_parallel)."""
    return max(1, min(cap, limiter.current()))


class _Ports:
    """The sixteen callbacks ``run_scheduler`` reads by name, with the state of one run."""

    def __init__(
        self, controller: SchedulerController, helpers: RunnerHelpers, settings: RunSettings, limiter: AimdLimiter
    ) -> None:
        self._c = controller
        self._h = helpers
        self._s = settings
        self._limiter = limiter
        self._ready: dict[str, TaskRow] = {}
        self._agents: dict[str, TaskRow] = {}
        # the completions' AIMD record: the state Scheduler._backpressure keeps on the controller
        self._streak: set[str] = set()
        self._streak_tripped = False
        self._last_decrease = 0.0

    def _task(self, task_id: str) -> TaskRow:
        return self._ready[task_id]

    # ---- pass --------------------------------------------------------------------------------------------------------
    def begin_pass(self, round_no: int, in_flight: list[str]) -> list[_ReadyRow]:
        # The controller's inflight set is the same set `in_flight` names, and it is what dispatchable skips.
        c = self._c
        c.rounds = round_no
        store, corr, emit = c.store, c.corr, c.ctx.emit
        stop = c.cancel.event.is_set
        c.log += reconcile(store, corr, emit, leases=c.leases, handoffs=c.handoffs, stop_requested=stop)
        log_phase("reconcile", {"round": round_no}, source="A01@swarm_run", correlation_id=corr, root=c.repo)
        if c.handoffs is not None:
            c.handoffs.flush(stop_requested=stop)
        if stop():
            return []
        ready = self._h.dispatchable(
            store, corr, emit, handoffs=c.handoffs, skip=frozenset(c.inflight), stop_requested=stop
        )
        self._ready = {task["task_id"]: task for task in ready}
        self._agents = {}
        queue_depth = len(ready)
        for task in store.list(correlation_id=corr):
            if stop():
                break  # the rows gathered so far are returned; the native loop admits nothing once a stop is seen
            tid = task["task_id"]
            if tid in c.inflight:
                continue
            if tid in self._ready:
                c.book.seen(task, waiting=False, queue_depth=queue_depth)
            elif task["state"] in (S.CREATED.value, S.VALIDATED.value, S.PLANNED.value, S.RETRY.value):
                c.book.seen(task, waiting=True, queue_depth=queue_depth)
        return [_row(task) for task in ready]

    def observe_pass(self, info: dict[str, Any]) -> None:
        c = self._c
        c.rounds = info["round"]
        # Oracle: Scheduler._release_unsubmitted. A pair already in flight belongs to its worker.
        for task_id, _agent_id in info["unsubmitted"]:
            if task_id in c.inflight:
                continue
            c.claims.release(task_id)
            if c.leases is not None:
                c.leases.settle(task_id)
        effective = int(info["effective_parallel"])
        self._check_effective(effective)
        skipped: dict[str, str] = {task_id: reason for task_id, reason in info["skipped"]}
        c._tick(info["ready"], info["submitted"], effective, info["free"], skipped)

    def _check_effective(self, effective: int) -> None:
        # The native loop decides the limit; the controller's limiter replays its completions (_observe_backpressure), so
        # the two must agree at every pass. A difference is reported, not repaired.
        expected = _effective_of(self._limiter, self._c.mgr.max_concurrent)
        if effective != expected:
            _LOG.warning("native effective_parallel %d differs from the controller's %d", effective, expected)

    def _observe_backpressure(self, run: _RunHandle, failed: bool, task_id: str) -> None:
        """One completion's AIMD record, fed to the controller's limiter the way Scheduler._backpressure feeds it.

        The native result has neither the before/after of a decrease nor the streak count, and the native loop writes its
        own log lines, so this keeps only the state and the swarm.degraded records. Same events, same limiter class."""
        c, limiter = self._c, self._limiter
        before = limiter.current()
        if run.rate_limited is not None:
            if run.submitted_at < self._last_decrease:
                return  # one provider burst hits every session already running: one decrease, not k
            limiter.on_rate_limit(run.rate_limited or None)
            after = limiter.current()
            if after < before:
                self._last_decrease = time.monotonic()
                self._h.safe(c.tel.degraded, "rate_limited", mode=_REDUCED_MODE,
                             detail=f"effective_parallel {before} -> {after}")
        elif failed:
            if c.fail_streak <= 0 or c.cancel.event.is_set():  # circuit off, or a shutdown killing sessions
                return
            self._streak.add(task_id)
            if len(self._streak) < c.fail_streak or self._streak_tripped:
                return
            self._streak_tripped = True
            limiter.on_rate_limit(None)
            after = limiter.current()
            if after < before:
                self._h.safe(c.tel.degraded, "consecutive_failures", mode=_REDUCED_MODE,
                             detail=f"{len(self._streak)} failures in a row; effective_parallel {before} -> {after}")
        else:
            self._streak.clear()
            self._streak_tripped = False
            limiter.on_success()
            after = limiter.current()
            if after > before and after >= c.mgr.max_concurrent:
                self._h.safe(c.tel.clear_degraded, _REDUCED_MODE)

    # ---- selection ---------------------------------------------------------------------------------------------------
    def agent_for(self, task_id: str) -> tuple[str, str, int] | None:
        task = self._task(task_id)
        try:
            agent = self._h.resolve_agent(task)
        except (KeyError, IndexError):
            return None
        self._agents[task_id] = agent
        return (agent["id"], agent["slug"], max(1, int(agent.get("max_parallel") or 1)))

    def claims_for(self, task_id: str) -> tuple[list[_ClaimPair], None] | tuple[None, str]:
        try:
            claims = parse_claims(self._task(task_id)["notes_json"])
        except ValueError as exc:
            return (None, str(exc))
        return ([(claim.pattern, claim.mode) for claim in claims], None)

    def block(self, task_id: str, reason: str) -> None:
        self._c.store.transition(task_id, S.BLOCKED, reason=reason)

    def lease(self, task_id: str, agent_id: str) -> tuple[bool, str, str | None] | None:
        leases = self._c.leases
        if leases is None:
            return None
        claim = leases.acquire(self._task(task_id), agent_id)
        holder = (claim.holder or {}).get("session_id")
        return (claim.dispatch, claim.status, holder)

    # ---- dispatch ----------------------------------------------------------------------------------------------------
    def submit(
        self, task_id: str, agent_id: str
    ) -> tuple[Literal["started"], str] | tuple[Literal["failed"], _DoneTuple]:
        agent = self._agents[task_id]
        if agent["id"] != agent_id:
            raise ValueError(f"submit agent {agent_id!r} is not the agent resolved for {task_id!r}")
        opened = self._c._open_run(self._task(task_id), agent)
        failure = opened.failure
        if failure is not None:
            return ("failed", _done_tuple(failure))
        return ("started", opened.item.sid)

    def worker(self, task_id: str, agent_id: str, sid: str) -> _DoneTuple:
        item = self._c.inflight[task_id]
        return _done_tuple(self._c._worker(item.task, item.agent, sid, item.run))

    def internal_failure(self, task_id: str, agent_id: str, text: str) -> _DoneTuple:
        item = self._c.inflight[task_id]
        message = text.removeprefix(f"{ErrorCode.E_INTERNAL.value}: ")
        failure = SwarmError(ErrorCode.E_INTERNAL, message)
        return _done_tuple(self._c._internal_failure(item.task, item.agent, failure))

    def complete(self, done: _DoneTuple) -> tuple[bool, float | None, int | None, bool, bool]:
        # Oracle: Scheduler._complete. The removal, SID release and claim release come first, then _finish_run.
        c = self._c
        task_id, agent_id, outcome, error = done
        item = c.inflight.pop(task_id)
        if item.sid:
            c.mgr.release(item.sid)
        c.claims.release(task_id)
        finished = c._finish_run(item, _Done(task_id, agent_id, outcome, error))
        self._observe_backpressure(item.run, finished.failed, task_id)
        cancelled = c.cancel.event.is_set()
        retry_attempt: int | None = None
        if finished.failed and not cancelled:
            current = c.store.get(task_id)
            if current["attempt"] < current["max_attempts"] and is_transient(None, finished.reason):
                retry_attempt = int(current["attempt"])
        extension_down = self._h.extension_failed(c.store, task_id)
        return (finished.failed, item.run.rate_limited, retry_attempt, extension_down, cancelled)

    def set_retry_after(self, task_id: str, wall: float) -> None:
        # The native loop logs the transient-retry line itself; this writes only the note. Oracle: _schedule_retry.
        c = self._c
        if c.cancel.event.is_set():
            return
        c.store.set_notes(task_id, retry_after=wall)

    def random_unit(self) -> float:
        return random.uniform(-1.0, 1.0)

    # ---- cancel ------------------------------------------------------------------------------------------------------
    def cancel_state(self) -> tuple[bool, bool, str | None, float]:
        cancel = self._c.cancel
        is_set = cancel.event.is_set()
        elapsed = max(0.0, time.monotonic() - cancel.at) if is_set and cancel.at is not None else 0.0
        return (is_set, cancel.force.is_set(), cancel.reason, elapsed)

    def cancel_begin(self, reason: str, _wait_s: float, in_flight: int) -> None:
        # Oracle: the head of Scheduler._cancel_drain. The native loop waits to the same deadline; what it advertises (the
        # flag, the log line, the degraded detail) is the planned grace, not the time still left on the wait.
        c = self._c
        grace, _kill_grace = self._drain_plan()
        c.kill_reason = _kill_reason(reason)
        capped = ""
        if grace < c.cancel_grace:
            capped = f" (SWARM_CANCEL_GRACE_S {c.cancel_grace:g}s, shutdown budget {c.shutdown_budget:g}s)"
        c.log.append(f"cancelling: {reason}; {in_flight} session(s), grace {grace:g}s{capped}")
        self._h.safe(c.tel.degraded, f"cancel requested: {reason}", mode="cancelling", detail=f"grace {grace:g}s")
        try:
            self._h.write_cancel_flag(c.sdir, c.corr, reason, grace)
        except OSError:
            _LOG.warning("cancel flag could not be written; sessions will only see the kill", exc_info=True)

    def _drain_plan(self) -> tuple[float, float]:
        """(grace_s, kill_grace_s) of Scheduler._cancel_drain, with the bounds drain.rs uses for the same plan."""
        c, s = self._c, self._s
        kill_grace = max(_KILL_GRACE_FLOOR_S, min(s.kill_grace_s, _STOP_GRACE_S, c.shutdown_budget - s.kill_settle_s))
        grace = max(0.0, min(c.cancel_grace, c.shutdown_budget - kill_grace - s.kill_settle_s))
        return grace, kill_grace

    def kill(self, grace_s: float, reason: str) -> None:
        self._h.kill_sessions(grace=grace_s, reason=_kill_reason(reason))

    def log(self, line: str) -> None:
        self._c.log.append(line)


class RustScheduler:
    """Runs the native scheduler loop around the active controller and maps its summary back onto that controller."""

    def __init__(
        self,
        controller: SchedulerController,
        *,
        helpers: RunnerHelpers,
        settings: RunSettings,
        run_native: Callable[..., dict[str, object]],
        fence_factory: Callable[[], object],
        limiter: AimdLimiter | None = None,
    ) -> None:
        self._controller = controller
        self._helpers = helpers
        self._settings = settings
        self._run_native = run_native
        self._fence_factory = fence_factory
        self._limiter = limiter

    def run(self) -> dict[str, object]:
        """Starts the controller's monitor, runs the native loop to its end, and returns the eight-key summary."""
        c, s = self._controller, self._settings
        c.t0 = time.monotonic()
        c._start_monitor()
        # the limiter the controller reads as its effective parallelism; a controller without one gets a fresh limiter
        limiter = self._limiter if self._limiter is not None else AimdLimiter(c.mgr.max_concurrent, cooldown_s=s.cooldown_s)
        ports = _Ports(c, self._helpers, s, limiter)
        summary = self._run_native(
            ports,
            cleanup_fence=self._fence_factory(),
            max_concurrent=c.mgr.max_concurrent,
            barrier=c.barrier,
            once=c.once,
            budget=c.budget,
            max_rounds=c.max_rounds,
            fail_streak=c.fail_streak,
            cooldown_s=s.cooldown_s,
            retry_cap_s=s.retry_cap_s,
            backoff_base_s=s.backoff_base_s,
            backoff_cap_s=s.backoff_cap_s,
            wait_tick_s=s.wait_tick_s,
            internal_failure_limit=s.internal_failure_limit,
            cancel_grace_s=c.cancel_grace,
            shutdown_budget_s=c.shutdown_budget,
            kill_grace_s=s.kill_grace_s,
            kill_settle_s=s.kill_settle_s,
        )
        self._apply(summary, limiter)
        return summary

    def _apply(self, summary: dict[str, object], limiter: AimdLimiter) -> None:
        """Maps the summary onto the controller state, and writes the lines the Python oracle writes for the stop."""
        if set(summary) != _SUMMARY_KEYS:
            raise ValueError("run_scheduler summary must have exactly the eight documented keys")
        c = self._controller
        c.rounds = _as_int(summary, "rounds")
        expected = _effective_of(limiter, c.mgr.max_concurrent)
        if _as_int(summary, "effective_parallel") != expected:
            _LOG.warning("run_scheduler effective_parallel %s differs from the controller's %d",
                         summary["effective_parallel"], expected)
        c.dispatched = _as_int(summary, "dispatched")
        c.closed = _as_bool(summary, "closed")
        c.extension_down = _as_bool(summary, "extension_down")
        abandoned = _as_strs(summary, "abandoned")
        waiting = _as_strs(summary, "waiting")
        stopped = _as_str(summary, "stopped")
        if stopped not in _STOP_REASONS:
            raise ValueError(f"run_scheduler summary stopped={stopped!r} is not a stop reason")
        if abandoned:
            c.log.append(f"abandoned after kill: {abandoned}")
        if stopped == "lease-wait":
            c.log.append(f"waiting on leases: {waiting}")
        elif stopped == "stalled":
            c.log.append("stalled: ready tasks could not be dispatched")
        elif stopped == "nothing-ready":
            remaining = [t for t in c.store.list(correlation_id=c.corr) if t["state"] not in _TERMINAL_STATES]
            if remaining:
                c.log.append(f"stalled: {[(t['task_id'], t['state']) for t in remaining]}")


def _as_int(summary: dict[str, object], key: str) -> int:
    value = summary[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"run_scheduler summary {key!r} must be an int")
    return value


def _as_bool(summary: dict[str, object], key: str) -> bool:
    value = summary[key]
    if not isinstance(value, bool):
        raise ValueError(f"run_scheduler summary {key!r} must be a bool")
    return value


def _as_str(summary: dict[str, object], key: str) -> str:
    value = summary[key]
    if not isinstance(value, str):
        raise ValueError(f"run_scheduler summary {key!r} must be a str")
    return value


def _as_strs(summary: dict[str, object], key: str) -> list[str]:
    value = summary[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"run_scheduler summary {key!r} must be a list of str")
    return [str(item) for item in value]
