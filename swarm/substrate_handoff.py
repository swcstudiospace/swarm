"""Signed handoffs at accountable-agent boundaries (ADR 0001 S2 criteria 7-9; HAND-01..03).

When work crosses from one accountable agent to another, the runner (A01, scripts/swarm_run.py) writes one
`coord_handoff` packet **as the sender**: through the same `substrate_lease._call` seam the lease bridge uses, with the
sender's own token, so the substrate records and signs the packet as that agent and no agent can sign as another. The
boundaries, all found in code the runner already runs:

  dependency   a task is dispatched and one of its `depends_on` tasks belongs to another agent: that agent → this one
  gate         a gate fails a task (CHANGES_REQUESTED → rework): the gate agent (A08/A09/A10/A12) → the producer
  escalation   the swarm gives up on a task (ESCALATED: rework cap or max_attempts): the producer → A14 when the task
               carries out an A14 work order, else A01; a producer still holding the node hands its lease over

`reconstruct(graph_id, node_id, agent=)` is the receiving side (HAND-02): a task's context rebuilt from
`coord_handoff_list` and `events_query` alone, with no Task Store, run log or bus read.

Fail-open like the rest of the integration: no answer means a `handoff.unsent` warning, a retry on a later round and
a run that carries on; an unsigned packet (no SUBSTRATE_HANDOFF_KEY on the server) is recorded, said to be unsigned
and used. Design notes: docs/substrate-handoffs.md.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import substrate_lease, substrate_tee
from .errors import SwarmError
from .taskstore import MAX_REWORK_LOOPS, TaskStore

ORCH = "A01"
# A14 writes `patch.task` / `hotfix.task` (04-integration-plan §1), the work order an A05/A07 patch carries out (hotfix:
# rca → patch; dependency: patch → bump), and its decision rules own a patch whose gates will not pass (A14 "bump breaks
# gates"). A task the swarm gives up on goes to A14 when it carries out such an order, else to A01.
MAINTENANCE = "A14"
GATE_AGENTS = {"quality": "A08", "review": "A09", "security": "A10", "release": "A12"}

# The idempotency key rides on the first line of the packet's `notes`. A key is `<round>/<part>`: the round names one
# boundary event on one node (`dispatch@T`, `rework2@T`, `escalated-a1r2@T`) and the part one packet of it.
BOUNDARY_PREFIX = "boundary: "
BOUNDARY_TIME_PREFIX = "boundary-at: "
# The server drops `notes` first over 12 kB and caps it at 1200 UTF-16 units. Fit the complete request with room for
# its signed packet envelope before sending, and compact oversized key components without changing ordinary keys.
PACKET_BUDGET = 9_000
PACKET_OVERHEAD = 1_500  # generated packet id/time, chain hash, signature and envelope fields
MAX_KEY_PART_BYTES, MAX_NOTES = 256, 1_200
MAX_FILES, MAX_DOD, MAX_BLOCKERS, MAX_LINE, MAX_BODY, MAX_GOAL = 40, 24, 16, 300, 900, 500
EVENTS_LIMIT = 50

# How far a packet's verdict (coord_handoff_list) is trusted when a context is rebuilt.
VERIFIED, UNSIGNED, REJECTED = "verified", "unsigned", "rejected"
# Unauthenticated, not forged (the server's own wording): used, and flagged. Everything else that is not `ok` is a
# packet that does not match what was signed, or cannot be checked against a key the server knows: listed, never used.
UNAUTHENTICATED = frozenset({"unsigned", "no-keys-configured"})

_AGENT_RE = re.compile(r"^(A\d{2})(?:@|$)")


def trust_of(verdict: object) -> str:
    if isinstance(verdict, dict) and verdict.get("ok") is True:
        return VERIFIED
    if (isinstance(verdict, dict) and verdict.get("ok") is False
            and isinstance(verdict.get("reason"), str) and verdict["reason"] in UNAUTHENTICATED):
        return UNSIGNED
    return REJECTED


def boundary_key(packet: Mapping[str, Any]) -> str | None:
    notes = packet.get("notes")
    if not isinstance(notes, str) or not notes.startswith(BOUNDARY_PREFIX):
        return None
    return notes.splitlines()[0][len(BOUNDARY_PREFIX):].strip() or None


def _round(key: str | None) -> str | None:
    return key.rsplit("/", 1)[0] if key and "/" in key else None


def _valid_packet(packet: object, graph: str) -> bool:
    if not isinstance(packet, dict) or packet.get("graph_id") != graph:
        return False
    if type(packet.get("version")) is not int or packet["version"] != 1:
        return False
    if any(not isinstance(packet.get(name), str) or not packet[name]
           for name in ("handoff_id", "ts", "goal", "node_id")):
        return False
    if any(not isinstance(packet.get(name), list)
           or not all(isinstance(item, str) for item in packet[name]) for name in ("files", "dod", "blockers")):
        return False
    return all(isinstance(packet.get(name), dict)
               and isinstance(packet[name].get("surface"), str) and packet[name]["surface"] for name in ("from", "to"))


def _boundary_time(packet: Mapping[str, Any]) -> float:
    """Signed logical boundary time, unchanged by delivery; older packets use their signed storage time."""
    notes = packet.get("notes")
    lines = notes.splitlines() if isinstance(notes, str) else []
    if len(lines) > 1 and lines[1].startswith(BOUNDARY_TIME_PREFIX):
        try:
            value = float(lines[1][len(BOUNDARY_TIME_PREFIX):])
            if math.isfinite(value) and value > 0:
                return value
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(packet["ts"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, OverflowError):
        return 0.0


def _text(item: object) -> str:
    return item if isinstance(item, str) else json.dumps(item, sort_keys=True, default=str)


def _lines(items: Iterable[object] | None, limit: int = MAX_LINE) -> list[str]:
    out: list[str] = []
    for item in items or []:
        if item is None:
            continue
        line = _text(item).strip()[:limit]
        if line and line not in out:
            out.append(line)
    return out


def _merge(*lists: list[str]) -> list[str]:
    return _lines([i for lst in lists for i in lst])


def dod_of(task: Mapping[str, Any]) -> list[str]:
    """The Definition of Done is the task's own `acceptance[]` (HAND-01), one line per criterion."""
    return _lines(task.get("acceptance"))


