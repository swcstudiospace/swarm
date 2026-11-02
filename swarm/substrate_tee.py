"""Tee of the swarm run log to Agent Substrate (ADR 0001, phase S1).

`runlog.emit()` writes the local JSONL record first and then calls `tee()`. The tee turns the record into one
substrate event and POSTs it to /events. It is strictly additive and fail-open: no SUBSTRATE_URL, SUBSTRATE_DISABLED=1,
an unknown Graph ID, an unmapped type, a timeout or any exception means "send nothing" and the caller carries on.
Design notes (the two tables, session format, binding cache, dedupe): docs/substrate-tee.md.
"""
from __future__ import annotations
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, TypeGuard

from . import substrate_client, workspace
from .paths import swarm_dir

# --- identity ----------------------------------------------------------------------------------------------
# Swarm agent id -> substrate surface (the closed set in agent-substrate `SURFACES`). The server-side token env var
# is SUBSTRATE_TOKEN_<SURFACE, '-'->'_', upper>. agents.json is cross-checked against this table in the tests.
AGENT_SURFACES: dict[str, str] = {
    "A01": "swarm-a01-orch",
    "A02": "swarm-a02-req",
    "A03": "swarm-a03-arch",
    "A04": "swarm-a04-uxd",
    "A05": "swarm-a05-be",
    "A06": "swarm-a06-fe",
    "A07": "swarm-a07-data",
    "A08": "swarm-a08-qa",
    "A09": "swarm-a09-rev",
    "A10": "swarm-a10-sec",
    "A11": "swarm-a11-devops",
    "A12": "swarm-a12-rel",
    "A13": "swarm-a13-obs",
    "A14": "swarm-a14-maint",
    "A15": "swarm-a15-doc",
    "A16": "swarm-a16-cloud",
    "A17": "swarm-a17-plat",
    "A18": "swarm-a18-supply",
    "A19": "swarm-a19-event",
    "A20": "swarm-a20-finops",
    "A21": "swarm-a21-k8s",
    "A22": "swarm-a22-gitops",
    "A23": "swarm-a23-mesh",
    "A24": "swarm-a24-ksec",
    "A25": "swarm-a25-resil",
    "A26": "swarm-a26-sol",
    "A27": "swarm-a27-audit",
    "A28": "swarm-a28-chain",
    "A29": "swarm-a29-dapp",
    "A30": "swarm-a30-custody",
}

# A swarm session names its agent, replica and graph: `<AGENT>@<replica>:<graph_id>`. The substrate refuses a swarm claim
# whose replica is outside this charset (LEASE-01), so the runner checks it at start instead of failing every claim.
REPLICA_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
DEFAULT_REPLICA = "r0"


def replica_id(env: Mapping[str, str] | None = None) -> str:
    """SWARM_REPLICA (blank or unset: `r0`). ValueError when it is not 1-64 of [A-Za-z0-9._-]."""
    e = os.environ if env is None else env
    replica = (e.get("SWARM_REPLICA") or "").strip() or DEFAULT_REPLICA
    if REPLICA_RE.fullmatch(replica) is None:
        raise ValueError(f"SWARM_REPLICA {replica[:80]!r} must be 1-64 characters of [A-Za-z0-9._-]")
    return replica


def session_id(agent: str, graph_id: str, env: Mapping[str, str] | None = None) -> str:
    """`<AGENT>@<replica>:<graph_id>`: the one session format the tee and the lease bridge both use."""
    return f"{agent}@{replica_id(env)}:{graph_id}"

