"""Swarm memory is substrate memory (ADR 0001, phase S3).

There is no swarm-side memory store. This module is a thin client over the substrate's one governed door:

* ``memory_write``  -> ``POST {SUBSTRATE_URL}/memory`` (the REST face of the substrate's ``memory_write``). The server decides
  the writer from the bearer token, applies RBAC / quarantine / idempotency / conflict rules and answers with an outcome.
* ``memory_query``  -> the substrate's ``memory_search`` MCP tool.
* ``run_start_context`` -> ``POST /brief`` (the substrate's ``memory_brief``), A01's run-start context.

The read side is fail-open (``[]`` / ``''`` when the substrate is off or down). The write side never pretends: a write is done
only when :attr:`WriteOutcome.ok` is true, i.e. the server said ``accepted`` AND ``verified_persisted``. Everything else is a
named, non-ok status, and a refusal (``denied``) raises ``SwarmError(E-POLICY)``. Storage is reached only through
``swarm.substrate_client``; ``tests/test_swarm_memory.py`` proves that no second memory store exists in this repository.
"""
from __future__ import annotations
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import substrate_client
from .errors import ErrorCode, SwarmError

# swarm kind -> substrate MemoryKind. The substrate's MEMORY_KINDS is not widened for the swarm.
KIND_MAP: dict[str, str] = {"decision": "decision", "retro": "fact", "pattern": "fact"}
SCOPES: tuple[str, ...] = ("graph", "repo", "agent", "global")

# The 30 swarm surfaces (substrate `SWARM_SURFACES`); cross-checked against swarm.substrate_tee.AGENT_SURFACES in the tests.
SWARM_SURFACES: tuple[str, ...] = (
    "swarm-a01-orch", "swarm-a02-req", "swarm-a03-arch", "swarm-a04-uxd", "swarm-a05-be", "swarm-a06-fe", "swarm-a07-data",
    "swarm-a08-qa", "swarm-a09-rev", "swarm-a10-sec", "swarm-a11-devops", "swarm-a12-rel", "swarm-a13-obs", "swarm-a14-maint",
    "swarm-a15-doc", "swarm-a16-cloud", "swarm-a17-plat", "swarm-a18-supply", "swarm-a19-event", "swarm-a20-finops",
    "swarm-a21-k8s", "swarm-a22-gitops", "swarm-a23-mesh", "swarm-a24-ksec", "swarm-a25-resil", "swarm-a26-sol",
    "swarm-a27-audit", "swarm-a28-chain", "swarm-a29-dapp", "swarm-a30-custody",
)
ORCH_SURFACE = "swarm-a01-orch"

MAX_UNVERIFIED_POSTS = 3                 # POSTs of one write that came back accepted-but-unverified (or in-flight), same key
UNVERIFIED_BACKOFF_S = (0.5, 1.0)        # sleeps between those POSTs
MAX_VERSION_RESUBMITS = 2                # re-submits after a version conflict (with the reported or re-read version in force)
VERSION_REASONS = frozenset({"version.required", "version.stale", "version.raced", "standing.raced"})
REVIEW_REASON = "review.required"
IN_FLIGHT_REASON = "write.in-flight"     # another attempt holds the key; the server says "retry with the same key"

# WriteOutcome.status values. `denied` is never returned: it raises SwarmError(E-POLICY).
ACCEPTED = "accepted"                    # accepted AND verified_persisted: the only ok status
ACCEPTED_UNVERIFIED = "accepted-unverified"  # accepted, read-back never confirmed after the retries: NOT ok
PROPOSAL = "proposal"                    # review.required: parked until a reviewer releases it: NOT ok
QUARANTINED = "quarantined"
CONFLICT = "conflict"
UNAVAILABLE = "unavailable"              # off/unreachable/store down, or the reply was lost: result UNKNOWN, NOT ok


