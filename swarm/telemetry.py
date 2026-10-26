"""One telemetry channel for runtime lifecycle: events.jsonl records, a bounded in-memory history and `.swarm/live.json`.

`Telemetry` is the producer used by a scheduler process: every run goes through the `swarm.lifecycle` state machine, every
change is emitted through `swarm.runlog.emit` (redacted there) and mirrored into `<swarm_dir>/live.json`
(`swarm.live.v1`, atomic temp-file + `os.replace`, at most one write per second unless forced).

`derive_snapshot` / `refresh_live` are the stateless path: the same snapshot rebuilt from Task Store rows plus the tail of
`events.jsonl`, so an in-session run (A01 driving `scripts/orch_status.py`, no scheduler process) also feeds the bar.
`TaskStore.transition` calls `refresh_live` after a committed transition.

Durations are ALWAYS differences of `Clock.monotonic()`; `wall()` only stamps display fields. The stateless path has no
monotonic origin across processes, so it uses stored wall timestamps clamped at zero.
"""
from __future__ import annotations

import calendar
import contextlib
import datetime as _dt
import functools
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
import weakref
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar

try:  # POSIX advisory locks serialise the live.json read-derive-write cycle across processes
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: derived writers fall back to the producer-freshness check alone
    fcntl = None  # type: ignore[assignment]  # fcntl is absent off POSIX; the None fallback is the intended non-POSIX path

from .clock import SYSTEM_CLOCK, Clock
from .errors import ErrorCode, SwarmError
from .lifecycle import RunEvent, RunState, WAIT_DEPENDENCY, WAIT_INPUT, WAIT_KINDS, from_task_state, is_terminal, next_state
from .paths import swarm_dir
from .redact import redact_text
from . import runlog
from .schema import load_schema, validate

log = logging.getLogger("swarm.telemetry")

LIVE_SCHEMA = "swarm.live.v1"
LIVE_FILE = "live.json"
MAX_RUNS = 64  # runs listed in live.json
CLIP = 120  # characters kept of every free-text field in live.json
ACTIVITY_EMIT_INTERVAL_S = 1.0
LIVE_FLUSH_INTERVAL_S = 1.0
DEFAULT_STALL_AFTER_S = 180.0
DEFAULT_HEARTBEAT_S = 10.0
DEFAULT_MAX_PARALLEL = 4
TAIL_BYTES = 256 * 1024  # events.jsonl tail read by the stateless path
STATUS_DIR = "status"  # `<state dir>/status/<task_id>.json`, written by shared_status_report
STATUS_MAX_BYTES = 64 * 1024

EV_QUEUED = "swarm.run.queued"
EV_STARTED = "swarm.run.started"
EV_STATE = "swarm.run.state"
EV_ACTIVITY = "swarm.run.activity"
EV_HEARTBEAT = "swarm.run.heartbeat"
EV_STALLED = "swarm.run.stalled"
EV_FINISHED = "swarm.run.finished"
EV_HANDOVER = "swarm.run.handover"
EV_TICK = "swarm.scheduler.tick"
EV_DEGRADED = "swarm.degraded"
RECOVERED_MODE = "recovered"  # `swarm.degraded` mode that ends a fallback (Telemetry.clear_degraded)
EVENT_TYPES = (EV_QUEUED, EV_STARTED, EV_STATE, EV_ACTIVITY, EV_HEARTBEAT, EV_STALLED, EV_FINISHED, EV_HANDOVER,
               EV_TICK, EV_DEGRADED)

# outcome name -> (RunState reached, RunEvent from a non-handing-off state)
_OUTCOMES: dict[str, tuple[RunState, RunEvent]] = {
    "ok": (RunState.COMPLETED, RunEvent.FINISH_OK),
    "completed": (RunState.COMPLETED, RunEvent.FINISH_OK),
    "error": (RunState.FAILED, RunEvent.FINISH_ERROR),
    "failed": (RunState.FAILED, RunEvent.FINISH_ERROR),
    "cancelled": (RunState.CANCELLED, RunEvent.CANCEL),
    "timeout": (RunState.TIMED_OUT, RunEvent.DEADLINE_EXCEEDED),
    "timed_out": (RunState.TIMED_OUT, RunEvent.DEADLINE_EXCEEDED),
}

# live.json ordering: problems first, then active work, then settled
_RANK = {RunState.FAILED: 0, RunState.TIMED_OUT: 1, RunState.RUNNING: 3, RunState.HANDING_OFF: 4, RunState.STARTING: 5,
         RunState.WAITING: 6, RunState.QUEUED: 7, RunState.COMPLETED: 8, RunState.CANCELLED: 9}
_STALLED_RANK = 2


# ---- small helpers ---------------------------------------------------------------------------------------------
def clip(value: object, limit: int = CLIP) -> str | None:
    """Redact and clip free text for live.json; None stays None, non-strings are rendered with `str`."""
    if value is None:
        return None
    return _clip_text(value if isinstance(value, str) else str(value), limit)


def clip_id(value: object, limit: int = 64) -> str | None:
    """Clip an identifier (task, run, handover id) for live.json WITHOUT redaction: validated ids are not free text, and a token-shaped
    one (`sk-onboarding-flow-v2`) must stay readable and comparable. Anything that is not id-shaped is treated as free text (`clip`)."""
    if isinstance(value, str) and runlog.ID_RE.fullmatch(value):
        return value[:limit]
    return clip(value, limit)


