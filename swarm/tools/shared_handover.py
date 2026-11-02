"""Shared handover tools: write and read the versioned handover document, and the checks a receiver runs on it.

Every handler is `fn(payload) -> dict` wrapped by `support.respond`. READ tools read only the Task Store (read-only
connection) and the handover directory; WRITE tools (`handover_write`, `handover_receipt`) write only under
`swarm_dir()`. Agent identity is the agent id (`A05`): `handover_write` refuses a `from_agent` that is not the
authenticated caller. A substrate lease id never leaves this module: lease output is whitelisted and every string is
scrubbed of the task's lease ids.

Tools: handover_write, handover_read, handover_validate, handover_bundle, handover_verify, handover_receipt,
artifact_digest, artifact_check, dod_check, open_findings, attempt_history, lease_state, open_questions.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

from swarm import handover as ho
from swarm.clock import SYSTEM_CLOCK, Clock
from swarm.paths import latest_correlation, swarm_dir
from swarm.redact import redact_text
from swarm.runlog import redact_leases
from swarm.taskstore import MAX_REWORK_LOOPS, SATISFIED_STATES
from swarm.tools.support import current_caller, respond, target_root

BLOCKING = ("major", "critical", "blocker")
_SEVERITY_ORDER = {"blocker": 0, "critical": 1, "major": 2, "minor": 3, "info": 4}
MAX_FILE_DIGEST_BYTES = 256 << 20
MAX_OUTPUTS = 100
MAX_CRITERIA = 100
MAX_FINDINGS = 100
MAX_TRANSITIONS = 50
MAX_QUESTIONS = 50
LEASE_FIELDS = ("state", "agent", "surface", "graph_id", "ttl_s", "reason", "action", "holder", "at")
_FAILED_TO = frozenset({"FAILED", "ESCALATED", "CHANGES_REQUESTED"})
Report = dict[str, Any]  # a handover tool result: JSON-shaped, its values are validated where they are read


# ---- shared helpers --------------------------------------------------------------------------------------------
def _invalid(field: str, message: str, problems: list[str] | None = None) -> Report:
    out: Report = {"state": "INVALID_INPUT", "field": field, "message": message[:1000]}
    if problems:
        out["problems"] = [p[:400] for p in problems[:20]]
    return out


def _guarded(work: Callable[[], Report]) -> Report:
    """Run `work`; a refused handover or id becomes INVALID_INPUT naming the field, anything else propagates."""
    try:
        return work()
    except ho.HandoverInvalid as exc:
        return _invalid(exc.field, "; ".join(exc.problems), exc.problems)


def _reply(payload: Report, work: Callable[[], Report]) -> Report:
    """`support.respond` is annotated to return Any; this keeps every handler's return type explicit."""
    result: Report = respond(payload, work)
    return result


def _task_id(payload: Report) -> str:
    value = payload["task_id"]
    if not isinstance(value, str) or not ho._ID_RE.match(value):
        raise ho.HandoverInvalid([f"/task_id: {value!r} is not a valid task id (letters, digits, '.', '_', '-')"])
    return value


