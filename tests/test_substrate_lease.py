"""ADR 0001 S2 / LEASE-05..09: the runner holds each swarm task's substrate lease for the agent working it.

The substrate is a fake behind `substrate_client._open` that keeps substrate-mcp's lease rules (lease.ts plus the Phase 11
contract: swarm session shape, holder-only `lease_id`, `not-holder`) on a clock the test moves. No socket is opened except
by the one test that runs a real agent session process."""
import importlib.util
import io
import itertools
import json
import os
import re
import sys
import threading
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from conftest import ROOT

from swarm import substrate_client, substrate_lease as lease_mod, substrate_tee as tee_mod, workspace as ws_mod
from swarm.errors import ErrorCode, SwarmError
from swarm.manifest import load_manifest
from swarm.taskstore import TaskStore

GID = "ut-mabc123-0123abcd"
CORR = "corr-s2"
TOKENS = {f"tok-{s}": s for s in tee_mod.AGENT_SURFACES.values()}
SESSION_RE = re.compile(r"(A\d{2})@([A-Za-z0-9._-]{1,64}):(.+)")


# --- fake substrate ------------------------------------------------------------------------------------------
class _Resp(io.BytesIO):
    def __init__(self, body: str, status: int = 200):
        super().__init__(body.encode("utf-8"))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@dataclass
class Err:
    """An `isError` tool reply. `refusal()` in mcp.ts words its text as JSON {"error": …}; a thrown error is plain text."""
    text: str


def _refusal(msg: str) -> Err:
    return Err(json.dumps({"error": msg}))


def _refuse_tool(fake, monkeypatch, tool, refusal):
    """Exercise the HTTP and MCP identity-refusal paths without replacing their outcome classification."""
    if refusal == "identity":
        monkeypatch.setitem(fake.forced, tool, _refusal("caller identity is not authorized"))
        return

    def denied(req, timeout=None):
        if urlparse(req.full_url).path == "/mcp" and json.loads(req.data)["params"]["name"] == tool:
            raise urllib.error.HTTPError(req.full_url, refusal, "denied", Message(), io.BytesIO(b'{"error":"unauthorized"}'))
        return fake(req, timeout)

    monkeypatch.setattr(substrate_client, "_open", denied)



def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class FakeSubstrate:
    def __init__(self):
        self._now = 1_700_000_000.0
        self.clock = None  # a callable that replaces the stepped clock (a test's simulated clock that runs on its own)
        self.gate = None  # a callable each call waits in before it is served; the many-leases test drives time with it
        self.lock = threading.Lock()
        self.nodes: dict[tuple[str, str], dict] = {}
        self.trail: list[dict] = []  # what the server writes to the ledger: claim / warning / note
        self.calls: list[dict] = []
        self.down = False
        self.forced: dict[str, object] = {}  # tool -> canned reply
        self.grant_ttl: int | None = None  # a server that grants (and enforces) another TTL than the one asked for
        self._ids = itertools.count(1)

    @property
    def now(self) -> float:
        return self.clock() if self.clock is not None else self._now

    @now.setter
    def now(self, value: float) -> None:
        self._now = value

    def register(self, *node_ids, graph=GID):
        for n in node_ids:
            self.nodes.setdefault((graph, n), {"lease_id": None, "surface": None, "session": None, "epoch": 0,
                                               "expires": 0.0, "state": None})

    def node(self, node_id, graph=GID):
        return self.nodes[(graph, node_id)]

    def at(self, tool):
        return [c for c in self.calls if c["tool"] == tool]

    def trail_of(self, node_id):
        return [(e["kind"], e["action"]) for e in self.trail if e["node"] == node_id]

    def force_release(self, node_id):
        n = self.node(node_id)
        self.trail.append({"kind": "warning", "action": "forced", "node": node_id, "session": n["session"],
                           "lease_id": n["lease_id"]})
        n.update(lease_id=None, surface=None, session=None, expires=0.0)

    def __call__(self, req, timeout=None):
        if self.down:
            raise OSError("connection refused")
        path = urlparse(req.full_url).path
        if path != "/mcp":  # the tee's /events and the memory brief: accepted / absent, never what is under test
            return _Resp('{"ok":true}', 200) if path == "/events" else _Resp("{}", 404)
        body = json.loads(req.data)
        name, args = body["params"]["name"], body["params"]["arguments"]
        token = {k.lower(): v for k, v in req.header_items()}.get("authorization", "").removeprefix("Bearer ")
        caller = TOKENS.get(token)
        if self.gate is not None:
            self.gate()  # the simulated latency: a round of concurrent calls returns together (see the many-leases test)
        with self.lock:
            self.calls.append({"tool": name, "args": args, "caller": caller, "at": self.now})
            out = self.forced[name] if name in self.forced else getattr(self, "tool_" + name)(args, caller)
        if isinstance(out, Err):
            result = {"content": [{"type": "text", "text": out.text}], "isError": True}
        else:
            result = {"content": [{"type": "text", "text": json.dumps(out)}]}
        return _Resp(json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}))

    # -- tools
    def tool_graph_bind(self, a, caller):
        return {"status": "existing", "correlation_id": a["correlation_id"], "graph_id": GID} if a.get("correlation_id") == CORR \
            else {"status": "unbound", "correlation_id": a.get("correlation_id"), "graph_id": None}

    def tool_graph_register(self, a, caller):
        self.register(*(n["node_id"] for n in a.get("nodes", [])), graph=a["graph_id"])
        return {"graph_id": a["graph_id"], "registered": len(a.get("nodes", []))}

    def tool_graph_claim(self, a, caller):
        if a.get("surface") and caller and a["surface"] != caller:
            return _refusal(f"surface {a['surface']} is not this token's surface")
        surface = a.get("surface") or caller
        if surface.startswith("swarm-"):  # LEASE-01: a swarm session names this token's agent, a replica and this graph
            m = SESSION_RE.fullmatch(a["session_id"])
            if m is None or tee_mod.AGENT_SURFACES.get(m.group(1)) != surface or m.group(3) != a["graph_id"]:
                return _refusal("a swarm session must be <AGENT>@<replica>:<graph_id> for this surface and graph")
        node = self.nodes.get((a["graph_id"], a["node_id"]))
        if node is None:
            return Err(f"unknown node {a['graph_id']}/{a['node_id']}")
        ttl = a.get("ttl_seconds", 900)
        if not 30 <= ttl <= 86_400:
            return Err("ttl_seconds: out of range")
        ttl = self.grant_ttl or ttl  # the hold the server grants, enforced as well as reported
        live = node["lease_id"] is not None and node["expires"] > self.now
        same = node["surface"] == surface and node["session"] == a["session_id"]
        if live and not same:
            return self._claim_reply(False, node, "denied", ttl)
        if live:
            action = "renewed"
        else:
            action = "granted" if node["lease_id"] is None else ("reclaimed" if same else "stolen")
            node.update(lease_id=f"lse_{next(self._ids)}", surface=surface, session=a["session_id"], epoch=node["epoch"] + 1)
            self.trail.append({"kind": "warning" if action == "stolen" else "claim", "action": action,
                               "node": a["node_id"], "session": a["session_id"], "lease_id": node["lease_id"]})
        node["expires"] = self.now + ttl
        return self._claim_reply(True, node, action, ttl)

    def _claim_reply(self, claimed, node, action, ttl):
        lease = {"lease_id": node["lease_id"] if claimed else None,  # LEASE-02: a denied claim never shows the holder's id
                 "holder": {"surface": node["surface"], "session_id": node["session"]}, "epoch": node["epoch"],
                 "state": "held", "expires_at": _iso(node["expires"])}
        # the server's reply: `action` is what the index did, `decision` the verdict once the ledger is accounted
        # for, `ttl_seconds` the granted TTL (null when no hold stands)
        return {"claimed": claimed, "lease": lease, "action": action, "decision": "granted" if claimed else "denied",
                "reason": None if claimed else "held by another session", "ttl_seconds": ttl if claimed else None}

    def tool_graph_heartbeat(self, a, caller):
        node = self.node(a["node_id"], a["graph_id"])
        if node["lease_id"] is None:
            return {"ok": False, "reason": "unheld"}
        if caller and caller != node["surface"]:
            return {"ok": False, "reason": "not-holder"}
        if node["lease_id"] != a["lease_id"]:
            return {"ok": False, "reason": "lost"}
        if node["expires"] <= self.now:
            return {"ok": False, "reason": "expired"}
        ttl = self.grant_ttl or a.get("ttl_seconds", 900)
        node["expires"] = self.now + ttl
        return {"ok": True, "ttl_seconds": ttl}

    def _drop(self, a, caller, completing):
        node = self.node(a["node_id"], a["graph_id"])
        if node["lease_id"] is None:
            return ({"completed": False, "released": True, "action": "noop", "reason": "unheld"} if completing
                    else {"released": True, "action": "noop"})
        if caller and caller != node["surface"]:
            return {"released": False, "completed": False, "reason": "not-holder"}
        if node["lease_id"] != a.get("lease_id"):
            return {"released": False, "completed": False, "reason": "stale-lease"}
        self.trail.append({"kind": "note", "action": "completed" if completing else "released", "node": a["node_id"],
                           "session": node["session"], "lease_id": node["lease_id"]})
        node.update(lease_id=None, surface=None, session=None, expires=0.0)
        if completing:
            node["state"] = "completed"
        return {"released": True, "action": "released", **({"completed": True} if completing else {})}

    def tool_graph_release(self, a, caller):
        return self._drop(a, caller, completing=False)

    def tool_graph_complete(self, a, caller):
        return self._drop(a, caller, completing=True)