@functools.lru_cache(maxsize=4096)
def _clip_text(raw: str, limit: int) -> str:
    text = " ".join(redact_text(raw).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def status_label(kind: str, step: str, percent: object = None) -> str:
    """The one-line form of a `shared_status_report` for the bar: `progress: build (40%)`."""
    pct = f" ({percent:g}%)" if isinstance(percent, (int, float)) and not isinstance(percent, bool) else ""
    return f"{kind}: {step}{pct}"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = float(raw)
    except ValueError:
        raise SwarmError(ErrorCode.E_INPUT, f"{name} must be a number of seconds, got {raw!r}") from None
    if not val > 0 or val == float("inf"):
        raise SwarmError(ErrorCode.E_INPUT, f"{name} must be a finite number > 0, got {raw!r}")
    return val


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        raise SwarmError(ErrorCode.E_INPUT, f"{name} must be an integer, got {raw!r}") from None
    if val < 1:
        raise SwarmError(ErrorCode.E_INPUT, f"{name} must be >= 1, got {raw!r}")
    return val


def stall_after_default() -> float:
    """Seconds without activity before a `running` run is `stalled` (`SWARM_STALL_AFTER_S`, default 180)."""
    return _env_float("SWARM_STALL_AFTER_S", DEFAULT_STALL_AFTER_S)


def heartbeat_default() -> float:
    """Heartbeat cadence in seconds (`SWARM_HEARTBEAT_S`, default 10)."""
    return _env_float("SWARM_HEARTBEAT_S", DEFAULT_HEARTBEAT_S)


def _state_dir(root: str | Path | None, db_path: str | Path | None) -> Path:
    return Path(db_path).resolve().parent if db_path is not None else swarm_dir(root)


_HANDOVER_FILE = re.compile(r"^(?P<task>.+)\.(?P<seq>\d+)\.json$")


def _handovers_in(sdir: Path, corr: str | None, tasks: list[dict[str, Any]], side: str = "to_agent") -> dict[str, str]:
    """`{task_id: handover_id}` for each task whose newest handover is addressed to the agent the task runs on
    (`side="to_agent"`, the in marker) or was written by that agent (`side="from_agent"`, the out marker).

    Read from `<state dir>/handovers/<correlation>/` (swarm.handover's layout; the digest is checked, the full schema
    is not: this is a display projection). A malformed newest file means no marker, as `latest_handover` raises
    rather than skip to an older one; a handover of another agent is passed over. Never raises."""
    out: dict[str, str] = {}
    if not corr:
        return out
    try:
        from . import handover  # lazy: handover imports this module lazily too
        names = os.listdir(sdir / "handovers" / corr)
    except (OSError, ValueError, ImportError):
        return out
    by_task: dict[str, list[tuple[int, str]]] = {}
    for name in names:
        m = _HANDOVER_FILE.match(name)
        if m:
            by_task.setdefault(m["task"], []).append((int(m["seq"]), name))
    try:
        for t in tasks:
            agent = handover.agent_id_of(t.get("agent_id"))
            cands = by_task.get(t["task_id"])
            if not agent or not cands:
                continue
            for _, name in sorted(cands, reverse=True):
                path = sdir / "handovers" / corr / name
                try:
                    if not 0 < path.stat().st_size <= handover.MAX_FILE_BYTES:
                        break
                    doc = json.loads(path.read_text(encoding="utf-8"))
                    valid = (isinstance(doc, dict) and doc.get("task_id") == t["task_id"]
                             and isinstance(doc.get("handover_id"), str) and isinstance(doc.get(side), str)
                             and doc.get("digest") == handover.compute_digest(doc))
                except (OSError, ValueError):
                    break
                if not valid:
                    break
                if handover.agent_id_of(doc[side]) == agent:
                    out[t["task_id"]] = clip_id(doc["handover_id"], 64) or doc["handover_id"][:64]
                    break
    except Exception:  # noqa: BLE001 - a display projection: a handover module that misbehaves hides handover_in only
        log.debug("handover_in derivation failed", exc_info=True)
    return out


def live_path(root: str | Path | None = None) -> Path:
    """Where `live.json` lives for `root` (the resolved swarm dir)."""
    return swarm_dir(root) / LIVE_FILE


def _write_atomic(path: Path, doc: dict[str, Any]) -> None:
    """Write `doc` as JSON to a unique sibling temp file, then `os.replace`: readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        data = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))  # C encoder: much cheaper than dump(fh)
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


#: live.json path -> (inode, size, mtime_ns) of the last file that passed schema validation: an unchanged file is
#: parsed again (cheap C decode) but never re-validated (the Python validator was the cost).
_LIVE_VALID: dict[str, tuple[int, int, int]] = {}


def _remember_valid(path: Path) -> None:
    with contextlib.suppress(OSError):
        st = path.stat()
        _LIVE_VALID[str(path)] = (st.st_ino, st.st_size, st.st_mtime_ns)


def _read_live_at(path: Path) -> dict[str, Any] | None:
    try:
        st = path.stat()
        sig = (st.st_ino, st.st_size, st.st_mtime_ns)
        doc: object = json.loads(path.read_text(encoding="utf-8"))
        if _LIVE_VALID.get(str(path)) == sig and isinstance(doc, dict) and doc.get("schema") == LIVE_SCHEMA:
            return doc
        if not isinstance(doc, dict) or not _valid_live(doc):
            return None
        _LIVE_VALID[str(path)] = sig
        return doc
    except (OSError, ValueError):
        return None


def _valid_live(doc: object) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == LIVE_SCHEMA and not validate(doc, load_schema(LIVE_SCHEMA))


@contextlib.contextmanager
def _live_lock(target: Path, timeout: float = 2.0) -> Iterator[bool]:
    """Exclusive `flock` on `<live.json>.lock` for the read-derive-write cycle. Yields True when held, False when it
    could not be taken within `timeout` seconds (or there is no flock): callers decide whether to proceed unlocked."""
    if fcntl is None:
        yield False
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = open(f"{target}.lock", "a")  # noqa: SIM115 - closed in the finally below
    except OSError:
        yield False
        return
    held = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError):
                fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _producer_fresh(doc: dict[str, Any] | None, now_wall: float, *, owned_here: bool) -> bool:
    """True while `doc` was written by a scheduler `Telemetry` that is still alive: `producer == "scheduler"`, its
    `producer_updated` is within 3 heartbeats of `now_wall`, and its pid is a live process. A document stamped with this
    process's pid is only fresh while a `Telemetry` here still owns the file (`owned_here`)."""
    if not doc or doc.get("producer") != "scheduler":
        return False
    updated = doc.get("producer_updated")
    if not isinstance(updated, (int, float)) or isinstance(updated, bool):
        return False
    try:
        window = 3 * heartbeat_default()
    except SwarmError:
        window = 3 * DEFAULT_HEARTBEAT_S
    if now_wall - updated > window:
        return False
    pid = doc.get("pid")
    if pid == os.getpid():
        return owned_here
    return _pid_alive(pid)


# ---- snapshot assembly (shared by the producer and the stateless path) -----------------------------------------
def _sort_key(row: dict[str, Any], order: int) -> tuple[int, int]:
    rank = _STALLED_RANK if row["stalled"] and row["state"] == RunState.RUNNING.value else _RANK[RunState(row["state"])]
    return rank, order


def build_snapshot(rows: list[dict[str, Any]], *, correlation_id: str | None, seq: int, updated_wall: float, elapsed_s: float,
                   max_parallel: int, effective_parallel: int, degraded: dict[str, Any] | None) -> dict[str, Any]:
    """Assemble a `swarm.live.v1` document from complete run rows (counts cover every row, the list is capped)."""
    counts = {s: 0 for s in ("running", "queued", "waiting", "completed", "failed", "cancelled", "timed_out")}
    stalled = 0
    for r in rows:
        st = RunState(r["state"])
        if st in (RunState.RUNNING, RunState.HANDING_OFF):
            counts["running"] += 1
        elif st in (RunState.QUEUED, RunState.STARTING):
            counts["queued"] += 1
        else:
            counts[st.value] += 1
        stalled += 1 if r["stalled"] and st is RunState.RUNNING else 0
    if not rows:
        state = "idle"
    elif counts["running"] or counts["queued"] or counts["waiting"]:
        state = "running"
    elif counts["failed"] or counts["timed_out"]:
        state = "failed"
    elif counts["cancelled"]:
        state = "cancelled"  # a cancelled swarm must not read as a clean finish (the bar would paint a green check)
    else:
        state = "completed"
    ordered = [r for _, r in sorted(((_sort_key(r, i), r) for i, r in enumerate(rows)), key=lambda p: p[0])]
    return {
        "schema": LIVE_SCHEMA, "correlation_id": correlation_id, "seq": seq, "updated_wall": updated_wall,
        "swarm": {"state": state, "elapsed_s": round(max(0.0, elapsed_s), 3), "max_parallel": max_parallel,
                  "effective_parallel": effective_parallel, **counts, "stalled": stalled, "degraded": degraded},
        "runs": ordered[:MAX_RUNS],
    }


#: live-target path -> weakref of the Telemetry that owns it in this process (so refresh_live never clobbers its richer file)
_ACTIVE: dict[str, "weakref.ReferenceType[Telemetry]"] = {}


# ---- producer ---------------------------------------------------------------------------------------------------
class _Run:
    """Mutable record of one run attempt inside a `Telemetry`."""

    def __init__(self, run_id: str, task_id: str, agent: str, attempt: int, state: RunState, wait_kind: str | None,
                 now: float, priority: str | None, deps: list[str]):
        self.run_id, self.task_id, self.agent, self.attempt = run_id, task_id, agent, attempt
        self.state, self.wait_kind = state, wait_kind
        self.priority, self.deps = priority, deps
        self.anchor_mono = now  # elapsed counts from the queue time, then from the process start
        self.state_mono = now
        self.end_mono: float | None = None
        self.activity_mono = now
        self.last_activity_emit: float | None = None
        self.activity: str | None = None
        self.last_tool: str | None = None
        self.tool_calls = 0
        self.turns = 0
        self.tokens = 0
        self.stalled = False
        self.error: str | None = None
        self.handover_out: str | None = None
        self.handover_in: str | None = None
        self.status_sig: tuple[int, int, int] | None = None  # (inode, size, mtime_ns) of the last status file parsed
        self.parent: str | None = None
        self.finished_emitted = False


class Telemetry:
    """Validated run lifecycle + event emission + live snapshot for one scheduler process. Thread-safe."""

    def __init__(self, *, root: str | Path | None = None, correlation_id: str | None = None, clock: Clock = SYSTEM_CLOCK,
                 stall_after_s: float | None = None, history_max: int = 256, tee: bool = True, live: bool = True):
        if not isinstance(history_max, int) or isinstance(history_max, bool) or history_max < 1:
            raise SwarmError(ErrorCode.E_INPUT, f"history_max must be an integer >= 1, got {history_max!r}")
        if stall_after_s is not None and not (isinstance(stall_after_s, (int, float)) and stall_after_s > 0):
            raise SwarmError(ErrorCode.E_INPUT, f"stall_after_s must be a number > 0, got {stall_after_s!r}")
        self.root = root
        self.correlation_id = correlation_id
        self.clock = clock
        self.stall_after_s = float(stall_after_s) if stall_after_s is not None else stall_after_default()
        self.tee = tee
        self._live = live
        self._lock = threading.RLock()
        self._runs: dict[str, _Run] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=history_max)
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._seq = 0
        self._t0: float | None = None
        self._t_end: float | None = None
        self._last_flush: float | None = None
        self._tick: dict[str, Any] = {}
        self._degraded: dict[str, Any] | None = None
        self._handover_key: tuple[object, ...] | None = None  # (folder listing, live runs) the handover marks below were scanned for
        self._handover_marks: tuple[dict[str, str], dict[str, str]] = ({}, {})
        if live:
            _ACTIVE[str(self._live_target())] = weakref.ref(self)

    # -- plumbing --
    def _live_target(self) -> Path:
        return live_path(self.root)

    def _run(self, run_id: str) -> _Run:
        run = self._runs.get(run_id)
        if run is None:
            raise SwarmError(ErrorCode.E_INPUT, f"unknown run_id {run_id!r}", run_id=run_id)
        return run

    def emit(self, event_type: str, payload: dict[str, Any], *, task_id: str | None = None, local_only: bool = False) -> dict[str, Any]:
        """Write one record through `runlog.emit` (redacted there), keep it in the ring buffer and notify subscribers.
        Internal: every call site passes a literal `swarm.*` type so tests/test_substrate_tee.py can scan them. High-
        frequency types (`local_only`) never go to the substrate tee, whatever `tee` says."""
        record = runlog.emit(event_type, payload, source="telemetry", correlation_id=self.correlation_id,
                             task_id=task_id, root=self.root, tee=self.tee and not local_only)
        self._history.append(record)
        for cb in list(self._subscribers):
            try:
                cb(record)
            except Exception:  # noqa: BLE001 - a subscriber must never break telemetry
                log.warning("telemetry subscriber %r raised; ignored", cb, exc_info=True)
        return record

    def subscribe(self, cb: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Call `cb(record)` for every emitted event; returns an `unsubscribe()` callable. Exceptions are logged only."""
        with self._lock:
            self._subscribers.append(cb)

        def unsubscribe() -> None:
            with self._lock:
                if cb in self._subscribers:
                    self._subscribers.remove(cb)

        return unsubscribe

    def recent_events(self) -> list[dict[str, Any]]:
        """The last `history_max` emitted records, oldest first (a copy)."""
        with self._lock:
            return list(self._history)

    # -- lifecycle --
    def run_queued(self, run_id: str, agent: str, *, attempt: int = 1, priority: str | None = None,
                   deps: Iterable[str] = (), queue_depth: int = 0, task_id: str | None = None,
                   waiting_on_deps: bool = False, parent: str | None = None) -> RunState:
        """Register a new run at `queued` (or `waiting` on dependencies) and emit `swarm.run.queued`."""
        if not isinstance(run_id, str) or not run_id:
            raise SwarmError(ErrorCode.E_INPUT, "run_id must be a non-empty string")
        if not isinstance(agent, str) or not agent:
            raise SwarmError(ErrorCode.E_INPUT, "agent must be a non-empty string")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise SwarmError(ErrorCode.E_INPUT, f"attempt must be an integer >= 1, got {attempt!r}")
        with self._lock:
            if run_id in self._runs:
                raise SwarmError(ErrorCode.E_CONTRACT, f"run {run_id} already registered", run_id=run_id)
            now = self.clock.monotonic()
            tid = task_id or (run_id.rsplit(".a", 1)[0] if ".a" in run_id else run_id)
            dep_list = [str(d) for d in deps]
            run = _Run(run_id, tid, agent, attempt, RunState.WAITING if waiting_on_deps else RunState.QUEUED,
                       WAIT_DEPENDENCY if waiting_on_deps else None, now, priority, dep_list)
            run.parent = parent
            self._runs[run_id] = run
            self._t0 = now if self._t0 is None else self._t0
            self._t_end = None
            self.emit("swarm.run.queued", {"run_id": run_id, "agent": agent, "attempt": attempt, "priority": priority,
                                   "deps": dep_list, "queue_depth": queue_depth}, task_id=tid)
            self.flush_live()
            return run.state

    def _apply(self, run: _Run, event: RunEvent | str, *, reason: str | None, wait_kind: str | None) -> RunState:
        """Validate and apply one event; state is untouched when `next_state` raises."""
        if wait_kind is not None and wait_kind not in WAIT_KINDS:
            raise SwarmError(ErrorCode.E_CONTRACT, f"unknown wait_kind {wait_kind!r}; expected one of {list(WAIT_KINDS)}",
                             wait_kind=str(wait_kind))
        before = run.state
        current_kind = run.wait_kind if before is RunState.WAITING else None
        after = next_state(before, event, wait_kind=current_kind)
        ev = RunEvent(event)
        now = self.clock.monotonic()
        run.state = after
        run.state_mono = now
        run.wait_kind = (wait_kind or WAIT_INPUT) if after is RunState.WAITING else None
        if after is RunState.RUNNING:
            run.activity_mono = now  # entering `running` starts a fresh activity window
            run.stalled = False
        if is_terminal(after):
            run.end_mono = now
            run.stalled = False
            self._t_end = now if all(r.end_mono is not None for r in self._runs.values()) else None
        self.emit("swarm.run.state", {"run_id": run.run_id, "agent": run.agent, "from": before.value, "to": after.value,
                              "event": ev.value, "reason": reason, "wait_kind": run.wait_kind}, task_id=run.task_id)
        return after

    def transition(self, run_id: str, event: RunEvent | str, *, reason: str | None = None,
                   wait_kind: str | None = None) -> RunState:
        """Apply `event` through `lifecycle.next_state` (E-CONTRACT and no state change when illegal). `wait_kind`
        names the wait entered by `await_input`/new waiting states (default `input`)."""
        with self._lock:
            after = self._apply(self._run(run_id), event, reason=reason, wait_kind=wait_kind)
            self.flush_live()
            return after

    def run_started(self, run_id: str, *, sid: str | None = None, runtime: str | None = None,
                    deadline_wall: float | None = None, budget_max_wall_s: float | None = None,
                    started_wall: float | None = None) -> RunState:
        """Drive `queued -> starting -> running` as needed and emit `swarm.run.started`."""
        with self._lock:
            run = self._run(run_id)
            if run.state is RunState.QUEUED:
                self._apply(run, RunEvent.SLOT_ACQUIRED, reason=None, wait_kind=None)
            self._apply(run, RunEvent.PROCESS_STARTED, reason=None, wait_kind=None)
            now = self.clock.monotonic()
            run.anchor_mono = now
            run.activity_mono = now
            wall = started_wall if started_wall is not None else self.clock.wall()
            if deadline_wall is None and budget_max_wall_s is not None:
                deadline_wall = wall + budget_max_wall_s
            self.emit("swarm.run.started", {"run_id": run_id, "agent": run.agent, "attempt": run.attempt, "sid": sid,
                                    "runtime": runtime, "started_wall": wall, "deadline_wall": deadline_wall,
                                    "budget_max_wall_s": budget_max_wall_s}, task_id=run.task_id)
            self.flush_live()
            return run.state

    def activity(self, run_id: str, *, tool: str | None = None, text: str | None = None, tool_calls: int | None = None,
                 turns: int | None = None, tokens: int | None = None) -> None:
        """Record progress: resets the stall window. Counters are absolute when given; `tool_calls` otherwise counts
        calls that name a `tool`. The event is throttled to one per second per run. Late activity on a settled run is
        ignored."""
        with self._lock:
            run = self._run(run_id)
            if run.end_mono is not None:
                return
            now = self.clock.monotonic()
            run.activity_mono = now
            run.stalled = False  # activity clears the stall episode
            if tool is not None:
                run.last_tool = clip(tool, 64)
            if text is not None:
                run.activity = clip(text)
            elif tool is not None:
                run.activity = run.last_tool
            if tool_calls is not None:
                run.tool_calls = int(tool_calls)
            elif tool is not None:
                run.tool_calls += 1
            if turns is not None:
                run.turns = int(turns)
            if tokens is not None:
                run.tokens = int(tokens)
            if run.last_activity_emit is None or now - run.last_activity_emit >= ACTIVITY_EMIT_INTERVAL_S:
                run.last_activity_emit = now
                self.emit("swarm.run.activity", {"run_id": run_id, "agent": run.agent, "tool": run.last_tool,
                                         "text": run.activity, "tool_calls": run.tool_calls, "turns": run.turns,
                                         "tokens": run.tokens}, task_id=run.task_id, local_only=True)
            self.flush_live()

    def stalled_runs(self) -> list[str]:
        """Run ids that are `running` with no activity for more than `stall_after_s`. `swarm.run.stalled` is emitted
        once per stall episode; activity (or a state change) ends the episode."""
        with self._lock:
            self._fold_external()
            now = self.clock.monotonic()
            out = []
            for run in self._runs.values():
                since = now - run.activity_mono
                if run.state is RunState.RUNNING and since > self.stall_after_s:
                    out.append(run.run_id)
                    if not run.stalled:
                        run.stalled = True
                        self.emit("swarm.run.stalled", {"run_id": run.run_id, "agent": run.agent,
                                                "since_activity_s": round(since, 3), "threshold_s": self.stall_after_s},
                                   task_id=run.task_id)
            return out

    def _fold_external(self) -> None:
        """Fold what agents report out of band into the runs this producer owns (fail-open, display only).

        `shared_status_report` runs in the agent's process and writes `<state dir>/status/<task_id>.json`; a derived
        refresh may not replace this scheduler's live.json, so the producer reads those files itself. Only runs still
        running are looked at, a file is parsed only when its (inode, size, mtime) changed, and the newest of the report
        and the last tool activity wins `activity` (a report also ends a stall episode). `handover_in` / `handover_out`
        come from the handover files of the correlation, re-scanned only when that folder's listing changed."""
        live = [r for r in self._runs.values() if r.end_mono is None and r.state in (RunState.RUNNING, RunState.HANDING_OFF)]
        if not live:
            return
        sdir = _state_dir(self.root, None)
        now_mono, now_wall = self.clock.monotonic(), self.clock.wall()
        for run in live:
            try:
                self._fold_status(run, sdir / STATUS_DIR / f"{run.task_id}.json", now_mono, now_wall)
            except Exception:  # noqa: BLE001 - a display projection: a bad status file hides that report only
                log.debug("status fold failed for %s", run.run_id, exc_info=True)
        self._fold_handovers(live, sdir)

    def _fold_status(self, run: _Run, path: Path, now_mono: float, now_wall: float) -> None:
        try:
            st = path.stat()
        except OSError:
            return
        sig = (st.st_ino, st.st_size, st.st_mtime_ns)
        if sig == run.status_sig:
            return
        run.status_sig = sig
        if not 0 < st.st_size <= STATUS_MAX_BYTES:
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            run.status_sig = None  # a torn or unreadable read: look again next time
            return
        updated = doc.get("updated_wall") if isinstance(doc, dict) else None
        if (not isinstance(updated, (int, float)) or isinstance(updated, bool) or doc.get("task_id") != run.task_id
                or (self.correlation_id and doc.get("correlation_id") not in (None, self.correlation_id))
                or not isinstance(doc.get("kind"), str) or not isinstance(doc.get("step"), str)):
            return
        reported_mono = now_mono - max(0.0, now_wall - float(updated))
        if reported_mono < run.anchor_mono - 1.0:
            return  # a report of an earlier attempt of this task: the file is per task, not per attempt
        if reported_mono >= run.activity_mono:  # newest wins against tool activity; a tie goes to the report
            run.activity = clip(status_label(doc["kind"], doc["step"], doc.get("percent")))
            run.activity_mono = min(reported_mono, now_mono)
            run.stalled = False

    def _fold_handovers(self, live: list[_Run], sdir: Path) -> None:
        corr = self.correlation_id
        if not corr:
            return
        folder = sdir / "handovers" / corr
        try:
            names = tuple(sorted(os.listdir(folder)))
        except OSError:
            return
        key = (names, tuple(sorted((r.task_id, r.agent) for r in live)))
        if key != self._handover_key:
            self._handover_key = key
            tasks = [{"task_id": r.task_id, "agent_id": r.agent} for r in live]
            self._handover_marks = (_handovers_in(sdir, corr, tasks), _handovers_in(sdir, corr, tasks, "from_agent"))
        marks_in, marks_out = self._handover_marks
        for run in live:
            run.handover_in = marks_in.get(run.task_id, run.handover_in)
            run.handover_out = marks_out.get(run.task_id, run.handover_out)

    def _row(self, run: _Run, now: float) -> dict[str, Any]:
        end = run.end_mono if run.end_mono is not None else now
        active = run.end_mono is None
        return {
            "run_id": run.run_id, "task_id": run.task_id, "agent": run.agent, "state": run.state.value,
            "attempt": run.attempt, "elapsed_s": round(max(0.0, end - run.anchor_mono), 3),
            "state_elapsed_s": round(max(0.0, end - run.state_mono), 3),
            "since_activity_s": round(max(0.0, now - run.activity_mono), 3) if active else None,
            "stalled": bool(run.stalled and run.state is RunState.RUNNING), "activity": run.activity,
            "tool_calls": run.tool_calls, "last_tool": run.last_tool, "parent": run.parent,
            "handover_in": run.handover_in, "handover_out": run.handover_out, "error": run.error,
        }

    def heartbeat(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Emit `swarm.run.heartbeat` for one run (or every unsettled run) and return the payloads. Runs the stall check
        first, so a heartbeat is also what notices a silent run."""
        with self._lock:
            self.stalled_runs()
            now = self.clock.monotonic()
            runs = [self._run(run_id)] if run_id is not None else [r for r in self._runs.values() if r.end_mono is None]
            payloads = []
            for run in runs:
                row = self._row(run, now)
                payload = {"run_id": run.run_id, "agent": run.agent, "state": run.state.value,
                           "elapsed_s": row["elapsed_s"], "state_elapsed_s": row["state_elapsed_s"],
                           "since_activity_s": row["since_activity_s"], "stalled": row["stalled"],
                           "tool_calls": run.tool_calls, "last_tool": run.last_tool, "activity": run.activity}
                self.emit("swarm.run.heartbeat", payload, task_id=run.task_id, local_only=True)
                payloads.append(payload)
            self.flush_live(force=True)
            return payloads

    def finish(self, run_id: str, outcome: str, *, error_code: str | None = None, error: str | None = None) -> RunState:
        """Settle a run. `outcome` is ok|error|cancelled|timeout (or a terminal state name). The matching lifecycle event
        is applied unless the run is already settled; `swarm.run.finished` is always emitted once."""
        spec = _OUTCOMES.get(outcome.value if isinstance(outcome, RunState) else outcome)
        if spec is None:
            raise SwarmError(ErrorCode.E_INPUT, f"unknown outcome {outcome!r}; expected one of {sorted(_OUTCOMES)}")
        target, event = spec
        with self._lock:
            run = self._run(run_id)
            if run.end_mono is not None:
                if run.state is not target:
                    raise SwarmError(ErrorCode.E_CONTRACT,
                                     f"run {run_id} already {run.state.value}, cannot finish as {target.value}",
                                     run_id=run_id)
                if run.finished_emitted:
                    return run.state
            else:
                if run.state is RunState.HANDING_OFF and event is RunEvent.FINISH_OK:
                    event = RunEvent.HANDOFF_DONE
                self._apply(run, event, reason=error or error_code, wait_kind=None)
            run.finished_emitted = True
            run.error = clip(error or error_code) if target is not RunState.COMPLETED else None
            end = run.end_mono if run.end_mono is not None else self.clock.monotonic()
            self.emit("swarm.run.finished", {"run_id": run_id, "agent": run.agent, "state": run.state.value,
                                     "outcome": target.value, "duration_s": round(max(0.0, end - run.anchor_mono), 3),
                                     "error_code": error_code, "error": clip(error, 500)}, task_id=run.task_id)
            self.flush_live(force=True)
            return run.state

    def scheduler_tick(self, *, round: int, ready: int, running: int, queued: int, waiting: int, free_slots: int,
                       max_parallel: int, effective_parallel: int, per_agent: dict[str, Any] | None = None,
                       skipped: dict[str, Any] | None = None) -> None:
        """Emit one scheduler round and remember its parallelism for the snapshot."""
        with self._lock:
            self._tick = {"max_parallel": int(max_parallel), "effective_parallel": int(effective_parallel)}
            self.emit("swarm.scheduler.tick", {"round": round, "ready": ready, "running": running, "queued": queued,
                                 "waiting": waiting, "free_slots": free_slots, "max_parallel": max_parallel,
                                 "effective_parallel": effective_parallel, "per_agent": per_agent or {},
                                 "skipped": skipped or {}})
            self.flush_live()

    def degraded(self, reason: str, *, mode: str = "degraded", detail: str | None = None) -> None:
        """Record that a capability fell back (the bar shows a marker) and emit `swarm.degraded`."""
        with self._lock:
            self._degraded = {"reason": clip(reason) or "", "mode": clip(mode, 32) or "degraded"}
            self.emit("swarm.degraded", {"reason": reason, "mode": mode, "detail": detail})
            self.flush_live(force=True)

    def clear_degraded(self, mode: str | None = None) -> None:
        """The fallback is over: drop the `degraded` marker from `live.json` and emit `swarm.degraded` with
        `mode="recovered"` (its `detail` names the mode that ended) so a derived snapshot clears it too. With `mode`, only
        a marker of that mode is cleared (a later, different fallback stays). No marker, nothing happens."""
        with self._lock:
            if self._degraded is None or (mode is not None and self._degraded["mode"] != clip(mode, 32)):
                return
            ended = self._degraded["mode"]
            self._degraded = None
            self.emit("swarm.degraded", {"reason": "recovered", "mode": RECOVERED_MODE, "detail": ended})
            self.flush_live(force=True)

    def handover(self, run_id: str, *, to_agent: str, handover_id: str, digest: str | None, nbytes: int,
                 summarized: bool = False) -> None:
        """Emit `swarm.run.handover` for the run's agent handing work to `to_agent`."""
        with self._lock:
            run = self._run(run_id)
            run.handover_out = clip_id(handover_id, 64)
            self.emit("swarm.run.handover", {"run_id": run_id, "from_agent": run.agent, "to_agent": to_agent,
                                     "handover_id": handover_id, "digest": digest, "bytes": nbytes,
                                     "summarized": summarized}, task_id=run.task_id)
            self.flush_live()

    # -- snapshot / live.json --
    def snapshot(self) -> dict[str, Any]:
        """Current `swarm.live.v1` document (runs capped at 64, strings redacted and clipped)."""
        with self._lock:
            self.stalled_runs()
            now = self.clock.monotonic()
            rows = [self._row(r, now) for r in self._runs.values()]
            t0 = self._t0 if self._t0 is not None else now
            elapsed = (self._t_end if self._t_end is not None else now) - t0
            max_parallel = self._tick.get("max_parallel") or _env_int("SWARM_MAX_PARALLEL", DEFAULT_MAX_PARALLEL)
            effective = self._tick.get("effective_parallel") or max_parallel
            return build_snapshot(rows, correlation_id=self.correlation_id, seq=self._seq,
                                  updated_wall=self.clock.wall(), elapsed_s=elapsed, max_parallel=max_parallel,
                                  effective_parallel=effective, degraded=self._degraded)

    def flush_live(self, force: bool = False) -> bool:
        """Write `live.json` atomically; at most one write per second unless `force`. Returns True when written.
        A write failure is logged and reported as False: the file is a display projection, never load-bearing."""
        if not self._live:
            return False
        with self._lock:
            now = self.clock.monotonic()
            if not force and self._last_flush is not None and now - self._last_flush < LIVE_FLUSH_INTERVAL_S:
                return False
            self._seq += 1
            snap = self.snapshot()
            doc = {**snap, "producer": "scheduler", "pid": os.getpid(), "producer_updated": snap["updated_wall"]}
            try:
                with _live_lock(self._live_target()):  # a held lock keeps a derived writer out of the check-then-write window
                    _write_atomic(self._live_target(), doc)
            except OSError:
                log.warning("could not write %s", self._live_target(), exc_info=True)
                return False
            self._last_flush = now
            return True


# ---- readers and the stateless path -----------------------------------------------------------------------------
def read_live(root: str | Path | None = None) -> dict[str, Any] | None:
    """The parsed `live.json`, or None when it is absent, unreadable, malformed or off-schema. Never raises."""
    try:
        return _read_live_at(live_path(root))
    except SwarmError:
        return None


_TS_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)Z")


def _parse_ts(ts: object) -> float | None:
    """Epoch seconds of a `%Y-%m-%dT%H:%M:%SZ` UTC stamp; None for anything else."""
    return _parse_ts_str(ts) if isinstance(ts, str) else None


@functools.lru_cache(maxsize=8192)
def _parse_ts_str(ts: str) -> float | None:
    """Hand-rolled parse (strptime was the hottest line of the stateless path), memoised: stamps repeat per second."""
    m = _TS_RE.fullmatch(ts)
    if m is None:
        return None
    try:
        year, month, day, hour, minute, second = map(int, m.groups())
        return float(calendar.timegm(_dt.datetime(year, month, day, hour, minute, second).timetuple()))
    except ValueError:
        return None


class _Tail:
    """Incrementally parsed events.jsonl tail: `records` cover bytes [start, end) of one inode."""

    def __init__(self, ino: int, start: int):
        self.ino, self.start, self.end = ino, start, start
        self.records: list[dict[str, Any]] = []


_TAIL_CACHE: dict[str, _Tail] = {}


def _parse_events(data: bytes, out: list[dict[str, Any]]) -> None:
    for line in data.decode("utf-8", errors="replace").splitlines():
        if '"swarm.' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("type"), str) and rec["type"].startswith("swarm.") \
                and isinstance(rec.get("payload"), dict):
            out.append(rec)


