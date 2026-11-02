"""Substrate leases for swarm tasks (ADR 0001 S2; LEASE-05..09).

The runner (A01, scripts/swarm_run.py) holds a lease on each task's Graph-of-Thought node on behalf of the agent it
dispatches the task to. It calls as that agent's surface, with the token in that agent's env file (swarm/workspace.py, the
same token the agent's own session gets) and session `<AGENT>@<replica>:<graph_id>`, so two replicas of
one agent class are two holders and racing claims grant exactly one. The substrate lease decides who may touch the work;
the Task Store stays A01's record of lifecycle state and keeps only an advisory mirror of the lease in `notes.lease`.

Fail-open like the rest of the integration: off (no SUBSTRATE_URL, --dry-run) means no call and no record; no answer or a
server error means the task runs unleased and says so. A *refusal* is different: the server answered and said no, so the
task is not dispatched. Local request-slot contention also defers dispatch, never masquerading as an outage.
Design notes, the refusal table and the event types: docs/substrate-leases.md.
"""
from __future__ import annotations
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import substrate_client, substrate_tee, workspace as workspace_mod
from .taskstore import TaskStore, TaskState as S

DEFAULT_TTL_S = 900  # the substrate's own default (SUBSTRATE_LEASE_TTL_SECONDS)
MIN_TTL_S = 30  # the substrate's bounds: its schema rejects a ttl_seconds outside them, so the request is clamped here
MAX_TTL_S = 86_400
TTL_ENV = "SWARM_LEASE_TTL_S"
IDLE_POLL_S = 1.0  # the keeper's longest sleep, so a lease taken while it sleeps is never beaten late
# Heartbeats in flight at once. Held leases are not capped by --max-parallel (review leases outlive their round), so one
# beat at a time falls behind a TTL/3 deadline once replies are slow. One request slot of substrate_client's is left
# free for the run's other calls; local contention waits within the request deadline, never starting outage back-off.
HEARTBEAT_WORKERS = max(1, substrate_client.MAX_IN_FLIGHT - 1)

# Lifecycle states in which the task's node stays leased: from the claim, through review, until DONE completes it.
# APPROVED is held for the instant between it and DONE, so the completion still has a lease to fence on.
HOLDING = frozenset({S.CLAIMED.value, S.IN_PROGRESS.value, S.IN_REVIEW.value, S.APPROVED.value})
# States in which the work is over for now, so a lease the keeper still beats is settled instead. Narrower than "not
# HOLDING": a task is PLANNED/RETRY between its claim and its CLAIMED transition, and that lease must survive the gap.
LET_GO = frozenset({S.DONE.value, S.FAILED.value, S.BLOCKED.value, S.ESCALATED.value, S.CANCELLED.value})
# Work an earlier runner process may have left holding a lease that nothing dispatches again: `resume` re-claims it.
REVIEW = frozenset({S.IN_REVIEW.value, S.APPROVED.value})

# Claim.status values
HELD, DENIED, REFUSED, BUSY, UNLEASED, OFF = "held", "denied", "refused", "busy", "unleased", "off"

# What a refused heartbeat does while the task's agent session runs (LEASE-06). `expired` alone re-claims: the lease
# lapsed but nobody took the node, so the same session takes it back under a new id and the trail shows `reclaimed`.
# Every other row stops the session and fails the task with that reason. FAILED runs the existing ladder (RETRY behind a
# fresh claim, bounded by max_attempts, then ESCALATED), so a lease that keeps slipping escalates instead of looping.
RECLAIM = "reclaim"
SESSION_REFUSALS: dict[str, str] = {
    "expired": RECLAIM,
    "lost": "E-TIMEOUT: lease lost: another session holds the node",
    "unheld": "E-TIMEOUT: lease unheld: the node was released while the session ran",
    "not-holder": "E-POLICY: lease not-holder: the runner's token is not the holder's surface",
    REFUSED: "E-POLICY: lease refused: the substrate no longer authorizes this caller",
}
RECLAIM_NOT_GRANTED = "E-TIMEOUT: lease expired and the re-claim was not granted"


def session_transition(refusal: str) -> str:
    """The FAILED reason for a refused heartbeat that stops a session (RECLAIM for `expired`, which does not)."""
    return SESSION_REFUSALS.get(refusal, f"E-TIMEOUT: lease refused ({refusal[:80]})")