# --- swarm event type -> substrate EVENT_KINDS --------------------------------------------------------------
# EVENT_KINDS is closed: session.start, prompt, claim, tool.call, file.edit, shell, commit, pr, handoff,
# session.end, note, warning. EVERY type the repo emits via runlog.emit()/Ctx.emit() is listed here (or matched by
# a PREFIX_KINDS family); tests/test_substrate_tee.py parses every emit site and fails on an unmapped type.
# An unmapped type is never tee'd and never silently downgraded to `note`. The swarm type always rides in
# payload.swarm_type, so the mapping loses no information.
TYPE_KINDS: dict[str, str] = {
    # plan / lifecycle bookkeeping -> note
    "plan.updated": "note",              # A01 wrote a plan snapshot (orch_plan)
    "task.transition": "note",           # A01 moved a task between states (orch_status --transition)
    "task.approval.skipped": "note",     # advisory ingest saved a result and did not attempt APPROVED
    "task.result.raw": "note",           # runner captured a raw agent result before validation
    # a task changing hands -> claim
    "task.claimed": "claim",             # an agent took a task (ingest of a task.result that skipped the claim)
    # gate verdict / findings housekeeping and rejected or escalated work -> warning
    "gate.verdict.unrecorded": "warning",      # a gate ran but its verdict could not be recorded against a task
    "gate.findings.coerced": "warning",        # runner had to coerce a gate's findings into shape
    "gate.findings.synthesized": "warning",    # runner synthesized findings the gate did not produce
    "gate.findings.unattributed": "warning",   # findings named no known gate target
    "task.result.rejected": "warning",         # a task.result failed schema/contract validation
    "task.result.deduplicated": "note",        # a replayed ingest was skipped: its result is already applied (results.apply_result)
    "orch_status.effects_lost": "warning",     # a Rust-served transition committed but its effects were not all replayed (orch_status)
    "orch_plan.effects_lost": "warning",       # a Rust-served plan committed but its plan.updated replay was lost (orch_plan)
    "escalation.request": "warning",           # rework loops / attempts exhausted: a human is needed
    "security.dev_key": "warning",             # an envelope was signed with the development key
    # the runner's substrate leases (substrate_lease.py); grants, releases and completions are written by the server itself
    "lease.denied": "note",                    # another holder has the node: the task waits, nothing went wrong
    "lease.unleased": "warning",               # dispatched without a lease (substrate unreachable or erroring): fail-open
    "lease.refused": "warning",                # the server refused this agent's claim (token / session): not dispatched
    "lease.lost": "warning",                   # a refused heartbeat stopped a session or dropped a held lease
    "lease.unsettled": "warning",              # a release or completion did not land; the lease is left to lapse
    # the runner's handoff packets (substrate_handoff.py); a packet that lands is written by the server as `handoff`
    "handoff.unsent": "warning",               # the substrate did not take a boundary's packet: retried next round
    "handoff.refused": "warning",              # refused, or the sender has no token of its own: not written, not retried
    "handoff.unsigned": "warning",             # the substrate has no handoff key: packets are recorded unsigned (once a run)
    "handoff.misattributed": "warning",        # the ledger recorded the packet under another surface than its sender's
    # hook and watchdog observability events
    "hook-fired": "note",                      # hook trigger fired
    "per-unit": "note",                        # per-unit progress update
    "batch-end": "note",                       # batch execution round completed
    "stale": "warning",                        # watchdog detected stale state or lease
    "task.stranded": "warning",                # a later run failed a task left IN_PROGRESS past its deadline (T-06-26)
    # run lifecycle: signing-key state at start (envelope.perform_signing_key_lifecycle; status full|reduced rides in payload),
    # the end-of-run report (runlog.log_summary) -> note
    "signing.key.lifecycle": "note",
    "run.summary": "note",
    # subagent and OMP gsd-autonomous progress streams (subagent_lifecycle.py, observability.py) -> note, as per-unit
    "gsd.state": "note",                       # state driven to the fallback emit when the state store is unavailable
    "gsd.autonomous.omp": "note",              # per-run OMP status update (emit_gsd_omp)
    "subagent.status.stream": "note",          # per-subagent status line streamed to the OMP session
    "subagent.spawn.immediate": "note",        # a subagent id was minted and its slot taken
    "spawn.event": "note",                     # spawn notice for the same subagent
    "subagent.complete.immediate": "note",     # a subagent released its slot
    # an aggregated error for a FAILED/ERROR subagent (observability.emit_error_agg) -> warning
    "error.agg": "warning",
    # swarm run start: the hook trigger began a swarm (hook_trigger.py) -> session.start, the swarm's own opening record
    "swarm.start": "session.start",
    # hook trigger could not wait on a follow-on subagent (hook_trigger.py): the follow-on did not run to a verdict -> warning
    "follow_on.wait_failed": "warning",
    # runtime telemetry (telemetry.py, v2.2): run lifecycle -> note, a silent run or a degraded capability -> warning. Rows carry
    # source="telemetry", which names no agent surface, so build_event/tee never forward them; swarm.run.activity and
    # swarm.run.heartbeat are high-frequency and are additionally written with tee=False (local only).
    "swarm.run.queued": "note",
    "swarm.run.started": "note",
    "swarm.run.state": "note",
    "swarm.run.activity": "note",
    "swarm.run.heartbeat": "note",
    "swarm.run.stalled": "warning",
    "swarm.run.finished": "note",
    "swarm.run.handover": "note",              # the packet itself is written by the server as `handoff`; this is the run-side record
    "swarm.scheduler.tick": "note",
    "swarm.degraded": "warning",
    # registry audit (tool_registry.call_tool, v2.2 SEC-03): one row per registry call, source="registry" (no agent surface, so
    # build_event/tee never forward it) and written with tee=False: names, states and durations stay on this machine.
    "swarm.tool.call": "tool.call",
}
# `script.<name>` and `script.<name>.error` are the exit record of one agent script run -> tool.call.
# `phase.<name>` is a run-phase marker written by runlog.log_phase (init, reconcile, hook.main, ...) -> note.
PREFIX_KINDS: tuple[tuple[str, str], ...] = (("script.", "tool.call"), ("phase.", "note"))