def _clip(value: object, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    text = redact_text(text.strip())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _lease_ids(*rows: Report | None) -> set[str]:
    """Lease ids mirrored in the given task rows: the values that must never appear in an output."""
    found: set[str] = set()
    for row in rows:
        lease = ((row or {}).get("notes_json") or {}).get("lease")
        if isinstance(lease, dict) and isinstance(lease.get("lease_id"), str) and lease["lease_id"]:
            found.add(lease["lease_id"])
    return found


def _scrub(value: Report, secrets: set[str]) -> Report:
    """A copy of `value` without lease id keys and with every given lease id replaced in strings."""
    scrubbed = redact_leases(value)
    # cast: redact_leases and _replace are annotated object, and both keep the dict shape of `value`
    return cast(Report, _replace(scrubbed, secrets) if secrets else scrubbed)


def _replace(value: object, secrets: set[str]) -> object:
    if isinstance(value, dict):
        return {k: _replace(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
    return value


def _agent_of_row(row: Report | None) -> str:
    agent = ho.agent_id_of((row or {}).get("agent_id") or (row or {}).get("assigned_to"))
    return agent or ""


def _acceptance(row: Report | None) -> list[str]:
    out = []
    for item in (row or {}).get("acceptance") or []:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("criterion") or item.get("description") or json.dumps(item, sort_keys=True))
        else:
            text = str(item)
        if text.strip():
            out.append(_clip(text))
    return out


def _questions(needs: object) -> list[str]:
    """Plain question lines from a `needs` value (string, list or object such as `clarification_request`'s)."""
    if needs is None:
        return []
    if isinstance(needs, str):
        return [needs.strip()] if needs.strip() else []
    if isinstance(needs, list):
        return [q for item in needs for q in _questions(item)]
    if isinstance(needs, dict):
        out: list[str] = []
        lead = next((needs[k] for k in ("question", "reason", "summary") if isinstance(needs.get(k), str) and needs[k].strip()), None)
        if lead:
            out.append(lead.strip())
        for key in ("questions", "needs", "missing"):
            for item in _questions(needs.get(key)):
                out.append(f"missing: {item}" if key == "missing" else item)
        return out or [json.dumps(needs, sort_keys=True)]
    return [str(needs)]


def _upstream_questions(row: Report) -> list[str]:
    result = (row.get("notes_json") or {}).get("result")
    return [_clip(q) for q in _questions(result.get("needs") if isinstance(result, dict) else None)]


def _unresolved(row: Report) -> bool:
    """A needs entry is still open while its task has not moved past the point of asking."""
    return row.get("state") not in SATISFIED_STATES and row.get("state") != "CANCELLED"


def _lease_view(row: Report | None, clock: Clock) -> Report:
    lease = ((row or {}).get("notes_json") or {}).get("lease")
    if not isinstance(lease, dict) or not lease:
        return {"has_lease": False, "state": "none", "agent": "", "surface": "", "ttl_s": 0, "age_s": 0.0, "expired": False,
                "reason": "", "action": "", "holder": "", "graph_id": ""}
    at = lease.get("at") if isinstance(lease.get("at"), (int, float)) else None
    ttl = lease.get("ttl_s") if isinstance(lease.get("ttl_s"), (int, float)) and not isinstance(lease.get("ttl_s"), bool) else 0
    age = max(0.0, clock.wall() - at) if at is not None else 0.0
    view = {k: lease[k] for k in LEASE_FIELDS if k in lease and k not in ("at", "ttl_s") and isinstance(lease[k], (str, int, float))}
    view = {k: _clip(str(v), 200) for k, v in view.items()}
    return {"has_lease": True, "state": view.get("state", ""), "agent": view.get("agent", ""),
            "surface": view.get("surface", ""), "ttl_s": ttl, "age_s": round(age, 3),
            "expired": bool(ttl and at is not None and age > ttl and view.get("state") == "held"),
            "reason": view.get("reason", ""), "action": view.get("action", ""), "holder": view.get("holder", ""),
            "graph_id": view.get("graph_id", "")}


def _history_view(store: ho.StoreReader, task_id: str, row: Report, limit: int = MAX_TRANSITIONS) -> Report:
    transitions = store.transitions(task_id)
    trimmed = [{"from": t.get("from_state") or "", "to": t.get("to_state") or "", "actor": _clip(t.get("actor") or "", 80),
                "reason": _clip(t.get("reason") or "", 200), "ts": float(t.get("ts") or 0.0)} for t in transitions[-limit:]]
    last_error = ""
    for t in reversed(transitions):
        if t.get("to_state") in _FAILED_TO and (t.get("reason") or "").strip():
            last_error = _clip(f"{t['to_state']}: {t['reason']}", 300)
            break
    result = (row.get("notes_json") or {}).get("result")
    if not last_error and isinstance(result, dict) and isinstance(result.get("error"), dict):
        err = result["error"]
        last_error = _clip(f"{err.get('code', 'error')}: {err.get('message', '')}", 300)
    attempt = int(row.get("attempt") or 0)
    return {"state": row.get("state") or "", "attempt": attempt, "max_attempts": int(row.get("max_attempts") or 0),
            "rework_loops": int(row.get("rework_loops") or 0), "max_rework_loops": MAX_REWORK_LOOPS,
            "retries_left": max(0, int(row.get("max_attempts") or 0) - attempt),
            "transition_count": len(transitions), "transitions": trimmed, "last_error": last_error}


def _task_view(row: Report) -> Report:
    return {"task_id": row["task_id"], "correlation_id": row.get("correlation_id") or "", "title": _clip(row.get("title") or "", 200),
            "state": row.get("state") or "", "agent": _agent_of_row(row), "attempt": int(row.get("attempt") or 0),
            "risk_class": row.get("risk_class") or "", "depends_on": [d for d in row.get("depends_on") or [] if isinstance(d, str)]}


# ---- handover_write --------------------------------------------------------------------------------------------
def _correlation(payload: Report, task_id: str) -> str:
    row = ho.read_task(task_id)
    given: str | None = payload.get("correlation_id")
    if given is not None:
        if row and row.get("correlation_id") and row["correlation_id"] != given:
            raise ho.HandoverInvalid([f"/correlation_id: task {task_id} belongs to plan {row['correlation_id']!r}, not {given!r}"])
        return given
    if row and row.get("correlation_id"):
        correlation: str = row["correlation_id"]
        return correlation
    latest = latest_correlation()
    if latest:
        return latest
    raise ho.HandoverInvalid([f"/correlation_id: task {task_id} is not in the Task Store and no swarm run is active; pass correlation_id"])


def handover_write(payload: Report) -> Report:
    def work() -> Report:
        caller = current_caller()
        if not caller:
            return _invalid("from_agent", "no authenticated caller: a handover can only be written through the registry")
        sender = payload.get("from_agent", caller)
        if sender != caller:
            return _invalid("from_agent", f"from_agent {sender!r} must be the calling agent id {caller}; identity cannot be set by payload")
        task_id = _task_id(payload)
        doc = {
            "task_id": task_id, "correlation_id": _correlation(payload, task_id), "from_agent": caller,
            "to_agent": payload["to_agent"], "goal": payload["goal"],
            "completed": payload.get("completed", []), "remaining": payload.get("remaining", []),
            "decisions": payload.get("decisions", []), "artifacts": payload.get("artifacts", []),
            "open_questions": payload.get("open_questions", []), "blockers": payload.get("blockers", []),
            "constraints": payload.get("constraints", []),
            "verification": payload.get("verification", {"status": "unverified", "confidence": 0.0, "checks": []}),
        }
        written = ho.write_handover(doc)
        status = ho.handover_status(task_id, correlation_id=doc["correlation_id"], seq=written["seq"])
        return {"state": "SUCCESS", "handover_id": written["handover_id"], "ref": written["ref"], "seq": written["seq"],
                "digest": written["digest"], "bytes": written["bytes"], "summarized": written["summarized"],
                "duplicate": written["duplicate"], "redacted": written["redacted"], "status": status["status"]}

    return _reply(payload, lambda: _guarded(work))


# ---- handover selection ----------------------------------------------------------------------------------------
def _seq(payload: Report) -> int | None:
    """The optional `seq` input: the exact handover of the task to act on (None: the caller's own, else the newest)."""
    value: object = payload.get("seq")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ho.HandoverInvalid([f"/seq: {value!r} is not a handover sequence number (an integer of at least 1)"])
    return value


def _inspect(payload: Report, task_id: str, **kw: Any) -> tuple[Report, Report | None]:
    """`ho.inspect_handover` for the caller with the shared selection order: the explicit `seq`, else the newest handover
    addressed to the caller, else the newest as context (`context_only`). A `seq` the task does not have is `missing`
    (`found` false for a read), exactly like a task without handovers: a reader that scans `seq` upward ends on it. It is
    never answered with a different handover. Only a `seq` that is not a positive integer is INVALID_INPUT."""
    return ho.inspect_handover(task_id, seq=_seq(payload), to_agent=current_caller() or None, **kw)


# ---- handover_read ---------------------------------------------------------------------------------------------
def handover_read(payload: Report) -> Report:
    def work() -> Report:
        task_id = _task_id(payload)
        status, doc = _inspect(payload, task_id, correlation_id=payload.get("correlation_id"))
        row = ho.read_task(task_id)
        out = {"state": "SUCCESS", "found": doc is not None, "status": status["status"], "reasons": status["reasons"],
               "handover": doc or {}}
        if status.get("context_only"):
            out["context_only"] = True
        return _scrub(out, _lease_ids(row))

    return _reply(payload, lambda: _guarded(work))


# ---- handover_validate -----------------------------------------------------------------------------------------
def handover_validate(payload: Report) -> Report:
    def work() -> Report:
        doc = payload["document"]
        if payload.get("draft") is True:
            doc = ho._prepare(doc, SYSTEM_CLOCK)
        problems = ho.validate_handover(doc)
        cap = ho.max_bytes_default()
        sizing = doc
        if payload.get("draft") is True and isinstance(doc.get("task_id"), str):
            # `handover_write` sizes with `<task_id>.<seq>` (a task id of up to 128 characters): the shorter provisional id
            # would declare a document near the cap fine that the write then summarizes
            sizing = {**doc, "handover_id": f"{doc['task_id']}.{ho._PENDING_ID}"}
        size = ho.stored_size(sizing) if not problems else 0
        return {"state": "SUCCESS", "valid": not problems, "errors": [p[:400] for p in problems[:50]],
                "bytes": size, "cap": cap, "would_summarize": size > cap}

    return _reply(payload, lambda: _guarded(work))


# ---- bundle / verify -------------------------------------------------------------------------------------------
def _merge_artifacts(*sources: tuple[str, list[Report]]) -> list[Report]:
    """Distinct (uri, digest) artifacts across the sources. An entry without a digest is dropped when another entry names
    the same uri WITH one (the final result's output next to the handover's artifact is one file, not two)."""
    digested = {str(i.get("uri") or "") for _t, items in sources for i in items if i.get("digest")}
    seen: set[tuple[str, str]] = set()
    merged: list[Report] = []
    for task_id, items in sources:
        for item in items:
            key = (str(item.get("uri") or ""), str(item.get("digest") or ""))
            if key in seen or not key[0] or (not key[1] and key[0] in digested):
                continue
            seen.add(key)
            merged.append({"task_id": task_id, "kind": _clip(item.get("kind") or "artifact", 60), "uri": _clip(key[0], 300),
                           "digest": key[1], "producer": _clip(item.get("producer") or "", 60)})
    return merged


def _verdict_summaries(rows: list[Report], clock: Clock) -> list[Report]:
    from swarm.envelope import verify_envelope  # lazy: only the bundle needs signature checks

    out = []
    for row in rows:
        envelope = _loads_obj(row.get("envelope_json"))
        if not row.get("sig") or not envelope:
            signature = "absent"
        else:
            try:
                signature = "valid" if verify_envelope(envelope) else "unverified"
            except Exception:  # noqa: BLE001 - no key configured here: report it, never fail the bundle
                signature = "unverified"
        raw_findings = row.get("findings")
        findings: list[Any] = raw_findings if isinstance(raw_findings, list) else []
        blocking = sum(1 for f in findings if isinstance(f, dict) and f.get("severity") in BLOCKING)
        expires = row.get("expires_at")
        out.append({"gate": str(row.get("gate") or ""), "verdict": str(row.get("verdict") or ""),
                    "agent": ho.agent_id_of(row.get("agent_id")) or "", "signature": signature,
                    "expired": bool(isinstance(expires, (int, float)) and expires <= clock.wall()),
                    "stale": bool(row.get("stale")), "findings": len(findings), "blocking": blocking})
    return out


def _loads_obj(text: object) -> Report:
    try:
        value = json.loads(text) if isinstance(text, str) and text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _bundle_size(out: Report) -> int:
    return len(ho.canonical(out))


def handover_bundle(payload: Report) -> Report:
    def work() -> Report:
        task_id = _task_id(payload)
        clock = SYSTEM_CLOCK
        status, doc = _inspect(payload, task_id, clock=clock)
        with ho.StoreReader() as store:
            row = store.task(task_id)
            upstream_rows = [r for d in (row or {}).get("depends_on") or [] if isinstance(d, str) and (r := store.task(d))]
            own_artifacts = store.artifacts(task_id) if row else []
            upstream_artifacts = [(r["task_id"], store.artifacts(r["task_id"])) for r in upstream_rows]
            verdicts = _verdict_summaries(store.verdicts(task_id, row), clock) if row else []
            attempts = _history_view(store, task_id, row, limit=10) if row else {}
        questions: list[Report] = []
        for up in upstream_rows:
            questions += [{"task_id": up["task_id"], "origin": "upstream", "question": q} for q in _upstream_questions(up) if _unresolved(up)]
        if doc:
            questions += [{"task_id": task_id, "origin": "handover", "question": _clip(q)} for q in doc["open_questions"]]
        # the task's own outputs as `artifact_check` judges them (newest digest per uri, superseded ones dropped), then the upstream rows
        own_current, _superseded = _current_outputs(doc, own_artifacts)
        sources = [(task_id, [item for item, _inherited in own_current]), *upstream_artifacts]
        artifacts = _merge_artifacts(*sources)
        acceptance = _acceptance(row)
        upstream = []
        for up in upstream_rows:
            result = (up.get("notes_json") or {}).get("result")
            result = result if isinstance(result, dict) else {}
            upstream.append({"task_id": up["task_id"], "state": up.get("state") or "", "agent": _agent_of_row(up),
                             "satisfied": up.get("state") in SATISFIED_STATES, "summary": _clip(result.get("summary_md") or "", 300),
                             "needs": _upstream_questions(up)[:5]})
        out = {
            "state": "SUCCESS", "task_known": row is not None, "task": _task_view(row) if row else {},
            "goal": _clip((doc or {}).get("goal") or (row or {}).get("title") or "", 2000),
            "acceptance": acceptance[:25],
            "handover": {"status": status["status"], "reasons": status["reasons"], "document": doc or {},
                         "handover_id": status.get("handover_id", ""), "digest": status.get("digest", ""),
                         **({"context_only": True} if status.get("context_only") else {})},
            "upstream": upstream[:10], "artifacts": artifacts[:25], "verdicts": verdicts[:8],
            "lease": _lease_view(row, clock), "attempts": attempts, "open_questions": questions[:MAX_QUESTIONS],
            # a list cut here is cut for the receiver too: `truncated` is the one signal that something is missing
            "truncated": (len(acceptance) > 25 or len(upstream) > 10 or len(artifacts) > 25 or len(verdicts) > 8
                          or len(questions) > MAX_QUESTIONS),
            "over_cap": False, "bytes": 0,
        }
        out = _scrub(out, _lease_ids(row, *upstream_rows))
        return _bound_bundle(out, ho.max_bytes_default() * 2)

    return _reply(payload, lambda: _guarded(work))


def _bound_bundle(out: Report, cap: int) -> Report:
    """Shrink the bundle until it fits `cap` and record the exact size.

    Other sections shrink first, the handover document last, and only to the room the other sections leave. This is a
    READ path: it never raises because the reader's `SWARM_HANDOVER_MAX_BYTES` is lower than the writer's (the
    mandatory sections are then returned whole). `handover.digest` and `handover.handover_id` always name the stored
    file, so a receiver can acknowledge it even when `handover.document` was shortened. `over_cap` is true when the
    bundle still exceeds `cap` (mandatory handover sections plus the minimal other sections)."""
    steps = (
        lambda o: [u.update(summary="") for u in o["upstream"]],
        lambda o: o.update(artifacts=o["artifacts"][:10]),
        lambda o: o["attempts"].update(transitions=o["attempts"].get("transitions", [])[-3:]) if o["attempts"] else None,
        lambda o: o.update(open_questions=o["open_questions"][:15]),
        lambda o: o.update(upstream=o["upstream"][:3], verdicts=o["verdicts"][:4]),
    )
    shrunk = False
    for step in steps:
        out["bytes"] = _bundle_size(out)
        if out["bytes"] <= cap:
            break
        step(out)
        shrunk = True
    document = out["handover"]["document"]
    if document and _bundle_size(out) > cap:
        others = _bundle_size(out) - len(ho.canonical(document))
        room = max(0, cap - others - 256)  # 256: digits of `bytes`, flags and the summary marker the shortening adds
        out["handover"]["document"] = ho.summarize_handover(document, room, best_effort=True)
        shrunk = True
    out["truncated"] = out["truncated"] or shrunk
    _settle_bytes(out)
    if out["bytes"] > cap:
        out["over_cap"] = True
        _settle_bytes(out)
    return out


def _settle_bytes(out: Report) -> None:
    """`bytes` is part of the output: settle on its own size."""
    for _ in range(4):
        size = _bundle_size(out)
        if out["bytes"] == size:
            return
        out["bytes"] = size


def handover_verify(payload: Report) -> Report:
    def work() -> Report:
        task_id = _task_id(payload)
        status, doc = _inspect(payload, task_id)
        with ho.StoreReader() as store:
            row = store.task(task_id)
            upstream = [{"task_id": d, "state": (r or {}).get("state") or "unknown", "satisfied": bool(r and r.get("state") in SATISFIED_STATES)}
                        for d in (row or {}).get("depends_on") or [] if isinstance(d, str) for r in [store.task(d)]]
        reasons = list(status["reasons"])
        for up in upstream:
            if not up["satisfied"]:
                reasons.append(f"upstream {up['task_id']} is {up['state']}, not yet reviewed or approved")
        ready = status["status"] == "ok" and not [r for r in reasons if r not in status["reasons"]]
        out = {"state": "SUCCESS", "task_known": row is not None, "status": status["status"], "reasons": reasons,
               "handover_id": status.get("handover_id", ""), "upstream": upstream, "ready": ready,
               "substrate": "not used: Task Store and handover files only"}
        if status.get("context_only"):
            out["context_only"] = True
        return out

    return _reply(payload, lambda: _guarded(work))


# ---- handover_receipt ------------------------------------------------------------------------------------------
def handover_receipt(payload: Report) -> Report:
    def work() -> Report:
        caller = current_caller()
        if not caller:
            return _invalid("task_id", "no authenticated caller: a receipt can only be written through the registry")
        receipt, duplicate = ho.write_receipt(
            _task_id(payload), by=caller, digest=payload["digest"], understood=payload.get("understood", []),
            questions=payload.get("questions", []), accepted=payload.get("accepted", True),
            correlation_id=payload.get("correlation_id"))
        return {"state": "SUCCESS", "receipt": ho.receipt_ref(receipt), "handover_id": receipt["handover_id"],
                "digest": receipt["digest"], "accepted": receipt["accepted"], "duplicate": duplicate,
                "questions": len(receipt["questions"])}

    return _reply(payload, lambda: _guarded(work))


# ---- artifacts -------------------------------------------------------------------------------------------------
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_REFERENCE_KINDS = frozenset({"image", "container", "registry", "package", "url", "link"})
# Kinds whose uri is a file or an image reference (`devops_build_record.py` saves `--image` under `build.artifact`; A11
# lists the attestation under `provenance.attestation`). Only an image-shaped uri is a reference for these.
_FILE_OR_IMAGE_KINDS = frozenset({"build.artifact", "provenance.attestation"})
_IMAGE_RE = re.compile(
    r"(?:localhost|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z][A-Za-z0-9-]*|[A-Za-z0-9.-]+:\d+)"  # host: dotted (letter TLD), localhost or host:port
    r"/[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?(?:@sha(?:256|512):[0-9A-Fa-f]+)?\Z")


def _is_reference(text: str, kind: str, root: Path) -> bool:
    """A relative `text` that names an image or package reference, not a file. Declared forms count: a digest pin
    (`app@sha256:...`, `@sha512:`), an output `kind` of image, container, registry, package, url or link, or, for the
    kinds `build.artifact` and `provenance.attestation` ONLY, an image-shaped uri (`host[:port]/path[:tag][@sha256:...]`
    whose host has a dot with a letter TLD or a port, or is `localhost`: `ghcr.io/org/app:1.2`). Everything else is a
    local path, however dotted or colon-bearing (`v1.2/app.js`, `ghcr.io/x/y.md` of kind code), so an absent file is
    `missing`, never `unchecked`. A workspace file of that name always wins; `./name` and absolute paths are files."""
    if text.startswith(("/", "./", "../")):
        return False
    kind = kind.strip().lower()
    if "@sha256:" not in text and "@sha512:" not in text and kind not in _REFERENCE_KINDS \
            and not (kind in _FILE_OR_IMAGE_KINDS and _IMAGE_RE.match(text)):
        return False
    try:
        return not (root / text).exists()
    except OSError:
        return True


def _local_path(uri: str, kind: str = "") -> tuple[Path | None, str]:
    """The workspace file a uri names, else (None, why it is not checked). Only the workspace and the swarm dir are read.
    `kind` is the output's declared kind: it marks a scheme-less uri as a registry or package reference."""
    text = uri.strip()
    root = target_root()
    if text.startswith("file://"):
        parsed = urlparse(text)
        if parsed.netloc not in ("", "localhost"):
            return None, "file uri on a remote host"
        text = unquote(parsed.path)
    elif _SCHEME_RE.match(text):
        return None, f"not a local file ({text.split(':', 1)[0]})"
    elif _is_reference(text, kind, root):
        return None, "not a local file (registry or package reference)"
    path = Path(text)
    resolved = (path if path.is_absolute() else root / path).resolve()
    for base in (root.resolve(), swarm_dir().resolve()):
        if resolved == base or base in resolved.parents:
            return resolved, ""
    return None, "outside the workspace and the swarm directory"


def _file_digest(path: Path) -> tuple[str, int]:
    size = path.stat().st_size
    if size > MAX_FILE_DIGEST_BYTES:
        raise ho.HandoverInvalid([f"/path: {path.name} is {size} bytes, over the {MAX_FILE_DIGEST_BYTES} byte digest limit"])
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest(), size


def artifact_digest(payload: Report) -> Report:
    def work() -> Report:
        raw = payload["path"]
        if not isinstance(raw, str) or not raw.strip():
            return _invalid("path", "path must be a non-empty string")
        path, why = _local_path(raw)
        if path is None:
            return _invalid("path", f"{raw!r} cannot be digested: {why}")
        if not path.is_file():
            return _invalid("path", f"{raw!r} is not a file in the workspace")
        digest, size = _file_digest(path)
        root = target_root().resolve()
        shown = path.relative_to(root).as_posix() if root in path.parents else path.name
        return {"state": "SUCCESS", "digest": digest, "bytes": size, "path": shown}

    return _reply(payload, lambda: _guarded(work))


def _check_output(item: Report, expected: str, inherited: Report | None = None) -> Report:
    uri = item.get("uri") if isinstance(item.get("uri"), str) else ""
    declared = item.get("digest") if isinstance(item.get("digest"), str) else ""
    producer = item.get("producer") if isinstance(item.get("producer"), str) else ""
    out: Report = {"kind": _clip(item.get("kind") or "", 60), "uri": _clip(uri, 300), "status": "unchecked", "exists": "unchecked",
           "digest_match": "unchecked", "producer_ok": "unchecked", "actual_digest": "", "reason": ""}
    if inherited:
        out["inherited"] = inherited
    if expected:
        out["producer_ok"] = "yes" if producer == expected else "no"
    path, why = _local_path(uri, item["kind"] if isinstance(item.get("kind"), str) else "") if uri else (None, "no uri")
    unreadable = ""
    if path is not None:
        out["exists"] = "yes" if path.is_file() else "no"
        if out["exists"] == "yes":
            try:
                actual, _size = _file_digest(path)
            except ho.HandoverInvalid as exc:
                out["reason"] = exc.problems[0][:200]
            except FileNotFoundError:
                out["exists"] = "no"  # removed between the check and the read
            except OSError as exc:  # one locked or unreadable file never costs the results of the other outputs
                unreadable = f"unreadable: {exc.strerror or type(exc).__name__}"[:200]
            else:
                out["actual_digest"] = actual
                out["digest_match"] = "unchecked" if not declared else ("yes" if declared == actual else "no")
    else:
        out["reason"] = why
    if unreadable:
        out["status"], out["reason"] = "missing", unreadable
    elif out["exists"] == "no":
        out["status"], out["reason"] = "missing", out["reason"] or "file does not exist"
    elif out["digest_match"] == "no":
        out["status"], out["reason"] = "digest_mismatch", f"declared {declared}, actual {out['actual_digest']}"
    elif out["producer_ok"] == "no":
        out["status"] = "producer_mismatch"
        out["reason"] = f"produced by {producer or 'nobody named'}, expected {expected}"
    elif out["exists"] == "yes" and not declared:
        out["status"], out["reason"] = "no_digest", "the output declares no digest to compare"
    elif out["exists"] == "yes" and out["digest_match"] == "yes":
        out["status"] = "ok"
    return out


def _current_outputs(doc: Report | None, stored: list[Report]) -> tuple[list[tuple[Report, Report | None]], list[str]]:
    """`(item, inherited)` per output (a workspace file or uri) from the handover's artifacts and the stored rows.

    `apply_result` appends a row per version of an output, so after a rework the old digest sits next to the new one. The
    reference of an output is its newest row WITH a digest; a newer row without one (the final `task.result` after a
    handover that carried the digest) never discards it: the item is the newest row with the reference digest and
    `inherited` names where the digest came from. Older rows with other digests are history, named in the returned notes
    as superseded. A document counts as written at its `created_wall`, a stored row at its `ts`; on a tie the stored row
    wins."""
    origin = f"handover {(doc or {}).get('handover_id') or ''}".strip()
    dated = [(float((doc or {}).get("created_wall") or 0), 0, a, origin) for a in (doc or {}).get("artifacts") or [] if isinstance(a, dict)]
    dated += [(float(r.get("ts") or 0), 1, r, "stored result row") for r in stored if isinstance(r, dict)]
    groups: dict[str, list[tuple[tuple[float, int, int], Report, str]]] = {}
    for index, (ts, source, item, where) in enumerate(sorted(dated, key=lambda d: d[:2])):
        uri = item.get("uri") if isinstance(item.get("uri"), str) else ""
        path, _why = _local_path(uri, item["kind"] if isinstance(item.get("kind"), str) else "") if uri else (None, "")
        groups.setdefault(str(path) if path is not None else uri or f"#{index}", []).append(((ts, source, index), item, where))
    ranked: list[tuple[tuple[float, int, int], Report, Report | None]] = []
    notes = []
    for key, rows in groups.items():
        rank, item, _where = rows[-1]
        reference = next(((r, i, w) for r, i, w in reversed(rows) if isinstance(i.get("digest"), str) and i["digest"]), None)
        inherited = None
        if reference is not None:
            digest = reference[1]["digest"]
            if not (isinstance(item.get("digest"), str) and item["digest"]):
                item, inherited = {**item, "digest": digest}, {"digest": digest, "source": reference[2]}
            old = sorted({i["digest"] for _r, i, _w in rows if isinstance(i.get("digest"), str) and i["digest"] and i["digest"] != digest})
            if old:
                notes.append(_clip(f"{item.get('uri') or key}: {len(old)} older digest(s) superseded and not checked: "
                                   f"{', '.join(old)}", 400))
        ranked.append((rank, item, inherited))
    return [(item, inherited) for _rank, item, inherited in sorted(ranked, key=lambda v: v[0])], notes


def artifact_check(payload: Report) -> Report:
    def work() -> Report:
        task_id = _task_id(payload)
        notes: list[str] = []
        outputs = payload.get("outputs")
        if isinstance(outputs, list) and len(outputs) > MAX_OUTPUTS:
            return _invalid("outputs", f"at most {MAX_OUTPUTS} outputs per call, got {len(outputs)}; split them across calls "
                                       "(a partial check never reports verified)")
        with ho.StoreReader() as store:
            row = store.task(task_id)
            stored = store.artifacts(task_id) if row else []
        expected = ho.agent_id_of(payload.get("agent")) or _agent_of_row(row)
        complete = True
        if outputs is None:
            status, doc = _inspect(payload, task_id)  # the explicit seq, else the caller's own handover, else the newest
            if doc is None and status["status"] != "missing":
                notes.append(f"handover unreadable: {(status['reasons'] or [status['status']])[0][:200]}")
            elif doc is None and payload.get("seq") is not None:
                notes.append(f"no handover with seq {payload['seq']}: only the stored result rows were checked")
            entries, superseded = _current_outputs(doc, stored)
            notes += superseded
            if len(entries) > MAX_OUTPUTS:
                complete = False
                notes.append(f"only the first {MAX_OUTPUTS} of {len(entries)} outputs were checked; verified is false")
        else:
            entries = [(o, None) for o in outputs if isinstance(o, dict)]
        results = [_check_output(item, expected, inherited) for item, inherited in entries[:MAX_OUTPUTS]]
        failed = sum(1 for r in results if r["status"] in ("missing", "digest_mismatch", "producer_mismatch"))
        unchecked = sum(1 for r in results if r["status"] in ("unchecked", "no_digest"))
        good = sum(1 for r in results if r["status"] == "ok")
        return {"state": "SUCCESS", "task_known": row is not None, "expected_producer": expected or "", "results": results,
                "checked": len(results), "ok_count": good, "failed": failed, "unchecked": unchecked,
                "ok": failed == 0 and complete, "verified": complete and bool(results) and good == len(results), "notes": notes}

    return _reply(payload, lambda: _guarded(work))


# ---- definition of done ----------------------------------------------------------------------------------------
_STOP = frozenset(
    "a an the and or of to in on for with by is are be been being was were it its this that these those as at from into than "
    "then so such must should shall will can may not no only all any each per via use used using when where which who what "
    "also have has had does do done make made ensure ensures".split())
_NEGATIVE = re.compile(r"\b(not|never|without|fail(ed|s|ing|ure)?|missing|unmet|todo|pending|incomplete|blocked|unresolved|"
                       r"skipped|broken|outstanding|remaining|cannot|can't|couldn't|isn't|aren't|didn't|wasn't)\b", re.I)
_PASS = frozenset({"pass", "passed", "ok", "met", "verified", "true", "success", "succeeded", "done", "green"})
_FAIL = frozenset({"fail", "failed", "false", "unmet", "error", "red", "blocked", "missing"})
_AC_REF = re.compile(r"\b(AC|REQ|US)[-_ ]?(\d+)\b", re.I)


def _tokens(text: str) -> set[str]:
    out = set()
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in _STOP or (len(word) < 3 and not any(c.isdigit() for c in word)):
            continue
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return out


def _refs(text: str) -> set[str]:
    return {f"{m.group(1).upper()}{int(m.group(2))}" for m in _AC_REF.finditer(text)}


def _evidence(result: Report) -> list[tuple[str, str, bool]]:
    """(source, text, affirmative) items drawn from a task result or a handover-shaped object."""
    items: list[tuple[str, str, bool]] = []

    def line(source: str, text: str) -> None:
        text = text.strip().lstrip("-*+ ").strip()
        if not text:
            return
        unchecked = text.startswith("[ ]")
        text = re.sub(r"^\[[ xX]\]\s*", "", text)
        items.append((source, text, not unchecked and not _NEGATIVE.search(text)))

    summary = result.get("summary_md")
    if isinstance(summary, str):
        for raw in summary.splitlines():
            line("summary_md", raw)
    for key in ("completed",):
        if isinstance(result.get(key), list):
            for entry in result[key]:
                if isinstance(entry, str):
                    line(key, entry)
    for key in ("remaining", "blockers"):
        if isinstance(result.get(key), list):
            for entry in result[key]:
                if isinstance(entry, str) and entry.strip():
                    items.append((key, entry.strip(), False))
    for output in result.get("outputs") or []:
        if isinstance(output, dict):
            text = f"{output.get('kind', '')} {output.get('uri', '')}".strip()
            if text:
                items.append(("outputs", text, True))
    checks = list(result.get("checks") or [])
    verification = result.get("verification")
    if isinstance(verification, dict):
        checks += list(verification.get("checks") or [])
    for check in checks:
        if isinstance(check, str):
            line("checks", check)
        elif isinstance(check, dict):
            text = " ".join(str(check[k]) for k in ("criterion", "ac", "name", "check", "description", "evidence", "summary") if check.get(k))
            flag = next((str(check[k]).lower() for k in ("status", "result", "verdict", "passed") if k in check), "")
            if text and flag in _PASS:
                items.append(("checks", text, True))
            elif text and flag in _FAIL:
                items.append(("checks", text, False))
    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        for name, value in metrics.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
                bad = bool(re.search(r"fail|error", str(name), re.I))
                items.append(("metrics", f"{name} {value}", not bad))
    verdicts = result.get("verdicts")
    if isinstance(verdicts, dict):
        for gate, entry in verdicts.items():
            verdict = str((entry or {}).get("verdict", "")).lower() if isinstance(entry, dict) else ""
            if verdict in ("pass", "fail"):
                items.append(("verdicts", f"{gate} gate verdict {verdict}", verdict == "pass"))
    return items


def _judge(criterion: str, evidence: list[tuple[str, str, bool]]) -> Report:
    tokens, refs = _tokens(criterion), _refs(criterion)
    out = {"criterion": _clip(criterion, 300), "status": "unknown", "evidence": [], "reason": ""}
    if not tokens and not refs:
        out["reason"] = "the criterion has no checkable terms"
        return out
    need = min(len(tokens), max(2, math.ceil(0.6 * len(tokens)))) if tokens else 0
    support: list[str] = []
    against: list[str] = []
    for source, text, affirmative in evidence:
        matched = (refs and refs & _refs(text)) or (tokens and len(tokens & _tokens(text)) >= need)
        if not matched:
            continue
        (support if affirmative else against).append(f"{source}: {_clip(text, 160)}")
    if against:
        out["evidence"], out["reason"] = against[:3], "contradicting or outstanding evidence"
    elif support:
        out["status"], out["evidence"], out["reason"] = "met", support[:3], f"{len(support)} supporting evidence item(s)"
    else:
        out["reason"] = "no evidence in the result"
    return out


def dod_check(payload: Report) -> Report:
    def work() -> Report:
        criteria = payload.get("criteria")
        row = None
        if criteria is None and payload.get("task_id") is not None:
            row = ho.read_task(_task_id(payload))
            criteria = _acceptance(row)
        if not isinstance(criteria, list) or not criteria or not all(isinstance(c, str) and c.strip() for c in criteria):
            return _invalid("criteria", "pass criteria (non-empty strings) or a task_id whose Task Store row has acceptance criteria")
        evidence = _evidence(payload["result"])
        if len(criteria) > MAX_CRITERIA:
            return _invalid("criteria", f"at most {MAX_CRITERIA} criteria per call, got {len(criteria)}; split them across "
                                        "calls (a partial check never reports all_met)")
        results = [_judge(c, evidence) for c in criteria]
        met = sum(1 for r in results if r["status"] == "met")
        return {"state": "SUCCESS", "results": results, "total": len(results), "met": met, "unknown": len(results) - met,
                "all_met": met == len(results)}

    return _reply(payload, lambda: _guarded(work))


# ---- Task Store views ------------------------------------------------------------------------------------------
def open_findings(payload: Report) -> Report:
    def work() -> Report:
        task_id = _task_id(payload)
        now = SYSTEM_CLOCK.wall()
        with ho.StoreReader() as store:
            row = store.task(task_id)
            rows = store.verdicts(task_id, row) if row else []
        findings: list[Report] = []
        for verdict in rows:
            expires = verdict.get("expires_at")
            if verdict.get("verdict") != "fail" or (isinstance(expires, (int, float)) and expires <= now):
                continue
            for f in verdict.get("findings") or []:
                if not isinstance(f, dict):
                    continue
                severity = str(f.get("severity") or "info")
                findings.append({
                    "gate": str(verdict.get("gate") or ""), "agent": ho.agent_id_of(verdict.get("agent_id")) or "",
                    "id": _clip(f.get("id") or "", 80), "severity": severity, "kind": _clip(f.get("kind") or "", 60),
                    "summary": _clip(f.get("summary") or "", 300), "location": _clip(f.get("location") or "", 200),
                    "ac_ref": _clip(f.get("ac_ref") or "", 60), "blocking": severity in BLOCKING,
                    "stale": bool(verdict.get("stale")),
                    "expires_in_s": round(float(expires) - now, 1) if isinstance(expires, (int, float)) else 0.0})
        findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 5), f["gate"], f["id"]))
        out = {"state": "SUCCESS", "task_known": row is not None, "findings": findings[:MAX_FINDINGS], "count": len(findings),
               "blocking": sum(1 for f in findings if f["blocking"]), "truncated": len(findings) > MAX_FINDINGS,
               "stale_count": sum(1 for f in findings if f["stale"])}
        return _scrub(out, _lease_ids(row))

    return _reply(payload, lambda: _guarded(work))


