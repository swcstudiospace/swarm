"""Subagent lifecycle manager.

Boring, no needless abstraction: centralizes spawn of concurrent subagent sessions
for the runner (swarm_run) and integrates with hook init/trigger. Enhanced for
gsd-autonomous OMP concurrent: per-subagent status streaming to OMP session,
ID registration, OMP gsd emit integrate via observability.

Resilience: spawn (new_subagent_id) and completion reporting retry with exponential backoff (1s base + jitter);
a per-subagent overdue watchdog (threading.Timer, started when the slot is acquired; it flags the subagent and
never releases it or reports completion); pre-spawn validation; errors isolated with try; wired to gsd states.

- unique IDs: subagent-<task>-<shortuuid>-<ts>
- timestamp logging: via runlog.emit on spawn/complete (with duration)
- bounded concurrency: global cap = min(requested, SWARM_MAX_PARALLEL_CEILING); the request defaults to
  SWARM_MAX_PARALLEL, else 4 (D5). Per-agent ceilings (agents.json max_parallel) are the scheduler's business.
- acquire/release slots around execution; streams status. A slot that cannot be had within ACQUIRE_TIMEOUT_S is an
  E-CAPACITY error, never an unbounded wait.
- every progress event carries the run's correlation_id and the task_id; a subagent's completion is reported once
- prior init/trigger + OMP: trigger can call for detached; supports OMP session visibility
- states: integrate LOADING, INITIALIZING, RUNNING, SPAWNING, etc via state_store or emits
- workspace: state and status writes go to the root of the task they report
"""
from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import ErrorCode, SwarmError
from .observability import CONTROLLER_AGENT, emit_gsd_omp, stream_subagent_status
from .runlog import emit

log = logging.getLogger("swarm.lifecycle")

DEFAULT_MAX_PARALLEL = 4
DEFAULT_MAX_PARALLEL_CEILING = 32
ACQUIRE_TIMEOUT_S = 30.0
DEFAULT_OVERDUE_S = 300.0


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if value < 1:
        raise SwarmError(ErrorCode.E_INPUT, f"{name} must be a positive integer, got {raw!r}")
    return value


def parallel_ceiling() -> int:
    """Upper bound of the global cap: SWARM_MAX_PARALLEL_CEILING, default 32."""
    return _env_positive_int("SWARM_MAX_PARALLEL_CEILING", DEFAULT_MAX_PARALLEL_CEILING)


def default_max_parallel() -> int:
    """The global cap when none is requested: SWARM_MAX_PARALLEL, default 4."""
    return _env_positive_int("SWARM_MAX_PARALLEL", DEFAULT_MAX_PARALLEL)


def resolve_max_parallel(requested: int | None = None) -> int:
    """The global cap: `requested` (default SWARM_MAX_PARALLEL, else 4), at least 1 and at most the ceiling."""
    want = default_max_parallel() if requested is None else int(requested)
    return min(parallel_ceiling(), max(1, want))


def _exp_backoff(attempt: int, base: float = 1.0) -> float:
    """Exponential backoff (base * 2**attempt, capped at 60s) plus up to 0.5s jitter."""
    return min(base * 2.0 ** attempt, 60.0) + random.uniform(0, 0.5)


def _pre_spawn_validate(task_id: str) -> bool:
    """Pre-spawn validation. Called before spawn/id gen."""
    if not task_id or not isinstance(task_id, str) or len(task_id.strip()) == 0:
        raise ValueError("pre_spawn_val: invalid or empty task_id")
    return True


def _resolve_overdue(overdue_after_s: float | None) -> float:
    """Overdue threshold in seconds: DEFAULT_OVERDUE_S when None; otherwise must be finite and positive."""
    if overdue_after_s is None:
        return DEFAULT_OVERDUE_S
    value = float(overdue_after_s)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"overdue_after_s must be a finite positive number of seconds, got {overdue_after_s!r}")
    return value