def files_of(task: Mapping[str, Any]) -> list[str]:
    """The artifacts the task registered (`outputs[].uri`)."""
    return _lines([o.get("uri") for o in task.get("outputs") or [] if isinstance(o, dict)])


def _finding_line(gate: str, finding: object) -> str:
    if not isinstance(finding, dict):
        return f"{gate}: {_text(finding)}"
    where = str(finding.get("file") or finding.get("path") or "")
    if where and finding.get("line"):
        where = f"{where}:{finding['line']}"
    what = finding.get("summary") or finding.get("message") or finding.get("kind") or _text(finding)
    return f"{gate} [{finding.get('severity') or '?'}] {what}" + (f" ({where})" if where else "")


def _finding_files(findings: Iterable[object] | None) -> list[str]:
    return _lines([f.get("file") or f.get("path") for f in findings or [] if isinstance(f, dict)])


def _agent_in(source: object) -> str | None:
    m = _AGENT_RE.match(source) if isinstance(source, str) else None
    return m.group(1) if m and m.group(1) in substrate_tee.AGENT_SURFACES else None


@dataclass
class Boundary:
    """One packet to write: who hands what to whom, on which node (the node the receiver works next)."""
    key: str
    sender: str
    receiver: str
    node_id: str
    goal: str
    dod: list[str]
    files: list[str]
    blockers: list[str]
    notes: str
    to_session: bool = True  # the receiver works the node under this runner, so its session is known
    lease: substrate_lease.Lease | None = None  # the sender's lease on node_id, handed over with the packet


def boundary(*, round_: str, part: str, sender: str, receiver: str, node_id: str, goal: str, dod: list[str],
             files: list[str], blockers: list[str], body: str, occurred_at: float, to_session: bool = True,
             lease: substrate_lease.Lease | None = None) -> Boundary:
    """A stable key and signed transition time; retries never rewrite when this boundary happened."""
    def key_part(value: str) -> str:
        encoded = value.encode("utf-8")
        return "sha256-" + hashlib.sha256(encoded).hexdigest() if len(encoded) > MAX_KEY_PART_BYTES else value

    key = f"{key_part(round_)}/{key_part(part)}"
    return Boundary(key, sender, receiver, node_id, goal.strip()[:MAX_GOAL] or node_id[:MAX_GOAL],
                    dod[:MAX_DOD], files[:MAX_FILES], blockers[:MAX_BLOCKERS],
                    f"{BOUNDARY_PREFIX}{key}\n{BOUNDARY_TIME_PREFIX}{occurred_at:.9f}\n\n{body.strip()[:MAX_BODY]}",
                    to_session, lease)


