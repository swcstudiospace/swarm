"""Shared runtime tools (DESIGN 8): what any running agent needs besides the handover set.

Tools (owner `shared`, callable by all 15 agents):
- shared_status_report: record the latest progress/blocked/done report of the caller's own task and emit swarm.run.activity.
- shared_scratchpad_put / shared_scratchpad_get: bounded per-correlation key/value store with owner rules and compare-and-swap.
- shared_escalate: record escalation.request and return a ready BLOCKED task.result patch plus the escalation owner.
- shared_cancel_check: cooperative-cancel flag and deadline arithmetic for a task.
- shared_log_emit: one structured `agent.log` run-log event.
- shared_route_check: whether ACP allows a message type between two agents (swarm.acp.message_allowed).
- shared_escalation_owner: who becomes accountable when the swarm gives up on a task (substrate_handoff logic).

WRITE tools write only inside the swarm state dir: status/, scratch/<correlation_id>/ and events.jsonl. Every free-text
value is redacted (swarm.redact) before it reaches disk or the run log. Time comes from the module-level CLOCK so tests
inject a FakeClock; cross-process timestamps (cancel flag, running_deadline, status age) are wall readings.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, TypeVar, cast

from swarm import runlog
from swarm.clock import SYSTEM_CLOCK, Clock
from swarm.paths import latest_correlation, swarm_dir
from swarm.redact import redact, redact_text
from swarm.tools.support import current_caller, respond

if TYPE_CHECKING:
    from swarm.taskstore import TaskStore

try:  # POSIX advisory locks give the scratchpad cross-process mutual exclusion that dies with the holder
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]  # no fcntl module on non-POSIX; None is the fallback

CLOCK: Clock = SYSTEM_CLOCK  # tests replace this with a FakeClock

TEXT_CLIP = 240  # status step/note and escalation text
LOG_MESSAGE_MAX = 500
LOG_FIELDS_MAX = 20
LOG_FIELD_VALUE_CLIP = 200
SCRATCH_KEY_RE = re.compile(r"[a-z0-9._-]{1,64}")
SCRATCH_VALUE_MAX_BYTES = 8 * 1024
SCRATCH_MAX_KEYS = 64
SCRATCH_LOCK_WAIT_S = 5.0
STATUS_EMIT_INTERVAL_S = 1.0
DEFAULT_CANCEL_GRACE_S = 10.0
STATUS_KINDS = ("progress", "blocked", "done")
LOG_LEVELS = ("debug", "info", "warn", "error")
REASON_CODES = ("blocked", "ambiguous-input", "stale-handover", "budget", "policy", "conflict", "other")
ESCALATION_OPTIONS = ["human review", "cancel", "re-plan"]
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class _Refusal(Exception):
    """A handler outcome that is not an error of the tool: the result dict is returned as is."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result.get("message") or result.get("reason") or result.get("state"))
        self.result = result


def _invalid(field: str, message: str, **extra: Any) -> _Refusal:
    return _Refusal({"state": "INVALID_INPUT", "field": field, "message": message, **extra})


def _guarded(payload: dict[str, Any], fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """`respond` plus the refusal convention: `fn()` may raise `_Refusal` to return a non-SUCCESS result."""

    def work() -> dict[str, Any]:
        try:
            return fn()
        except _Refusal as refusal:
            return refusal.result

    # respond() in swarm/tools/support.py is untyped, so its result is Any; work() always returns a result dict.
    return cast(dict[str, Any], respond(payload, work))


# ---- shared helpers ----------------------------------------------------------------------------------------------
def _name(payload: dict[str, Any], field: str) -> str:
    """A file-name safe identifier (task or correlation id); never a path."""
    value = payload.get(field)
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value) or set(value) <= {"."}:
        raise _invalid(field, f"{field} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,127}}")
    return value