class _SubTimeout:
    """Overdue watchdog: calls on_overdue(sid) once after after_s seconds if the subagent is still running. Cancel on release."""

    def __init__(self, sid: str, on_overdue: Callable[[str], None], after_s: float) -> None:
        self.sid = sid
        self.on_overdue = on_overdue
        self.after_s = after_s
        self.timer: threading.Timer | None = None

    def start(self) -> None:
        self.timer = threading.Timer(self.after_s, self._on_timeout)
        self.timer.daemon = True
        self.timer.start()

    def _on_timeout(self) -> None:
        try:
            self.on_overdue(self.sid)
        except Exception:  # noqa: BLE001 - a watchdog callback must not kill its timer thread noisily
            log.warning("overdue callback for %s failed", self.sid, exc_info=True)

    def cancel(self) -> None:
        timer, self.timer = self.timer, None
        if timer is not None:
            timer.cancel()


def _drive_gsd_state(state: str, *, root: str | Path | None = None, dry_run: bool = False,
                     corr: str | None = None, **extra: Any) -> None:
    """Drive states LOADING etc via state store calls or emits. For wave1 gsd-autonomous OMP concurrent.
    State is written to the workspace at `root` (current directory when None). Errors isolated with try."""
    try:
        from swarm.state_store import get_swarm_state_store
        ss = get_swarm_state_store(root=root)
        ss.append_gsd_line(state)
        ss.update(state, corr=corr, extra=extra or None)
    except Exception:  # noqa: BLE001 - the state store is a projection; fall back to a plain event
        emit("gsd.state", {"state": state, **extra}, source=f"{CONTROLLER_AGENT}@subagent_lifecycle", root=root,
             correlation_id=corr, task_id=extra.get("task_id"), tee=not dry_run)
    try:
        emit_gsd_omp("state", state=state, root=root, dry_run=dry_run, corr=corr, **extra)
    except Exception:  # noqa: BLE001
        log.warning("gsd state %s could not be streamed", state, exc_info=True)


