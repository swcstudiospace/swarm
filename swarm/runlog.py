"""Append-only JSONL run log (the logger unit) — local stand-in for NATS/OTel.

Emits structured logs at every phase of init, lifecycle, run loop (via log_phase) +
end-of-run summary reports (via log_summary).

Every agent script + swarm_run + hooks + subagent_lifecycle use it for causal audit by correlation_id.
Logging paths spec lives in swarm.paths.logging_paths().
"""
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .paths import append_text, swarm_dir
from .redact import redact


def _log_file(root: str | Path | None, *, create: bool) -> Path:
    return swarm_dir(root, create=create) / "events.jsonl"


def redact_leases(value: object) -> object:
    """A copy of `value` without substrate lease ids. A lease id is the holder's fencing token (LEASE-02): the Task Store
    mirrors it in notes.lease for A01's own release, and no run-log record, nor its tee, carries it. A task row carries it
    twice, in `notes_json` and in the raw `notes` JSON text."""
    if isinstance(value, dict):
        return {k: redact_leases(v) for k, v in value.items() if k != "lease_id"}
    if isinstance(value, (list, tuple)):
        return [redact_leases(v) for v in value]
    if isinstance(value, str) and '"lease_id"' in value:
        try:
            return json.dumps(redact_leases(json.loads(value)))
        except ValueError:
            return value
    return value


# Identifier fields are validated identifiers, not free text (the top-level `task_id` / `correlation_id` of a record are not redacted
# either), and readers join on them (`telemetry.derive_snapshot` keys activity, handover and timeout state by `run_id`): a token-shaped
# task id such as `sk-onboarding-flow-v2` must survive redaction instead of becoming `[REDACTED].a1`. Only free text is redacted.
_ID_KEYS = ("run_id", "handover_id", "task_id", "parent_id", "correlation_id")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


# swarm.redact.redact is untyped (returns Any); the Callable annotation keeps its call site typed.
_redact: Callable[..., object] = redact


def _redact_payload(payload: Mapping[str, object]) -> object:
    plain = redact_leases(payload)
    clean = _redact(plain)
    if isinstance(plain, dict) and isinstance(clean, dict):
        for key in _ID_KEYS:
            value = plain.get(key)
            if isinstance(value, str) and key in clean and ID_RE.fullmatch(value):
                clean[key] = value
    return clean


def emit(event_type: str, payload: Mapping[str, object], *, source: str, correlation_id: str | None = None,
         task_id: str | None = None, root: str | Path | None = None, tee: bool = True) -> dict[str, Any]:
    """Append one record to the run log; `tee=False` (dry runs) keeps it local and never sends it to substrate. Lease ids
    are dropped from the payload (`redact_leases`), then secrets are replaced (`redact.redact`) in everything but the identifier
    fields `_ID_KEYS`; the caller's own payload object is left as it is."""
    log_file = _log_file(root, create=True)
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": event_type,
        "source": source,
        "correlation_id": correlation_id,
        "task_id": task_id,
        "payload": _redact_payload(payload),
        "msg_id": uuid.uuid4().hex,
    }
    append_text(log_file, json.dumps(record, ensure_ascii=False) + "\n")
    if not tee:
        return record
    try:  # substrate tee (ADR 0001 S1): after the local write, fail-open, a no-op without SUBSTRATE_URL
        from .substrate_tee import tee as tee_record
        tee_record(record, root)
    except Exception:  # noqa: BLE001 - the local log is the source of truth; the tee never disturbs it
        pass
    return record


def read_events(*, correlation_id: str | None = None, task_id: str | None = None,
                event_type: str | None = None, root: str | Path | None = None) -> list[dict[str, Any]]:
    log_file = _log_file(root, create=False)
    if not log_file.exists():
        return []
    out: list[dict[str, Any]] = []
    skipped = 0
    for line in log_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:  # a torn trailing line from a crash mid-append must not fail every reader (telemetry skips it too)
            skipped += 1
            continue
        if correlation_id and rec.get("correlation_id") != correlation_id:
            continue
        if task_id and rec.get("task_id") != task_id:
            continue
        if event_type and rec.get("type") != event_type:
            continue
        out.append(rec)
    if skipped:
        logging.getLogger(__name__).warning("skipped %d undecodable line(s) in %s", skipped, log_file)
    return out


def log_phase(phase: str, payload: Mapping[str, object], *, source: str = "swarm", correlation_id: str | None = None,
              task_id: str | None = None, root: str | Path | None = None, tee: bool = True) -> dict[str, Any]:
    """Logger unit: emit structured log at a run phase (e.g. 'init', 'reconcile', 'dispatch', 'execute', 'summary').
    Every phase of init/lifecycle/run gets a typed event in events.jsonl.
    """
    p = {"phase": phase, **(payload or {})}
    return emit(f"phase.{phase}", p, source=source, correlation_id=correlation_id, task_id=task_id, root=root, tee=tee)


def log_summary(report: Mapping[str, object], *, source: str = "swarm_run", correlation_id: str | None = None,
                root: str | Path | None = None, tee: bool = True) -> dict[str, Any]:
    """Logger unit: emit end-of-run summary report (counts, complete, escalated, timings, log excerpts).
    Called at lifecycle end.
    """
    return emit("run.summary", report, source=source, correlation_id=correlation_id, root=root, tee=tee)