def _caller() -> str:
    caller = current_caller()
    if not caller:
        raise _Refusal({"state": "UNAUTHORIZED", "caller": "", "target": "shared_runtime", "reason": "the calling agent is unknown"})
    return caller


def _text(value: object, limit: int = TEXT_CLIP) -> str:
    """Redacted then clipped: a secret is replaced on the full text, so clipping never leaves half of one."""
    clean = redact_text(value) if isinstance(value, str) else ""
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _atomic_json(path: Path, doc: dict[str, Any]) -> None:
    """Temp file in the same directory, then os.replace: readers see the old or the new document, never a torn one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    """The parsed object, or None when the file is absent. A file that is not a JSON object is an OSError."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise OSError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise OSError(f"{path.name} is not a JSON object")
    return doc


def _load_task(task_id: str) -> dict[str, Any] | None:
    """The Task Store row, read-only. No store file or no such task is None; the store is never created here."""
    db = swarm_dir() / "tasks.db"
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise OSError(f"task store unavailable: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise OSError(f"task store unavailable: {exc}") from exc
    finally:
        conn.close()
    if row is None:
        return None
    task = dict(row)
    task["depends_on"] = _json_value(task.get("depends_on"), [])
    notes: object = _json_value(task.get("notes"), {})
    task["notes_json"] = notes if isinstance(notes, dict) else {}
    return task


_T = TypeVar("_T")


def _json_value(text: str | None, default: _T) -> _T:
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _require_task(task_id: str) -> dict[str, Any]:
    task = _load_task(task_id)
    if task is None:
        raise _invalid("task_id", f"unknown task {task_id}")
    return task


def _assigned_agent(task: dict[str, Any]) -> str | None:
    return task.get("agent_id") or task.get("assigned_to") or None


def _require_assigned(task: dict[str, Any], caller: str) -> None:
    assigned = _assigned_agent(task)
    if assigned != caller:
        raise _Refusal({"state": "UNAUTHORIZED", "caller": caller, "target": task["task_id"],
                        "reason": f"caller is not the agent assigned to the task (assigned: {assigned or 'none'})"})


def _run_id(task: dict[str, Any]) -> str:
    return f"{task['task_id']}.a{max(int(task.get('attempt') or 0), 1)}"


# ---- status ------------------------------------------------------------------------------------------------------
def _status_path(task_id: str) -> Path:
    return swarm_dir() / "status" / f"{task_id}.json"


def _carried_calls(run_id: str) -> int:
    """tool_calls the live snapshot already shows for this run, so a status report does not zero the counter."""
    from swarm.telemetry import read_live

    for row in (read_live() or {}).get("runs") or []:
        if isinstance(row, dict) and row.get("run_id") == run_id:
            calls = row.get("tool_calls")
            return calls if isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0 else 0
    return 0


def _emit_activity(task: dict[str, Any], caller: str, text: str) -> None:
    from swarm.telemetry import Telemetry, refresh_live

    run_id = _run_id(task)
    calls = _carried_calls(run_id)
    telemetry = Telemetry(correlation_id=task["correlation_id"], clock=CLOCK, tee=False, live=False)
    telemetry.emit("swarm.run.activity", {"run_id": run_id, "agent": caller, "tool": "shared_status_report", "text": text,
                                          "tool_calls": calls, "turns": 0, "tokens": 0},
                   task_id=task["task_id"], local_only=True)
    # Fail-open. With no scheduler this derives live.json from the event above. During `swarm_run` the refresh refuses to
    # replace the scheduler's fresh file; that producer reads the status file written by `status_report` itself
    # (`Telemetry._fold_external`), so the report still reaches the bar.
    refresh_live(clock=CLOCK)


def status_report(payload: dict[str, Any]) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        task_id = _name(payload, "task_id")
        kind = payload.get("kind")
        if kind not in STATUS_KINDS:
            raise _invalid("kind", f"kind must be one of {', '.join(STATUS_KINDS)}")
        step = payload.get("step")
        if not isinstance(step, str) or not step.strip():
            raise _invalid("step", "step must be a non-empty string")
        percent = payload.get("percent")
        if percent is not None and (isinstance(percent, bool) or not isinstance(percent, (int, float))
                                    or not math.isfinite(percent) or not 0 <= percent <= 100):
            raise _invalid("percent", "percent must be a number from 0 to 100")
        note = payload.get("note")
        if note is not None and not isinstance(note, str):
            raise _invalid("note", "note must be a string")
        caller = _caller()
        task = _require_task(task_id)
        _require_assigned(task, caller)

        record = {"kind": kind, "step": _text(step), "percent": percent, "note": _text(note) if note else None}
        path = _status_path(task_id)
        previous = _read_json(path)
        now = CLOCK.wall()
        base = {"state": "SUCCESS", "task_id": task_id, "kind": kind, "step": record["step"]}
        if previous is not None and all(previous.get(key) == value for key, value in record.items()):
            return {**base, "recorded": False, "emitted": False, "seq": int(previous.get("seq") or 1)}
        seq = int(previous.get("seq") or 0) + 1 if previous else 1
        _atomic_json(path, {"task_id": task_id, "correlation_id": task["correlation_id"], "agent": caller,
                            **record, "updated_wall": now, "seq": seq})
        since = now - float(previous["updated_wall"]) if previous and isinstance(previous.get("updated_wall"), (int, float)) else None
        # a burst of progress reports is one event per second; a state change (blocked/done) is always worth an event
        throttled = (since is not None and 0 <= since < STATUS_EMIT_INTERVAL_S
                     and previous is not None and previous.get("kind") == kind and kind == "progress")
        if not throttled:
            from swarm.telemetry import status_label

            label = status_label(kind, record["step"], percent)
            _emit_activity(task, caller, _text(label))
        return {**base, "recorded": True, "emitted": not throttled, "seq": seq}

    return _guarded(payload, run)


# ---- scratchpad --------------------------------------------------------------------------------------------------
def _scratch_dir(correlation_id: str) -> Path:
    return swarm_dir() / "scratch" / correlation_id


@contextlib.contextmanager
def _scratch_lock(directory: Path) -> Iterator[None]:
    """Exclusive per-correlation lock shared by threads and processes (flock on separate open files excludes both)."""
    if fcntl is None:  # pragma: no cover - non-POSIX
        raise OSError("scratchpad locking needs POSIX flock")
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + SCRATCH_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("scratchpad is busy: lock not acquired in time") from None
                time.sleep(0.005)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _scratch_key(payload: dict[str, Any], *, required: bool) -> str | None:
    key = payload.get("key")
    if key is None and not required:
        return None
    if not isinstance(key, str) or not SCRATCH_KEY_RE.fullmatch(key):
        raise _invalid("key", "key must match [a-z0-9._-]{1,64}")
    return key


def _meta(entry: dict[str, Any]) -> dict[str, Any]:
    return {name: entry.get(name) for name in ("key", "owner", "shared", "version", "bytes", "updated", "updated_by")}


def _entry_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.json") if p.is_file()) if directory.is_dir() else []


def scratchpad_put(payload: dict[str, Any]) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        correlation_id = _name(payload, "correlation_id")
        key = _scratch_key(payload, required=True)
        value = payload.get("value")
        if not isinstance(value, dict):
            raise _invalid("value", "value must be a JSON object")
        shared = payload.get("shared")
        if shared is not None and not isinstance(shared, bool):
            raise _invalid("shared", "shared must be true or false")
        if_version = payload.get("if_version")
        if if_version is not None and (isinstance(if_version, bool) or not isinstance(if_version, int) or if_version < 0):
            raise _invalid("if_version", "if_version must be an integer >= 0 (0 means the key must not exist yet)")
        caller = _caller()
        try:
            raw_bytes = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise _invalid("value", f"value is not JSON serializable: {exc}") from exc
        if raw_bytes > SCRATCH_VALUE_MAX_BYTES:
            raise _invalid("value", f"value is {raw_bytes} bytes; the limit is {SCRATCH_VALUE_MAX_BYTES}")
        stored = redact(value)
        size = len(json.dumps(stored, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > SCRATCH_VALUE_MAX_BYTES:
            raise _invalid("value", f"redacted value is {size} bytes; the limit is {SCRATCH_VALUE_MAX_BYTES}")

        directory = _scratch_dir(correlation_id)
        path = directory / f"{key}.json"
        with _scratch_lock(directory):
            existing = _read_json(path)
            if existing is not None:
                owner = existing.get("owner")
                if owner != caller and existing.get("shared") is not True:
                    raise _Refusal({"state": "UNAUTHORIZED", "caller": caller, "target": key,
                                    "reason": f"key is owned by {owner}; only the owner may overwrite it unless it was created shared"})
            current = int(existing.get("version") or 0) if existing else 0
            if if_version is not None and if_version != current:
                raise _invalid("if_version", f"version conflict: the key is at version {current}", current_version=current)
            if existing is None and len(_entry_files(directory)) >= SCRATCH_MAX_KEYS:
                raise _invalid("key", f"correlation {correlation_id} already holds {SCRATCH_MAX_KEYS} keys")
            owner = existing["owner"] if existing else caller
            made_shared = bool(existing.get("shared")) if existing else bool(shared)
            if existing and owner == caller and shared is not None:
                made_shared = shared  # only the owner can change whether others may overwrite the key
            entry = {"key": key, "value": stored, "owner": owner, "shared": made_shared, "version": current + 1,
                     "bytes": size, "updated": CLOCK.wall(), "updated_by": caller}
            _atomic_json(path, entry)
        return {"state": "SUCCESS", "correlation_id": correlation_id, **_meta(entry)}

    return _guarded(payload, run)


def scratchpad_get(payload: dict[str, Any]) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        correlation_id = _name(payload, "correlation_id")
        key = _scratch_key(payload, required=False)
        directory = _scratch_dir(correlation_id)
        if key is not None:
            entry = _read_json(directory / f"{key}.json")
            if entry is None:
                return {"state": "SUCCESS", "correlation_id": correlation_id, "found": False}
            return {"state": "SUCCESS", "correlation_id": correlation_id, "found": True, "key": key,
                    "value": entry.get("value"), "meta": _meta(entry)}
        keys = []
        for path in _entry_files(directory):
            with contextlib.suppress(OSError):  # a file replaced or removed under us is simply not listed
                entry = _read_json(path)
                if entry is not None:
                    keys.append(_meta(entry))
        return {"state": "SUCCESS", "correlation_id": correlation_id, "found": bool(keys), "keys": keys}

    return _guarded(payload, run)


# ---- escalation --------------------------------------------------------------------------------------------------
class _StoreView:
    """The one method `substrate_handoff.escalation_owner` calls on a store, answered from the read-only loader."""

    def get(self, task_id: str) -> dict[str, Any]:
        from swarm.errors import ErrorCode, SwarmError

        task = _load_task(task_id)
        if task is None:
            raise SwarmError(ErrorCode.E_INPUT, f"unknown task {task_id}")
        return task


def _owner_of(task: dict[str, Any]) -> str:
    from swarm import substrate_handoff

    # escalation_owner() only calls store.get(), which _StoreView provides; its annotation names TaskStore.
    return substrate_handoff.escalation_owner(cast("TaskStore", _StoreView()), task)


def escalate(payload: dict[str, Any]) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        task_id = _name(payload, "task_id")
        reason_code = payload.get("reason_code")
        if reason_code not in REASON_CODES:
            raise _invalid("reason_code", f"reason_code must be one of {', '.join(REASON_CODES)}")
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise _invalid("summary", "summary must be a non-empty string")
        needs = payload.get("needs", [])
        if not isinstance(needs, list) or not all(isinstance(item, str) and item.strip() for item in needs) or len(needs) > 20:
            raise _invalid("needs", "needs must be a list of at most 20 non-empty strings")
        caller = _caller()
        task = _require_task(task_id)
        _require_assigned(task, caller)

        summary_text = _text(summary, 500)
        need_texts = [_text(item) for item in needs]
        owner = _owner_of(task)
        record = runlog.emit("escalation.request",
                             {"task_id": task_id, "reason_code": reason_code, "evidence": [summary_text, *need_texts],
                              "options": list(ESCALATION_OPTIONS), "summary": summary_text, "needs": need_texts,
                              "from_agent": caller, "owner": owner},
                             source="shared_escalate", correlation_id=task["correlation_id"], task_id=task_id)
        wanted = f"{reason_code}: {summary_text}" + (f" | needs: {'; '.join(need_texts)}" if need_texts else "")
        patch = {"task_id": task_id, "state": "BLOCKED", "needs": wanted[:500],
                 "summary_md": f"Escalated to {owner} ({reason_code}): {summary_text}"}
        return {"state": "SUCCESS", "task_id": task_id, "escalation_id": record["msg_id"], "owner": owner,
                "reason_code": reason_code, "result_patch": patch}

    return _guarded(payload, run)


def escalation_owner(payload: dict[str, Any]) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        task_id = _name(payload, "task_id")
        owner = _owner_of(_require_task(task_id))
        return {"state": "SUCCESS", "task_id": task_id, "owner": owner,
                "basis": "maintenance work order upstream" if owner != "A01" else "default: orchestrator"}

    return _guarded(payload, run)


# ---- cancellation ------------------------------------------------------------------------------------------------
def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _grace_default() -> float:
    try:
        value = float(os.environ["SWARM_CANCEL_GRACE_S"])
    except (KeyError, ValueError):
        return DEFAULT_CANCEL_GRACE_S
    return value if math.isfinite(value) and value >= 0 else DEFAULT_CANCEL_GRACE_S


def cancel_flag_path(sdir: str | Path, correlation_id: str) -> Path:
    """`<swarm_dir>/cancel/<correlation_id>` with every character outside `[A-Za-z0-9._-]` replaced by `_`.

    The one path rule for the flag: the runner (`scripts/swarm_run.py`) writes it and `shared_cancel_check` reads it
    through this function, so a correlation id with unusual characters cannot make the two sides disagree. A name that
    is empty or only dots would address a directory, so it is spelled with underscores."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", correlation_id)
    return Path(sdir) / "cancel" / ((name.replace(".", "_") or "_") if name.strip(".") == "" else name)


def _cancel_flag(correlation_id: str) -> tuple[float, str, float] | None:
    """(requested_wall, reason, grace_s) from the runner's flag file, or None when no cancel was requested.

    The file's existence is the request. A body that is missing, empty or not the documented JSON still counts: the
    request time falls back to the file's mtime and the grace to SWARM_CANCEL_GRACE_S."""
    path = cancel_flag_path(swarm_dir(), correlation_id)
    try:
        with open(path, "rb") as fh:
            body = fh.read(64 * 1024)
        modified = os.stat(path).st_mtime
    except FileNotFoundError:
        return None
    try:
        doc = json.loads(body.decode("utf-8"))
    except ValueError:
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    requested = _number(doc.get("requested_wall"))
    grace = _number(doc.get("grace_s"))
    reason = doc.get("reason")
    return (requested if requested is not None else modified,
            _text(reason) if isinstance(reason, str) else "",
            grace if grace is not None and grace >= 0 else _grace_default())


def cancel_check(payload: dict[str, Any]) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        task_id = _name(payload, "task_id")
        task = _require_task(task_id)
        now = CLOCK.wall()
        flag = _cancel_flag(task["correlation_id"])
        out = {"state": "SUCCESS", "task_id": task_id, "cancel_requested": flag is not None, "reason": "",
               "grace_remaining_s": 0.0}
        if flag is not None:
            requested, reason, grace = flag
            elapsed = max(0.0, now - requested)  # a wall clock that stepped back must not stretch the grace window
            out["reason"] = reason
            out["grace_remaining_s"] = round(max(0.0, grace - elapsed), 3)
        deadline = _number(task["notes_json"].get("running_deadline"))
        out["has_deadline"] = deadline is not None
        out["deadline_remaining_s"] = round(max(0.0, deadline - now), 3) if deadline is not None else 0.0
        out["deadline_exceeded"] = deadline is not None and now >= deadline
        return out

    return _guarded(payload, run)


# ---- log ---------------------------------------------------------------------------------------------------------
def log_emit(payload: dict[str, Any]) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        level = payload.get("level")
        if level not in LOG_LEVELS:
            raise _invalid("level", f"level must be one of {', '.join(LOG_LEVELS)}")
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > LOG_MESSAGE_MAX:
            raise _invalid("message", f"message must be a non-empty string of at most {LOG_MESSAGE_MAX} characters")
        fields = payload.get("fields", {})
        if not isinstance(fields, dict) or len(fields) > LOG_FIELDS_MAX:
            raise _invalid("fields", f"fields must be an object of at most {LOG_FIELDS_MAX} scalar entries")
        for name, value in fields.items():
            if len(name) > 64 or not (value is None or isinstance(value, (str, int, float, bool))):
                raise _invalid("fields", f"field {name[:64]!r} must have a scalar value and a name of at most 64 characters")
        task_id = payload.get("task_id")
        if task_id is not None:
            task_id = _name(payload, "task_id")
        caller = _caller()
        correlation_id = payload.get("correlation_id")
        if correlation_id is not None:
            correlation_id = _name(payload, "correlation_id")
        elif task_id is not None:
            task = _load_task(task_id)
            correlation_id = task["correlation_id"] if task else None
        if correlation_id is None:
            correlation_id = latest_correlation()
        body = {"level": level, "message": _text(message, LOG_MESSAGE_MAX), "agent": caller,
                "fields": {name: (_text(v, LOG_FIELD_VALUE_CLIP) if isinstance(v, str) else v)
                           for name, v in cast(dict[str, Any], redact(fields)).items()}}  # redact() copies a dict; its annotation is object
        record = runlog.emit("agent.log", body, source=f"agent.{caller}", correlation_id=correlation_id, task_id=task_id,
                             tee=False)
        return {"state": "SUCCESS", "msg_id": record["msg_id"], "level": level, "correlation_id": correlation_id or "",
                "task_id": task_id or ""}

    return _guarded(payload, run)


# ---- routing -----------------------------------------------------------------------------------------------------
def _route_reason(table: dict[str, dict[str, Any]], sender: str, recipient: str, message_type: str) -> str:
    if sender not in table:
        return f"unknown sender {sender}"
    if recipient not in table:
        return f"unknown recipient {recipient}"
    if sender == recipient:
        return "an agent cannot message itself"
    if message_type not in table[sender]["produces"]:
        return f"{sender} does not produce {message_type}"
    if message_type not in table[recipient]["accepts"]:
        return f"{recipient} does not accept {message_type}"
    return ""


def route_check(payload: dict[str, Any]) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        values = {}
        for field in ("sender", "recipient", "message_type"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise _invalid(field, f"{field} must be a non-empty string")
            values[field] = value
        from swarm import acp

        data = acp.load_agents()
        allowed = acp.message_allowed(data, values["sender"], values["recipient"], values["message_type"])
        reason = "" if allowed else _route_reason(acp.routing_table(data), **values)
        return {"state": "SUCCESS", "allowed": allowed, "reason": reason, **values}

    return _guarded(payload, run)