def moved_on_reason(state: str) -> str:
    """Why the keeper stops a dispatch whose task another process moved out of the holding states (no transition:
    that process's own transition stands)."""
    return f"the task moved on to {state} outside this runner"


def clamp_ttl(ttl_s: int) -> int:
    return max(MIN_TTL_S, min(MAX_TTL_S, int(ttl_s)))


def ttl_seconds(env: Mapping[str, str] | None = None) -> int:
    """The TTL to request: SWARM_LEASE_TTL_S (default 900), clamped to [30, 86400]. ValueError when it is not an integer."""
    e = os.environ if env is None else env
    raw = (e.get(TTL_ENV) or "").strip()
    try:
        return clamp_ttl(int(raw)) if raw else DEFAULT_TTL_S
    except ValueError:
        raise ValueError(f"{TTL_ENV} {raw[:40]!r} is not a whole number of seconds") from None


def heartbeat_interval(ttl_s: int) -> float:
    """A third of the (clamped) TTL: two beats can be missed before the lease lapses, and never more often than 10 s."""
    return clamp_ttl(ttl_s) / 3


@dataclass(frozen=True)
class Claim:
    """The outcome of asking for a task's node. `dispatch` is whether the task may run now."""
    status: str
    lease_s: int | None = None  # the granted TTL, when held
    reason: str = ""  # the claim action when held; why, otherwise
    holder: dict[str, Any] | None = None  # who holds it, when denied
    expires_at: str | None = None  # until when, when denied

    @property
    def dispatch(self) -> bool:
        return self.status in (HELD, UNLEASED, OFF)


@dataclass
class Lease:
    """One lease this process holds. `due` (keeper clock) is when to beat next; `stop` ends the dispatch working it;
    `halted` is set once the keeper has stopped that dispatch because the task moved on elsewhere."""
    task_id: str
    agent_id: str
    surface: str
    session_id: str
    graph_id: str
    lease_id: str
    ttl_s: int
    action: str = ""
    due: float = 0.0
    stop: Callable[[str], None] | None = None
    halted: str | None = None
    workspace: str | Path | None = None  # whose agent env files hold the token every call on this lease carries


def _call(tool: str, arguments: dict[str, Any], agent_id: str, workspace: str | Path | None,
          env: Mapping[str, str]) -> substrate_client.Outcome:
    """Every lease call, and every handoff call (substrate_handoff.py), goes through here, as agent `agent_id`'s
    surface, with that agent's own token from `workspace.credential`: its env file for `workspace` (INST-03), the same
    token its session, its handoffs and its teed run-log rows carry. The runner's SUBSTRATE_TOKEN (A01's) never stands
    in, so an agent whose token was never delivered is refused here, by name, instead of claiming with someone else's."""
    if not substrate_client.enabled(env):
        return substrate_client.Outcome("off")
    token, _source, problem = workspace_mod.credential(agent_id, workspace, env)
    if token is None:
        return substrate_client.Outcome("refused", None, f"no token for {agent_id}: {problem}; re-run "
                                        "scripts/build_agents.py --install-workspace")
    own = {k: v for k, v in env.items() if not workspace_mod.carries_token(k)}
    return substrate_client.mcp_call_outcome(tool, arguments, {**own, workspace_mod.TOKEN: token})


def has_own_token(agent_id: str, workspace: str | Path | None, env: Mapping[str, str]) -> bool:
    """Whether `_call` as agent `agent_id` has a token of that agent's own (`workspace.credential`: its env file for
    `workspace`, else its SUBSTRATE_TOKEN_<SURFACE>). Never the runner's SUBSTRATE_TOKEN (A01's). A claim names its
    surface, so the server would refuse one on the wrong token; a handoff names none (the sender is whoever the token
    says), so a handoff checks this first and sends nothing without one."""
    return workspace_mod.credential(agent_id, workspace, env)[0] is not None