@dataclass(frozen=True)
class WriteOutcome:
    """The result of one ``memory_write``. Only :attr:`ok` means done.

    ``status == UNAVAILABLE`` means the result is UNKNOWN: the substrate was off or unreachable, or the request reached it and
    the reply was lost (the entry may be stored). Recover by retrying the SAME logical write, which reuses ``idempotency_key``.
    """
    status: str
    reason: str = ""
    remedy: str = ""
    idempotency_key: str = ""
    verified_persisted: bool = False
    entry_id: str | None = None
    version: int | None = None
    review_id: str | None = None
    quarantine_id: str | None = None
    attempts: int = 0                    # POSTs sent for this write

    @property
    def ok(self) -> bool:
        """True ONLY when the server said accepted and read the row back. Nothing may treat a write as done otherwise."""
        return self.status == ACCEPTED and self.verified_persisted


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SwarmError(ErrorCode.E_INPUT, f"memory write needs a non-empty {name}", field=name)
    return value


def idempotency_key(correlation_id: str, task_id: str, attempt: int, kind: str, scope: str, text: str, *,
                    subject: str | None = None) -> str:
    """sha256 hex of the compact JSON array ``[correlation_id, task_id, attempt, kind, scope, subject, text]``.

    ``kind`` is the SUBSTRATE kind (after the kind map), ``scope`` the resolved substrate scope (e.g. ``repo:acme/widgets``)
    and ``subject`` is normalised (``strip().lower()``, ``""`` when absent). A JSON array is unambiguous: no field value can
    shift into its neighbour the way ``"a|b", "c"`` and ``"a", "b|c"`` would under a joined string. ``expected_version`` is
    deliberately NOT part of the key, so a version re-submit stays the same write. Stable across every retry of one logical
    write, so bus redelivery and a retry after a lost reply cannot double-write; any other field differing is a new key.
    """
    _text(correlation_id, "correlation_id")
    _text(task_id, "task_id")
    _text(kind, "kind")
    _text(scope, "scope")
    _text(text, "text")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise SwarmError(ErrorCode.E_INPUT, "memory write needs attempt >= 1", field="attempt")
    if subject is not None and not isinstance(subject, str):
        raise SwarmError(ErrorCode.E_INPUT, "memory write subject must be a string", field="subject")
    fields = [correlation_id, task_id, attempt, kind, scope, (subject or "").strip().lower(), text]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def resolve_scope(scope: str, *, graph_id: str | None = None, repo: str | None = None, surface: str | None = None) -> str:
    """Swarm scope -> substrate scope: graph -> ``graph:<id>``, repo -> ``repo:<owner/name>``, agent -> ``agent:<surface>``, global."""
    if scope not in SCOPES:
        raise SwarmError(ErrorCode.E_INPUT, f"unknown memory scope {scope!r}; expected one of {', '.join(SCOPES)}", field="scope")
    if scope == "global":
        return "global"
    if scope == "graph":
        ident, name = graph_id, "graph_id"
    elif scope == "repo":
        ident, name = repo, "repo"
    else:
        ident, name = surface, "surface"
    if not isinstance(ident, str) or not ident.strip() or any(c.isspace() for c in ident):
        raise SwarmError(ErrorCode.E_INPUT, f"memory scope {scope!r} needs {name}", field=name)
    if scope == "agent" and ident not in SWARM_SURFACES:
        raise SwarmError(ErrorCode.E_INPUT, f"memory scope 'agent' needs one of the 30 swarm surfaces, got {ident!r}", field="surface")
    return f"{scope}:{ident}"