def kind_for(swarm_type: str) -> str | None:
    """EVENT_KINDS value for a swarm event type, or None when the type is unmapped."""
    if swarm_type in TYPE_KINDS:
        return TYPE_KINDS[swarm_type]
    for prefix, kind in PREFIX_KINDS:
        if swarm_type.startswith(prefix) and len(swarm_type) > len(prefix):
            return kind
    return None


_AGENT_RE = re.compile(r"^(A\d{2})(?:@|$)")


def agent_of(record: Mapping[str, Any]) -> str | None:
    """The emitting agent id (A01..A30) of a run-log record, or None when it names none.

    Ctx.emit stamps ``source = "<agent id>@<script>"``. The one non-agent source that still has an identity is
    ``swarm.envelope`` (security.dev_key), whose payload carries the signing agent as ``source``.
    """
    src = record.get("source")
    m = _AGENT_RE.match(src) if isinstance(src, str) else None
    if m is None and src == "swarm.envelope":
        inner = (record.get("payload") or {}).get("source")
        m = _AGENT_RE.match(inner) if isinstance(inner, str) else None
    return m.group(1) if m and m.group(1) in AGENT_SURFACES else None


# --- Graph ID ----------------------------------------------------------------------------------------------
GRAPH_ID_RE = re.compile(r"ut-[0-9a-z]+-[0-9a-f]{8}")
_GRAPH_ID_IN_TEXT = re.compile(r"(?<![0-9A-Za-z-])ut-[0-9a-z]+-[0-9a-f]{8}(?![0-9A-Za-z])")
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def is_graph_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and GRAPH_ID_RE.fullmatch(value) is not None


def mint_graph_id(now_ms: int | None = None) -> str:
    """`ut-<base36 epoch ms>-<8 random hex>`: the same format the ultrathink hook mints."""
    n = int(time.time() * 1000) if now_ms is None else int(now_ms)
    digits = ""
    while True:
        n, r = divmod(n, 36)
        digits = _B36[r] + digits
        if n == 0:
            break
    return f"ut-{digits}-{secrets.token_hex(4)}"


def graph_id_in_text(text: str | None) -> str | None:
    """First Graph ID in a brief/spec (e.g. `<ISSUES graphId="ut-...">` or `Graph ID: ut-...`)."""
    m = _GRAPH_ID_IN_TEXT.search(text or "")
    return m.group(0) if m else None


def resolve_graph_id(correlation_id: str | None = None, *, explicit: str | None = None, env: Mapping[str, str] | None = None,
                     brief_text: str | None = None) -> str:
    """Graph ID to offer for a new run: --graph-id > env SUBSTRATE_GRAPH_ID > one named in the brief > freshly minted.

    This only decides what to *offer*; the authoritative id is whatever `bind_graph` gets back from the server.
    """
    e = os.environ if env is None else env
    if is_graph_id(explicit):
        return explicit
    if is_graph_id((e.get("SUBSTRATE_GRAPH_ID") or "").strip()):
        return e["SUBSTRATE_GRAPH_ID"].strip()
    return graph_id_in_text(brief_text) or mint_graph_id()


