"""Hook trigger core (n3/n2). Opt-in, ID hash, dedup, fire context or detached, thin.

Opt-in is AIO_SWARM_AFTER_ORCH or SWARM_AFTER_ORCH set to 1, true, yes, or on.
Any other value, including unset, is off. When opt-in is off, or the payload has
no completion signal, fire_if_ready returns without writing a hook-fired event.

gsd-autonomous trigger integration: calls OMP path for concurrent launch (Promise
primitives, partition workload slices, stream per-subagent identifiers + real-time
status to OMP session). Wraps with timeouts/retries/backoff, limit=4 default even
partition; degrade warning if no primitive. Uses exact verbatim command
`/gsd-autonomous workflowz ultrathink orchestrate` . autonomous_run detached only
for non-gsd cases. Once first conditions hold, other cases record refusal.
SPE-13819 n3.6-7.
"""

from __future__ import annotations
import hashlib
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from .envelope import real_key_configured
from .signal_detector import detect_completion
from .state_store import get_trigger_store, get_swarm_state_store
from .runlog import emit as runlog_emit
from .observability import emit_hook_fired
from .subagent_lifecycle import SubagentLifecycleManager, get_manager  # for subagent lifecycle on trigger spawn (unique ID + ts log; prior init/trigger integration)
# Non-empty values mean this process is a cloud agent and must not detach a runner.
CLOUD_AGENT_MARKERS = ("CURSOR_AGENT", "CURSOR_CLOUD_AGENT", "CLOUD_AGENT")
# Same names scripts/swarm_run.py strips (AGENT_SESSION_STRIPPED). Follow-on model sessions get no signing material.
SIGNING_ENV_STRIPPED = (
    "SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY",
    "SWARM_TASK_ID", "SWARM_CORRELATION_ID",
)


def _follow_on_env() -> dict[str, str]:
    """Controller environment minus signing material, for follow-on sessions.

    The session runs with the controller identity (SWARM_AGENT=a01-orchestrator), so the swarm guard applies to it.
    """
    env = {k: v for k, v in os.environ.items() if k not in SIGNING_ENV_STRIPPED}
    env["SWARM_AGENT"] = "a01-orchestrator"
    env["SWARM_CHILD"] = "0"
    env["AIO_SWARM_AFTER_ORCH"] = "1"
    return env


def _release_when_ended(mgr: SubagentLifecycleManager, sid: str, proc: subprocess.Popen[bytes], *, root: str | Path | None) -> None:
    """Free sid's lifecycle slot once the detached follow-on session exits. A daemon thread waits on proc."""
    def _watch() -> None:
        try:
            proc.wait()
        except Exception as exc:
            # The session can no longer be observed; free the slot rather than hold it forever.
            runlog_emit("follow_on.wait_failed", {"subagent_id": sid, "error": type(exc).__name__}, source="hook_trigger", root=root)
        finally:
            mgr.release(sid)
    threading.Thread(target=_watch, name=f"follow-on-{sid}", daemon=True).start()


def is_opt_in() -> bool:
    v = os.environ.get("AIO_SWARM_AFTER_ORCH") or os.environ.get("SWARM_AFTER_ORCH")
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _spawn_refusal() -> tuple[str, str | None] | None:
    """None when the runner may be detached. Otherwise (reason, marker or None)."""
    if not real_key_configured():
        return ("no-signing-key", None)
    if os.environ.get("SWARM_ALLOW_AUTONOMOUS", "").strip() != "1":
        return ("autonomous-not-allowed", None)
    for name in CLOUD_AGENT_MARKERS:
        if os.environ.get(name, "").strip():
            return ("cloud-agent", name)
    return None


def compute_trigger_id(session_id: str, corr: str | None, event_type: str) -> str:
    """Stable ID (greptile: avoid payload jitter)."""
    key = f"{session_id}|{corr or ''}|{event_type}".encode()
    return hashlib.sha256(key).hexdigest()[:16]