def _json(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _str(reply: Mapping[str, Any] | None, name: str) -> str:
    value = reply.get(name) if reply is not None else None
    return value if isinstance(value, str) else ""


def _outcome(status: str, reply: Mapping[str, Any] | None, key: str, attempts: int, **over: Any) -> WriteOutcome:
    r = reply or {}
    raw_entry = r.get("entry")
    entry: Mapping[str, Any] = raw_entry if isinstance(raw_entry, dict) else {}
    version = entry.get("version")
    fields: dict[str, Any] = {
        "status": status, "reason": _str(r, "reason"), "remedy": _str(r, "remedy"), "idempotency_key": key,
        "verified_persisted": r.get("verified_persisted") is True,
        "entry_id": entry.get("id") if isinstance(entry.get("id"), str) else None,
        "version": version if isinstance(version, int) and not isinstance(version, bool) else None,
        "review_id": _str(r, "review_id") or None, "quarantine_id": _str(r, "quarantine_id") or None, "attempts": attempts,
    }
    fields.update(over)
    return WriteOutcome(**fields)


def _current_version(scope: str, subject: str | None, conflict_with: str, env: Mapping[str, str] | None) -> tuple[bool, int | None]:
    """Re-read the decision in force for (scope, subject): ``(readable, version)``; version None when none is in force.

    The fallback for an older server whose conflict reply names the colliding entry (``conflict_with``) but does not report
    ``current_version``: the version then comes from ``memory_search`` (superseded entries are excluded by the server). The
    read is targeted first (the subject as query text at the scope) and, only when that does not find it, scope-wide (empty
    query). Its limit: ``memory_search`` matches the query against the entry BODY only and returns at most the newest 100
    entries of the scope, so an older decision whose body lacks its subject words, with 100+ newer entries in its scope, is
    found by neither read. When the server named a standing entry but neither read finds it, the version is unreadable:
    ``(False, None)``, never "nothing in force" (that would drop ``expected_version`` and waste the re-submits), and the write
    stops as a conflict.
    """
    want = (subject or "").strip().lower()
    answered = True
    for query in ([(subject or "").strip()] if want else []) + [""]:
        entries = _search(query, scope, 100, env)
        if entries is None:
            answered = False
            continue
        versions = [e["version"] for e in entries
                    if e.get("kind") == "decision"
                    and (e.get("id") == conflict_with or (want and str(e.get("subject") or "").strip().lower() == want))
                    and isinstance(e.get("version"), int) and not isinstance(e.get("version"), bool)]
        if versions:
            return True, max(versions)
    if conflict_with or not answered:
        return False, None
    return True, None


def memory_write(kind: str, scope: str, text: str, *, correlation_id: str, task_id: str, attempt: int = 1,
                 graph_id: str | None = None, repo: str | None = None, surface: str | None = None,
                 subject: str | None = None, expected_version: int | None = None,
                 env: Mapping[str, str] | None = None, sleep: Callable[[float], None] = time.sleep) -> WriteOutcome:
    """One governed swarm memory write. Raises ``SwarmError`` E-INPUT (bad kind/scope/ids) or E-POLICY (the server denied it).

    ``subject`` names what a ``decision`` is about: only a decision with a subject is versioned (replacing one needs
    ``expected_version``). On a version conflict the client re-submits with the version in force that the server reports as
    ``current_version`` (null: none in force, so without ``expected_version``); an older server that does not report it gets
    the :func:`_current_version` re-read instead. Twice at most. The same ``idempotency_key`` is sent on every POST of this write.

    ``unavailable`` is an UNKNOWN result, not proof that nothing was written: when the request went out and the reply was
    lost, the server may have stored the entry. To recover, call again with the SAME logical write (same correlation, task,
    attempt, kind, scope, subject and text): it reuses the same idempotency key, so the server replays instead of writing a
    second entry. Never retry under a new ``attempt`` just because of a network failure.
    """
    if kind not in KIND_MAP:
        raise SwarmError(ErrorCode.E_INPUT, f"unknown memory kind {kind!r}; expected one of {', '.join(KIND_MAP)}", field="kind")
    target = resolve_scope(scope, graph_id=graph_id, repo=repo, surface=surface)
    key = idempotency_key(correlation_id, task_id, attempt, KIND_MAP[kind], target, text, subject=subject)
    body: dict[str, Any] = {"scope": target, "kind": KIND_MAP[kind], "text": text, "idempotency_key": key}
    if subject and subject.strip():
        body["subject"] = subject
    if graph_id:
        body["graph_id"] = graph_id
    if repo:
        body["repo"] = repo
    if expected_version is not None:
        body["expected_version"] = expected_version

    posts = waits = resubmits = 0
    while True:
        got = substrate_client.rest_post("/memory", body, env)
        posts += 1
        if got is None:  # off, unreachable, back-off, or the reply was lost after the server got it: result UNKNOWN, not "nothing written"
            why = "substrate.disabled" if not substrate_client.enabled(env) else "substrate.unreachable"
            return WriteOutcome(UNAVAILABLE, reason=why, idempotency_key=key, attempts=posts)
        http, raw = got
        reply = _json(raw)
        outcome = reply.get("outcome") if reply else None
        reason = _str(reply, "reason") if reply else ""
        if reply is None or outcome is None:  # not a MemoryWriteResult: an auth refusal, a 5xx from the audit gate, a proxy page
            if http in (401, 403):
                raise SwarmError(ErrorCode.E_POLICY, f"memory write refused (HTTP {http})", http_status=http, idempotency_key=key)
            return WriteOutcome(UNAVAILABLE, reason=f"http.{http}", idempotency_key=key, attempts=posts)

        if outcome == "denied":
            if http == 503 or reason.startswith("store."):  # the substrate's store is down: an outage, not a policy decision
                return _outcome(UNAVAILABLE, reply, key, posts)
            remedy = _str(reply, "remedy")
            raise SwarmError(ErrorCode.E_POLICY, f"{reason}: {remedy}" if remedy else (reason or "memory write denied"),
                             reason=reason, remedy=remedy, writer=_str(reply, "writer"), idempotency_key=key)

        if outcome == "quarantined":  # recorded by the server, surfaced to the caller, never retried
            return _outcome(QUARANTINED, reply, key, posts)

        if outcome == "conflict":
            if reason == REVIEW_REASON:  # a global write is a proposal until a reviewer releases it; never retried
                return _outcome(PROPOSAL, reply, key, posts)
            if reason in VERSION_REASONS and kind == "decision" and body.get("subject") and resubmits < MAX_VERSION_RESUBMITS:
                reported = reply.get("current_version")
                if "current_version" in reply and (reported is None or (isinstance(reported, int) and not isinstance(reported, bool))):
                    readable, version = True, reported  # the server named the version in force: no re-read
                else:  # an older server (or an unusable value): re-read it
                    readable, version = _current_version(target, body.get("subject"), _str(reply, "conflict_with"), env)
                if readable:
                    resubmits += 1
                    if version is None:
                        body.pop("expected_version", None)
                    else:
                        body["expected_version"] = version
                    continue
            if reason == IN_FLIGHT_REASON and waits < MAX_UNVERIFIED_POSTS - 1:
                sleep(UNVERIFIED_BACKOFF_S[min(waits, len(UNVERIFIED_BACKOFF_S) - 1)])
                waits += 1
                continue
            return _outcome(CONFLICT, reply, key, posts)

        if outcome == "accepted":
            if reply.get("verified_persisted") is True and 200 <= http < 300:
                return _outcome(ACCEPTED, reply, key, posts)
            if waits < MAX_UNVERIFIED_POSTS - 1:  # same key: the server replays and re-verifies instead of writing again
                sleep(UNVERIFIED_BACKOFF_S[min(waits, len(UNVERIFIED_BACKOFF_S) - 1)])
                waits += 1
                continue
            return _outcome(ACCEPTED_UNVERIFIED, reply, key, posts, verified_persisted=False)

        return WriteOutcome(UNAVAILABLE, reason=f"outcome.unknown:{outcome}", idempotency_key=key, attempts=posts)


def _search(query: str, scope: str | None, limit: int, env: Mapping[str, str] | None) -> list[dict[str, Any]] | None:
    """``memory_search`` entries, or None when the substrate did not answer with a list (the caller decides what that means)."""
    args: dict[str, Any] = {"query": query or "", "limit": max(1, min(100, int(limit)))}
    if scope:
        args["scope"] = scope
    got = substrate_client.mcp_call_json("memory_search", args, env)
    return [e for e in got if isinstance(e, dict)] if isinstance(got, list) else None


def memory_query(query: str, *, scope: str | None = None, limit: int = 10, env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """``memory_search`` over the substrate. A list of entries; ``[]`` when the substrate is off, down or answers badly.

    ``scope`` is a substrate scope string (``repo:o/n``, ``graph:<id>``, ``agent:<surface>``, ``global``). Quarantined and
    unreviewed writes are never returned by the server. Fail-open: a read must never stop a run.
    """
    return _search(query, scope, limit, env) or []


def run_start_context(repo: str | None, graph_id: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """A01's run-start context: the substrate's ``memory_brief`` markdown for the repo/graph, or ``''`` (fail-open)."""
    got = substrate_client.rest_post("/brief", {"repo": repo, "graph_id": graph_id, "surface": ORCH_SURFACE}, env)
    if got is None or got[0] != 200:
        return ""
    return got[1].strip()