def attempt_history(payload: Report) -> Report:
    def work() -> Report:
        task_id = _task_id(payload)
        with ho.StoreReader() as store:
            row = store.task(task_id)
            view = _history_view(store, task_id, row) if row else {}
        base = {"state": "SUCCESS", "task_known": row is not None, "task_state": "", "attempt": 0, "max_attempts": 0,
                "rework_loops": 0, "max_rework_loops": MAX_REWORK_LOOPS, "retries_left": 0, "transition_count": 0,
                "transitions": [], "last_error": ""}
        if view:
            view["task_state"] = view.pop("state")
            base.update(view)
        return _scrub(base, _lease_ids(row))

    return _reply(payload, lambda: _guarded(work))


def lease_state(payload: Report) -> Report:
    def work() -> Report:
        task_id = _task_id(payload)
        row = ho.read_task(task_id)
        out = {"state": "SUCCESS", "task_known": row is not None, "lease": _lease_view(row, SYSTEM_CLOCK)}
        return _scrub(out, _lease_ids(row))

    return _reply(payload, lambda: _guarded(work))


def open_questions(payload: Report) -> Report:
    def work() -> Report:
        task_id = _task_id(payload)
        with ho.StoreReader() as store:
            row = store.task(task_id)
            upstream = [r for d in (row or {}).get("depends_on") or [] if isinstance(d, str) and (r := store.task(d))]
        questions = [{"task_id": up["task_id"], "origin": "upstream", "task_state": up.get("state") or "", "question": q}
                     for up in upstream if _unresolved(up) for q in _upstream_questions(up)]
        doc = _inspect({}, task_id)[1]  # the handover addressed to the caller, else the newest; a malformed one is left to handover_verify
        if doc:
            questions += [{"task_id": task_id, "origin": "handover", "task_state": (row or {}).get("state") or "",
                           "question": _clip(q)} for q in doc["open_questions"]]
        out = {"state": "SUCCESS", "task_known": row is not None, "questions": questions[:MAX_QUESTIONS],
               "count": len(questions), "truncated": len(questions) > MAX_QUESTIONS}
        return _scrub(out, _lease_ids(row, *upstream))

    return _reply(payload, lambda: _guarded(work))