# --- the four calls, one node at a time ---------------------------------------------------------------------
def claim_node(agent_id: str, graph_id: str, node_id: str, *, ttl_s: int, workspace: str | Path | None,
               env: Mapping[str, str] | None = None) -> tuple[Claim, Lease | None]:
    """`graph_claim` for `node_id` as agent `agent_id`'s surface and session, with the token from that agent's env file
    for `workspace`. The Lease is set only when it is held."""
    e = os.environ if env is None else env
    surface = substrate_tee.AGENT_SURFACES.get(agent_id)
    if surface is None:
        return Claim(UNLEASED, reason=f"no substrate surface for agent {agent_id!r}"), None
    session = substrate_tee.session_id(agent_id, graph_id, e)
    args = {"graph_id": graph_id, "node_id": node_id, "session_id": session, "ttl_seconds": clamp_ttl(ttl_s),
            "surface": surface}
    got = _call("graph_claim", args, agent_id, workspace, e)
    if got.status == "error" and "unknown node" in got.detail:
        # a task created after orch_plan registered the node set (a gate rerun): register the node as A01, which upserts
        # nodes and leaves the edges alone when none are sent, then ask once more
        registered = substrate_client.mcp_call_outcome(
            "graph_register", {"graph_id": graph_id, "nodes": [{"node_id": node_id}]}, e,
            surface=substrate_tee.ORCH_SURFACE)
        if registered.status == "refused":
            return Claim(REFUSED, reason=registered.detail or "graph registration refused"), None
        got = _call("graph_claim", args, agent_id, workspace, e)
    if got.status == "off":
        return Claim(OFF), None
    if got.status == "busy":
        return Claim(BUSY, reason="substrate request slots busy"), None
    if got.status == "unreachable":
        return Claim(UNLEASED, reason="substrate unreachable"), None
    if got.status == "refused":
        return Claim(REFUSED, reason=got.detail or "refused"), None
    if got.status != "ok" or not isinstance(got.value, dict):
        return Claim(UNLEASED, reason=f"substrate error: {got.detail or 'no reply'}"[:300]), None
    reply = got.value
    lease_field = reply.get("lease")
    lease = lease_field if isinstance(lease_field, dict) else {}
    # `action` is what the index did (granted, renewed, reclaimed, stolen, denied); `ttl_seconds` is the TTL the
    # hold was granted for after the server's clamping, null when no hold stands
    action = str(reply.get("action") or "")
    lease_id = lease.get("lease_id")
    if reply.get("claimed") is True:
        if not isinstance(lease_id, str) or not lease_id:  # nothing to beat or fence with: it cannot be held
            return Claim(UNLEASED, reason="granted without a lease_id"), None
        granted = reply.get("ttl_seconds")
        ttl = granted if isinstance(granted, int) and not isinstance(granted, bool) and granted > 0 else clamp_ttl(ttl_s)
        held = Lease(node_id, agent_id, surface, session, graph_id, lease_id, ttl, action, workspace=workspace)
        return Claim(HELD, lease_s=ttl, reason=action), held
    holder = lease.get("holder") if isinstance(lease.get("holder"), dict) else None
    return Claim(DENIED, reason=str(reply.get("reason") or action or "held by another session")[:300], holder=holder,
                 expires_at=lease.get("expires_at")), None


OK, UNANSWERED = "ok", "unanswered"


def heartbeat(lease: Lease, env: Mapping[str, str] | None = None) -> str:
    """`graph_heartbeat`: OK, UNANSWERED (no answer or a server error: keep working, try again), or the refusal reason.

    A lease-level rejection or an explicit authentication/identity refusal stops work. A server that cannot answer
    is the fail-open case, not a statement that the lease is gone."""
    e = os.environ if env is None else env
    got = _call("graph_heartbeat", {"graph_id": lease.graph_id, "node_id": lease.task_id, "lease_id": lease.lease_id,
                                    "ttl_seconds": lease.ttl_s}, lease.agent_id, lease.workspace, e)
    if got.status == "ok" and isinstance(got.value, dict):
        return OK if got.value.get("ok") is True else str(got.value.get("reason") or "unknown")
    if got.status in ("refused", "error") and "not-holder" in got.detail:
        return "not-holder"
    if got.status == "refused":
        return REFUSED
    return UNANSWERED