# --- durable local state: binding cache + dedupe window ----------------------------------------------------
DEDUPE_WINDOW_S = 24 * 3600
PENDING_STALE_S = 60.0  # a `pending` claim older than this was left by a killed sender and may be re-claimed
PRUNE_EVERY_S = 60.0  # the expired-row DELETE runs at most this often per process
_DB_NAME = "substrate-tee.db"
_last_prune: float | None = None  # monotonic time of this process's last prune
ORCH_SURFACE = AGENT_SURFACES["A01"]  # graph_bind / graph_register speak as the orchestrator (swarm-a01-orch)


def _connect(root: str | Path | None) -> sqlite3.Connection:
    con = sqlite3.connect(swarm_dir(root, create=True) / _DB_NAME, timeout=2.0)
    # `bindings` (pre per-server cache) is left as is and no longer read: a binding is only valid for the server that made it
    con.execute("CREATE TABLE IF NOT EXISTS bindings_by_server (substrate_url TEXT NOT NULL, correlation_id TEXT NOT NULL, "
                "graph_id TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (substrate_url, correlation_id))")
    con.execute("CREATE TABLE IF NOT EXISTS seen (surface TEXT NOT NULL, msg_id TEXT NOT NULL, first_seen REAL NOT NULL, "
                "state TEXT NOT NULL DEFAULT 'sent', PRIMARY KEY (surface, msg_id))")
    if "state" not in {row[1] for row in con.execute("PRAGMA table_info(seen)")}:  # a db from before pending/sent
        try:
            con.execute("ALTER TABLE seen ADD COLUMN state TEXT NOT NULL DEFAULT 'sent'")
        except sqlite3.OperationalError:  # another process added it first
            pass
    con.execute("CREATE INDEX IF NOT EXISTS seen_first_seen ON seen(first_seen)")
    con.commit()
    return con