def _read_tail(path: Path, max_bytes: int = TAIL_BYTES) -> list[dict[str, Any]]:
    """Parsed `swarm.*` records from (about) the last `max_bytes` of events.jsonl; malformed lines (a torn tail) are
    skipped. Per process the parse is incremental: an unchanged file costs one `stat`, an appended file only its new
    bytes (a window past 2x `max_bytes`, a replaced inode or a shrunk file is re-read from scratch). An unterminated
    final line is left for the next call unless it already is a complete record. Callers must not mutate the result."""
    key = str(path)
    try:
        st = path.stat()
        tail = _TAIL_CACHE.get(key)
        fresh = tail is None or tail.ino != st.st_ino or st.st_size < tail.end or tail.end - tail.start > 2 * max_bytes
        if tail is None or fresh:
            tail = _Tail(st.st_ino, max(0, st.st_size - max_bytes))
        elif st.st_size == tail.end:
            return tail.records
        with path.open("rb") as fh:
            fh.seek(tail.end)
            data = fh.read()
    except OSError:
        return []
    base = tail.end
    if fresh and base > 0:  # a window that starts mid-file starts mid-line: drop that first line
        head, nl, data = data.partition(b"\n")
        base += len(head) + len(nl)
    complete, nl, rest = data.rpartition(b"\n")
    consumed_to = base + len(complete) + len(nl)
    records = tail.records if fresh else list(tail.records)  # lists handed out earlier stay unchanged
    _parse_events(complete, records)
    if rest.strip():
        extra: list[dict[str, Any]] = []
        _parse_events(rest, extra)
        if extra:  # unterminated, but already a whole record
            records += extra
            consumed_to = base + len(data)
    new = _Tail(tail.ino, tail.start)
    new.records, new.end = records, consumed_to
    _TAIL_CACHE[key] = new
    return records