def release(lease: Lease, env: Mapping[str, str] | None = None) -> str:
    """`graph_release`, fenced on the lease id: `released`, `not-ours` (nothing to release: someone else holds the node,
    or nobody does), or `failed: <why>`."""
    e = os.environ if env is None else env
    got = _call("graph_release", {"graph_id": lease.graph_id, "node_id": lease.task_id, "lease_id": lease.lease_id},
                lease.agent_id, lease.workspace, e)
    if got.status == "ok" and isinstance(got.value, dict):
        v = got.value
        if v.get("released") is True:
            return "released" if v.get("action") == "released" else "not-ours"
        if v.get("reason") == "stale-lease":
            return "not-ours"
        return f"failed: {v.get('reason') or 'not released'}"
    return f"failed: {got.status} {got.detail}".strip()[:300]


def complete(lease: Lease, env: Mapping[str, str] | None = None) -> str:
    """`graph_complete`, fenced on the lease id: `completed`, `not-ours` (the node is held by someone else or by nobody,
    so this lease cannot close it), or `failed: <why>`."""
    e = os.environ if env is None else env
    got = _call("graph_complete", {"graph_id": lease.graph_id, "node_id": lease.task_id, "lease_id": lease.lease_id},
                lease.agent_id, lease.workspace, e)
    if got.status == "ok" and isinstance(got.value, dict):
        v = got.value
        if v.get("completed") is True:
            return "completed"
        if v.get("reason") in ("stale-lease", "unheld"):
            return "not-ours"
        return f"failed: {v.get('reason') or 'not completed'}"
    return f"failed: {got.status} {got.detail}".strip()[:300]


# --- the advisory mirror in the Task Store -------------------------------------------------------------------
def _ident(lease: Lease) -> dict[str, object]:
    return {"lease_id": lease.lease_id, "agent": lease.agent_id, "surface": lease.surface,
            "session_id": lease.session_id, "graph_id": lease.graph_id, "ttl_s": lease.ttl_s}


def _mirror(store: TaskStore, task_id: str, **fields: object) -> None:
    """Replace notes.lease: the latest lease state only. The substrate trail and the run log hold the history."""
    store.set_notes(task_id, lease={**{k: v for k, v in fields.items() if v is not None}, "at": round(time.time(), 3)})


def _close(store: TaskStore, lease: Lease, state: str, emit: Callable[[str, dict[str, object]], object],
           env: Mapping[str, str]) -> str:
    """Complete (DONE) or release (any other state) a held lease and record what happened. Returns the outcome."""
    if state == S.DONE.value:
        outcome, op = complete(lease, env), "complete"
        settled = outcome == "completed"
    else:
        outcome, op = release(lease, env), "release"
        settled = outcome in ("released", "not-ours")  # a lease that is no longer ours has nothing left to release
    after = ("completed" if op == "complete" else "released") if settled else "unsettled"
    _mirror(store, lease.task_id, state=after, reason=f"{state}: {outcome}", **_ident(lease))
    if not settled:
        emit("lease.unsettled", {"task_id": lease.task_id, "agent": lease.agent_id, "op": op, "task_state": state,
                                 "reason": outcome})
    return outcome


def _agent_of(task: dict[str, Any]) -> str | None:
    agent = (task["notes_json"].get("lease") or {}).get("agent") or task.get("agent_id")
    return agent if agent in substrate_tee.AGENT_SURFACES else None


def settle_mirrored(store: TaskStore, task_id: str, *, emit: Callable[[str, dict[str, object]], object],
                    workspace: str | Path | None,
                    env: Mapping[str, str] | None = None) -> str:
    """For a process that does not hold the lease in memory (`orch_status --transition`): complete or release the lease
    mirrored in notes.lease when the task has left the holding states, with the token from the agent's env file for
    `workspace`. Returns `deferred` while a runner is still working the task (notes.running): releasing then would free
    the node under a live agent, so that runner's keeper stops the session on its next beat and its dispatch settles the
    lease once the session's process group is gone. Never raises."""
    try:
        e = os.environ if env is None else env
        if not substrate_client.enabled(e):
            return "off"
        task = store.get(task_id)
        m = task["notes_json"].get("lease") or {}
        if m.get("state") != HELD or not m.get("lease_id") or m.get("agent") not in substrate_tee.AGENT_SURFACES:
            return "none"
        if task["state"] in HOLDING:
            return "kept"
        if task["notes_json"].get("running") is not None:
            return "deferred"
        lease = Lease(task_id, m["agent"], substrate_tee.AGENT_SURFACES[m["agent"]], str(m.get("session_id") or ""),
                      str(m.get("graph_id") or ""), str(m["lease_id"]), int(m.get("ttl_s") or DEFAULT_TTL_S),
                      workspace=workspace)
        return _close(store, lease, task["state"], emit, e)
    except Exception as exc:  # noqa: BLE001 - a lease never changes a transition's outcome
        emit("lease.unsettled", {"task_id": task_id, "op": "settle", "reason": f"internal: {exc}"[:300]})
        return "error"