def cached_graph_id(correlation_id: str, root: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> str | None:
    """The Graph ID cached for `correlation_id` on the current SUBSTRATE_URL; None when there is none (or the tee is off)."""
    server = substrate_client.base_url(env)
    if server is None:
        return None
    with closing(_connect(root)) as con:
        row = con.execute("SELECT graph_id FROM bindings_by_server WHERE substrate_url = ? AND correlation_id = ?",
                          (server, correlation_id)).fetchone()
    return row[0] if row else None


def remember_graph_id(correlation_id: str, graph_id: str, root: str | Path | None = None, *,
                      env: Mapping[str, str] | None = None) -> None:
    server = substrate_client.base_url(env)
    if server is None:
        return
    with closing(_connect(root)) as con, con:
        con.execute("INSERT OR REPLACE INTO bindings_by_server(substrate_url, correlation_id, graph_id, created) VALUES (?, ?, ?, ?)",
                    (server, correlation_id, graph_id, time.time()))


def bind_graph(correlation_id: str, graph_id: str, *, root: str | Path | None = None,
               env: Mapping[str, str] | None = None) -> str | None:
    """Bind `correlation_id` to `graph_id` on substrate; return the graph_id that is bound, or None.

    The server's answer always wins: on `conflict` (another run bound this correlation first) the returned id is
    adopted and cached, never the offered one. None means substrate did not answer, nothing is cached.
    """
    got = substrate_client.mcp_call("graph_bind", {"correlation_id": correlation_id, "graph_id": graph_id}, env, surface=ORCH_SURFACE)
    bound = got.get("graph_id") if got else None
    if not is_graph_id(bound):
        return None
    remember_graph_id(correlation_id, bound, root, env=env)
    _unbound.pop(correlation_id, None)
    return bound


_UNBOUND_TTL_S = 60.0
_unbound: dict[str, float] = {}  # correlation_id -> monotonic expiry; in-process only, never persisted


class GraphLookupRefused(RuntimeError):
    """The substrate explicitly refused a graph lookup; a lease must not bypass that refusal."""


def lookup_graph_id(correlation_id: str, *, root: str | Path | None = None, env: Mapping[str, str] | None = None,
                    defer_busy: bool = False, require_auth: bool = False) -> str | None:
    """Graph ID of a correlation: local cache, else a forward `graph_bind` lookup (cached on success), else None.

    A correlation substrate calls `unbound` (or does not answer for) is remembered in memory for 60 s, so a burst of
    records for it costs one network call. That negative result is never written to sqlite; `bind_graph` clears it.
    Local contention is never cached as unbound; lease callers use `defer_busy` to raise RequestBusy and defer dispatch.
    `require_auth` raises GraphLookupRefused on authentication/identity refusals and bypasses negative cache entries:
    an earlier optional lookup's unbound/outage result must never hide a current refusal from a lease consumer.
    """
    hit = cached_graph_id(correlation_id, root, env=env)
    if hit:
        return hit
    now = time.monotonic()
    if not require_auth and _unbound.get(correlation_id, 0.0) > now:
        return None
    outcome = substrate_client.mcp_call_outcome("graph_bind", {"correlation_id": correlation_id}, env, surface=ORCH_SURFACE)
    if outcome.status == "busy":
        if defer_busy:
            raise substrate_client.RequestBusy("substrate graph lookup slots busy")
        return None
    if outcome.status == "refused":
        _unbound.pop(correlation_id, None)
        if require_auth:
            raise GraphLookupRefused(outcome.detail or "substrate graph lookup refused")
        return None
    got = outcome.value if outcome.status == "ok" and isinstance(outcome.value, dict) else None
    gid = got.get("graph_id") if got else None
    if got and got.get("status") == "existing" and is_graph_id(gid):
        _unbound.pop(correlation_id, None)
        remember_graph_id(correlation_id, gid, root, env=env)
        return gid
    _unbound[correlation_id] = now + _UNBOUND_TTL_S
    return None


def reset() -> None:
    """Forget the in-process negative lookups, memoized repo slugs, unsent-agent notes and prune clock (tests; a
    long-lived process)."""
    global _last_prune
    _unbound.clear()
    _SLUGS.clear()
    _UNSENT.clear()
    _last_prune = None


# agent -> why its run-log rows stay local (no token of its own); reported once per process
_UNSENT: dict[str, str] = {}


def _claim(root: str | Path | None, surface: str, msg_id: str) -> bool:
    """Atomically claim (surface, msg_id) as `pending`; False when it is `sent` inside the window or freshly `pending`.

    A `pending` row older than PENDING_STALE_S (its sender died between claim and send) and a row past the window are
    re-claimed in the same statement. Expired rows are pruned at most once per PRUNE_EVERY_S per process.
    """
    global _last_prune
    now, mono = time.time(), time.monotonic()
    with closing(_connect(root)) as con, con:
        if _last_prune is None or mono - _last_prune >= PRUNE_EVERY_S:
            con.execute("DELETE FROM seen WHERE first_seen < ?", (now - DEDUPE_WINDOW_S,))
            _last_prune = mono
        if con.execute("INSERT OR IGNORE INTO seen(surface, msg_id, first_seen, state) VALUES (?, ?, ?, 'pending')",
                       (surface, msg_id, now)).rowcount == 1:
            return True
        return con.execute("UPDATE seen SET first_seen = ?, state = 'pending' WHERE surface = ? AND msg_id = ? "
                           "AND ((state = 'pending' AND first_seen < ?) OR first_seen < ?)",
                           (now, surface, msg_id, now - PENDING_STALE_S, now - DEDUPE_WINDOW_S)).rowcount == 1


def _mark_sent(root: str | Path | None, surface: str, msg_id: str) -> None:
    with closing(_connect(root)) as con, con:
        con.execute("UPDATE seen SET state = 'sent' WHERE surface = ? AND msg_id = ?", (surface, msg_id))


def _release(root: str | Path | None, surface: str, msg_id: str) -> None:
    with closing(_connect(root)) as con, con:
        con.execute("DELETE FROM seen WHERE surface = ? AND msg_id = ? AND state = 'pending'", (surface, msg_id))


_ORIGIN_RE = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$")
_SLUGS: dict[str, str] = {}


def repo_slug(root: str | Path) -> str:
    """`owner/name` of the repo at `root` from `git remote get-url origin`; the directory name when there is none.

    Memoized per root for the life of the process. Never raises.
    """
    resolved = Path(root).resolve()
    key = str(resolved)
    got = _SLUGS.get(key)
    if got is None:
        got = resolved.name
        try:
            out = subprocess.run(["git", "-C", key, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=2,
                                 check=False)
            m = _ORIGIN_RE.search(out.stdout.strip()) if out.returncode == 0 else None
            if m:
                got = m.group(1)
        except Exception:  # noqa: BLE001 - no git, a timeout, a bad path: fall back to the directory name
            pass
        _SLUGS[key] = got
    return got


# --- the tee -----------------------------------------------------------------------------------------------
def build_event(record: Mapping[str, Any], graph_id: str, *, env: Mapping[str, str] | None = None,
                root: str | Path | None = None) -> dict[str, Any] | None:
    """The /events body for a run-log record, or None when the record cannot be attributed or its type is unmapped."""
    e = os.environ if env is None else env
    swarm_type, agent = record.get("type"), agent_of(record)
    kind = kind_for(swarm_type) if isinstance(swarm_type, str) else None
    msg_id, corr = record.get("msg_id"), record.get("correlation_id")
    if kind is None or agent is None or not msg_id or not corr:
        return None
    task_id = record.get("task_id")
    payload = {"correlation_id": corr, "msg_id": msg_id, "swarm_type": swarm_type}
    for key in ("trace_id", "causation_id"):
        if record.get(key):
            payload[key] = record[key]
    status = (record.get("payload") or {}).get("status")
    if isinstance(status, str):
        payload["status"] = status
    body = {
        "kind": kind,
        "summary": f"{swarm_type}" + (f" {task_id}" if task_id else f" {corr}"),
        "surface": AGENT_SURFACES[agent],
        "session_id": session_id(agent, graph_id, e),
        "graph_id": graph_id,
        "actor": "agent",
        "payload": payload,
    }
    if task_id:
        body["node_id"] = task_id
    if root is not None:
        body["repo"] = repo_slug(root)
    return body


def tee(record: Mapping[str, Any], ctx_root: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> bool:
    """Send one run-log record to substrate. True only when substrate accepted a new event. Never raises.

    The record goes out with the token of the agent it is attributed to, from `workspace.credential`: that agent's env
    file for the workspace `ctx_root`, else its SUBSTRATE_TOKEN_<SURFACE>, else nowhere. It is never sent with this
    process's SUBSTRATE_TOKEN, which in the runner is A01's: an A05 row sent with it would be misattributed or refused.
    A row with no token of its own stays local, and the first one per agent says so on stderr (fail-open, recorded)."""
    try:
        e = os.environ if env is None else env
        if not substrate_client.enabled(e):
            return False
        corr, agent = record.get("correlation_id"), agent_of(record)
        if not corr or agent is None or kind_for(str(record.get("type"))) is None or not record.get("msg_id"):
            return False
        token, _source, problem = workspace.credential(agent, ctx_root, e)
        if token is None:
            if agent not in _UNSENT:
                _UNSENT[agent] = problem
                print(f"[substrate-tee] {agent} run-log rows stay local: no token of its own ({problem})", file=sys.stderr)
            return False
        graph_id = lookup_graph_id(corr, root=ctx_root, env=e)
        if graph_id is None:  # unbound run: skip, never invent a Graph ID here
            return False
        body = build_event(record, graph_id, env=e, root=ctx_root)
        if body is None:
            return False
        if not _claim(ctx_root, body["surface"], record["msg_id"]):
            return False
        own = {k: v for k, v in e.items() if not workspace.carries_token(k)}
        got = substrate_client.rest_post("/events", body, {**own, workspace.TOKEN: token})
        if got is not None and 200 <= got[0] < 300:
            _mark_sent(ctx_root, body["surface"], record["msg_id"])
            return True
        _release(ctx_root, body["surface"], record["msg_id"])  # dropped: a republish may try again
        return False
    except Exception:  # noqa: BLE001 - the tee must never disturb the host program
        return False