class SubagentLifecycleManager:
    """Lifecycle for subagent spawns in run/executor + OMP gsd-autonomous.

    The global cap is `resolve_max_parallel(max_concurrent)`. Used by swarm_run's scheduler and hook_trigger's
    detached spawns. Streams per-subagent status+ID to the OMP session, integrates emit_gsd_omp.

    root: default workspace for status writes; new_subagent_id(root=...) overrides it per task.
    correlation_id: stamped on every event of subagents that do not name their own.
    """

    def __init__(self, max_concurrent: int | None = None, *, root: str | Path | None = None,
                 overdue_after_s: float | None = None, dry_run: bool = False,
                 correlation_id: str | None = None) -> None:
        overdue = _resolve_overdue(overdue_after_s)
        self.dry_run = dry_run
        self.root = Path(root) if root is not None else None
        self.correlation_id = correlation_id
        self.overdue_after_s = overdue
        self.max_concurrent = resolve_max_parallel(max_concurrent)
        self._sem = threading.BoundedSemaphore(self.max_concurrent)
        self._active: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._timeouts: dict[str, _SubTimeout] = {}
        _drive_gsd_state("LOADING", lifecycle="init", wave="wave1", consolidated=False, root=self.root,
                         dry_run=self.dry_run, corr=correlation_id)
        _drive_gsd_state("INITIALIZING", max=self.max_concurrent, wave="wave1", root=self.root,
                         dry_run=self.dry_run, corr=correlation_id)
        emit_gsd_omp("init.concurrent", max=self.max_concurrent, wave1="gsd-autonomous", audit="ready", root=self.root,
                     dry_run=self.dry_run, corr=correlation_id)
        stream_subagent_status("lifecycle", "initialized", phase="wave1", root=self.root, dry_run=self.dry_run,
                               corr=correlation_id)

    def new_subagent_id(self, task_id: str, *, root: str | Path | None = None,
                        correlation_id: str | None = None) -> str:
        """Return unique ID for a task whose status goes to workspace `root` (manager root when None).
        The overdue watchdog starts at acquire, not here, so tasks queued behind the semaphore are not flagged."""
        _pre_spawn_validate(task_id)
        task_root = self.root if root is None else Path(root)
        corr = correlation_id or self.correlation_id
        for attempt in range(5):
            try:
                _drive_gsd_state("SPAWNING", task_id=task_id, wave="wave1", root=task_root, dry_run=self.dry_run,
                                 corr=corr)
                ts = time.time()
                sid = f"subagent-{task_id}-{uuid.uuid4().hex[:12]}-{int(ts * 1000)}"
                with self._lock:
                    self._active[sid] = {
                        "task_id": task_id,
                        "correlation_id": corr,
                        "spawn_ts": ts,
                        "mono_start": time.monotonic(),
                        "root": task_root,
                        "holds_slot": False,
                        "overdue": False,
                    }
                source = f"{CONTROLLER_AGENT}@subagent_lifecycle"
                emit("subagent.spawn.immediate",
                     {"subagent_id": sid, "task_id": task_id, "spawn_ts": ts, "max_concurrent": self.max_concurrent,
                      "wave": "wave1"},
                     source=source, correlation_id=corr, task_id=task_id, root=task_root, tee=not self.dry_run)
                emit("spawn.event", {"subagent_id": sid, "task_id": task_id, "slice": task_id}, source=source,
                     correlation_id=corr, task_id=task_id, root=task_root, tee=not self.dry_run)
                stream_subagent_status(sid, "spawned", task_id=task_id, phase="launch", wave="wave1", root=task_root,
                                       dry_run=self.dry_run, corr=corr)
                emit_gsd_omp("spawn", subagent_id=sid, task_id=task_id, max=self.max_concurrent, wave1="gsd-autonomous",
                             root=task_root, dry_run=self.dry_run, corr=corr)
                _drive_gsd_state("RUNNING", subagent_id=sid, task_id=task_id, wave="wave1", root=task_root,
                                 dry_run=self.dry_run, corr=corr)
                emit_gsd_omp("consolidated", subagent_id=sid, task_id=task_id, audit="spawn", root=task_root,
                             dry_run=self.dry_run, corr=corr)
                return sid
            except Exception:  # noqa: BLE001 - retried with backoff; the last failure is re-raised
                if attempt == 4:
                    raise
                time.sleep(_exp_backoff(attempt))
        raise RuntimeError("spawn backoff exhausted")

    def _ctx_of(self, sid: str) -> tuple[Path | None, str | None, str | None]:
        """(workspace root, correlation id, task id) recorded for sid at spawn; manager defaults if untracked."""
        with self._lock:
            info = self._active.get(sid)
        if info is None:
            return self.root, self.correlation_id, None
        return info["root"], info.get("correlation_id") or self.correlation_id, info.get("task_id")

    def _root_of(self, sid: str) -> Path | None:
        """Workspace root recorded for sid at spawn; manager root if sid is untracked."""
        return self._ctx_of(sid)[0]

    def _mark_slot_held(self, sid: str) -> None:
        """Record that sid now holds a semaphore slot and start its overdue watchdog.
        Caller has just acquired the semaphore; if sid is untracked, nothing would ever free that slot, so it is given back."""
        with self._lock:
            info = self._active.get(sid)
            if info is None:
                tmo = None
            else:
                info["acquired_ts"] = time.time()
                info["holds_slot"] = True
                tmo = _SubTimeout(sid, self._on_overdue, self.overdue_after_s)
                self._timeouts[sid] = tmo
        if tmo is None:
            self._sem.release()
            return
        tmo.start()

    def _on_overdue(self, sid: str) -> None:
        """Watchdog expiry: flag sid as still running past the threshold. It does not free the slot or report
        completion; release() does that once the task has actually ended."""
        with self._lock:
            info = self._active.get(sid)
            if info is None:
                return  # already released; not overdue
            info["overdue"] = True
            # Streamed under the lock so a concurrent release() cannot publish "complete" before this "overdue".
            stream_subagent_status(sid, "overdue", task_id=info.get("task_id"), phase="run", wave="wave1",
                                   root=info["root"], dry_run=self.dry_run,
                                   corr=info.get("correlation_id") or self.correlation_id)

    def acquire(self, sid: str, *, timeout: float | None = None) -> None:
        """Take a slot for sid, waiting at most `timeout` (default ACQUIRE_TIMEOUT_S) seconds. A full manager raises
        E-CAPACITY: there is no unbounded wait. Execution begins once the slot is held.
        A sid that is not active (never spawned, or already released) is refused with E-CONTRACT before any slot is taken."""
        with self._lock:
            known = sid in self._active
        if not known:
            raise SwarmError(ErrorCode.E_CONTRACT,
                             f"{sid} is not an active subagent: acquire takes an id from new_subagent_id that was not released",
                             subagent_id=sid)
        root, corr, task_id = self._ctx_of(sid)
        wait = ACQUIRE_TIMEOUT_S if timeout is None else timeout
        if not self._sem.acquire(timeout=wait):
            raise SwarmError(ErrorCode.E_CAPACITY,
                             f"no lifecycle slot for {sid} within {wait:g}s (cap {self.max_concurrent})",
                             subagent_id=sid)
        self._mark_slot_held(sid)
        _drive_gsd_state("ACQUIRED", subagent_id=sid, task_id=task_id, wave="wave1", root=root, dry_run=self.dry_run,
                         corr=corr)
        stream_subagent_status(sid, "acquired", task_id=task_id, phase="run", wave="wave1", root=root,
                               dry_run=self.dry_run, corr=corr)
        emit_gsd_omp("acquired.immediate", subagent_id=sid, task_id=task_id, wave1="gsd-autonomous", root=root,
                     dry_run=self.dry_run, corr=corr)
        emit_gsd_omp("consolidated", subagent_id=sid, task_id=task_id, audit="acquire", root=root,
                     dry_run=self.dry_run, corr=corr)

    def release(self, sid: str) -> dict[str, Any] | None:
        """Release sid after its task has ended: cancel the watchdog, free the slot once, report completion.
        Idempotent: a repeat call for the same sid frees nothing and reports nothing, returning None."""
        with self._lock:
            info = self._active.pop(sid, None)
            tmo = self._timeouts.pop(sid, None)
        if tmo is not None:
            tmo.cancel()
        if info is None:
            return None
        if info.get("holds_slot"):
            self._sem.release()
        self._report_complete(sid, info)
        return info

    def _report_complete(self, sid: str, info: dict[str, Any]) -> None:
        """Emit completion for a released subagent, once. Each step is retried with backoff on its own, so a late
        failure never repeats the COMPLETE state; errors are isolated and logged."""
        root, task_id = info.get("root"), info.get("task_id")
        corr = info.get("correlation_id") or self.correlation_id
        dur = time.monotonic() - info.get("mono_start", time.monotonic())
        tee = not self.dry_run
        steps = (
            lambda: _drive_gsd_state("COMPLETE", subagent_id=sid, task_id=task_id, wave="wave1", root=root,
                                     dry_run=self.dry_run, corr=corr),
            lambda: emit("subagent.complete.immediate",
                         {"subagent_id": sid, "task_id": task_id, "duration_s": round(dur, 3),
                          "complete_ts": time.time(), "wave": "wave1", "slice": task_id},
                         source=f"{CONTROLLER_AGENT}@subagent_lifecycle", correlation_id=corr, task_id=task_id,
                         root=root, tee=tee),
            lambda: stream_subagent_status(sid, "complete", task_id=task_id, phase="done", wave="wave1", root=root,
                                           dry_run=self.dry_run, corr=corr),
            lambda: emit_gsd_omp("complete.immediate", subagent_id=sid, task_id=task_id, duration=round(dur, 3),
                                 wave1="gsd-autonomous", root=root, dry_run=self.dry_run, corr=corr),
            lambda: emit_gsd_omp("consolidated", subagent_id=sid, task_id=task_id, audit="release", root=root,
                                 dry_run=self.dry_run, corr=corr),
        )
        for step in steps:
            for attempt in range(5):
                try:
                    step()
                    break
                except Exception:  # noqa: BLE001 - completion reporting must never break the release
                    if attempt == 4:
                        log.warning("completion report for %s failed after 5 attempts", sid, exc_info=True)
                        break
                    time.sleep(_exp_backoff(attempt))

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def active_ids(self) -> list[str]:
        with self._lock:
            return list(self._active.keys())

    def current_concurrency(self) -> int:
        return self.active_count()

    def register_omp(self, session_ref: str | None = None) -> dict[str, Any]:
        _drive_gsd_state("REGISTERED", session=session_ref, wave="wave1", root=self.root, dry_run=self.dry_run,
                         corr=self.correlation_id)
        with self._lock:
            active = dict(self._active)
        emit_gsd_omp("omp.register.immediate", subagent_ids=list(active.keys()), session=session_ref,
                     wave1="gsd-autonomous", root=self.root, dry_run=self.dry_run, corr=self.correlation_id)
        for sid, info in active.items():
            corr = info.get("correlation_id") or self.correlation_id
            stream_subagent_status(sid, "registered", task_id=info.get("task_id"), phase="omp", wave="wave1",
                                   root=info["root"], dry_run=self.dry_run, corr=corr)
            emit_gsd_omp("status", subagent_id=sid, task_id=info.get("task_id"), omp_status="omp-registered",
                         root=info["root"], dry_run=self.dry_run, corr=corr)
        emit_gsd_omp("consolidated", audit="register", session=session_ref, root=self.root, dry_run=self.dry_run,
                     corr=self.correlation_id)
        return {"max": self.max_concurrent, "active": list(active.keys()), "count": len(active)}

    def stream_all_status(self, status: str = "active") -> None:
        _drive_gsd_state("STREAMING", omp_status=status, wave="wave1", root=self.root, dry_run=self.dry_run,
                         corr=self.correlation_id)
        with self._lock:
            for sid, info in list(self._active.items()):
                root, task_id = info["root"], info.get("task_id")
                corr = info.get("correlation_id") or self.correlation_id
                stream_subagent_status(sid, status, task_id=task_id, phase="omp", wave="wave1", root=root,
                                       dry_run=self.dry_run, corr=corr)
                emit_gsd_omp(status, subagent_id=sid, task_id=task_id, wave1="gsd-autonomous", root=root,
                             dry_run=self.dry_run, corr=corr)
                emit_gsd_omp("consolidated", subagent_id=sid, task_id=task_id, audit="stream", omp_status=status,
                             root=root, dry_run=self.dry_run, corr=corr)