# --- the runner's bridge -------------------------------------------------------------------------------------
class LeaseBridge:
    """The leases one runner process holds for one run (one correlation, one Graph ID), and the keeper that beats them.

    `resume` at the start of a run (work an earlier process left in review); `acquire` before dispatch; `watch` from
    before the session spawns until its result is applied; `settle` after any transition out of the holding states
    (DONE completes, anything else releases); `rework` after CHANGES_REQUESTED. The keeper thread beats every held lease
    every TTL/3, whether or not a session is running: the producer's lease is kept through review so DONE can complete it
    and CHANGES_REQUESTED can release and re-claim it. Claims, beats, settlements and reworks of one task run one at a
    time (a per-task lock), so a claim answered while another thread lets the task go is never left untracked. `root` is
    the workspace (the runner's --repo): every call is made with the token in the executing agent's env file for it.
    """

    def __init__(self, store_path: str | Path, correlation_id: str, *,
                 emit: Callable[[str, dict[str, object]], object],
                 root: str | Path | None = None, env: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.env = os.environ if env is None else env
        self.replica = substrate_tee.replica_id(self.env)  # ValueError: the runner refuses to start
        self.ttl_s = ttl_seconds(self.env)  # ValueError likewise
        self.store_path, self.correlation_id, self.root, self.emit = Path(store_path), correlation_id, root, emit
        self._clock = clock
        self._lock = threading.Lock()  # guards the collections below; never held while waiting for a task lock
        self._task_locks: dict[str, threading.RLock] = {}
        self._held: dict[str, Lease] = {}
        self._lost: dict[str, str] = {}  # task -> FAILED reason of a lease lost while no dispatch watched it
        self._inflight: set[str] = set()  # tasks the keeper is beating right now
        self._graph_id: str | None = None
        self._local = threading.local()
        self._halt = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # -- plumbing
    def _store(self) -> TaskStore:
        store = getattr(self._local, "store", None)  # sqlite: one connection per thread
        if store is None:
            store = self._local.store = TaskStore(self.store_path)
        return store

    def _task_lock(self, task_id: str) -> threading.RLock:
        with self._lock:
            lock = self._task_locks.get(task_id)
            if lock is None:
                lock = self._task_locks[task_id] = threading.RLock()
            return lock

    def transition(self, task_id: str) -> threading.RLock:
        """Guard a lifecycle transition, its handoff and its settlement against the keeper.

        Reentrant so handoff acknowledgement, `settle` and `rework` may use the same task lock. Never hold a Task
        Store transaction while acquiring it: keeper beats also read and update the store under this lock.
        """
        return self._task_lock(task_id)

    def graph_id(self, *, defer_busy: bool = False) -> str | None:
        if self._graph_id is None:
            self._graph_id = substrate_tee.lookup_graph_id(self.correlation_id, root=self.root, env=self.env,
                                                          defer_busy=defer_busy, require_auth=True)
        return self._graph_id

    def _moved_on(self, task_id: str) -> bool:
        return self._store().get(task_id)["state"] in LET_GO

    def _hold(self, lease: Lease, replaces: Lease | None = None) -> bool:
        """Track `lease`. With `replaces`, only while that lease is still the one held, taking over its dispatch;
        False when it is not (the caller then lets the new lease go)."""
        lease.due = self._clock() + heartbeat_interval(lease.ttl_s)
        with self._lock:
            if replaces is not None:
                if self._held.get(lease.task_id) is not replaces:
                    return False
                lease.stop, lease.halted = replaces.stop, replaces.halted
            self._held[lease.task_id] = lease
        _mirror(self._store(), lease.task_id, state=HELD, action=lease.action, **_ident(lease))
        return True

    def held(self, task_id: str) -> Lease | None:
        with self._lock:
            return self._held.get(task_id)

    def lease_s(self, task_id: str) -> int | None:
        """The granted TTL of the task's lease, for task.assign's `lease_s`; None when it runs unleased."""
        lease = self.held(task_id)
        return lease.ttl_s if lease else None

    # -- claim
    def acquire(self, task: dict[str, Any], agent_id: str) -> Claim:
        """Hold the task's node before it is dispatched (LEASE-05). A lease this process already holds (a rework
        re-claimed by `rework`) is reused. Denied, refused and unleased outcomes are recorded as a note and an event;
        local contention defers the task without changing its lease mirror."""
        tid = task["task_id"]
        with self._task_lock(tid):
            with self._lock:
                lease = self._held.get(tid)
                if lease is not None:
                    return Claim(HELD, lease_s=lease.ttl_s, reason=lease.action)
                self._lost.pop(tid, None)
            try:
                graph = self.graph_id(defer_busy=True)
            except substrate_client.RequestBusy:
                return Claim(BUSY, reason="substrate graph lookup slots busy")
            except substrate_tee.GraphLookupRefused as exc:
                claim, lease = Claim(REFUSED, reason=str(exc)), None
            else:
                if graph is None:
                    claim, lease = Claim(UNLEASED, reason="no Graph ID is bound to this run"), None
                else:
                    claim, lease = claim_node(agent_id, graph, tid, ttl_s=self.ttl_s, workspace=self.root, env=self.env)
            if lease is not None:
                self._hold(lease)
            elif claim.status == DENIED:
                _mirror(self._store(), tid, state=DENIED, agent=agent_id, holder=claim.holder,
                        expires_at=claim.expires_at, reason=claim.reason)
                self.emit("lease.denied", {"task_id": tid, "agent": agent_id, "holder": claim.holder,
                                           "expires_at": claim.expires_at})
            elif claim.status == REFUSED:
                _mirror(self._store(), tid, state=REFUSED, agent=agent_id, reason=claim.reason)
                self.emit("lease.refused", {"task_id": tid, "agent": agent_id, "reason": claim.reason})
            elif claim.status == UNLEASED:
                _mirror(self._store(), tid, state=UNLEASED, agent=agent_id, reason=claim.reason)
                self.emit("lease.unleased", {"task_id": tid, "agent": agent_id, "reason": claim.reason})
            return claim

    def resume(self) -> list[str]:
        """At the start of a run: hold again the leases of this correlation's work in review (IN_REVIEW, APPROVED) that
        an earlier runner process left (`--once`, `--max-rounds`, a restart). Nothing dispatches that work again, so
        without this the gates would run while the producer's lease ran out, DONE could not complete the node, and a
        rework would renew the old hold instead of releasing it. Called before the first reconcile and gate dispatch.
        The same replica's claim on its own live lease is `renewed` and keeps its id; denied, refused and unleased
        claims are recorded as for a dispatch. Returns the tasks now held."""
        try:
            if self.graph_id() is None:  # no Graph ID bound: nothing was leased, so nothing to hold again
                return []
        except substrate_tee.GraphLookupRefused:
            pass  # acquire records the refusal for each review task; none may run unleased
        held = []
        for t in self._store().list(correlation_id=self.correlation_id):
            if t["state"] not in REVIEW or self.held(t["task_id"]) is not None:
                continue
            agent = _agent_of(t)
            if agent is not None and self.acquire(t, agent).status == HELD:
                held.append(t["task_id"])
        return held

    # -- while a dispatch runs
    def watch(self, task_id: str, stop: Callable[[str], None] | None) -> None:
        """Attach the `stop(reason)` of the dispatch working the task (None detaches it). The runner attaches it before
        the session spawns and detaches it once the result is applied, so a lease lost at any point in between stops the
        session or gate script and rejects the result. A lease lost before it got here, or a task that has moved on
        elsewhere, stops it at once."""
        with self._lock:
            lease = self._held.get(task_id)
            if lease is not None:
                lease.stop = stop
                reason = lease.halted if stop is not None else None
            else:
                reason = self._lost.pop(task_id, None) if stop is not None else None
        if reason is not None and stop is not None:  # a reason exists only where a stop was attached
            stop(reason)

    def beat(self, task_id: str) -> str:
        """One heartbeat for the task's lease, with the refusal table applied. Returns what happened: ok, unanswered,
        reclaimed, stopped (a dispatch was stopped), dropped (no dispatch; the lease is let go), settled (the task had
        left the holding states and nothing here works it), halting (it had, but a dispatch here still works it: that
        dispatch is stopped and the lease kept until it ends) or gone (nothing held)."""
        with self._task_lock(task_id):
            lease = self.held(task_id)
            if lease is None:
                return "gone"
            state = self._store().get(task_id)["state"]
            halting = state in LET_GO  # moved on in another process (orch_status --transition CANCELLED)
            if halting:
                with self._lock:
                    stop, first = lease.stop, lease.halted is None
                    if stop is not None and first:
                        lease.halted = moved_on_reason(state)
                if stop is None:  # nothing here works the task any more: let the node go now
                    self.settle(task_id)
                    return "settled"
                # The dispatch here may still be writing: stop it, and keep beating so the node stays ours until the
                # dispatch has ended. execute_one's settle releases it then, once the session's process group is gone.
                if first and lease.halted is not None:  # first is set only where a stop was attached, which sets halted
                    stop(lease.halted)
            got = heartbeat(lease, self.env)
            if got == OK:
                lease.due = self._clock() + heartbeat_interval(lease.ttl_s)
                return "halting" if halting else OK
            if got == UNANSWERED:  # try again before the next third, so one blip cannot run the TTL out
                lease.due = self._clock() + min(heartbeat_interval(lease.ttl_s), substrate_client.BACKOFF_S)
                return UNANSWERED
            return self._refused(lease, got)

    def _refused(self, lease: Lease, reason: str) -> str:
        tid = lease.task_id
        with self._lock:
            if self._held.get(tid) is not lease:
                return "gone"
            watching = lease.stop is not None
        # Between rounds no session is at risk, so an `unheld` lease (lapsed and reaped, or released under us) is re-claimed
        # like an `expired` one. While a session runs only `expired` is: `unheld` may be an operator's deliberate release.
        if reason == "expired" or (reason == "unheld" and not watching):
            if not watching and self._moved_on(tid):
                self.settle(tid)
                return "settled"
            claim, fresh = claim_node(lease.agent_id, lease.graph_id, tid, ttl_s=self.ttl_s, workspace=self.root,
                                      env=self.env)
            if fresh is not None:
                if not self._hold(fresh, replaces=lease):  # let go of meanwhile: a grant nobody tracks is released
                    release(fresh, self.env)
                    return "gone"
                return "reclaimed"
            if claim.status in (UNLEASED, BUSY):  # no server refusal: re-claim on the next beat, including local contention
                lease.due = self._clock() + min(heartbeat_interval(lease.ttl_s), substrate_client.BACKOFF_S)
                return UNANSWERED
            reason, failed = f"{reason}; re-claim {claim.status}", RECLAIM_NOT_GRANTED
        else:
            failed = session_transition(reason)
        with self._lock:
            if self._held.get(tid) is not lease:
                return "gone"
            del self._held[tid]
            stop = lease.stop
            if stop is None:
                self._lost[tid] = failed
        _mirror(self._store(), tid, state="lost", reason=reason, **_ident(lease))
        self.emit("lease.lost", {"task_id": tid, "agent": lease.agent_id, "reason": reason,
                                 "session_stopped": stop is not None})
        if stop is None:
            return "dropped"
        stop(failed)
        return "stopped"

    # -- the keeper
    def start(self) -> None:
        if self._thread is None:
            self._halt.clear()
            self._thread = threading.Thread(target=self._keep, name="swarm-lease-keeper", daemon=True)
            self._thread.start()

    def close(self) -> None:
        """Stop beating. Leases still held (work left IN_REVIEW by an early stop) lapse within their TTL; the same
        replica's next run re-claims them as the same holder (`resume`)."""
        self._halt.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _keep(self) -> None:
        pool = ThreadPoolExecutor(max_workers=HEARTBEAT_WORKERS, thread_name_prefix="swarm-lease-beat")
        try:
            while not self._halt.is_set():
                self._wake.clear()  # before looking: a beat that ends after this wakes the wait below at once
                self._wake.wait(self.beat_due(pool))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def beat_due(self, pool: ThreadPoolExecutor | None = None) -> float:
        """Start a beat for every lease that is due and not being beaten already: each lease on its own due time, up to
        HEARTBEAT_WORKERS at once on `pool` (one after another without one). Returns the seconds until the next lease
        that is not in flight falls due (at most IDLE_POLL_S); a beat that ends sets `_wake`."""
        now = self._clock()
        with self._lock:
            due = [tid for tid, lease in self._held.items() if lease.due <= now and tid not in self._inflight]
            self._inflight.update(due)
        for tid in due:
            if pool is None:
                self._beat_one(tid)
            else:
                pool.submit(self._beat_one, tid)
        with self._lock:
            nxt = min((lease.due for tid, lease in self._held.items() if tid not in self._inflight), default=None)
        wait = IDLE_POLL_S if nxt is None else nxt - self._clock()
        return max(0.01, min(IDLE_POLL_S, wait))

    def _beat_one(self, task_id: str) -> None:
        try:
            if not self._halt.is_set():
                self.beat(task_id)
        except Exception:  # noqa: BLE001 - one bad beat must not end the keeper; that lease is tried again sooner
            lease = self.held(task_id)
            if lease is not None:
                lease.due = self._clock() + min(heartbeat_interval(lease.ttl_s), substrate_client.BACKOFF_S)
        finally:
            with self._lock:
                self._inflight.discard(task_id)
            self._wake.set()

    # -- after a transition
    def settle(self, task_id: str) -> str:
        """Line the node up with the task's state (LEASE-07): DONE → `graph_complete` (claiming first as the producer when
        this process holds no lease); CLAIMED/IN_PROGRESS/IN_REVIEW/APPROVED keep it; anything else → `graph_release`.
        Never raises: a lease never changes a transition's outcome."""
        try:
            with self._task_lock(task_id):
                task = self._store().get(task_id)
                state = task["state"]
                if state in HOLDING:
                    return "kept"
                with self._lock:
                    lease = self._held.pop(task_id, None)
                    self._lost.pop(task_id, None)
                if lease is None and state == S.DONE.value:
                    agent, graph = _agent_of(task), self.graph_id()
                    if agent is None or graph is None:
                        return "none"
                    claim, lease = claim_node(agent, graph, task_id, ttl_s=self.ttl_s, workspace=self.root,
                                              env=self.env)
                    if lease is None:
                        _mirror(self._store(), task_id, state="unsettled", agent=agent,
                                reason=f"DONE: claim {claim.status} {claim.reason}".strip()[:300], holder=claim.holder)
                        if claim.status in (DENIED, REFUSED):  # the swarm calls it DONE and someone else holds the node
                            self.emit("lease.unsettled", {"task_id": task_id, "agent": agent, "op": "complete",
                                                          "task_state": state, "reason": f"claim {claim.status}",
                                                          "holder": claim.holder})
                        return f"claim {claim.status}"
                if lease is None:
                    return "none"
                return _close(self._store(), lease, state, self.emit, self.env)
        except Exception as exc:  # noqa: BLE001
            self.emit("lease.unsettled", {"task_id": task_id, "op": "settle", "reason": f"internal: {exc}"[:300]})
            return "error"

    def handed_over(self, task_id: str) -> None:
        """A `coord_handoff` with `release_lease` released this task's lease once its packet was durable (Phase 13).
        Stop beating it and record the release, so `settle` does not release it a second time."""
        with self._task_lock(task_id):
            with self._lock:
                lease = self._held.pop(task_id, None)
                self._lost.pop(task_id, None)
            if lease is not None:
                _mirror(self._store(), task_id, state="released", action="handover", **_ident(lease))

    def rework(self, task_id: str) -> Claim:
        """CHANGES_REQUESTED (LEASE-07): release the producer's lease and claim the node again, so the rework loop reads
        as `released` then `claim` in the trail. The rework dispatch reuses the new lease; a denied re-claim leaves the
        task IN_PROGRESS without a session, and dispatch asks again."""
        with self._task_lock(task_id):
            with self._lock:
                lease = self._held.pop(task_id, None)
                self._lost.pop(task_id, None)
            store = self._store()
            if lease is not None:
                _close(store, lease, S.CHANGES_REQUESTED.value, self.emit, self.env)
            task = store.get(task_id)
            agent = lease.agent_id if lease is not None else _agent_of(task)
            if agent is None:
                return Claim(UNLEASED, reason="no agent to claim for")
            return self.acquire(task, agent)