def _load_tasks(db: Path, correlation_id: str | None, state_dir: Path) -> tuple[str | None, list[dict[str, Any]], dict[str, list[tuple[Any, ...]]]]:
    """(correlation, task rows, transitions by task) from a read-only connection; nothing when there is no store."""
    if not db.exists():
        return correlation_id, [], {}
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        if correlation_id is None:
            marker = state_dir / "latest_correlation"
            try:
                correlation_id = marker.read_text(encoding="utf-8").strip() or None
            except OSError:
                correlation_id = None
        rows = []
        if correlation_id is not None:
            rows = conn.execute("SELECT * FROM tasks WHERE correlation_id=? ORDER BY dag_depth, created_at",
                                (correlation_id,)).fetchall()
        if not rows:
            newest = conn.execute("SELECT correlation_id FROM tasks ORDER BY updated_at DESC LIMIT 1").fetchone()
            if newest is None:
                return correlation_id, [], {}
            correlation_id = newest[0]
            rows = conn.execute("SELECT * FROM tasks WHERE correlation_id=? ORDER BY dag_depth, created_at",
                                (correlation_id,)).fetchall()
        ids = [r["task_id"] for r in rows]
        marks = ",".join("?" * len(ids))
        trans: dict[str, list[tuple[Any, ...]]] = {tid: [] for tid in ids}
        for t in conn.execute(f"SELECT task_id, to_state, reason, ts FROM transitions WHERE task_id IN ({marks}) "
                              "ORDER BY id", ids):
            trans[t["task_id"]].append((t["to_state"], t["reason"], t["ts"]))
        return correlation_id, [dict(r) for r in rows], trans
    finally:
        conn.close()