# Singleton-ish for hook integration without passing everywhere (boring).
# Init on first use; runner overrides with its arg.
_global_mgr: SubagentLifecycleManager | None = None


def get_manager(
    max_concurrent: int | None = None,
    *,
    root: str | Path | None = None,
    overdue_after_s: float | None = None,
    dry_run: bool = False,
    correlation_id: str | None = None,
) -> SubagentLifecycleManager:
    """Get (or init) the lifecycle mgr. The cap is `resolve_max_parallel(max_concurrent)`: SWARM_MAX_PARALLEL else 4,
    never above SWARM_MAX_PARALLEL_CEILING. OMP command registers for status streams.
    root: default workspace for status writes; None keeps the existing manager's root.
    overdue_after_s: overdue threshold in seconds; None keeps the existing manager's threshold (300 by default).
    correlation_id: stamped on the manager's events; None keeps the existing manager's."""
    global _global_mgr
    if _global_mgr is None:
        cap = resolve_max_parallel(max_concurrent)
        _drive_gsd_state("MANAGER_INIT", max=cap, wave="wave1", root=root, dry_run=dry_run, corr=correlation_id)
        _global_mgr = SubagentLifecycleManager(cap, root=root, overdue_after_s=overdue_after_s, dry_run=dry_run,
                                               correlation_id=correlation_id)
    else:
        # rebind only if caller wants different (runner does)
        want_max = _global_mgr.max_concurrent if max_concurrent is None else resolve_max_parallel(max_concurrent)
        want_root = _global_mgr.root if root is None else Path(root)
        want_overdue = _global_mgr.overdue_after_s if overdue_after_s is None else _resolve_overdue(overdue_after_s)
        want_corr = _global_mgr.correlation_id if correlation_id is None else correlation_id
        if (want_max != _global_mgr.max_concurrent or want_root != _global_mgr.root
                or want_overdue != _global_mgr.overdue_after_s or dry_run != _global_mgr.dry_run):
            _drive_gsd_state("MANAGER_REBIND", max=want_max, wave="wave1", root=want_root, dry_run=dry_run,
                             corr=want_corr)
            _global_mgr = SubagentLifecycleManager(want_max, root=want_root, overdue_after_s=want_overdue,
                                                   dry_run=dry_run, correlation_id=want_corr)
        else:
            _global_mgr.correlation_id = want_corr
    emit_gsd_omp("manager.get", wave1="gsd-autonomous", root=_global_mgr.root, dry_run=dry_run,
                 corr=_global_mgr.correlation_id)
    emit_gsd_omp("consolidated", audit="get_manager", root=_global_mgr.root, dry_run=dry_run,
                 corr=_global_mgr.correlation_id)
    return _global_mgr


def reset_manager() -> None:
    """Test hook only."""
    global _global_mgr
    _global_mgr = None