def _fit_packet(args: dict[str, Any]) -> bool:
    """Fit payload below the signed-packet budget without truncating the key or transition time."""
    header, _, body = args["notes"].partition("\n\n")
    trimmed: dict[str, int] = {}

    def notes() -> None:
        suffix = f"\ntrimmed by the runner: {', '.join(f'{k} {v}' for k, v in trimmed.items())}" if trimmed else ""
        room = MAX_NOTES - len((header + "\n\n" + suffix).encode("utf-16-le")) // 2
        if len(body.encode("utf-16-le")) // 2 > room:
            trimmed["notes"] = 1
            suffix = f"\ntrimmed by the runner: {', '.join(f'{k} {v}' for k, v in trimmed.items())}"
            room = MAX_NOTES - len((header + "\n\n" + suffix).encode("utf-16-le")) // 2
        text = body.encode("utf-16-le")[:max(0, room) * 2].decode("utf-16-le", errors="ignore")
        args["notes"] = header + "\n\n" + text + suffix

    def size() -> int:
        return len(json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + PACKET_OVERHEAD

    notes()
    for name in ("files", "blockers", "dod"):
        args[name] = list(args[name])
        while args[name] and size() > PACKET_BUDGET:
            args[name].pop()
            trimmed[name] = trimmed.get(name, 0) + 1
            notes()
    while body and size() > PACKET_BUDGET:
        body = body[:len(body) // 2]
        trimmed["notes"] = 1
        notes()
    while len(args["goal"]) > 1 and size() > PACKET_BUDGET:
        args["goal"] = args["goal"][:max(1, len(args["goal"]) // 2)]
        trimmed["goal"] = 1
        notes()
    return size() <= PACKET_BUDGET


def escalation_owner(store: TaskStore, task: Mapping[str, Any]) -> str:
    """Who becomes accountable when the swarm gives up on `task`: A14 when a direct upstream task is A14's (the work
    order this task carries out), else A01."""
    for dep_id in task.get("depends_on") or []:
        try:
            if store.get(dep_id).get("agent_id") == MAINTENANCE:
                return MAINTENANCE
        except SwarmError:
            continue
    return ORCH


# --- the sending side: the runner's bridge ---------------------------------------------------------------------
class HandoffBridge:
    """The packets one runner process writes for one run (one correlation, one Graph ID).

    `dispatched` before a task's session starts, `gate_failed` and `escalated` from `reconcile` / `dispatchable`,
    `flush` once per round for packets the substrate did not take, `context` for the assignment prompt. Never raises
    into the runner: a handoff never changes a transition's outcome.
    """

    def __init__(self, store_path: str | Path, correlation_id: str, *, emit: Callable[[str, dict[str, Any]], object],
                 root: str | Path | None = None, env: Mapping[str, str] | None = None,
                 leases: substrate_lease.LeaseBridge | None = None):
        self.env = os.environ if env is None else env
        self.store_path, self.correlation_id, self.root, self.emit = Path(store_path), correlation_id, root, emit
        self.leases = leases  # substrate_lease.LeaseBridge, for the lease an escalation hands over
        self._lock = threading.Lock()
        self._known: set[tuple[str, str, str]] | None = None  # (sender surface, node, boundary key) in the ledger
        self._keys_revision, self._loaded_keys_revision = 0, -1
        self._inflight: set[tuple[str, str, str]] = set()  # reservations survive an uncertain write's cache invalidation
        self._pending: dict[str, Boundary] = {}  # not taken yet; retried by flush()
        self._warned: set[str] = set()
        self._said_unsigned = False
        self._graph_id: str | None = None
        self._local = threading.local()

    # -- plumbing
    def _store(self) -> TaskStore:
        store = getattr(self._local, "store", None)  # sqlite: one connection per thread
        if store is None:
            store = self._local.store = TaskStore(self.store_path)
        return store

    def graph_id(self) -> str | None:
        if self.leases is not None:
            return self.leases.graph_id()
        if self._graph_id is None:
            self._graph_id = substrate_tee.lookup_graph_id(self.correlation_id, root=self.root, env=self.env)
        return self._graph_id

    def _ledger_keys(self, graph: str) -> set[tuple[str, str, str]] | None:
        """Read persisted keys at startup and after uncertain writes, before any retry can append a duplicate.
        None when the list did not answer."""
        with self._lock:
            if self._known is not None and self._loaded_keys_revision == self._keys_revision:
                return self._known
            revision = self._keys_revision
        got = substrate_lease._call("coord_handoff_list", {"graph_id": graph}, ORCH, self.root, self.env)
        if got.status != "ok" or not isinstance(got.value, list):
            return None
        keys: set[tuple[str, str, str]] = set()
        for entry in got.value:
            if not isinstance(entry, dict) or trust_of(entry.get("verdict")) == REJECTED:
                continue
            packet = entry.get("packet")
            if not isinstance(packet, dict) or not _valid_packet(packet, graph):
                continue
            node = packet["node_id"]
            key, sender = boundary_key(packet), packet["from"]["surface"]
            # Only well-formed, accepted entries can reserve a sender's boundary.
            if (key and _round(key)
                    and isinstance(sender, str) and sender in substrate_tee.AGENT_SURFACES.values()):
                keys.add((sender, node, key))
        with self._lock:
            if self._known is None:
                self._known = keys
            else:
                self._known.update(keys)  # keep successful writes confirmed while this read was in flight
            if revision != self._keys_revision:
                return None  # another uncertain write occurred during the read; defer until a fresh snapshot
            self._loaded_keys_revision = revision
            return self._known

    def _unsent(self, b: Boundary, reason: str) -> str:
        b.lease = None  # a retry never hands a lease over: by then the escalation has released it the usual way
        with self._lock:
            self._pending[b.key] = b
            first = b.key not in self._warned
            self._warned.add(b.key)
        if first:
            self.emit("handoff.unsent", {"task_id": b.node_id, "boundary": b.key, "from": b.sender, "to": b.receiver,
                                         "reason": reason[:300]})
        return "unsent"

    def _refused(self, b: Boundary, reason: str) -> str:
        with self._lock:
            self._pending.pop(b.key, None)
        self.emit("handoff.refused", {"task_id": b.node_id, "boundary": b.key, "from": b.sender, "to": b.receiver,
                                      "reason": reason[:300]})
        return "refused"

    def send(self, b: Boundary) -> str:
        """Write one packet as its sender. Returns sent, duplicate (already in the ledger), unsent (queued for flush),
        refused, off or none (a party with no substrate surface)."""
        try:
            return self._send(b)
        except Exception as exc:  # noqa: BLE001 - fail open: a handoff never stops the run
            return self._unsent(b, f"internal: {exc}")

    def _send(self, b: Boundary) -> str:
        sender_surface = substrate_tee.AGENT_SURFACES.get(b.sender)
        receiver_surface = substrate_tee.AGENT_SURFACES.get(b.receiver)
        if sender_surface is None or receiver_surface is None:
            return "none"
        graph = self.graph_id()
        if graph is None:
            return self._unsent(b, "no Graph ID is bound to this run")
        # The sender is whoever the token is. On the runner's SUBSTRATE_TOKEN the packet would be recorded, and signed,
        # as the runner's, which is a false attribution with a valid seal on it: a sender without its own token (its
        # env file for this workspace, else its SUBSTRATE_TOKEN_<SURFACE>; workspace.credential) sends nothing.
        if not substrate_lease.has_own_token(b.sender, self.root, self.env):
            return self._refused(b, f"no token of its own for {sender_surface}: the packet would be recorded as the "
                                    "runner's")
        known = self._ledger_keys(graph)
        if known is None:
            return self._unsent(b, "coord_handoff_list did not answer")
        mark = (sender_surface, b.node_id, b.key)
        with self._lock:
            if mark in known or mark in self._inflight:
                self._pending.pop(b.key, None)
                return "duplicate"
            self._inflight.add(mark)
        try:
            return self._write(b, graph, sender_surface, receiver_surface, mark)
        finally:
            with self._lock:
                self._inflight.discard(mark)

    def _write(self, b: Boundary, graph: str, sender_surface: str, receiver_surface: str,
               mark: tuple[str, str, str]) -> str:
        args: dict[str, Any] = {"to": receiver_surface, "goal": b.goal, "files": b.files, "dod": b.dod, "blockers": b.blockers,
                "graph_id": graph, "node_id": b.node_id, "notes": b.notes,
                "session_id": substrate_tee.session_id(b.sender, graph, self.env)}
        if b.to_session:
            args["to_session_id"] = substrate_tee.session_id(b.receiver, graph, self.env)
        if b.lease is not None:  # released by the server only once the packet is durable; the packet never holds the id
            args.update(lease_id=b.lease.lease_id, release_lease=True)
        if not _fit_packet(args):
            return self._refused(b, "handoff routing fields exceed the packet budget")
        try:
            got = substrate_lease._call("coord_handoff", args, b.sender, self.root, self.env)
        except Exception:
            with self._lock:
                self._keys_revision += 1
            raise
        reply = got.value if got.status == "ok" and isinstance(got.value, dict) else None
        if reply is None or reply.get("stored") is not True:
            with self._lock:
                self._keys_revision += 1
            if got.status == "off":
                return "off"
            if got.status == "refused":
                return self._refused(b, got.detail or "refused")
            why = (reply or {}).get("error") or got.detail or got.status
            return self._unsent(b, f"{'not stored' if reply else got.status}: {why}")
        with self._lock:
            if self._known is not None:
                self._known.add(mark)
            self._pending.pop(b.key, None)
            say_unsigned = reply.get("signed") is not True and not self._said_unsigned
            self._said_unsigned = self._said_unsigned or say_unsigned
        recorded = ((reply.get("packet") or {}).get("from") or {}).get("surface")
        if recorded != sender_surface:  # a token configured under another agent's name
            self.emit("handoff.misattributed", {"task_id": b.node_id, "boundary": b.key, "from": b.sender,
                                                "expected": sender_surface, "recorded": recorded})
        if say_unsigned:
            self.emit("handoff.unsigned", {"task_id": b.node_id, "boundary": b.key,
                                           "reason": "no SUBSTRATE_HANDOFF_KEY on the substrate: packets are recorded "
                                                     "unsigned and the run carries on"})
        if b.lease is not None and reply.get("lease_released") is True and self.leases is not None:
            self.leases.handed_over(b.node_id)
        return "sent"

    def flush(self) -> int:
        """Send what the substrate did not take earlier. Returns how many landed."""
        with self._lock:
            pending = list(self._pending.values())
        return sum(self.send(b) == "sent" for b in pending)

    # -- the boundaries
    def dispatched(self, task: dict[str, Any], agent_id: str) -> list[str]:
        """Dependency boundaries of a task about to be dispatched to `agent_id`: one packet per upstream task owned by
        another agent, from that agent, carrying this task's goal and Definition of Done and the upstream's artifacts.
        Written once per edge: a rework or retry of the task finds them in the ledger."""
        try:
            store = self._store()
            out = []
            for dep_id in task["depends_on"]:
                dep = store.get(dep_id)
                sender = dep.get("agent_id")
                if not sender or sender == agent_id:
                    continue
                summary = str((dep["notes_json"].get("result") or {}).get("summary_md") or "").strip()
                # Initial task context precedes every rework; a redispatch must not give it a newer timestamp.
                out.append(self.send(boundary(
                    round_=f"dispatch@{task['task_id']}", part=f"dep:{dep_id}", sender=sender, receiver=agent_id,
                    node_id=task["task_id"], occurred_at=task["created_at"],
                    goal=f"{task['title'] or task['capability']} ({task['capability']}), building on {dep_id} "
                         f"({dep['title'] or dep['capability']}) from {sender}",
                    dod=dod_of(task), files=files_of(dep), blockers=[],
                    body=f"{sender} produced {dep_id} ({dep['state']}): " + (summary or "no summary was reported"))))
            return out
        except Exception as exc:  # noqa: BLE001
            self.emit("handoff.unsent", {"task_id": task.get("task_id"), "boundary": "dispatch",
                                         "reason": f"internal: {exc}"[:300]})
            return ["unsent"]

    def gate_failed(self, task_id: str, failing: list[str], verdicts: Mapping[str, dict[str, Any]], rework: int) -> list[str]:
        """CHANGES_REQUESTED → rework: one packet per failing gate, from the agent that issued the failing verdict to
        the producer, with the findings as blockers. `verdicts` are the rows `reconcile` acted on, read before the
        transition made them stale."""
        try:
            task = self._store().get(task_id)
            producer, out = task.get("agent_id"), []
            for gate in failing:
                row = verdicts.get(gate) or {}
                sender = _agent_in(row.get("agent_id")) or GATE_AGENTS.get(gate)
                if not producer or not sender or sender == producer:
                    continue
                findings = row.get("findings") or []
                out.append(self.send(boundary(
                    round_=f"rework{rework}@{task_id}", part=f"gate:{gate}", sender=sender, receiver=producer,
                    node_id=task_id, occurred_at=task["updated_at"],
                    goal=f"Rework {task['title'] or task_id}: the {gate} gate failed (rework {rework} of "
                         f"{MAX_REWORK_LOOPS})",
                    dod=dod_of(task), files=_merge(files_of(task), _finding_files(findings)),
                    blockers=[_finding_line(gate, f) for f in findings] or [f"{gate} gate failed with no findings"],
                    body=f"{sender} failed the {gate} gate on {task_id}. Address every blocker, then report IN_REVIEW.")))
            return out
        except Exception as exc:  # noqa: BLE001
            self.emit("handoff.unsent", {"task_id": task_id, "boundary": "rework", "reason": f"internal: {exc}"[:300]})
            return ["unsent"]

    def escalated(self, task_id: str, *, reason: str, failing: Sequence[str] = (),
                  verdicts: Mapping[str, dict[str, Any]] | None = None) -> str:
        """ESCALATED: the producer hands the task to A14 (an A14 work order) or A01. A lease it still holds (the rework
        cap is reached in review, where the producer holds the node) goes with the packet: the server releases it once
        the packet is durable, so the node never reads free without an account of why."""
        try:
            with self.leases.transition(task_id) if self.leases is not None else nullcontext():
                store = self._store()
                task = store.get(task_id)
                producer = task.get("agent_id")
                owner = escalation_owner(store, task)
                if not producer or producer == owner:
                    return "none"
                findings = {g: (verdicts or {}).get(g, {}).get("findings") or [] for g in failing}
                blockers = [_finding_line(g, f) for g, fs in findings.items() for f in fs] + [reason]
                lease = self.leases.held(task_id) if self.leases is not None else None
                return self.send(boundary(
                    round_=f"escalated-a{task['attempt']}r{task['rework_loops']}@{task_id}", part=producer,
                    sender=producer, receiver=owner, node_id=task_id, occurred_at=task["updated_at"],
                    goal=f"Take over {task['title'] or task_id}: {reason}",
                    dod=dod_of(task), files=_merge(files_of(task), *(_finding_files(fs) for fs in findings.values())),
                    blockers=blockers,
                    body=f"{producer} could not finish {task_id} (attempt {task['attempt']}, rework loops "
                         f"{task['rework_loops']}); the swarm escalated it. Claim the node with graph_claim before acting.",
                    to_session=False, lease=lease))
        except Exception as exc:  # noqa: BLE001
            self.emit("handoff.unsent", {"task_id": task_id, "boundary": "escalated", "reason": f"internal: {exc}"[:300]})
            return "unsent"

    # -- the receiving side, for the assignment prompt
    def context(self, task_id: str, agent_id: str) -> str:
        """The `## Handoffs` section of the task's assignment: its context rebuilt from the ledger, '' when there is
        nothing to show or the substrate does not answer."""
        try:
            graph = self.graph_id()
            return render(reconstruct(graph, task_id, agent=agent_id, workspace=self.root, env=self.env)) if graph else ""
        except Exception:  # noqa: BLE001 - fail open: the prompt keeps its Task Store sections
            return ""


# --- the receiving side --------------------------------------------------------------------------------------
@dataclass
class Context:
    """A task's context rebuilt from the ledger. `status` is ok, or the coord_handoff_list outcome that stopped it
    (unreachable, refused, error, off). goal and dod come from the newest usable boundary by signed transition time,
    not retry arrival; blockers come from its round and files from all usable packets. `packets` retains storage
    order, oldest first, with every verdict and trust, rejected rows included."""
    status: str
    graph_id: str
    node_id: str
    goal: str = ""
    dod: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    sender: str | None = None
    lease_handover: bool = False
    signed: bool = False  # every packet of the newest round verified
    packets: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""


def reconstruct(graph_id: str, node_id: str, *, agent: str, workspace: str | Path | None = None,
                env: Mapping[str, str] | None = None, events_limit: int = EVENTS_LIMIT) -> Context:
    """Rebuild `node_id`'s context as agent `agent` from two substrate reads, `coord_handoff_list` and `events_query`,
    and nothing else: no Task Store, no run log, no bus, no local cache (HAND-02). A packet whose signature does not
    verify is listed and never shapes the context. The reads carry `agent`'s own token for `workspace`."""
    e = os.environ if env is None else env
    if substrate_tee.AGENT_SURFACES.get(agent) is None:  # the reads go as the receiver, on its own token
        raise ValueError(f"no substrate surface for agent {agent!r}")
    listed = substrate_lease._call("coord_handoff_list", {"graph_id": graph_id}, agent, workspace, e)
    if listed.status != "ok" or not isinstance(listed.value, list):
        return Context("error" if listed.status == "ok" else listed.status, graph_id, node_id,
                       detail=listed.detail or "coord_handoff_list gave no list")
    queried = substrate_lease._call("events_query", {"graph_id": graph_id, "node_id": node_id, "order": "desc",
                                                     "limit": events_limit}, agent, workspace, e)
    events = queried.value.get("events") if queried.status == "ok" and isinstance(queried.value, dict) else None
    packets = []
    for entry in listed.value:
        packet = entry.get("packet") if isinstance(entry, dict) else None
        if not isinstance(packet, dict) or packet.get("node_id") != node_id:
            continue
        verdict = entry.get("verdict") if isinstance(entry.get("verdict"), dict) else {"ok": False, "reason": "malformed"}
        if not _valid_packet(packet, graph_id):
            verdict = {"ok": False, "reason": "malformed"}
        origin, destination = packet.get("from"), packet.get("to")
        packets.append({"event_id": entry.get("event_id"), "ts": str(entry.get("ts") or packet.get("ts") or ""),
                        "from": origin.get("surface") if isinstance(origin, dict) else None,
                        "to": destination.get("surface") if isinstance(destination, dict) else None,
                        "boundary": boundary_key(packet), "trust": trust_of(verdict), "verdict": verdict,
                        "packet": packet})
    packets.sort(key=lambda p: p["ts"])  # stable: the list's own order breaks ties
    ctx = Context("ok", graph_id, node_id, packets=packets, events=events if isinstance(events, list) else [],
                  detail="" if isinstance(events, list) else f"events_query {queried.status} {queried.detail}".strip())
    usable = [p for p in packets if p["trust"] != REJECTED]
    if usable:
        newest = max(usable, key=lambda p: (_boundary_time(p["packet"]), p["ts"]))
        rnd = _round(newest["boundary"])
        current = [p for p in usable if rnd is not None and _round(p["boundary"]) == rnd] or [newest]
        ctx.goal = str(newest["packet"].get("goal") or "")
        ctx.dod = _lines(newest["packet"].get("dod"), 10_000)
        ctx.blockers = _merge(*(_lines(p["packet"].get("blockers"), 10_000) for p in current))
        ctx.files = _merge(*(_lines(p["packet"].get("files"), 10_000) for p in usable))
        ctx.sender = newest["from"]
        ctx.lease_handover = newest["packet"].get("lease_handover") is True
        ctx.signed = all(p["trust"] == VERIFIED for p in current)
    return ctx


def render(ctx: Context) -> str:
    """The context as a prompt section; '' when there is no packet for the node or the ledger did not answer."""
    if ctx.status != "ok" or not ctx.packets:
        return ""
    out = ["\n## Handoffs (from the substrate ledger)",
           f"Rebuilt from coord_handoff_list and events_query for node {ctx.node_id} of graph {ctx.graph_id}. "
           "Claim the node with graph_claim before acting; a packet never says who holds it."]
    if ctx.goal:
        auth = "signed and verified" if ctx.signed else "UNSIGNED (no handoff key on the substrate): unauthenticated"
        out += [f"goal ({ctx.sender}, {auth}): {ctx.goal}"]
        out += ["Definition of Done:"] + [f"- {d}" for d in ctx.dod or ["(none given)"]]
        out += ["Blockers:"] + [f"- {b}" for b in ctx.blockers or ["(none known)"]]
        if ctx.files:
            out.append("Files: " + ", ".join(ctx.files))
    rejected = [p for p in ctx.packets if p["trust"] == REJECTED]
    if rejected:
        out.append(f"{len(rejected)} packet(s) failed verification and were ignored: "
                   + ", ".join(f"{p['from']} {p['verdict'].get('reason')}" for p in rejected))
    out.append("Packets, oldest first:")
    out += [f"- {p['ts']} {p['from']} → {p['to']} [{p['boundary'] or 'no boundary'}] {p['trust']}" for p in ctx.packets]
    return "\n".join(out) + "\n"