# --- fixtures and helpers --------------------------------------------------------------------------------------
_PINNED = {k: v for k, v in sys.modules.items() if k == "swarm" or k.startswith("swarm.")}
_REAL_OPEN = substrate_client._open


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for k, v in _PINNED.items():  # other modules' swarm_dir fixture reloads swarm.*; keep the objects patched here
        monkeypatch.setitem(sys.modules, k, v)
    for k in [k for k in os.environ if k.startswith(("SUBSTRATE_", "SWARM_")) or k.lower().endswith("_proxy")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.setenv("SWARM_PURE_PYTHON", "1")  # the oracle switch survives the scrub: child scripts import the pure modules
    monkeypatch.setenv("SWARM_SIGNING_KEY", "runner-secret")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    substrate_client.reset()
    tee_mod.reset()
    yield
    substrate_client.reset()
    tee_mod.reset()


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test:8787")
    monkeypatch.setenv("SUBSTRATE_TOKEN", "tok-swarm-a01-orch")  # the orchestrator's own token
    # and each agent's, where S4 delivers it: its env file for the workspace (here tmp_path), never the runner's env
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for agent in load_manifest():
        ws_mod.write_token(ws_mod.env_file(tmp_path, agent["slug"]), f"tok-{tee_mod.AGENT_SURFACES[agent['id']]}")
    f = FakeSubstrate()
    f.workspace = tmp_path
    monkeypatch.setattr(substrate_client, "_open", f)
    return f


@pytest.fixture()
def queued_fake(fake, monkeypatch):
    """Use the real bounded transport/worker path, with only its urllib exchange faked."""
    monkeypatch.setattr(substrate_client, "_open", _REAL_OPEN)
    monkeypatch.setattr(substrate_client.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=fake))
    monkeypatch.setattr(substrate_client, "_slots", threading.BoundedSemaphore(substrate_client.MAX_IN_FLIGHT))
    yield fake
    assert substrate_client._wait_idle(2)


def _store(tmp_path, name="tasks.db") -> TaskStore:
    return TaskStore(tmp_path / ".swarm" / name)


def _plan(store, tid="T-be", agent="A05", gates=(), **notes) -> dict:
    store.create(task_id=tid, correlation_id=CORR, capability="code.backend", agent_id=agent,
                 notes={"gates": list(gates), **notes})
    store.transition(tid, "VALIDATED")
    store.transition(tid, "PLANNED")
    return store.get(tid)


def _bridge(store, events, env=None, **kw) -> lease_mod.LeaseBridge:
    kw.setdefault("root", store.path.parent.parent)  # the workspace whose agent env files the fixture wrote
    return lease_mod.LeaseBridge(store.path, CORR, emit=lambda t, p: events.append((t, p)), env=env, **kw)


def _replica(name):
    return {**os.environ, "SWARM_REPLICA": name}


def _types(events):
    return [t for t, _ in events]