_T = TypeVar("_T")


def _loads(text: str | None, default: _T) -> _T:
    try:
        val = json.loads(text) if text else default
    except ValueError:
        return default
    return val if isinstance(val, type(default)) else default


def derive_snapshot(root: str | Path | None = None, *, clock: Clock = SYSTEM_CLOCK, db_path: str | Path | None = None,
                    correlation_id: str | None = None, stall_after_s: float | None = None) -> dict[str, Any]:
    """A `swarm.live.v1` document rebuilt, statelessly, from Task Store rows (mapped with `lifecycle.from_task_state`)
    and the `events.jsonl` tail (activity and heartbeat). Runs without a scheduler process show up too.

    The Task Store keeps wall timestamps only, so elapsed values here are wall differences clamped at zero;
    `Telemetry.snapshot` is the producer-monotonic variant. Writes nothing."""
    from .taskstore import SATISFIED_STATES  # lazy: taskstore imports this module lazily too

    sdir = _state_dir(root, db_path)
    db = Path(db_path).resolve() if db_path is not None else sdir / "tasks.db"
    stall_after = stall_after_s if stall_after_s is not None else stall_after_default()
    corr, tasks, trans = _load_tasks(db, correlation_id, sdir)
    now_wall = clock.wall()

    events = _read_tail(sdir / "events.jsonl")
    last_by_run: dict[str, dict[str, dict[str, Any]]] = {}  # key -> {"activity": rec, "heartbeat": rec, "handover": rec}
    tick: dict[str, Any] | None = None
    degraded: dict[str, Any] | None = None
    finished: dict[str, str] = {}
    for rec in events:
        if corr is not None and rec.get("correlation_id") not in (None, corr):
            continue
        kind, p = rec["type"], rec["payload"]
        if kind == EV_TICK:
            tick = p
        elif kind == EV_DEGRADED:
            if p.get("mode") == RECOVERED_MODE:  # clear_degraded(): the fallback it names (`detail`) is over
                if degraded is not None and p.get("detail") in (None, degraded["mode"]):
                    degraded = None
            else:
                degraded = {"reason": clip(p.get("reason")) or "", "mode": clip(p.get("mode"), 32) or "degraded"}
        elif kind in (EV_ACTIVITY, EV_HEARTBEAT, EV_HANDOVER):
            key = p.get("run_id") or rec.get("task_id")
            if isinstance(key, str):
                last_by_run.setdefault(key, {})[kind] = rec
        elif kind == EV_FINISHED and isinstance(p.get("run_id"), str):
            finished[p["run_id"]] = str(p.get("state"))

    by_id = {t["task_id"]: t for t in tasks}
    handover_in = _handovers_in(sdir, corr, tasks)
    rows: list[dict[str, Any]] = []
    starts: list[float] = []
    ends: list[float] = []
    for t in tasks:
        deps: list[str] = _loads(t.get("depends_on"), [])
        deps_met = all((by_id.get(d) or {}).get("state") in SATISFIED_STATES for d in deps)
        state = from_task_state(t["state"], deps_met=deps_met)
        attempt = max(int(t.get("attempt") or 0), 1)
        run_id = f"{t['task_id']}.a{attempt}"
        if state is RunState.FAILED and finished.get(run_id) == RunState.TIMED_OUT.value:
            state = RunState.TIMED_OUT
        history = trans.get(t["task_id"], [])
        last_ts = history[-1][2] if history else (t.get("updated_at") or now_wall)
        claimed = [ts for to, _, ts in history if to == "CLAIMED"]
        start_ts = claimed[-1] if claimed else (history[0][2] if history else (t.get("created_at") or last_ts))
        settled = is_terminal(state)
        end_ts = last_ts if settled else now_wall
        starts.append(start_ts)
        ends.append(end_ts)
        activity = last_by_run.get(run_id) or last_by_run.get(t["task_id"]) or {}
        act, hb = activity.get(EV_ACTIVITY), activity.get(EV_HEARTBEAT)
        latest = max((r for r in (act, hb) if r), key=lambda r: r.get("ts") or "", default=None)
        last_act_wall: float | None = None
        if act and _parse_ts(act.get("ts")) is not None:
            last_act_wall = _parse_ts(act["ts"])
        hb_wall = _parse_ts(hb.get("ts")) if hb else None
        if hb and hb_wall is not None and isinstance(hb["payload"].get("since_activity_s"), (int, float)):
            cand = hb_wall - float(hb["payload"]["since_activity_s"])
            last_act_wall = cand if last_act_wall is None else max(last_act_wall, cand)
        if last_act_wall is None or last_act_wall < start_ts:
            last_act_wall = last_ts if state is RunState.RUNNING else None
        since = max(0.0, now_wall - last_act_wall) if (last_act_wall is not None and not settled) else None
        lp = latest["payload"] if latest else {}
        ho = (activity.get(EV_HANDOVER) or {}).get("payload", {})
        error = None
        if state in (RunState.FAILED, RunState.TIMED_OUT) and history:
            error = clip(history[-1][1])
        rows.append({
            "run_id": run_id, "task_id": t["task_id"], "agent": clip(t.get("agent_id") or t.get("capability"), 64) or "?",
            "state": state.value, "attempt": attempt, "elapsed_s": round(max(0.0, end_ts - start_ts), 3),
            "state_elapsed_s": round(max(0.0, end_ts - last_ts), 3) if not settled else 0.0,
            "since_activity_s": round(since, 3) if since is not None else None,
            "stalled": bool(state is RunState.RUNNING and since is not None and since > stall_after),
            "activity": clip(lp.get("text") or lp.get("activity")), "tool_calls": int(lp.get("tool_calls") or 0),
            "last_tool": clip(lp.get("tool") or lp.get("last_tool"), 64), "parent": t.get("parent_id"),
            "handover_in": handover_in.get(t["task_id"]), "handover_out": clip_id(ho.get("handover_id"), 64), "error": error,
        })

    max_parallel = int((tick or {}).get("max_parallel") or _env_int("SWARM_MAX_PARALLEL", DEFAULT_MAX_PARALLEL))
    effective = int((tick or {}).get("effective_parallel") or max_parallel)
    elapsed = (max(ends) - min(starts)) if rows else 0.0
    return build_snapshot(rows, correlation_id=corr, seq=0, updated_wall=now_wall, elapsed_s=elapsed,
                          max_parallel=max_parallel, effective_parallel=effective, degraded=degraded)


