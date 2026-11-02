"""Thin obs facade over runlog + state_store (n5/n2). 6-state, events, projection.
Boring; no mutation of source.

Enhanced n6 for gsd-autonomous workflowz ultrathink orchestrate : full states LOADING IN_PROGRESS COMPLETED FAILED EMPTY ERROR in SwarmStateStore,
separate audit logs, enhance emits/stream for immediate spawn/ID/slices/status to OMP, support consolidated metrics.
OMP session visibility, subagent IDs. SPE-13823 n6.
"""

from __future__ import annotations
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .atomic_file import write_text_atomic
from .paths import swarm_dir
from .runlog import emit
from .state_store import claude_state_dir, get_swarm_state_store
from .lifecycle import TRANSITIONS, RunState, from_task_state

if TYPE_CHECKING:
    from .taskstore import TaskStore

log = logging.getLogger("swarm.observability")

# a subagent id is `subagent-<task_id>-<12 hex>-<epoch ms>` (subagent_lifecycle.new_subagent_id)
_SID_TASK = re.compile(r"^subagent-(.+)-[0-9a-f]{12}-\d+$")


def _task_of(subagent_id: str | None) -> str | None:
    """The task id a subagent id names, or None for ids that name no task (`lifecycle`, `runner-init`, ...)."""
    match = _SID_TASK.match(subagent_id or "")
    return match.group(1) if match else None

# Progress events of the subagent lifecycle and the OMP gsd-autonomous run are the controller's (A01): the lifecycle
# manager is only created in the controller process (hook_trigger), and a subagent id names no agent. substrate_tee
# attributes a record by the `<AGENT>@<script>` prefix of its source, so these sources carry the controller's id.
CONTROLLER_AGENT = "A01"

def emit_hook_fired(hook: str, *, corr: str | None = None, deduped: bool = False, root: str | Path | None = None, **kw: Any) -> dict[str, Any]:
    return emit("hook-fired", {"hook": hook, "deduped": deduped, **kw}, source="hook_trigger", correlation_id=corr, root=root)
def emit_per_unit(task_id: str, agent: str, phase: str, state: str, *, corr: str | None = None, root: str | Path | None = None) -> dict[str, Any]:
    return emit("per-unit", {"task_id": task_id, "agent": agent, "phase": phase, "state": state}, source="execution", correlation_id=corr, task_id=task_id, root=root)


def emit_batch_end(round_n: int, dispatched: int, counts: dict[str, Any], *, corr: str | None = None, root: str | Path | None = None) -> dict[str, Any]:
    return emit("batch-end", {"round": round_n, "dispatched": dispatched, "counts": counts}, source="swarm_run", correlation_id=corr, root=root)


def emit_stale(kind: str, reason: str, *, task_id: str | None = None, corr: str | None = None, root: str | Path | None = None) -> dict[str, Any]:
    return emit("stale", {"kind": kind, "reason": reason}, source="watchdog", correlation_id=corr, task_id=task_id, root=root)


def build_swarm_state(*, corr: str | None = None, root: str | Path | None = None, slice_id: str | None = None, subagent_id: str | None = None) -> dict[str, Any]:
    """Build 6-state projection (n5). Extended for full states LOADING IN_PROGRESS COMPLETED FAILED EMPTY ERROR, immediate transitions.
    Supports gsd-autonomous, wires OMP gsd command use, calls audit for lifecycle/ids.
    Add support perID partial on FAILED/ERROR states. Isolate errors with try.
    States are counted from the Task Store rows only; a Task Store that cannot be read is ERROR, never derived from events.
    """
    store = get_swarm_state_store(root=root)
    done = 0
    total = 0
    active = 0
    try:
        from .taskstore import TaskStore
        ts = TaskStore(root=root)
        tasks = ts.list(correlation_id=corr) if corr else ts.list()
    except Exception:  # noqa: BLE001 - an unreadable Task Store is ERROR; the state is never derived from events
        state = "ERROR"
    else:
        total = len(tasks)
        done = sum(1 for t in tasks if t.get("state") in ("DONE", "APPROVED"))
        failures = sum(1 for t in tasks if t.get("state") in ("FAILED", "ESCALATED"))
        active = sum(1 for t in tasks if t.get("state") in ("PENDING", "RUNNING", "IN_PROGRESS"))
        if total == 0:
            state = "EMPTY"
        elif done == total:
            state = "COMPLETED"
        elif failures > 0:
            state = "PARTIAL_FAILURE" if done > 0 else "ERROR"
        else:
            state = "IN_PROGRESS"

    progress = {"done": done, "total": total, "active": active}
    # support consolidated metrics
    consolidated = {"done": done, "total": total, "active": active, "completion": (done / total * 100) if total else 0}
    extra: dict[str, Any] = {"consolidated_metrics": consolidated, "slice_id": slice_id}
    if subagent_id and state in ("FAILED", "ERROR"):
        extra["per_id_partial"] = {"subagent_id": subagent_id, "state": state, "partial": True}
    try:
        data = store.update(state, corr=corr, progress=progress, extra=extra)
    except Exception:
        data = {"swarm_state": state, "progress": progress}
    # gsd append extended
    try:
        store.append_gsd_line(f"{corr or 'no-corr'} {state} {done}/{total}", root=root, state=state)
        store.append_gsd_audit("build_swarm_state", subagent_id=subagent_id, task_slice=slice_id, state=state, identity=corr, root=root)
    except Exception:
        pass  # isolate
    return data