def fire_if_ready(payload: str | dict[str, Any], *, session_id: str = "unknown", corr: str | None = None, root: str | Path | None = None) -> dict[str, Any]:
    """Idempotent fire. Returns {fired, deduped, id, event}."""
    if not is_opt_in():
        return {"fired": False, "reason": "opt-in-off"}
    event = detect_completion(payload)
    if not event:
        return {"fired": False, "reason": "no-signal"}
    event_type = event.get("type", "unknown")
    tid = compute_trigger_id(session_id, corr, event_type)
    blocked = _spawn_refusal()
    if blocked is not None:
        reason, marker = blocked
        fields: dict[str, str] = {"reason": reason}
        if marker:
            fields["marker"] = marker
        emit_hook_fired("a01_complete", corr=corr, deduped=False, root=root, **fields)
        out = {"fired": False, "reason": reason, "id": tid}
        if marker:
            out["marker"] = marker
        return out
    store = get_trigger_store(root=root)
    if store.seen_trigger(tid, correlation_id=corr, brief_hash=event_type):
        emit_hook_fired("a01_complete", corr=corr, deduped=True, root=root)
        return {"fired": False, "deduped": True, "id": tid}
    # record + update obs, then forward to OMP gsd-autonomous command for concurrent
    # launch integration (subagents in parallel via TS primitives e.g. Promise.allSettled,
    # even partition at launch time default 4, per-subagent ID + real-time status stream
    # to OMP session, timeouts+retries+backoff, degrade to seq only with warning if no
    # primitives). Uses exact verbatim `/gsd-autonomous workflowz ultrathink orchestrate`
    # to enforce Agent Swarm activation + concurrent on command path (no silent bypass).
    # A spawn error is recorded, not swallowed. SPE-13819 n3.6-7.
    # Agent Swarm initialization logic: emit swarm ID on start, explicit INITIALIZING for default activation observable
    swarm_id = f"swarm-{tid}"
    runlog_emit("swarm.start", {"swarm_id": swarm_id, "trigger_id": tid, "corr": corr, "session_id": session_id, "event_type": event_type}, source="init", correlation_id=corr, root=root)
    emit_hook_fired("a01_complete", corr=corr, deduped=False, root=root, event=event, swarm_id=swarm_id)
    ss = get_swarm_state_store(root=root)
    ss.update("INITIALIZING", corr=corr, extra={"last_trigger": tid, "swarm_id": swarm_id})
    mgr = None
    sid = None
    try:
        target_repo = Path(root).resolve() if root else Path.cwd().resolve()
        omp_ext = Path(__file__).resolve().parent.parent / "omp"
        env = _follow_on_env()
        mgr = get_manager()
        sid = mgr.new_subagent_id(corr or "autonomous", root=root)
        # hold a lifecycle slot for the follow-on session: acquired before spawn, released on spawn failure or session exit
        mgr.acquire(sid)
        # lifecycle: spawn logged with unique ID+ts; use OMP -p + verbatim command for concurrent
        # visibility of spawn/IDs/tasks/status to OMP session (replaces sequential autonomous_run)
        omp_cmd = [
            "omp", "-p",
            "--mode", "json",
            "--no-session", "--no-title", "--no-extensions",
            "-e", str(omp_ext),
            "--cwd", str(target_repo),
            "--approval-mode", "write",
            f"/gsd-autonomous workflowz ultrathink orchestrate after a01 {corr or ''} sid={sid}",
        ]
        proc = subprocess.Popen(
            omp_cmd,
            cwd=str(target_repo),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _release_when_ended(mgr, sid, proc, root=root)
    except Exception as exc:
        if mgr is not None and sid is not None:
            mgr.release(sid)
        emit_hook_fired("a01_complete", corr=corr, deduped=False, root=root,
                        reason="spawn-failed", error=type(exc).__name__)
        return {"fired": False, "reason": "spawn-failed", "id": tid}
    return {"fired": True, "deduped": False, "id": tid, "event": event, "subagent_id": sid}