def refresh_live(root: str | Path | None = None, *, db_path: str | Path | None = None, clock: Clock = SYSTEM_CLOCK,
                 correlation_id: str | None = None) -> bool:
    """Derive the snapshot and write `live.json` atomically. Fail-open: any error is logged and returns False. When a
    `Telemetry` in this process owns the same file it is flushed instead (its in-memory view is richer).

    A derived write never replaces a document another LIVE scheduler process produced (`producer == "scheduler"`, fresh
    within 3 heartbeats, pid alive): that file is richer and monotonic, and the call returns False. The whole
    read-check-derive-write cycle runs under an flock on `live.json.lock`, so two derived writers cannot interleave and
    a stale derivation cannot overwrite a newer one."""
    try:
        target = (_state_dir(root, db_path) / LIVE_FILE)
        owner = _ACTIVE.get(str(target))
        tel = owner() if owner is not None else None
        if tel is not None:
            return tel.flush_live()
        with _live_lock(target) as held:
            if not held and fcntl is not None:
                log.warning("live.json lock busy; skipping derived refresh")
                return False
            prior = _read_live_at(target)
            if _producer_fresh(prior, clock.wall(), owned_here=False):
                return False
            doc = derive_snapshot(root, clock=clock, db_path=db_path, correlation_id=correlation_id)
            doc["seq"] = (prior["seq"] + 1) if prior else 1
            doc["producer"] = "derived"
            doc["pid"] = os.getpid()
            _write_atomic(target, doc)
            _remember_valid(target)  # our own derived document: the next refresh need not re-validate it
        return True
    except Exception:  # noqa: BLE001 - observer path: a display projection must never break a Task Store transition
        log.warning("refresh_live failed", exc_info=True)
        return False