def _load(script):
    spec = importlib.util.spec_from_file_location(f"{script}_s2_under_test", ROOT / "scripts" / f"{script}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def runner():
    mod = _load("swarm_run")
    # a fixed, unsigned envelope so the test reads task.assign back out of the prompt
    mod.build_envelope = lambda **kw: {"msg_type": kw["msg_type"], "payload": kw["payload"]}
    mod.sign_envelope = lambda env, **kw: env
    return mod


def _args(**kw):
    return SimpleNamespace(dry_run=False, task_timeout=30, runtime="claude", claude_bin="claude", grok_bin="grok",
                           omp_bin="omp", permission_mode="acceptEdits", max_turns=5, model="", allowed_tools="", **kw)


def _assign(prompt: str) -> dict:
    return json.loads(re.search(r"```json\n(.*?)\n```", prompt, re.S).group(1))["payload"]


def _session(seen: dict, state="IN_REVIEW"):
    """Stands in for run_agent_headless: records the prompt, registers with the keeper like the real one, reports `state`."""
    def run(agent, prompt, repo, args, *, on_session=None):
        seen["prompt"] = prompt
        if on_session is not None:
            on_session(lambda reason: seen.setdefault("stopped", reason))
            on_session(None)
        tid = re.search(r'"task_id": "([^"]+)"', prompt).group(1)
        if state is None:
            return "the session printed no result", {"returncode": 0}
        return "done\n```json\n" + json.dumps({"task_id": tid, "state": state, "summary_md": "ok"}) + "\n```", {"returncode": 0}
    return run


def _execute(runner, store, task, bridge, tmp_path, events, args=None):
    repo = tmp_path / "work"
    repo.mkdir(exist_ok=True)
    agent = {"id": task["agent_id"], "slug": "a05-backend"}
    ctx = SimpleNamespace(emit=lambda t, p, **k: events.append((t, p)))
    return runner.execute_one(store.path, task, agent, args or _args(), ctx, repo, bridge)


# --- LEASE-05: claim before dispatch ---------------------------------------------------------------------------
def test_grant_dispatches_with_lease_s_from_the_granted_ttl(tmp_path, fake, runner, monkeypatch):
    monkeypatch.setenv("SWARM_LEASE_TTL_S", "120")
    fake.grant_ttl = 90  # the server's grant wins over what was asked for
    fake.register("T-be")
    store, events = _store(tmp_path), []
    task = _plan(store)
    bridge = _bridge(store, events)
    batch, waiting = runner.select_batch(store, [task], 3, bridge)
    assert [t["task_id"] for t, _ in batch] == ["T-be"] and waiting == []
    (claim,) = fake.at("graph_claim")
    assert claim["caller"] == "swarm-a05-be"  # A05's own token, not the runner's
    assert claim["args"] == {"graph_id": GID, "node_id": "T-be", "session_id": f"A05@r0:{GID}", "ttl_seconds": 120,
                             "surface": "swarm-a05-be"}
    seen = {}
    monkeypatch.setattr(runner, "run_agent_headless", _session(seen))
    _, _, outcome = _execute(runner, store, task, bridge, tmp_path, events)
    assert outcome == "IN_REVIEW"
    assert _assign(seen["prompt"])["lease_s"] == 90
    node = fake.node("T-be")
    mirror = store.get("T-be")["notes_json"]["lease"]
    assert mirror["state"] == "held" and mirror["lease_id"] == node["lease_id"] and node["session"] == f"A05@r0:{GID}"
    assert node["expires"] == fake.now + 90  # the server holds it for the TTL it granted, not the one asked for
    assert bridge.held("T-be") is not None  # IN_REVIEW keeps the lease for DONE / CHANGES_REQUESTED
    assert not [t for t in _types(events) if t.startswith("lease.")]


def test_denied_claim_leaves_the_task_undispatched(tmp_path, fake, runner):
    fake.register("T-be")
    other = _store(tmp_path, "other.db")
    assert _bridge(other, [], env=_replica("r9")).acquire(_plan(other), "A05").status == "held"
    store, events = _store(tmp_path), []
    task = _plan(store)
    batch, waiting = runner.select_batch(store, [task], 3, _bridge(store, events))
    assert batch == [] and waiting == [f"T-be: denied (held by A05@r9:{GID})"]
    t = store.get("T-be")
    assert t["state"] == "PLANNED" and t["attempt"] == 0 and [h["to_state"] for h in store.history("T-be")][-1] == "PLANNED"
    assert t["notes_json"]["lease"]["state"] == "denied"
    assert t["notes_json"]["lease"]["holder"] == {"surface": "swarm-a05-be", "session_id": f"A05@r9:{GID}"}
    assert _types(events) == ["lease.denied"]


def test_a_denied_task_does_not_take_a_parallel_slot(tmp_path, fake, runner):
    fake.register("T-a", "T-b")
    other = _store(tmp_path, "other.db")
    _bridge(other, [], env=_replica("r9")).acquire(_plan(other, "T-a"), "A05")
    store = _store(tmp_path)
    ready = [_plan(store, "T-a"), _plan(store, "T-b")]
    batch, waiting = runner.select_batch(store, ready, 1, _bridge(store, []))
    assert [t["task_id"] for t, _ in batch] == ["T-b"] and len(waiting) == 1


def test_refused_claim_is_not_dispatched(tmp_path, fake, runner, monkeypatch):
    # A05's token was never delivered: no env file and no SUBSTRATE_TOKEN_SWARM_A05_BE. The runner's own SUBSTRATE_TOKEN
    # (A01's) does not stand in, so nothing reaches the server and the task is not run as the
    # misconfigured-but-looks-healthy agent the ADR warns about.
    ws_mod.env_file(tmp_path, "a05-backend").unlink()
    fake.register("T-be")
    store, events = _store(tmp_path), []
    batch, waiting = runner.select_batch(store, [_plan(store)], 3, _bridge(store, events))
    assert batch == [] and waiting == ["T-be: refused"]
    assert fake.at("graph_claim") == []
    assert store.get("T-be")["state"] == "PLANNED" and store.get("T-be")["notes_json"]["lease"]["state"] == "refused"
    assert _types(events) == ["lease.refused"]
    assert "a05-backend.env does not exist" in events[0][1]["reason"]


def test_without_an_env_file_the_agents_own_server_side_variable_is_used(tmp_path, fake, monkeypatch):
    # a pre-S4 runner given every agent's token under the server's names still claims as A05: the same secret, the
    # second step of the one lookup (workspace.credential) that the session, the handoff and the tee share
    ws_mod.env_file(tmp_path, "a05-backend").unlink()
    monkeypatch.setenv("SUBSTRATE_TOKEN_SWARM_A05_BE", "tok-swarm-a05-be")
    fake.register("T-be")
    store = _store(tmp_path)
    assert _bridge(store, []).acquire(_plan(store), "A05").status == "held"
    assert [c["caller"] for c in fake.at("graph_claim")] == ["swarm-a05-be"]


def test_lease_calls_carry_the_token_in_the_agents_env_file(tmp_path, fake):
    # INST-03: the runner's environment holds only A01's token, yet the claim arrives as A05, because the lease call
    # reads A05's env file for the workspace, the same file A05's own session gets its SUBSTRATE_TOKEN from
    assert not [k for k in os.environ if k.startswith("SUBSTRATE_TOKEN_")]
    fake.register("T-be")
    store = _store(tmp_path)
    assert _bridge(store, []).acquire(_plan(store), "A05").status == "held"
    assert [c["caller"] for c in fake.at("graph_claim")] == ["swarm-a05-be"]


def test_an_env_file_others_can_read_is_not_used(tmp_path, fake):
    ws_mod.env_file(tmp_path, "a05-backend").chmod(0o644)
    fake.register("T-be")
    store, events = _store(tmp_path), []
    assert _bridge(store, events).acquire(_plan(store), "A05").status == "refused"
    assert fake.at("graph_claim") == [] and "must be 0600" in events[0][1]["reason"]


@pytest.mark.parametrize("refusal", [401, 403, "identity"])
@pytest.mark.parametrize("negative_cache", [False, True])
def test_graph_lookup_refusal_never_dispatches_unleased(tmp_path, fake, runner, monkeypatch, refusal, negative_cache):
    fake.register("T-be")
    store, events = _store(tmp_path), []
    task = _plan(store)
    if negative_cache:
        fake.forced["graph_bind"] = {"status": "unbound", "graph_id": None}
        assert tee_mod.lookup_graph_id(CORR, root=tmp_path) is None
        del fake.forced["graph_bind"]
    bridge = _bridge(store, events, root=tmp_path)
    with monkeypatch.context() as patch:
        _refuse_tool(fake, patch, "graph_bind", refusal)
        batch, waiting = runner.select_batch(store, [task], 1, bridge)
        assert batch == [] and waiting == ["T-be: refused"]
        mirror = store.get("T-be")["notes_json"]["lease"]
        assert mirror["state"] == "refused"
        assert ("identity" if refusal == "identity" else f"HTTP {refusal}") in mirror["reason"]
        assert store.get("T-be")["state"] == "PLANNED"
        assert _types(events) == ["lease.refused"] and fake.at("graph_claim") == []
    # Neither a preceding tee lookup nor the refusal itself may suppress recovery after credentials are repaired.
    batch, waiting = runner.select_batch(store, [task], 1, bridge)
    assert [t["task_id"] for t, _ in batch] == ["T-be"] and waiting == []
    assert bridge.held("T-be") is not None


@pytest.mark.parametrize("failure", ["unbound", "down", "server-error"])
def test_graph_lookup_without_auth_refusal_still_dispatches_unleased(tmp_path, fake, runner, failure):
    store, events = _store(tmp_path), []
    task = _plan(store)
    if failure == "down":
        fake.down = True
    else:
        fake.forced["graph_bind"] = ({"status": "unbound", "graph_id": None} if failure == "unbound"
                                     else Err("database unavailable"))
    batch, waiting = runner.select_batch(store, [task], 1, _bridge(store, events))
    assert [t["task_id"] for t, _ in batch] == ["T-be"] and waiting == []
    assert store.get("T-be")["notes_json"]["lease"]["state"] == "unleased"
    assert _types(events) == ["lease.unleased"]


def test_saturated_slots_wait_then_dispatch_with_a_lease(tmp_path, queued_fake, runner):
    queued_fake.register("T-be")
    store, events = _store(tmp_path), []
    task = _plan(store)
    bridge = _bridge(store, events)
    assert bridge.graph_id() == GID
    slots = substrate_client._slots
    for _ in range(substrate_client.MAX_IN_FLIGHT):
        assert slots.acquire(blocking=False)
    released = threading.Timer(0.1, slots.release)
    released.start()
    started = time.monotonic()
    try:
        batch, waiting = runner.select_batch(store, [task], 1, bridge)
        assert time.monotonic() - started >= 0.08
        assert len(batch) == 1 and waiting == []
        assert bridge.held("T-be") is not None
        assert store.get("T-be")["notes_json"]["lease"]["state"] == "held"
        assert substrate_client._down_until == 0
        assert "lease.unleased" not in _types(events)
    finally:
        released.join()
        for _ in range(substrate_client.MAX_IN_FLIGHT - 1):
            slots.release()


@pytest.mark.parametrize("cached_graph", [False, True])
def test_saturated_slots_defer_claim_without_unleased_dispatch(tmp_path, queued_fake, runner, monkeypatch, cached_graph):
    queued_fake.register("T-be")
    # Another replica already holds the node; local congestion must not bypass that fact.
    claim, _ = lease_mod.claim_node("A05", GID, "T-be", ttl_s=900, workspace=queued_fake.workspace, env=_replica("other"))
    assert claim.status == "held"
    store, events = _store(tmp_path), []
    task = _plan(store)
    bridge = _bridge(store, events)
    if cached_graph:
        assert bridge.graph_id() == GID
    monkeypatch.setattr(substrate_client, "TIMEOUT_S", 0.1)
    monkeypatch.setattr(substrate_client, "DEADLINE_SLACK_S", 0.05)
    slots = substrate_client._slots
    for _ in range(substrate_client.MAX_IN_FLIGHT):
        assert slots.acquire(blocking=False)
    before = len(queued_fake.calls)
    try:
        batch, waiting = runner.select_batch(store, [task], 1, bridge)
        assert batch == [] and waiting == ["T-be: busy"]
        assert store.get("T-be")["state"] == "PLANNED"
        assert bridge.held("T-be") is None and "lease.unleased" not in _types(events)
        assert len(queued_fake.calls) == before and substrate_client._down_until == 0
        assert CORR not in tee_mod._unbound
    finally:
        for _ in range(substrate_client.MAX_IN_FLIGHT):
            slots.release()
    # No outage/negative lookup cache was poisoned: the next round reaches the actual holder.
    batch, waiting = runner.select_batch(store, [task], 1, bridge)
    assert batch == [] and waiting == [f"T-be: denied (held by A05@other:{GID})"]


@pytest.mark.parametrize("tool", ["graph_claim", "coord_handoff"])
def test_busy_mcp_outcome_and_event_contention_never_back_off(queued_fake, monkeypatch, tool):
    monkeypatch.setattr(substrate_client, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(substrate_client, "DEADLINE_SLACK_S", 0.05)
    slots = substrate_client._slots
    for _ in range(substrate_client.MAX_IN_FLIGHT):
        assert slots.acquire(blocking=False)
    try:
        assert substrate_client.mcp_call_outcome(tool, {}).status == "busy"
        assert substrate_client.rest_post("/events", {}) is None
        assert substrate_client._down_until == 0 and queued_fake.calls == []
    finally:
        for _ in range(substrate_client.MAX_IN_FLIGHT):
            slots.release()
    assert substrate_client.rest_post("/events", {}) == (200, '{"ok":true}')


@pytest.mark.parametrize("failure", ["down", "server-error"])
def test_unreachable_substrate_dispatches_unleased_and_records_it(tmp_path, fake, runner, monkeypatch, failure):
    fake.register("T-be")
    store, events = _store(tmp_path), []
    task = _plan(store)
    bridge = _bridge(store, events)  # the Graph ID is resolved before the outage
    assert bridge.graph_id() == GID
    if failure == "down":
        fake.down = True
    else:
        fake.forced["graph_claim"] = Err("graph_claim requires SUBSTRATE_PG_URL")
    batch, waiting = runner.select_batch(store, [task], 3, bridge)
    assert len(batch) == 1 and waiting == []
    mirror = store.get("T-be")["notes_json"]["lease"]
    assert mirror["state"] == "unleased"
    assert ("unreachable" if failure == "down" else "SUBSTRATE_PG_URL") in mirror["reason"]
    assert _types(events) == ["lease.unleased"]
    if failure == "down":
        assert substrate_client._down_until > time.monotonic()
        fake.down = False
        before = len(fake.calls)
        assert substrate_client.mcp_call_outcome("graph_claim", {}).status == "unreachable"
        assert len(fake.calls) == before  # a real outage, unlike contention, suppresses another request
    seen = {}
    monkeypatch.setattr(runner, "run_agent_headless", _session(seen))
    _, _, outcome = _execute(runner, store, task, bridge, tmp_path, events)
    assert outcome == "IN_REVIEW"  # the run carries on as before (LEASE-09)
    assert _assign(seen["prompt"])["lease_s"] == task["budget"]["max_wall_s"]  # no grant: the old meaning


def test_an_unregistered_node_is_registered_as_the_orchestrator_then_claimed(tmp_path, fake):
    store, events = _store(tmp_path), []
    task = _plan(store, "T-qa.r1")  # a gate rerun, created after orch_plan registered the node set
    claim = _bridge(store, events).acquire(task, "A05")
    assert claim.status == "held"
    (reg,) = fake.at("graph_register")
    assert reg["caller"] == "swarm-a01-orch" and reg["args"] == {"graph_id": GID, "nodes": [{"node_id": "T-qa.r1"}]}
    assert len(fake.at("graph_claim")) == 2


# --- LEASE-06: heartbeats and the refusal table ------------------------------------------------------------------
def _held(tmp_path, fake, events, tid="T-be", state="IN_PROGRESS", **kw):
    fake.register(tid)
    store = _store(tmp_path)
    task = _plan(store, tid)
    bridge = _bridge(store, events, clock=lambda: fake.now, **kw)
    assert bridge.acquire(task, "A05").status == "held"
    for s in ("CLAIMED", "IN_PROGRESS", "IN_REVIEW")[: ("CLAIMED", "IN_PROGRESS", "IN_REVIEW").index(state) + 1]:
        store.transition(tid, s)
    return store, bridge


def _steal(fake, tid="T-be", replica="r2", ttl=lease_mod.DEFAULT_TTL_S, agent="A05"):
    """The runner's lease lapses and another replica of the same agent class takes the node."""
    fake.now += ttl + 1
    claim = lease_mod.claim_node(agent, GID, tid, ttl_s=ttl, workspace=fake.workspace, env=_replica(replica))[0]
    assert claim.status == "held" and claim.reason == "stolen"


def test_heartbeat_extends_and_keeps_the_session(tmp_path, fake):
    events = []
    store, bridge = _held(tmp_path, fake, events)
    stops = []
    bridge.watch("T-be", stops.append)
    before = fake.node("T-be")["expires"]
    fake.now += 200
    assert bridge.beat("T-be") == "ok" and fake.node("T-be")["expires"] == fake.now + lease_mod.DEFAULT_TTL_S > before
    assert stops == [] and events == []


@pytest.mark.parametrize("reason", ["lost", "unheld", "not-holder"])
def test_a_refusal_stops_the_session_with_its_tabled_reason(tmp_path, fake, monkeypatch, reason):
    events = []
    store, bridge = _held(tmp_path, fake, events)
    stops = []
    bridge.watch("T-be", stops.append)
    if reason == "lost":
        _steal(fake)
    elif reason == "unheld":
        fake.force_release("T-be")  # an operator prised it loose: the session must not carry on
    else:  # A05's env file now holds a token that names another surface
        ws_mod.write_token(ws_mod.env_file(fake.workspace, "a05-backend"), "tok-swarm-a06-fe")
    assert bridge.beat("T-be") == "stopped"
    assert stops == [lease_mod.SESSION_REFUSALS[reason]]
    assert stops[0].startswith("E-POLICY" if reason == "not-holder" else "E-TIMEOUT")
    assert bridge.held("T-be") is None
    assert store.get("T-be")["notes_json"]["lease"]["state"] == "lost"
    assert events == [("lease.lost", {"task_id": "T-be", "agent": "A05", "reason": reason, "session_stopped": True})]


@pytest.mark.parametrize("refusal", [401, 403, "identity"])
def test_auth_refused_heartbeat_stops_dispatch_instead_of_retrying(tmp_path, fake, monkeypatch, refusal):
    events, stops = [], []
    store, bridge = _held(tmp_path, fake, events)
    bridge.watch("T-be", stops.append)
    _refuse_tool(fake, monkeypatch, "graph_heartbeat", refusal)
    assert bridge.beat("T-be") == "stopped"
    assert stops == [lease_mod.SESSION_REFUSALS[lease_mod.REFUSED]]
    assert bridge.held("T-be") is None
    assert store.get("T-be")["notes_json"]["lease"]["state"] == "lost"
    assert events == [("lease.lost", {"task_id": "T-be", "agent": "A05", "reason": "refused",
                                     "session_stopped": True})]
    assert len(fake.at("graph_claim")) == 1  # authorization failures never re-claim


def test_expired_reclaims_instead_of_beating_and_the_session_carries_on(tmp_path, fake):
    events = []
    store, bridge = _held(tmp_path, fake, events)
    stops = []
    bridge.watch("T-be", stops.append)
    old = fake.node("T-be")["lease_id"]
    fake.now += lease_mod.DEFAULT_TTL_S + 1  # host under load: the beat came too late, but nobody took the node
    assert bridge.beat("T-be") == "reclaimed"
    new = fake.node("T-be")["lease_id"]
    assert new != old and bridge.held("T-be").lease_id == new
    assert fake.trail_of("T-be") == [("claim", "granted"), ("claim", "reclaimed")]
    assert stops == [] and store.get("T-be")["notes_json"]["lease"]["lease_id"] == new
    stops_after = []
    bridge.watch("T-be", stops_after.append)  # the session is still the one watched on the new lease
    fake.force_release("T-be")
    assert bridge.beat("T-be") == "stopped" and len(stops_after) == 1


def test_expired_reclaim_defers_local_contention_without_stopping_the_session(tmp_path, queued_fake, monkeypatch):
    store, bridge = _held(tmp_path, queued_fake, [])
    stops = []
    bridge.watch("T-be", stops.append)
    old = bridge.held("T-be")
    queued_fake.now += lease_mod.DEFAULT_TTL_S + 1
    monkeypatch.setattr(substrate_client, "TIMEOUT_S", 0.01)
    monkeypatch.setattr(substrate_client, "DEADLINE_SLACK_S", 0.05)
    real_claim = lease_mod.claim_node

    def congested_claim(*args, **kwargs):
        slots = substrate_client._slots
        for _ in range(substrate_client.MAX_IN_FLIGHT):
            assert slots.acquire(timeout=1)
        try:
            return real_claim(*args, **kwargs)
        finally:
            for _ in range(substrate_client.MAX_IN_FLIGHT):
                slots.release()

    monkeypatch.setattr(lease_mod, "claim_node", congested_claim)
    assert bridge.beat("T-be") == "unanswered"
    assert bridge.held("T-be") is old and stops == []
    assert old.due == queued_fake.now + substrate_client.BACKOFF_S
    assert store.get("T-be")["notes_json"]["lease"]["state"] == "held"
    assert substrate_client._down_until == 0
    monkeypatch.setattr(lease_mod, "claim_node", real_claim)
    assert bridge.beat("T-be") == "reclaimed"
    assert bridge.held("T-be").lease_id != old.lease_id and stops == []


def test_expired_whose_reclaim_is_denied_stops_the_session(tmp_path, fake):
    events = []
    store, bridge = _held(tmp_path, fake, events)
    stops = []
    bridge.watch("T-be", stops.append)
    _steal(fake)
    fake.forced["graph_heartbeat"] = {"ok": False, "reason": "expired"}  # the steal lands between the beat and re-claim
    assert bridge.beat("T-be") == "stopped"
    assert stops == [lease_mod.RECLAIM_NOT_GRANTED]
    assert events[-1][1]["reason"] == "expired; re-claim denied"


@pytest.mark.parametrize("refusal", [401, 403, "identity"])
def test_expired_lease_whose_reclaim_is_refused_stops_dispatch(tmp_path, fake, monkeypatch, refusal):
    events, stops = [], []
    store, bridge = _held(tmp_path, fake, events)
    bridge.watch("T-be", stops.append)
    fake.now += lease_mod.DEFAULT_TTL_S + 1
    _refuse_tool(fake, monkeypatch, "graph_claim", refusal)
    assert bridge.beat("T-be") == "stopped"
    assert stops == [lease_mod.RECLAIM_NOT_GRANTED]
    assert bridge.held("T-be") is None
    assert store.get("T-be")["notes_json"]["lease"]["state"] == "lost"
    assert events[-1][1]["reason"] == "expired; re-claim refused"


def test_no_answer_keeps_the_session_and_retries_sooner(tmp_path, fake):
    events = []
    store, bridge = _held(tmp_path, fake, events)
    stops = []
    bridge.watch("T-be", stops.append)
    fake.down = True
    assert bridge.beat("T-be") == "unanswered"
    assert bridge.held("T-be").due == fake.now + substrate_client.BACKOFF_S < fake.now + lease_mod.heartbeat_interval(900)
    assert stops == [] and events == []


def test_a_lease_lost_before_the_session_starts_stops_it_on_arrival(tmp_path, fake):
    events = []
    store, bridge = _held(tmp_path, fake, events)
    _steal(fake)
    assert bridge.beat("T-be") == "dropped"  # nothing watching yet
    stops = []
    bridge.watch("T-be", stops.append)
    assert stops == [lease_mod.SESSION_REFUSALS["lost"]]


def test_between_rounds_a_lost_lease_is_dropped_and_the_task_left_to_a01(tmp_path, fake):
    events = []
    store, bridge = _held(tmp_path, fake, events, state="IN_REVIEW")
    _steal(fake)
    assert bridge.beat("T-be") == "dropped"
    assert store.get("T-be")["state"] == "IN_REVIEW" and bridge.held("T-be") is None
    assert events[-1] == ("lease.lost", {"task_id": "T-be", "agent": "A05", "reason": "lost", "session_stopped": False})
    # the gates pass: A01 still calls it DONE, and says the node is someone else's
    from swarm.results import reconcile
    reconcile(store, CORR, lambda *a, **k: None, leases=bridge)
    assert store.get("T-be")["state"] == "DONE"
    assert fake.node("T-be")["state"] is None and fake.node("T-be")["session"] == f"A05@r2:{GID}"
    assert events[-1][0] == "lease.unsettled" and events[-1][1]["reason"] == "claim denied"


def test_between_rounds_an_unheld_lease_is_reclaimed(tmp_path, fake):
    events = []
    store, bridge = _held(tmp_path, fake, events, state="IN_REVIEW")
    fake.force_release("T-be")  # e.g. lapsed and reaped while the runner was paused
    assert bridge.beat("T-be") == "reclaimed" and fake.node("T-be")["session"] == f"A05@r0:{GID}"


def test_session_transition_table():
    assert lease_mod.session_transition("expired") == lease_mod.RECLAIM
    assert lease_mod.session_transition("lost").startswith("E-TIMEOUT: lease lost")
    assert lease_mod.session_transition("unheld").startswith("E-TIMEOUT: lease unheld")
    assert lease_mod.session_transition("not-holder").startswith("E-POLICY: lease not-holder")
    assert lease_mod.session_transition("brand-new") == "E-TIMEOUT: lease refused (brand-new)"


def test_the_fake_expires_on_the_granted_ttl(tmp_path, fake, monkeypatch):
    """The heartbeat cadence is a third of the *granted* TTL, and the lease lapses on that clock: a fake that reported
    90 s but kept the requested 120 s would let a beat arrive 30 s late and still pass."""
    monkeypatch.setenv("SWARM_LEASE_TTL_S", "120")
    fake.grant_ttl = 90
    store, bridge = _held(tmp_path, fake, [])
    granted_at = fake.now
    lease = bridge.held("T-be")
    assert lease.ttl_s == 90 and lease.due == granted_at + 30
    assert fake.node("T-be")["expires"] == granted_at + 90
    fake.now = granted_at + 30
    assert bridge.beat("T-be") == "ok" and fake.node("T-be")["expires"] == granted_at + 30 + 90
    fake.now = granted_at + 30 + 89  # unbeaten from here: still held one second before the granted TTL runs out
    r2 = _replica("r2")
    assert lease_mod.claim_node("A05", GID, "T-be", ttl_s=120, workspace=fake.workspace, env=r2)[0].status == "denied"
    fake.now = granted_at + 30 + 90  # and free on the granted clock, 30 s before the requested one
    assert lease_mod.claim_node("A05", GID, "T-be", ttl_s=120, workspace=fake.workspace, env=r2)[0].reason == "stolen"


def test_many_held_leases_are_all_renewed_within_a_third_of_the_ttl(tmp_path, fake, monkeypatch):
    """Held leases are not capped by --max-parallel (review leases outlive their round). With 1 s replies, one beat at a
    time would take longer than TTL/3 to get round; the keeper beats up to HEARTBEAT_WORKERS at once, each lease on its
    own due time."""
    monkeypatch.setenv("SWARM_LEASE_TTL_S", "30")
    n = 6 * lease_mod.HEARTBEAT_WORKERS  # one at a time: n simulated seconds, past TTL/3 = 10
    ids = [f"T{i:02d}-be" for i in range(n)]
    fake.register(*ids)
    store, events = _store(tmp_path), []
    bridge = _bridge(store, events, clock=lambda: fake.now)
    for tid in ids:
        assert bridge.acquire(_plan(store, tid), "A05").status == "held"
    interval = lease_mod.heartbeat_interval(30)
    due = fake.now + interval  # every lease falls due at once
    # Simulated time moves only at the round barrier: a call takes one simulated second, and the HEARTBEAT_WORKERS calls
    # of a round overlap. The last of n / HEARTBEAT_WORKERS rounds therefore ends that many seconds after `due`, however
    # the host schedules threads.
    tick = {"t": due}
    fake.clock = lambda: tick["t"]
    rounds = threading.Barrier(lease_mod.HEARTBEAT_WORKERS, timeout=5, action=lambda: tick.update(t=tick["t"] + 1))
    fake.gate = rounds.wait
    bridge.start()
    try:
        deadline = time.monotonic() + 30
        while len({c["args"]["node_id"] for c in fake.at("graph_heartbeat")}) < n and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        bridge.close()
        fake.gate = None
    first: dict[str, float] = {}
    for c in fake.at("graph_heartbeat"):
        first.setdefault(c["args"]["node_id"], c["at"])
    assert set(first) == set(ids)
    assert max(first.values()) - due <= interval, sorted(round(v - due, 2) for v in first.values())
    assert all(fake.node(t)["lease_id"] == bridge.held(t).lease_id for t in ids) and events == []


def test_a_settle_racing_a_reclaim_waits_for_it_and_releases_the_fresh_lease(tmp_path, fake, monkeypatch):
    """The keeper's re-claim and the worker's settlement of one task run one at a time: a settlement that arrives while
    the re-claim is on the wire waits, then releases the lease that claim brought back instead of leaving it untracked."""
    store, bridge = _held(tmp_path, fake, [])
    bridge.watch("T-be", lambda reason: None)  # a session runs: `expired` re-claims
    fake.now += lease_mod.DEFAULT_TTL_S + 1
    real, settler = lease_mod.claim_node, {}

    def claim_while_the_worker_settles(*a, **k):
        got = real(*a, **k)
        store.transition("T-be", "FAILED")  # the worker finishes meanwhile and settles the task
        t = threading.Thread(target=lambda: settler.setdefault("out", bridge.settle("T-be")))
        t.start()
        t.join(0.3)
        settler.update(waited=t.is_alive(), thread=t)
        return got

    monkeypatch.setattr(lease_mod, "claim_node", claim_while_the_worker_settles)
    assert bridge.beat("T-be") == "reclaimed"
    settler["thread"].join(5)
    fresh = [e["lease_id"] for e in fake.trail if e["action"] == "reclaimed"]
    assert settler["waited"] and settler["out"] == "released" and len(fresh) == 1
    assert [c["args"]["lease_id"] for c in fake.at("graph_release")] == fresh
    assert fake.node("T-be")["lease_id"] is None and bridge.held("T-be") is None


def test_a_fresh_claim_that_cannot_be_installed_is_released(tmp_path, fake, monkeypatch):
    store, bridge = _held(tmp_path, fake, [])
    bridge.watch("T-be", lambda reason: None)
    fake.now += lease_mod.DEFAULT_TTL_S + 1
    real = lease_mod.claim_node

    def claim_after_the_lease_was_let_go(*a, **k):
        got = real(*a, **k)
        with bridge._lock:  # whatever let the old lease go while the claim was on the wire
            bridge._held.pop("T-be")
        return got

    monkeypatch.setattr(lease_mod, "claim_node", claim_after_the_lease_was_let_go)
    assert bridge.beat("T-be") == "gone"
    fresh = [e["lease_id"] for e in fake.trail if e["action"] == "reclaimed"]
    assert len(fresh) == 1 and [c["args"]["lease_id"] for c in fake.at("graph_release")] == fresh
    assert fake.node("T-be")["lease_id"] is None and bridge.held("T-be") is None


def test_the_keeper_stops_a_real_session_whose_node_was_taken(tmp_path, fake, runner, monkeypatch):
    """End to end on a real process group: the keeper thread beats, the substrate answers `lost`, the runner kills the
    session and fails the task with the tabled reason, long before the session would have finished."""
    monkeypatch.setattr(lease_mod, "heartbeat_interval", lambda ttl: 0.05)
    child = [sys.executable, "-c", "import sys, time; sys.stdin.read(); time.sleep(60)"]
    monkeypatch.setattr(runner, "headless_command", lambda runtime, agent, repo, sdir, args: (child, dict(os.environ), repo))
    fake.register("T-be")
    store, events = _store(tmp_path), []
    task = _plan(store)
    bridge = _bridge(store, events)
    batch, _ = runner.select_batch(store, [task], 1, bridge)
    assert batch
    _steal(fake)
    bridge.start()
    try:
        t0 = time.monotonic()
        _, _, outcome = _execute(runner, store, task, bridge, tmp_path, events, args=_args())
        elapsed = time.monotonic() - t0
    finally:
        bridge.close()
    assert outcome == "FAILED" and elapsed < 20
    assert store.history("T-be")[-1]["reason"] == lease_mod.SESSION_REFUSALS["lost"]
    assert store.get("T-be")["notes_json"]["meta"]["lease_stopped"] == lease_mod.SESSION_REFUSALS["lost"]
    assert ("lease.lost" in _types(events)) and fake.node("T-be")["session"] == f"A05@r2:{GID}"  # never released by us


@pytest.mark.parametrize("refusal", [401, 403, "identity"])
def test_auth_refusal_kills_the_process_group_and_rejects_its_result(tmp_path, fake, runner, monkeypatch, refusal):
    pidfile = tmp_path / "session.pid"
    result = "```json\n" + json.dumps({"task_id": "T-be", "state": "IN_REVIEW",
                                     "summary_md": "untrusted after revocation"}) + "\n```"
    child = [sys.executable, "-c",
             "import os, sys, time; sys.stdin.read(); "
             f"print({result!r}, flush=True); open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(10)"]
    monkeypatch.setattr(runner, "headless_command", lambda runtime, agent, repo, sdir, args: (child, dict(os.environ), repo))
    monkeypatch.setattr(lease_mod, "heartbeat_interval", lambda ttl: 0.01)
    fake.register("T-be")
    store, events = _store(tmp_path), []
    task = _plan(store)
    bridge = _bridge(store, events)
    assert bridge.acquire(task, "A05").status == "held"
    _refuse_tool(fake, monkeypatch, "graph_heartbeat", refusal)
    beat = bridge.beat

    def after_spawn(tid):
        _wait_for(pidfile)
        return beat(tid)

    monkeypatch.setattr(bridge, "beat", after_spawn)
    bridge.start()
    try:
        _, _, outcome = _execute(runner, store, task, bridge, tmp_path, events)
    finally:
        bridge.close()
    assert outcome == "FAILED"
    assert not _alive(int(pidfile.read_text())) and not runner._SESSIONS
    assert store.history("T-be")[-1]["reason"] == lease_mod.SESSION_REFUSALS[lease_mod.REFUSED]
    assert "IN_REVIEW" not in [h["to_state"] for h in store.history("T-be")]
    assert store.get("T-be")["notes_json"]["meta"]["lease_stopped"] == lease_mod.SESSION_REFUSALS[lease_mod.REFUSED]


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def _wait_for(path, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def test_a_stopped_session_is_forgotten_only_once_its_whole_group_is_gone(tmp_path, runner, monkeypatch):
    """A tool with its own pipes that ignores SIGTERM outlives its parent: `communicate` returns, but the group is not
    gone. The runner waits out the grace and SIGKILLs it before it forgets the group."""
    monkeypatch.setattr(runner, "STOP_GRACE_S", 1)
    pidfile = tmp_path / "tool.pid"
    tool = ("import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)")
    parent = ("import subprocess, sys, time; "
              f"subprocess.Popen([sys.executable, '-c', {tool!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
              "stderr=subprocess.DEVNULL); sys.stdin.read(); time.sleep(60)")
    monkeypatch.setattr(runner, "headless_command",
                        lambda runtime, agent, repo, sdir, args: ([sys.executable, "-c", parent], dict(os.environ), repo))
    groups = []

    def on_session(stop):
        if stop is not None:
            groups.extend(runner._SESSIONS)
            threading.Thread(target=lambda: (_wait_for(pidfile), stop("E-TIMEOUT: lease lost")), daemon=True).start()

    with pytest.raises(runner.LeaseStopped):
        runner.run_agent_headless({"slug": "a05-backend", "id": "A05"}, "go", tmp_path, _args(), on_session=on_session)
    tool_pid = int(pidfile.read_text())
    try:
        assert not _alive(tool_pid), "the tool outlived its stopped session"
        assert groups and not set(groups) & set(runner._SESSIONS)
    finally:
        if _alive(tool_pid):
            os.kill(tool_pid, 9)


def _gate_result(tid: str) -> str:
    return "done\n```json\n" + json.dumps({"task_id": tid, "state": "IN_REVIEW", "summary_md": "ok"}) + "\n```"


def test_a_lease_lost_after_the_session_ends_rejects_its_result(tmp_path, fake, runner, monkeypatch):
    """The dispatch stays watched after the session exits: a lease taken before the result is written fails the task
    with the tabled reason, and the IN_REVIEW the session reported is never applied."""
    fake.register("T-be")
    store, events = _store(tmp_path), []
    task = _plan(store)
    bridge = _bridge(store, events, clock=lambda: fake.now)
    runner.select_batch(store, [task], 1, bridge)
    beats = []

    def session(agent, prompt, repo, args, *, on_session=None):
        on_session(lambda reason: None)
        on_session(None)  # the session has ended; its result is not written yet
        _steal(fake)
        beats.append(bridge.beat("T-be"))
        return _gate_result("T-be"), {"returncode": 0}

    monkeypatch.setattr(runner, "run_agent_headless", session)
    _, _, outcome = _execute(runner, store, task, bridge, tmp_path, events)
    lost = lease_mod.SESSION_REFUSALS["lost"]
    assert beats == ["stopped"] and outcome == "FAILED"
    assert "IN_REVIEW" not in [h["to_state"] for h in store.history("T-be")]
    assert store.history("T-be")[-1]["reason"] == lost and store.get("T-be")["notes_json"]["meta"]["lease_stopped"] == lost
    assert fake.node("T-be")["session"] == f"A05@r2:{GID}"  # the new holder's, untouched


def test_a_lease_lost_while_the_gate_script_runs_stops_it_and_rejects_the_result(tmp_path, fake, runner, monkeypatch):
    """The runner's gate script writes signed verdicts: it runs watched, in its own process group, and a lost lease
    stops it and fails the gate task instead of accepting its result."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    pidfile = tmp_path / "gate.pid"
    (root / "scripts" / "rel_plan.py").write_text(
        f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    monkeypatch.setattr(runner, "ROOT", root)
    fake.register("S-be", "S-rel")
    store, events = _store(tmp_path), []
    _plan(store, "S-be", gates=["release"])
    gate = _plan(store, "S-rel", agent="A12", gate="release", gate_for=["S-be"])
    bridge = _bridge(store, events)
    batch, _ = runner.select_batch(store, [gate], 1, bridge)
    assert batch and bridge.held("S-rel") is not None
    monkeypatch.setattr(runner, "run_agent_headless", lambda *a, **k: (_gate_result("S-rel"), {"returncode": 0}))
    beats = []

    def take_the_node_while_the_script_runs():
        _wait_for(pidfile)
        _steal(fake, "S-rel", agent="A12")
        beats.append(bridge.beat("S-rel"))

    taker = threading.Thread(target=take_the_node_while_the_script_runs, daemon=True)
    taker.start()
    t0 = time.monotonic()
    _, _, outcome = _execute(runner, store, gate, bridge, tmp_path, events)
    taker.join(5)
    script_pid = int(pidfile.read_text())
    try:
        assert time.monotonic() - t0 < 20 and beats == ["stopped"] and not _alive(script_pid)
    finally:
        if _alive(script_pid):
            os.kill(script_pid, 9)
    lost = lease_mod.SESSION_REFUSALS["lost"]
    assert outcome == "FAILED" and store.history("S-rel")[-1]["reason"] == lost
    assert "IN_REVIEW" not in [h["to_state"] for h in store.history("S-rel")]
    assert store.get("S-rel")["notes_json"]["meta"]["lease_stopped"] == lost


@pytest.mark.parametrize("exit_kind", ["signal", "shell-signal", "shutdown", "findings"])
def test_interrupted_gate_verdicts_are_not_applied_but_findings_are(tmp_path, runner, monkeypatch, exit_kind):
    """Commit verdict rows, then terminate before the release plan: only a completed findings exit is accepted."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    pidfile, planfile = tmp_path / "gate.pid", tmp_path / "release-plan.json"
    ending = {
        "signal": "os.kill(os.getpid(), signal.SIGTERM)",
        "shell-signal": "sys.exit(128 + signal.SIGTERM)",
        "shutdown": "time.sleep(60)",
        "findings": f"open({str(planfile)!r}, 'w').write('{{}}'); sys.exit(1)",
    }[exit_kind]
    (root / "scripts" / "rel_plan.py").write_text(
        "import os, signal, sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from swarm.taskstore import TaskStore\n"
        "from swarm.verdicts import record_gate_verdicts, SIM_FINDING\n"
        "record_gate_verdicts(TaskStore(), gate_task_id='S-rel', gate='release', agent_id='A12@local', "
        f"findings={'[SIM_FINDING]' if exit_kind == 'findings' else '[]'}, runs={{}}, "
        f"correlation_id={CORR!r}, expires_s=3600, emit=lambda *a, **k: None)\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n{ending}\n")
    monkeypatch.setattr(runner, "ROOT", root)
    store, events = _store(tmp_path), []
    _plan(store, "S-be", gates=["release"])
    for state in ("CLAIMED", "IN_PROGRESS", "IN_REVIEW"):
        store.transition("S-be", state)
    gate = _plan(store, "S-rel", agent="A12", gate="release", gate_for=["S-be"])
    monkeypatch.setattr(runner, "run_agent_headless", lambda *a, **k: (_gate_result("S-rel"), {"returncode": 0}))
    applied, groups = [], []
    apply = runner.apply_result

    def apply_result(*args, **kwargs):
        applied.append(args[1]["task_id"])
        return apply(*args, **kwargs)

    monkeypatch.setattr(runner, "apply_result", apply_result)
    killer = None
    if exit_kind == "shutdown":
        def shutdown():
            _wait_for(pidfile)
            groups.extend(runner._SESSIONS.values())
            runner.kill_sessions(grace=0)
        killer = threading.Thread(target=shutdown, daemon=True)
        killer.start()
    try:
        _, _, outcome = _execute(runner, store, gate, None, tmp_path, events)
    finally:
        if killer is not None:
            killer.join(5)
        runner._STOPPING.clear()
    assert pidfile.exists() and store.latest_verdicts("S-be")  # verdict committed before interruption
    assert store.get("S-rel")["notes_json"]["running"] is None
    if exit_kind == "findings":
        assert outcome == "IN_REVIEW" and applied == ["S-rel"] and planfile.exists()
        assert store.latest_verdicts("S-be")["release"]["verdict"] == "fail"
    else:
        assert outcome == "FAILED" and applied == [] and not planfile.exists()
        assert "IN_REVIEW" not in [h["to_state"] for h in store.history("S-rel")]
        runner.reconcile(store, CORR, lambda *a, **k: None)
        assert store.get("S-be")["state"] == "IN_REVIEW"
    if exit_kind == "shutdown":
        assert groups and all(g.stopped == ["E-TIMEOUT: runner shutdown"] for g in groups)
        assert not runner._SESSIONS


def test_group_spawned_after_shutdown_is_marked_stopped(tmp_path, runner):
    runner.kill_sessions(grace=0)
    group = runner.ChildGroup([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path, env=dict(os.environ))
    try:
        group.proc.communicate(timeout=5)
        assert group.stopped == ["E-TIMEOUT: runner shutdown"]
    finally:
        group.close()
        runner._STOPPING.clear()
    assert not runner._SESSIONS


# --- LEASE-07: DONE, CHANGES_REQUESTED, FAILED, CANCELLED ----------------------------------------------------------
def test_done_goes_through_graph_complete_fenced_on_the_lease(tmp_path, fake):
    from swarm.results import reconcile
    events = []
    store, bridge = _held(tmp_path, fake, events, state="IN_REVIEW")
    lease_id = fake.node("T-be")["lease_id"]
    reconcile(store, CORR, lambda *a, **k: None, leases=bridge)
    assert store.get("T-be")["state"] == "DONE"
    (done,) = fake.at("graph_complete")
    assert done["args"]["lease_id"] == lease_id and done["caller"] == "swarm-a05-be"
    assert fake.node("T-be")["state"] == "completed" and fake.trail_of("T-be")[-1] == ("note", "completed")
    assert store.get("T-be")["notes_json"]["lease"]["state"] == "completed" and bridge.held("T-be") is None
    assert events == []


def test_a_restarted_runner_completes_as_the_same_holder(tmp_path, fake):
    from swarm.results import reconcile
    store, first = _held(tmp_path, fake, [], state="IN_REVIEW")
    lease_id = fake.node("T-be")["lease_id"]
    fresh = _bridge(store, [])  # same replica, new process: no lease in memory
    reconcile(store, CORR, lambda *a, **k: None, leases=fresh)
    assert fake.at("graph_complete")[0]["args"]["lease_id"] == lease_id  # `renewed` keeps the id
    assert fake.node("T-be")["state"] == "completed"
    assert fake.trail_of("T-be") == [("claim", "granted"), ("note", "completed")]


def test_changes_requested_releases_and_reclaims_and_the_rework_reuses_it(tmp_path, fake, runner):
    from swarm.results import reconcile
    from swarm.verdicts import record_gate_verdicts
    fake.register("S-be", "S-qa")
    store, events = _store(tmp_path), []
    target = _plan(store, "S-be", gates=["quality"])
    _plan(store, "S-qa", agent="A08", gate="quality", gate_for=["S-be"])
    bridge = _bridge(store, events)
    assert bridge.acquire(target, "A05").status == "held"
    for s in ("CLAIMED", "IN_PROGRESS", "IN_REVIEW"):
        store.transition("S-be", s)
    for s in ("CLAIMED", "IN_PROGRESS"):
        store.transition("S-qa", s)
    record_gate_verdicts(store, gate_task_id="S-qa", gate="quality", agent_id="A08@local", verdict="fail", runs={},
                         findings=[{"id": "F1", "severity": "major", "kind": "functional", "summary": "broken"}],
                         correlation_id=CORR, expires_s=600, emit=lambda *a, **k: None)
    first = fake.node("S-be")["lease_id"]
    reconcile(store, CORR, lambda *a, **k: None, leases=bridge)
    assert store.get("S-be")["state"] == "IN_PROGRESS" and store.get("S-be")["rework_loops"] == 1
    assert fake.trail_of("S-be") == [("claim", "granted"), ("note", "released"), ("claim", "granted")]
    second = fake.node("S-be")["lease_id"]
    assert second != first and bridge.held("S-be").lease_id == second
    assert [c["args"]["lease_id"] for c in fake.at("graph_release")] == [first]  # fenced on the lease it held
    claims = len(fake.at("graph_claim"))
    batch, _ = runner.select_batch(store, [store.get("S-be")], 3, bridge)
    assert [t["task_id"] for t, _ in batch] == ["S-be"] and len(fake.at("graph_claim")) == claims  # no third claim
    assert events == []


def test_failed_releases(tmp_path, fake, runner, monkeypatch):
    fake.register("T-be")
    store, events = _store(tmp_path), []
    task = _plan(store)
    bridge = _bridge(store, events)
    runner.select_batch(store, [task], 1, bridge)
    lease_id = fake.node("T-be")["lease_id"]
    monkeypatch.setattr(runner, "run_agent_headless", _session({}, state=None))  # no result: E-CONTRACT, FAILED
    _, _, outcome = _execute(runner, store, task, bridge, tmp_path, events)
    assert outcome == "FAILED"
    assert [c["args"]["lease_id"] for c in fake.at("graph_release")] == [lease_id]
    assert fake.node("T-be")["lease_id"] is None and fake.trail_of("T-be")[-1] == ("note", "released")
    assert store.get("T-be")["notes_json"]["lease"]["state"] == "released" and bridge.held("T-be") is None


@pytest.mark.parametrize("terminal", ["BLOCKED", "ESCALATED"])
def test_blocked_and_rework_cap_escalated_tasks_release_their_held_lease(tmp_path, fake, terminal):
    store, bridge = _held(tmp_path, fake, [], state="IN_REVIEW" if terminal == "ESCALATED" else "IN_PROGRESS")
    lease_id = fake.node("T-be")["lease_id"]
    if terminal == "ESCALATED":
        from swarm.taskstore import MAX_REWORK_LOOPS
        store.update("T-be", rework_loops=MAX_REWORK_LOOPS)
        store.transition("T-be", "CHANGES_REQUESTED", reason="rework exhausted")
    else:
        store.transition("T-be", "BLOCKED", reason="needs human approval")
    assert store.get("T-be")["state"] == terminal
    assert bridge.settle("T-be") == "released"
    assert [c["args"]["lease_id"] for c in fake.at("graph_release")] == [lease_id]
    assert fake.node("T-be")["lease_id"] is None and bridge.held("T-be") is None
    assert store.get("T-be")["notes_json"]["lease"]["state"] == "released"


def test_a_stale_mirror_cannot_release_a_successor_lease(tmp_path, fake):
    store, bridge = _held(tmp_path, fake, [])
    stale = fake.node("T-be")["lease_id"]
    _steal(fake)
    successor = dict(fake.node("T-be"))
    store.transition("T-be", "FAILED", reason="old runner ended")
    assert lease_mod.settle_mirrored(store, "T-be", emit=lambda *a: None, workspace=fake.workspace) == "not-ours"
    assert [c["args"]["lease_id"] for c in fake.at("graph_release")] == [stale]
    assert fake.node("T-be") == successor
    assert successor["lease_id"] != stale and successor["session"] == f"A05@r2:{GID}"
    assert fake.trail_of("T-be") == [("claim", "granted"), ("warning", "stolen")]


def test_cancelled_by_orch_status_releases_the_mirrored_lease(tmp_path, fake):
    events = []
    store, bridge = _held(tmp_path, fake, events, state="IN_PROGRESS")
    lease_id = fake.node("T-be")["lease_id"]
    orch = _load("orch_status")  # another process: it knows the lease only from notes.lease
    rc = orch.AgentScript("A01", "orch_status", orch.run, description=orch.__doc__, add_args=orch.add_args).main(
        ["--json", "--root", str(tmp_path), "--transition", "T-be", "CANCELLED"])
    assert rc == 0 and store.get("T-be")["state"] == "CANCELLED"
    rel = [c for c in fake.at("graph_release")]
    assert [(c["caller"], c["args"]["lease_id"]) for c in rel] == [("swarm-a05-be", lease_id)]
    assert fake.node("T-be")["lease_id"] is None and store.get("T-be")["notes_json"]["lease"]["state"] == "released"
    # the runner's keeper notices on its next beat and lets go instead of re-claiming a cancelled task's node
    assert bridge.beat("T-be") == "settled" and bridge.held("T-be") is None and fake.node("T-be")["lease_id"] is None


def test_cancel_while_a_session_runs_stops_it_before_the_release(tmp_path, fake, runner, monkeypatch):
    """orch_status leaves a lease to the runner while the task's session runs (notes.running). The runner's keeper stops
    the session on its next beat and keeps the node; the dispatch releases it only after the session has ended."""
    fake.register("T-be")
    store, events = _store(tmp_path), []
    task = _plan(store)
    bridge = _bridge(store, events, clock=lambda: fake.now)
    runner.select_batch(store, [task], 1, bridge)
    lease_id = fake.node("T-be")["lease_id"]
    orch = _load("orch_status")
    seen = {}

    def session(agent, prompt, repo, args, *, on_session=None):
        stops = []
        on_session(stops.append)
        seen["rc"] = orch.AgentScript("A01", "orch_status", orch.run, description=orch.__doc__, add_args=orch.add_args).main(
            ["--json", "--root", str(tmp_path), "--transition", "T-be", "CANCELLED"])
        seen["released_by_orch_status"] = len(fake.at("graph_release"))
        seen["beats"] = [bridge.beat("T-be"), bridge.beat("T-be")]
        seen["stops"] = list(stops)
        seen["released_while_running"] = len(fake.at("graph_release"))
        on_session(None)
        raise runner.LeaseStopped(stops[0], "partial", {"returncode": -15, "lease_stopped": stops[0]})

    monkeypatch.setattr(runner, "run_agent_headless", session)
    _, _, outcome = _execute(runner, store, task, bridge, tmp_path, events)
    assert seen["rc"] == 0 and seen["released_by_orch_status"] == 0
    assert seen["beats"] == ["halting", "halting"] and seen["stops"] == [lease_mod.moved_on_reason("CANCELLED")]
    assert seen["released_while_running"] == 0 and fake.node("T-be")["lease_id"] is None  # released after, not during
    assert outcome == "CANCELLED" and store.get("T-be")["state"] == "CANCELLED"
    assert [c["args"]["lease_id"] for c in fake.at("graph_release")] == [lease_id]
    assert store.get("T-be")["notes_json"]["lease"]["state"] == "released" and bridge.held("T-be") is None


def test_orch_status_keeps_lease_ids_out_of_the_run_log(tmp_path, fake, capsys):
    store, bridge = _held(tmp_path, fake, [], state="IN_REVIEW")
    lease_id = fake.node("T-be")["lease_id"]
    orch = _load("orch_status")
    rc = orch.AgentScript("A01", "orch_status", orch.run, description=orch.__doc__, add_args=orch.add_args).main(
        ["--json", "--root", str(tmp_path), "--transition", "T-be", "CANCELLED"])
    assert rc == 0 and lease_id in capsys.readouterr().out  # the CLI prints the task as stored, mirror included
    assert store.get("T-be")["notes_json"]["lease"]["lease_id"] == lease_id  # and the mirror keeps it
    log = (tmp_path / ".swarm" / "events.jsonl").read_text()
    assert '"script.orch_status"' in log and '"notes_json"' in log
    assert lease_id not in log and "lease_id" not in log


def test_a_resumed_runner_holds_review_leases_again_and_reworks_through_a_release(tmp_path, fake):
    """A runner that stopped with work IN_REVIEW (`--once`) leaves its lease to lapse. The next run claims it again
    before reconciling, so the keeper beats it through the gates, and a rework releases it rather than renewing it."""
    from swarm.results import reconcile
    from swarm.verdicts import record_gate_verdicts
    fake.register("S-be", "S-qa")
    store, events = _store(tmp_path), []
    target = _plan(store, "S-be", gates=["quality"])
    _plan(store, "S-qa", agent="A08", gate="quality", gate_for=["S-be"])
    assert _bridge(store, []).acquire(target, "A05").status == "held"
    for s in ("CLAIMED", "IN_PROGRESS", "IN_REVIEW"):
        store.transition("S-be", s)
    old = fake.node("S-be")["lease_id"]
    fake.now += 200  # the first runner is gone: nothing beats the lease
    resumed = _bridge(store, events, clock=lambda: fake.now)
    assert resumed.resume() == ["S-be"] and resumed.held("S-be").lease_id == old  # `renewed`: same holder, same id
    assert fake.node("S-be")["expires"] == fake.now + lease_mod.DEFAULT_TTL_S
    fake.now += lease_mod.heartbeat_interval(lease_mod.DEFAULT_TTL_S)
    assert resumed.beat("S-be") == "ok"
    for s in ("CLAIMED", "IN_PROGRESS"):
        store.transition("S-qa", s)
    record_gate_verdicts(store, gate_task_id="S-qa", gate="quality", agent_id="A08@local", verdict="fail", runs={},
                         findings=[{"id": "F1", "severity": "major", "kind": "functional", "summary": "broken"}],
                         correlation_id=CORR, expires_s=600, emit=lambda *a, **k: None)
    reconcile(store, CORR, lambda *a, **k: None, leases=resumed)
    assert store.get("S-be")["state"] == "IN_PROGRESS"
    assert [c["args"]["lease_id"] for c in fake.at("graph_release")] == [old]
    assert fake.trail_of("S-be") == [("claim", "granted"), ("note", "released"), ("claim", "granted")]
    assert resumed.held("S-be").lease_id not in (None, old) and events == []


def test_run_resumes_review_leases_before_the_first_reconcile(tmp_path, monkeypatch, runner):
    calls = []

    class Bridge:
        def resume(self):
            calls.append("resume")
            return []

        def start(self):
            calls.append("start")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(runner, "lease_bridge", lambda *a, **k: Bridge())
    monkeypatch.setattr(runner, "handoff_bridge", lambda *a, **k: None)  # handoffs are not what this test is about
    monkeypatch.setattr(runner, "reconcile", lambda *a, **k: calls.append("reconcile") or [])
    store = _store(tmp_path)
    _plan(store)
    for s in ("CLAIMED", "IN_PROGRESS", "IN_REVIEW"):
        store.transition("T-be", s)
    args = _args(repo=str(tmp_path), max_parallel=1, once=True, max_rounds=1)
    args.claude_bin = sys.executable
    runner.run(args, SimpleNamespace(correlation_id=CORR, emit=lambda *a, **k: None))
    assert calls[:3] == ["resume", "start", "reconcile"]


def test_full_non_dry_run_outage_preserves_the_disabled_dag_budgets_and_results(tmp_path, fake, runner, monkeypatch):
    from swarm import runlog
    child = (
        "import json, pathlib, re, sys\n"
        "prompt = sys.stdin.read()\n"
        "assignment = json.loads(re.search(r'```json\\n(.*?)\\n```', prompt, re.S).group(1))['payload']\n"
        "tid = assignment['task_id']\n"
        "pathlib.Path(tid + '.assignment.json').write_text(json.dumps(assignment))\n"
        "print('```json\\n' + json.dumps({'task_id': tid, 'state': 'IN_REVIEW', 'summary_md': 'finished'}) + '\\n```')\n"
    )
    monkeypatch.setattr(runner, "headless_command",
                        lambda runtime, agent, repo, sdir, args: ([sys.executable, "-c", child], dict(os.environ), repo))
    snapshots = []
    for mode in ("disabled", "outage"):
        substrate_client.reset()
        tee_mod.reset()
        repo = tmp_path / mode
        repo.mkdir()
        for agent in load_manifest():
            ws_mod.write_token(ws_mod.env_file(repo, agent["slug"]), f"tok-{tee_mod.AGENT_SURFACES[agent['id']]}")
        monkeypatch.setenv("SWARM_DIR", str(repo / ".swarm"))
        monkeypatch.setenv("SUBSTRATE_DISABLED", "1" if mode == "disabled" else "0")
        fake.down = mode == "outage"
        store = _store(repo)
        _plan(store, "T-first", agent="A05")
        _plan(store, "T-next", agent="A06")
        store.update("T-next", depends_on=["T-first"])
        for tid in ("T-first", "T-next"):
            store.update(tid, budget={**store.get(tid)["budget"], "max_wall_s": 15})

        def emit(kind, payload, **kwargs):
            return runlog.emit(kind, payload, source="A01@swarm_run", correlation_id=CORR,
                               task_id=payload.get("task_id"), root=repo)

        args = _args(repo=str(repo), max_parallel=1, once=False, max_rounds=5)
        args.claude_bin = sys.executable
        result = runner.run(args, SimpleNamespace(correlation_id=CORR, emit=emit))
        assert result["complete"] is True and result["counts"] == {"DONE": 2}
        tasks = store.list(correlation_id=CORR)
        records = runlog.read_events(correlation_id=CORR, root=repo)
        snapshots.append({
            "tasks": [(t["task_id"], t["state"], t["depends_on"], t["budget"], t["attempt"],
                       t["notes_json"]["result"]) for t in tasks],
            "histories": [[h["to_state"] for h in store.history(t["task_id"])] for t in tasks],
            # Scheduler and run-level records (no task_id) count wall-clock poll ticks, which vary under load; the per-task records
            # are deterministic and carry the outage comparison.
            "records": [(r["type"], r["task_id"]) for r in records
                        if r["task_id"] is not None and not r["type"].startswith(("lease.", "handoff."))],
        })
        for tid in ("T-first", "T-next"):
            assignment = json.loads((repo / f"{tid}.assignment.json").read_text())
            assert assignment["budget"] == store.get(tid)["budget"]
            assert assignment["lease_s"] == 15
        if mode == "outage":
            assert [(r["type"], r["task_id"]) for r in records if r["type"] == "lease.unleased"] == [
                ("lease.unleased", "T-first"), ("lease.unleased", "T-next")]
            assert all(t["notes_json"]["lease"]["state"] == "unleased" for t in tasks)
        store.conn.close()
    assert snapshots[0] == snapshots[1]


# --- LEASE-08: two replicas, one node -----------------------------------------------------------------------------
def test_two_replicas_racing_for_a_node_dispatch_it_once(tmp_path, fake, runner):
    for n in range(5):
        tid = f"R{n}-be"
        fake.register(tid)
        stores = [_store(tmp_path, f"r{i}-{n}.db") for i in (1, 2)]
        bridges = [_bridge(s, [], env=_replica(f"r{i}")) for i, s in zip((1, 2), stores)]
        tasks = [_plan(s, tid) for s in stores]
        results: list = [None, None]
        gate = threading.Barrier(2)

        def race(i):
            gate.wait()
            results[i] = runner.select_batch(stores[i], [tasks[i]], 1, bridges[i])

        threads = [threading.Thread(target=race, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dispatched = [i for i in (0, 1) if results[i][0]]
        assert len(dispatched) == 1, results
        winner = f"A05@r{dispatched[0] + 1}:{GID}"
        assert results[1 - dispatched[0]][1] == [f"{tid}: denied (held by {winner})"]
        assert [k for k, _ in fake.trail_of(tid)] == ["claim"]


def test_a_dead_replicas_node_is_free_to_the_next_claimant_within_one_ttl(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("SWARM_LEASE_TTL_S", "30")
    fake.register("T-be")
    s1, s2 = _store(tmp_path, "r1.db"), _store(tmp_path, "r2.db")
    r1 = _bridge(s1, [], env=_replica("r1"), clock=lambda: fake.now)
    assert r1.acquire(_plan(s1), "A05").status == "held"
    granted_at = fake.now
    # r1 is killed: no keeper, no release. Nothing reaps; the lease simply lapses.
    r2 = _bridge(s2, [], env=_replica("r2"), clock=lambda: fake.now)
    task2 = _plan(s2)
    fake.now = granted_at + 29
    assert r2.acquire(task2, "A05").status == "denied"  # never two holders at once
    fake.now = granted_at + 30
    claim = r2.acquire(task2, "A05")
    assert claim.status == "held" and claim.reason == "stolen"
    assert fake.trail_of("T-be") == [("claim", "granted"), ("warning", "stolen")]
    assert not [c for c in fake.calls if c["tool"].startswith("coord_reap")]
    # had r1 merely stalled, its next beat finds the node gone
    assert r1.beat("T-be") == "dropped"


# --- off, dry-run, configuration -----------------------------------------------------------------------------------
def test_no_bridge_when_off_or_dry_run(tmp_path, monkeypatch, runner):
    calls = []
    monkeypatch.setattr(substrate_client, "_open", lambda *a, **k: calls.append(a))
    ctx = SimpleNamespace(emit=lambda *a, **k: None)
    assert runner.lease_bridge(SimpleNamespace(dry_run=False), tmp_path / "t.db", CORR, tmp_path, ctx) is None
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test:8787")
    assert runner.lease_bridge(SimpleNamespace(dry_run=True), tmp_path / "t.db", CORR, tmp_path, ctx) is None
    assert calls == []
    store = _store(tmp_path)
    ready = [_plan(store, "T-a"), _plan(store, "T-b")]
    batch, waiting = runner.select_batch(store, ready, 1, None)
    assert [t["task_id"] for t, _ in batch] == ["T-a"] and waiting == []


@pytest.mark.parametrize("var,value", [("SWARM_REPLICA", "r 1"), ("SWARM_REPLICA", "x" * 65), ("SWARM_LEASE_TTL_S", "15m")])
def test_bad_replica_or_ttl_refuses_the_run_at_start(tmp_path, monkeypatch, runner, var, value):
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test:8787")
    monkeypatch.setenv(var, value)
    with pytest.raises(SwarmError) as e:
        runner.lease_bridge(SimpleNamespace(dry_run=False), tmp_path / "t.db", CORR, tmp_path,
                            SimpleNamespace(emit=lambda *a, **k: None))
    assert e.value.code is ErrorCode.E_INPUT and var in str(e.value)


def test_replica_and_session_shape():
    assert tee_mod.replica_id({}) == "r0" and tee_mod.replica_id({"SWARM_REPLICA": "  "}) == "r0"
    assert tee_mod.replica_id({"SWARM_REPLICA": "host-1.a_b"}) == "host-1.a_b"
    assert tee_mod.session_id("A05", GID, {"SWARM_REPLICA": "r2"}) == f"A05@r2:{GID}"
    for bad in ("r:1", "r@1", "é", "x" * 65):
        with pytest.raises(ValueError):
            tee_mod.replica_id({"SWARM_REPLICA": bad})


@pytest.mark.parametrize("raw,ttl,interval", [("", 900, 300.0), ("5", 30, 10.0), ("120", 120, 40.0), ("999999", 86_400, 28_800.0)])
def test_ttl_is_clamped_and_beaten_every_third(raw, ttl, interval):
    assert lease_mod.ttl_seconds({"SWARM_LEASE_TTL_S": raw}) == ttl
    assert lease_mod.heartbeat_interval(ttl) == interval
    assert lease_mod.heartbeat_interval(1) == 10.0  # the 30 s floor holds even for a nonsense grant


# --- substrate_client.mcp_call_outcome: "no" is not "no answer" ------------------------------------------------------
def _rpc(result: dict) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})


@pytest.mark.parametrize("reply,status,detail", [
    ((_rpc({"content": [{"type": "text", "text": '{"ok": true}'}]}), 200), "ok", ""),
    (('{"error":"no"}', 401), "refused", 'HTTP 401: {"error":"no"}'),
    (('{"error":"no"}', 403), "refused", 'HTTP 403: {"error":"no"}'),
    (("boom", 500), "error", "HTTP 500: boom"),
    ((_rpc({"isError": True, "content": [{"type": "text", "text": '{"error": "session refused"}'}]}), 200), "refused",
     "session refused"),
    ((_rpc({"isError": True, "content": [{"type": "text", "text": "unknown node g/n"}]}), 200), "error", "unknown node g/n"),
    (("not json at all", 200), "error", "no tool result"),
    (OSError("down"), "unreachable", ""),
])
def test_mcp_call_outcome_classifies_and_mcp_call_json_is_unchanged(monkeypatch, reply, status, detail):
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test:8787")

    def answer(*a, **k):
        if isinstance(reply, Exception):
            raise reply
        return _Resp(*reply)

    monkeypatch.setattr(substrate_client, "_open", answer)
    got = substrate_client.mcp_call_outcome("graph_claim", {})
    assert (got.status, got.detail) == (status, detail)
    substrate_client.reset()  # an unreachable host backs off; the second door is asked afresh
    assert substrate_client.mcp_call_json("graph_claim", {}) == ({"ok": True} if status == "ok" else None)


def test_mcp_call_outcome_is_off_without_a_url(monkeypatch):
    monkeypatch.setattr(substrate_client, "_open", lambda *a, **k: pytest.fail("no socket when off"))
    assert substrate_client.mcp_call_outcome("graph_claim", {}).status == "off"


@pytest.mark.parametrize("refusal", [401, 403, "identity"])
def test_refused_registration_never_turns_an_unknown_node_into_unleased_work(tmp_path, fake, runner, monkeypatch, refusal):
    store, events = _store(tmp_path), []
    task = _plan(store)
    _refuse_tool(fake, monkeypatch, "graph_register", refusal)
    batch, waiting = runner.select_batch(store, [task], 1, _bridge(store, events))
    assert batch == [] and waiting == ["T-be: refused"]
    assert store.get("T-be")["notes_json"]["lease"]["state"] == "refused"
    assert _types(events) == ["lease.refused"]
    assert len(fake.at("graph_claim")) == 1