def write_claude_state(data: dict[str, Any], *, root: str | Path | None = None) -> Path:
    """Atomic write .claude/swarm-state.json (n5); SWARM_CLAUDE_STATE_DIR replaces the .claude directory."""
    cdir = claude_state_dir(Path(swarm_dir(root, create=True)).parent / ".claude")
    cdir.mkdir(parents=True, exist_ok=True)
    p = cdir / "swarm-state.json"
    write_text_atomic(p, json.dumps(data, indent=2) + "\n")
    return p


def update_from_events(corr: str | None = None, *, root: str | Path | None = None, slice_id: str | None = None, subagent_id: str | None = None) -> dict[str, Any]:
    """Update from events, wired to support slice, sub ID for gsd OMP visibility."""
    data = build_swarm_state(corr=corr, root=root, slice_id=slice_id)
    write_claude_state(data, root=root)
    # emit gsd for OMP
    if subagent_id or slice_id:
        emit_gsd_omp("updated", subagent_id=subagent_id, task_id=None, corr=corr, root=root, task_slice=slice_id, state=data.get("swarm_state"))
    return data
def emit_gsd_omp(status: str = "launched", *, subagent_id: str | None = None, task_id: str | None = None, corr: str | None = None, root: str | Path | None = None, task_slice: str | None = None, spawn_event: bool = False, state: str | None = None, error_agg: dict[str, Any] | None = None, dry_run: bool = False, **kw: Any) -> dict[str, Any]:
    """Integrate emit for OMP gsd-autonomous. Per-subagent status streaming for OMP session visibility in concurrent launches.
    Enhanced for real-time per-sub, spawn events, IDs, task slices, all states (LOADING IN_PROGRESS COMPLETED FAILED EMPTY ERROR etc).
    Add support emit for error agg, perID partial on FAILED/ERROR. Isolate with try. Wire to gsd states.
    Support consolidated metrics. Wire for OMP gsd command use.
    """
    task_id = task_id or _task_of(subagent_id)
    effective_state = state or status
    payload = {
        "subagent_id": subagent_id,
        "status": status,
        "state": effective_state,
        "task_id": task_id,
        "task_slice": task_slice,
        "spawn_event": spawn_event,
        "ts": time.time(),
        "consolidated": kw.get("consolidated"),
        "error_agg": error_agg,
        **kw
    }
    if effective_state in ("FAILED", "ERROR") and subagent_id:
        payload.setdefault("per_id_partial", True)
    # audit for lifecycle/identities , isolate
    try:
        store = get_swarm_state_store(root=root)
        store.append_gsd_audit("emit_gsd_omp", subagent_id=subagent_id, task_slice=task_slice, state=effective_state, identity=subagent_id or corr, root=root)
    except Exception:
        pass
    return emit("gsd.autonomous.omp", payload, source=f"{CONTROLLER_AGENT}@omp_gsd", correlation_id=corr, task_id=task_id, root=root, tee=not dry_run)
def stream_subagent_status(subagent_id: str, status: str, *, task_id: str | None = None, phase: str = "exec", corr: str | None = None, root: str | Path | None = None, task_slice: str | None = None, spawn_event: bool = False, state: str | None = None, consolidated: dict[str, Any] | None = None, error_agg: dict[str, Any] | None = None, dry_run: bool = False, **kw: Any) -> dict[str, Any]:
    """Stream per-subagent identifier + real-time status directly to OMP session.
    Enhanced for immediate spawn events, IDs, task slices, all states (LOADING IN_PROGRESS COMPLETED FAILED EMPTY ERROR).
    Add support emit for error agg, perID partial on FAILED/ERROR. Isolate errors with try.
    Used for gsd-autonomous workflowz ultrathink orchestrate concurrent visibility.
    Support consolidated metrics. Also emits per-unit. Wire OMP gsd command.
    """
    task_id = task_id or _task_of(subagent_id)
    effective_state = state or status
    payload = {
        "subagent_id": subagent_id,
        "status": status,
        "state": effective_state,
        "task_id": task_id,
        "phase": phase,
        "task_slice": task_slice,
        "spawn_event": spawn_event,
        "ts": time.time(),
        "consolidated": consolidated,
        "error_agg": error_agg,
        **kw,
    }
    if effective_state in ("FAILED", "ERROR") and subagent_id:
        payload.setdefault("per_id_partial", True)
    if task_id:
        try:
            emit_per_unit(task_id, "subagent", phase, effective_state, corr=corr, root=root)
        except Exception:
            pass  # isolate
    # audit lifecycle identities , isolate
    try:
        store = get_swarm_state_store(root=root)
        store.append_gsd_audit("stream_subagent_status", subagent_id=subagent_id, task_slice=task_slice, state=effective_state, identity=subagent_id, root=root)
    except Exception:
        pass
    return emit("subagent.status.stream", payload, source=f"{CONTROLLER_AGENT}@subagent_lifecycle", correlation_id=corr, task_id=task_id, root=root, tee=not dry_run)
def emit_error_agg(subagent_id: str, agg: dict[str, Any], *, task_id: str | None = None, corr: str | None = None, root: str | Path | None = None, state: str = "ERROR", dry_run: bool = False, **kw: Any) -> dict[str, Any]:
    """Dedicated support emit for error agg from CentralErrorAggregator.
    Per sub ID details/partials/retry surfaced. Called on FAILED/ERROR.
    Wire to gsd states, isolate with try.
    """
    try:
        payload = {
            "subagent_id": subagent_id,
            "error_agg": agg,
            "state": state,
            "task_id": task_id,
            "ts": time.time(),
            "per_id_partial": True,
            **kw
        }
        store = get_swarm_state_store(root=root)
        store.append_gsd_audit("emit_error_agg", subagent_id=subagent_id, state=state, identity=corr, root=root)
        # also stream via status
        try:
            stream_subagent_status(subagent_id, "error", task_id=task_id, state=state, error_agg=agg, root=root, dry_run=dry_run)
        except Exception:
            pass
        record = emit("error.agg", payload, source=f"{CONTROLLER_AGENT}@error_aggregator", correlation_id=corr, task_id=task_id, root=root, tee=not dry_run)
        return {**record, "written": True}
    except Exception:  # noqa: BLE001 - the aggregate is a side record; the caller learns that it was not written
        log.warning("error aggregate record could not be written", exc_info=True)
        return {"subagent_id": subagent_id, "error_agg": agg, "state": state, "written": False}


def emit_inline_transition(store: TaskStore, task: dict[str, Any], from_state: str, to_state: str, *, reason: str | None = None,
                           root: str | Path | None = None) -> list[dict[str, Any]]:
    """Run records for one in-session task transition (no scheduler process: A01 drives `orch_status.py`).

    `task` is the Task Store row after the move. TaskStore's observer already refreshes live.json; this writes the events
    a scheduler's Telemetry would have written: `swarm.run.state` for the projected RunState change and, when the run
    settled, `swarm.run.finished` (duration from the last CLAIMED transition). A task a runner is working
    (`notes.running`) is the runner's own business and gets nothing, as does a move the run state machine has no event
    for (the projection does not change, or a settled run is reopened as a new attempt). Same source and shape as
    telemetry.py."""
    if (task.get("notes_json") or {}).get("running") is not None:
        return []
    before, after = from_task_state(from_state), from_task_state(to_state)
    event = next((ev for (src, ev), dst in TRANSITIONS.items() if src is before and dst is after), None)
    if before is after or event is None:
        return []
    tid, corr = task["task_id"], task.get("correlation_id")
    run_id = f"{tid}.a{max(int(task.get('attempt') or 0), 1)}"
    agent = task.get("agent_id") or task.get("capability") or "?"
    records = [emit("swarm.run.state",
                    {"run_id": run_id, "agent": agent, "from": before.value, "to": after.value, "event": event.value,
                     "reason": reason, "wait_kind": None},
                    source="telemetry", correlation_id=corr, task_id=tid, root=root)]
    if after in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT):
        claimed = [h["ts"] for h in store.history(tid) if h["to_state"] == "CLAIMED"]
        duration = max(0.0, time.time() - claimed[-1]) if claimed else 0.0
        failed = after is not RunState.COMPLETED
        records.append(emit("swarm.run.finished",
                            {"run_id": run_id, "agent": agent, "state": after.value, "outcome": after.value,
                             "duration_s": round(duration, 3), "error_code": None,
                             "error": (reason or None) if failed else None},
                            source="telemetry", correlation_id=corr, task_id=tid, root=root))
    return records
