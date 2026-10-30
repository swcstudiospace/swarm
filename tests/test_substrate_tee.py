"""ADR 0001 S1: the swarm -> Agent Substrate tee, Graph ID binding and their fail-open guarantees.

Only the transport tests open sockets, and only to servers they start on 127.0.0.1."""
import ast
import http.server
import io
import importlib.util
import sys
import json
import os
import re
import select
import socket
import sqlite3
import time
import subprocess
import threading
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

import pytest

from conftest import ROOT, run_script

from swarm import runlog, substrate_client, substrate_tee as tee_mod, workspace

GID_A = "ut-mabc123-0123abcd"
GID_B = "ut-mxyz789-89abcdef"
CORR = "corr-s1"
TOKEN = "sekrit-token-value"


# --- fake substrate -----------------------------------------------------------------------------------------
class _Resp(io.BytesIO):
    def __init__(self, body: str, status: int = 200):
        super().__init__(body.encode("utf-8"))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeSubstrate:
    """Stands in for substrate_client._open: REST /events and a stateless MCP /mcp (graph_bind, graph_register)."""

    def __init__(self, bindings=None, sse=False, events_status=200):
        self.bindings = dict(bindings or {})
        self.sse, self.events_status = sse, events_status
        self.calls: list[dict] = []

    def __call__(self, req, timeout=None):
        call = {"path": urlparse(req.full_url).path, "headers": {k.lower(): v for k, v in req.header_items()},
                "body": json.loads(req.data), "timeout": timeout, "req": req}
        self.calls.append(call)
        if call["path"] == "/events":
            if self.events_status >= 400:
                raise urllib.error.HTTPError(req.full_url, self.events_status, "no", {}, io.BytesIO(b'{"error":"refused"}'))
            return _Resp('{"ok":true}', self.events_status)
        assert call["path"] == "/mcp", call["path"]
        params = call["body"]["params"]
        assert call["body"]["method"] == "tools/call"
        out = getattr(self, "tool_" + params["name"])(params["arguments"])
        rpc = {"jsonrpc": "2.0", "id": call["body"]["id"], "result": {"content": [{"type": "text", "text": json.dumps(out)}]}}
        return _Resp(f"event: message\ndata: {json.dumps(rpc)}\n\n" if self.sse else json.dumps(rpc))

    def tool_graph_bind(self, a):
        corr, gid = a.get("correlation_id"), a.get("graph_id")
        if corr and gid:
            first = corr not in self.bindings
            winner = self.bindings.setdefault(corr, gid)
            return {"status": "created" if first else "existing",
                    "correlation_id": corr, "graph_id": winner, "conflict": winner != gid}
        if corr:
            g = self.bindings.get(corr)
            return {"status": "existing" if g else "unbound", "correlation_id": corr, "graph_id": g}
        return {"graph_id": gid, "correlation_ids": [c for c, g in self.bindings.items() if g == gid]}

    def tool_graph_register(self, a):
        return {"graph_id": a["graph_id"], "registered": len(a.get("nodes", []))}

    def at(self, path, tool=None):
        return [c for c in self.calls if c["path"] == path and (tool is None or c["body"]["params"]["name"] == tool)]

    @property
    def events(self):
        return [c["body"] for c in self.at("/events")]


# Other test modules reload every `swarm*` module (see the swarm_dir fixture); pin the ones imported here so that
# runlog -> substrate_tee -> substrate_client inside a test are the very objects the test patches and inspects.
_PINNED = {k: v for k, v in sys.modules.items() if k == "swarm" or k.startswith("swarm.")}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for k, v in _PINNED.items():
        monkeypatch.setitem(sys.modules, k, v)
    for k in [k for k in os.environ if k.startswith(("SUBSTRATE_", "SWARM_")) or k.lower().endswith("_proxy")]:
        monkeypatch.delenv(k)  # an ambient HTTP_PROXY/NO_PROXY never leaks into a transport test
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.setenv("SWARM_PURE_PYTHON", "1")  # the oracle switch survives the scrub: child scripts import the pure modules
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))  # a tmp dir is never mistaken for a checkout's subdir
    substrate_client.reset()
    tee_mod.reset()
    yield
    substrate_client.reset()
    tee_mod.reset()


@pytest.fixture()
def on(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test:8787/")
    monkeypatch.setenv("SUBSTRATE_TOKEN", TOKEN)
    # a row goes out only with its own agent's token (workspace.credential), never with SUBSTRATE_TOKEN: here every agent
    # has one under the server's name, and no env files exist (XDG_CONFIG_HOME is an empty tmp dir)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for surface in tee_mod.AGENT_SURFACES.values():
        monkeypatch.setenv(workspace.server_var(surface), TOKEN)


@pytest.fixture()
def fake(monkeypatch):
    f = FakeSubstrate()
    monkeypatch.setattr(substrate_client, "_open", f)
    return f


def _no_open(monkeypatch):
    calls = []
    monkeypatch.setattr(substrate_client, "_open", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError("socket")))
    return calls


def _emit(tmp_path, typ="task.claimed", source="A05@code_checks", corr=CORR, task_id="T1-be"):
    return runlog.emit(typ, {"task_id": task_id, "status": "ok"}, source=source, correlation_id=corr, task_id=task_id,
                       root=tmp_path)


def _db(tmp_path):
    return tmp_path / ".swarm" / "substrate-tee.db"


def _load_orch_plan():
    spec = importlib.util.spec_from_file_location("orch_plan_under_test", ROOT / "scripts" / "orch_plan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _orch_plan(capsys, tmp_path, *args):
    mod = _load_orch_plan()
    repo = tmp_path / "myrepo"
    repo.mkdir(exist_ok=True)
    capsys.readouterr()
    rc = mod.AgentScript("A01", "orch_plan", mod.run, description=mod.__doc__, add_args=mod.add_args).main(
        ["--json", "--root", str(repo), *args])
    out = capsys.readouterr()
    return rc, json.loads(out.out), out


# --- off switches: no socket, no sqlite ---------------------------------------------------------------------
def test_url_unset_is_a_pure_noop(tmp_path, monkeypatch):
    calls = _no_open(monkeypatch)
    rec = _emit(tmp_path)
    assert calls == []
    assert not _db(tmp_path).exists()
    assert re.fullmatch(r"[0-9a-f]{32}", rec["msg_id"])
    line = json.loads((tmp_path / ".swarm" / "events.jsonl").read_text().splitlines()[0])
    assert line == rec and {"ts", "type", "source", "correlation_id", "task_id", "payload"} <= set(line)
    assert substrate_client.rest_post("/events", {}) is None and substrate_client.mcp_call("graph_bind", {}) is None
    assert tee_mod.tee(rec, tmp_path) is False and not _db(tmp_path).exists()


@pytest.mark.parametrize("url", ["", "   ", "file:///etc/passwd", "ftp://x"])
def test_blank_or_non_http_url_is_off(monkeypatch, url):
    calls = _no_open(monkeypatch)
    assert not substrate_client.enabled({"SUBSTRATE_URL": url})
    assert substrate_client.rest_post("/events", {}, {"SUBSTRATE_URL": url}) is None
    assert calls == []


def test_substrate_disabled_wins(tmp_path, monkeypatch, on):
    monkeypatch.setenv("SUBSTRATE_DISABLED", "1")
    calls = _no_open(monkeypatch)
    _emit(tmp_path)
    assert calls == [] and not _db(tmp_path).exists()
    assert not substrate_client.enabled()


# --- client -------------------------------------------------------------------------------------------------
def test_rest_post_headers_status_and_timeout(on, fake):
    got = substrate_client.rest_post("/events", {"a": 1})
    assert got == (200, '{"ok":true}')
    c = fake.calls[0]
    assert c["headers"]["authorization"] == f"Bearer {TOKEN}" and c["headers"]["content-type"] == "application/json"
    assert c["timeout"] == 1.5 and c["body"] == {"a": 1}


def test_rest_post_reports_http_errors_and_swallows_the_rest(on, fake, monkeypatch):
    fake.events_status = 403
    assert substrate_client.rest_post("/events", {})[0] == 403
    substrate_client.reset()
    monkeypatch.setattr(substrate_client, "_open", lambda *a, **k: (_ for _ in ()).throw(TimeoutError(TOKEN)))
    assert substrate_client.rest_post("/events", {}) is None


@pytest.mark.parametrize("sse", [False, True])
def test_mcp_call_json_and_sse(on, monkeypatch, sse):
    f = FakeSubstrate(sse=sse)
    monkeypatch.setattr(substrate_client, "_open", f)
    got = substrate_client.mcp_call("graph_bind", {"correlation_id": "c", "graph_id": GID_A})
    assert got == {"status": "created", "correlation_id": "c", "graph_id": GID_A, "conflict": False}
    c = f.calls[0]
    assert c["headers"]["accept"] == "application/json, text/event-stream"
    assert c["body"] == {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "graph_bind", "arguments": {"correlation_id": "c", "graph_id": GID_A}}}


@pytest.mark.parametrize("reply", [
    '{"jsonrpc":"2.0","id":1,"result":{"isError":true,"content":[{"type":"text","text":"{\\"x\\":1}"}]}}',
    '{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"no"}}',
    '{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"not json"}]}}',
    "data: nonsense\n\n",
    "",
])
def test_mcp_call_failures_are_none(on, monkeypatch, reply):
    monkeypatch.setattr(substrate_client, "_open", lambda *a, **k: _Resp(reply))
    assert substrate_client.mcp_call("graph_bind", {}) is None


def test_dead_host_costs_one_attempt_per_backoff(on, monkeypatch):
    n = []
    monkeypatch.setattr(substrate_client, "_open", lambda *a, **k: n.append(1) or (_ for _ in ()).throw(OSError("down")))
    assert substrate_client.rest_post("/events", {}) is None and substrate_client.rest_post("/events", {}) is None
    assert len(n) == 1


def test_bearer_is_unredirected(on, fake):
    substrate_client.rest_post("/events", {"a": 1})
    req = fake.calls[0]["req"]
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert "Authorization" in req.unredirected_hdrs and "Authorization" not in req.headers


@pytest.fixture()
def http_server():
    """start(handler_cls) -> a ThreadingHTTPServer on 127.0.0.1:0 serving on a daemon thread; all shut down after."""
    servers = []

    def start(handler_cls):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


class _Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _drain(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))


def _point_at(monkeypatch, srv):
    monkeypatch.setenv("SUBSTRATE_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("SUBSTRATE_TOKEN", TOKEN)


def test_redirect_is_not_followed(monkeypatch, http_server):
    hits, seen = [], []

    class Other(_Quiet):
        def do_POST(self):
            hits.append(dict(self.headers))
            self._drain()
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

    other = http_server(Other)

    class Redirect(_Quiet):
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            self._drain()
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{other.server_address[1]}/x")
            self.send_header("Content-Length", "0")
            self.end_headers()

    _point_at(monkeypatch, http_server(Redirect))
    got = substrate_client.rest_post("/events", {"a": 1})
    assert got == (302, "")  # a non-2xx answer: the caller treats it as a failure
    assert seen == [f"Bearer {TOKEN}"] and hits == []  # the other host never heard from us, token or not


@pytest.mark.parametrize("own", [None, "", "   "])
def test_a_row_without_its_agents_token_stays_local(tmp_path, on, fake, monkeypatch, capsys, own):
    # no env file and no (or a blank) SUBSTRATE_TOKEN_SWARM_A08_QA: the row is not sent with SUBSTRATE_TOKEN, the
    # runner's (A01's) own token, which would misattribute it; the first such row says so once
    monkeypatch.delenv("SUBSTRATE_TOKEN_SWARM_A08_QA")
    if own is not None:
        monkeypatch.setenv("SUBSTRATE_TOKEN_SWARM_A08_QA", own)
    fake.bindings[CORR] = GID_A
    rec = _emit(tmp_path, typ="script.qa_gate", source="A08@qa_gate", task_id=None)
    _emit(tmp_path, typ="script.qa_gate", source="A08@qa_gate", task_id=None)
    assert rec["type"] == "script.qa_gate" and fake.at("/events") == []
    err = capsys.readouterr().err
    assert err.count("A08 run-log rows stay local") == 1 and TOKEN not in err
    assert substrate_client._token({"SUBSTRATE_TOKEN": " t "}, "swarm-a08-qa") == "t"


def test_a_row_goes_out_with_its_agents_env_file_token(tmp_path, on, fake, monkeypatch):
    # an S4 runner: only A01's token in the environment, and A05's in A05's env file for this workspace
    for surface in tee_mod.AGENT_SURFACES.values():
        monkeypatch.delenv(workspace.server_var(surface))
    a05 = "a05-env-file-token-0123456789"
    workspace.write_token(workspace.env_file(tmp_path, "a05-backend"), a05)
    fake.bindings[CORR] = GID_A
    _emit(tmp_path)  # source A05@code_checks
    (sent,) = fake.at("/events")
    assert sent["headers"]["authorization"] == f"Bearer {a05}"
    assert sent["body"]["surface"] == "swarm-a05-be"


def test_a_bad_env_file_is_not_bypassed_by_the_surface_variable(tmp_path, on, fake):
    path = workspace.env_file(tmp_path, "a05-backend")
    workspace.write_token(path, "a05-env-file-token-0123456789")
    path.chmod(0o644)  # readable by others: refused, and SUBSTRATE_TOKEN_SWARM_A05_BE is not tried instead
    fake.bindings[CORR] = GID_A
    _emit(tmp_path)
    assert fake.at("/events") == []


def test_surface_token_override_wins_and_never_leaks(tmp_path, on, fake, monkeypatch, capsys):
    a08, a01 = "a08-own-token-value", "a01-own-token-value"
    monkeypatch.setenv("SUBSTRATE_TOKEN_SWARM_A08_QA", a08)
    fake.bindings[CORR] = GID_A
    _emit(tmp_path, typ="script.qa_gate", source="A08@qa_gate", task_id=None)
    (lookup,) = fake.at("/mcp", "graph_bind")
    assert fake.at("/events")[0]["headers"]["authorization"] == f"Bearer {a08}"  # the gate speaks as itself
    assert lookup["headers"]["authorization"] == f"Bearer {TOKEN}"  # graph_bind is A01's; no A01 override set
    monkeypatch.setenv("SUBSTRATE_TOKEN_SWARM_A01_ORCH", a01)
    assert tee_mod.bind_graph("corr-other", GID_B, root=tmp_path) == GID_B
    assert fake.at("/mcp", "graph_bind")[-1]["headers"]["authorization"] == f"Bearer {a01}"
    assert substrate_client.rest_post("/events", {}, surface="swarm-a05-be")[0] == 200  # no A05 override: the default
    assert fake.calls[-1]["headers"]["authorization"] == f"Bearer {TOKEN}"
    out = capsys.readouterr()
    logged = (tmp_path / ".swarm" / "events.jsonl").read_text() + json.dumps(fake.events)
    for tok in (a08, a01, TOKEN):
        assert tok not in out.out + out.err + logged


@pytest.fixture()
def trickle_server():
    """Raw socket server: per connection, reads the request, sends a status line, then one header byte every 0.05 s for
    ever. Yields (port, conns); each conns entry is (closed_event, thread), closed_event set once the peer is gone."""
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(8)
    lsock.settimeout(0.05)
    stop, conns = threading.Event(), []

    def serve(c, closed):
        try:
            c.settimeout(2.0)
            c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\n")
            while not stop.is_set():
                time.sleep(0.05)
                if select.select([c], [], [], 0)[0] and c.recv(1) == b"":
                    break  # the client closed its side
                c.sendall(b"X")
        except OSError:
            pass  # send to a shut-down peer fails
        finally:
            closed.set()
            c.close()

    def accept():
        while not stop.is_set():
            try:
                c, _ = lsock.accept()
            except OSError:
                continue
            closed = threading.Event()
            t = threading.Thread(target=serve, args=(c, closed), daemon=True)
            conns.append((closed, t))
            t.start()

    acceptor = threading.Thread(target=accept, daemon=True)
    acceptor.start()
    yield lsock.getsockname()[1], conns
    stop.set()
    acceptor.join(2)
    lsock.close()


def test_trickling_headers_are_cut_at_the_deadline_without_lingering_threads(monkeypatch, trickle_server):
    port, conns = trickle_server
    monkeypatch.setenv("SUBSTRATE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(substrate_client, "TIMEOUT_S", 0.2)
    monkeypatch.setattr(substrate_client, "DEADLINE_SLACK_S", 0.1)
    baseline = threading.active_count()
    for i in range(3):
        substrate_client.reset()
        started = time.monotonic()
        assert substrate_client.rest_post("/events", {}) is None
        assert time.monotonic() - started < 1.5
        closed, server_thread = conns[i]
        assert closed.wait(1.0)  # the server saw the connection go away shortly after the deadline
        server_thread.join(1.0)
        if i == 0:  # a network failure: backed off, no new connection
            assert substrate_client.mcp_call("graph_bind", {}) is None and len(conns) == 1
        assert substrate_client._wait_idle(1.0)
        assert threading.active_count() <= baseline  # no worker thread left behind
    assert len(conns) == 3


def test_reply_over_the_body_cap_is_a_failure(on, monkeypatch):
    assert substrate_client.MAX_BODY_BYTES == 1 << 20
    monkeypatch.setattr(substrate_client, "MAX_BODY_BYTES", 10)
    monkeypatch.setattr(substrate_client, "_open", lambda *a, **k: _Resp("x" * 10))
    assert substrate_client.rest_post("/events", {}) == (200, "x" * 10)
    n = []
    monkeypatch.setattr(substrate_client, "_open", lambda *a, **k: n.append(1) or _Resp("x" * 11))
    assert substrate_client.rest_post("/events", {}) is None
    assert substrate_client.rest_post("/events", {}) is None and len(n) == 1  # counted as a failure: backed off


def test_real_reply_over_the_body_cap_is_a_failure(monkeypatch, http_server):
    sizes, hits = [10, 11], []

    class Big(_Quiet):
        def do_POST(self):
            self._drain()
            body = b"x" * sizes[len(hits)]
            hits.append(1)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    _point_at(monkeypatch, http_server(Big))
    monkeypatch.setattr(substrate_client, "MAX_BODY_BYTES", 10)
    assert substrate_client.rest_post("/events", {}) == (200, "x" * 10)
    assert substrate_client.rest_post("/events", {}) is None
    assert substrate_client.rest_post("/events", {}) is None and len(hits) == 2  # backed off


def test_real_error_reply_over_the_body_cap_backs_off(monkeypatch, http_server):
    hits = []

    class BigError(_Quiet):
        def do_POST(self):
            self._drain()
            hits.append(1)
            body = b"x" * 11
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    _point_at(monkeypatch, http_server(BigError))
    monkeypatch.setattr(substrate_client, "MAX_BODY_BYTES", 10)
    assert substrate_client.rest_post("/events", {}) is None
    assert substrate_client.rest_post("/events", {}) is None and len(hits) == 1  # backed off


def test_trickling_error_body_is_cut_at_the_deadline_and_backs_off(monkeypatch):
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(4)
    lsock.settimeout(0.05)
    stop, conns = threading.Event(), []

    def serve(c):
        try:
            c.settimeout(2.0)
            c.recv(65536)
            c.sendall(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 100\r\n\r\n")
            while not stop.is_set():
                time.sleep(0.05)
                c.sendall(b"X")
        except OSError:
            pass
        finally:
            c.close()

    def accept():
        while not stop.is_set():
            try:
                c, _ = lsock.accept()
            except OSError:
                continue
            conns.append(c)
            threading.Thread(target=serve, args=(c,), daemon=True).start()

    acceptor = threading.Thread(target=accept, daemon=True)
    acceptor.start()
    try:
        monkeypatch.setenv("SUBSTRATE_URL", f"http://127.0.0.1:{lsock.getsockname()[1]}")
        monkeypatch.setattr(substrate_client, "TIMEOUT_S", 0.2)
        monkeypatch.setattr(substrate_client, "DEADLINE_SLACK_S", 0.1)
        started = time.monotonic()
        assert substrate_client.rest_post("/events", {}) is None
        assert time.monotonic() - started < 1.5
        assert substrate_client.rest_post("/events", {}) is None and len(conns) == 1  # backed off
        assert substrate_client._wait_idle(1.0)
    finally:
        stop.set()
        acceptor.join(2)
        lsock.close()


def test_fake_http_error_body_over_the_cap_backs_off_but_none_body_is_an_answer(on, monkeypatch):
    monkeypatch.setattr(substrate_client, "MAX_BODY_BYTES", 10)
    n = []

    def big(req, timeout=None):
        n.append(1)
        raise urllib.error.HTTPError(req.full_url, 500, "no", {}, io.BytesIO(b"x" * 11))

    monkeypatch.setattr(substrate_client, "_open", big)
    assert substrate_client.rest_post("/events", {}) is None
    assert substrate_client.rest_post("/events", {}) is None and len(n) == 1  # backed off
    substrate_client.reset()
    m = []

    def empty(req, timeout=None):
        m.append(1)
        raise urllib.error.HTTPError(req.full_url, 503, "no", {}, None)

    monkeypatch.setattr(substrate_client, "_open", empty)
    assert substrate_client.rest_post("/events", {}) == (503, "")
    assert substrate_client.rest_post("/events", {}) == (503, "") and len(m) == 2  # an answer: no back-off


@pytest.fixture()
def fake_dns(monkeypatch):
    """`*.invalid` never reaches real DNS: it fails at once, except `slow.invalid`, whose lookup blocks until the yielded
    event is set (then fails). Released and drained on teardown."""
    release, real = threading.Event(), socket.getaddrinfo

    def getaddrinfo(host, *a, **k):
        if isinstance(host, str) and host.endswith(".invalid"):
            if host == "slow.invalid":
                release.wait()
            raise socket.gaierror(socket.EAI_NONAME, "test: no such host")
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield release
    release.set()
    substrate_client._wait_idle(2.0)


def _request_workers() -> int:
    return sum(t.name == "substrate-request" and t.is_alive() for t in threading.enumerate())


def test_stuck_dns_is_bounded_and_lingering_workers_are_capped(monkeypatch, fake_dns):
    monkeypatch.setenv("SUBSTRATE_URL", "http://slow.invalid:9")
    monkeypatch.setattr(substrate_client, "TIMEOUT_S", 0.2)
    monkeypatch.setattr(substrate_client, "DEADLINE_SLACK_S", 0.1)
    assert substrate_client._wait_idle(2.0)
    baseline, cap = threading.active_count(), substrate_client.MAX_IN_FLIGHT
    for i in range(cap + 2):
        substrate_client.reset()
        started = time.monotonic()
        assert substrate_client.rest_post("/events", {}) is None
        took = time.monotonic() - started
        assert took < 0.3 + 0.5  # the caller's deadline holds while DNS is stuck
        if i >= cap:
            assert took >= 0.2 and substrate_client._down_until == 0  # waits for capacity, without recording an outage
        assert _request_workers() == min(i + 1, cap)  # stuck in DNS, nothing to kill yet; never more than the cap
    fake_dns.set()
    assert substrate_client._wait_idle(2.0)
    assert _request_workers() == 0 and threading.active_count() <= baseline


def test_a_connect_finishing_after_the_deadline_is_killed():
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    lsock.settimeout(2.0)
    holder = substrate_client._Holder()
    holder.kill()  # the caller already gave up
    conn = substrate_client._TrackedHTTPConnection("127.0.0.1", lsock.getsockname()[1], timeout=2.0, holder=holder)
    try:
        with pytest.raises(OSError, match="substrate deadline"):
            conn.connect()
        c, _ = lsock.accept()
        c.settimeout(2.0)
        assert c.recv(1) == b""  # the server saw the connection closed
        c.close()
    finally:
        conn.close()
        lsock.close()


def _proxy(http_server, seen):
    class Proxy(_Quiet):
        def do_POST(self):
            self._drain()
            seen.append((self.requestline, dict(self.headers)))
            body = b'{"accepted":true}'
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return http_server(Proxy)


def test_http_proxy_is_honoured_and_only_the_authorization_header_carries_the_bearer(monkeypatch, http_server, fake_dns):
    seen = []
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{_proxy(http_server, seen).server_address[1]}")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.invalid:9")  # unresolvable directly
    monkeypatch.setenv("SUBSTRATE_TOKEN", TOKEN)
    assert substrate_client.rest_post("/events", {"a": 1}) == (202, '{"accepted":true}')
    ((line, headers),) = seen
    assert line == "POST http://substrate.invalid:9/events HTTP/1.1"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert [k for k, v in headers.items() if TOKEN in v] == ["Authorization"]


def test_no_proxy_bypasses_the_proxy(monkeypatch, http_server, fake_dns):
    seen = []
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{_proxy(http_server, seen).server_address[1]}")
    monkeypatch.setenv("NO_PROXY", "substrate.invalid")
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.invalid:9")
    monkeypatch.setenv("SUBSTRATE_TOKEN", TOKEN)
    assert substrate_client.rest_post("/events", {"a": 1}) is None  # direct: the name does not resolve
    assert seen == []



# --- tables -------------------------------------------------------------------------------------------------
def test_surfaces_match_agents_json():
    agents = json.loads((ROOT / "agents.json").read_text())["agents"]
    assert {a["id"] for a in agents} == set(tee_mod.AGENT_SURFACES) and len(agents) == 30
    for a in agents:
        assert tee_mod.AGENT_SURFACES[a["id"]] == f"swarm-{a['id'].lower()}-{a['code'].lower()}"
    assert list(tee_mod.AGENT_SURFACES.values())[0] == "swarm-a01-orch" and list(tee_mod.AGENT_SURFACES.values())[-1] == "swarm-a30-custody"
    assert [i for i in tee_mod.AGENT_SURFACES] == [f"A{n:02d}" for n in range(1, 31)]


EVENT_KINDS = {"session.start", "prompt", "claim", "tool.call", "file.edit", "shell", "commit", "pr", "handoff", "session.end",
               "note", "warning"}
_SOURCES = [*sorted((ROOT / "swarm").glob("*.py")), *sorted((ROOT / "scripts").glob("*.py")), *sorted((ROOT / "hooks").glob("*.py"))]
# Ctx.emit and Telemetry.emit forward their own `event_type` argument to runlog.emit; every Telemetry.emit CALL site is literal.
# scripts/orch_status.py `_emit_ingest_events` replays event types the ingest manifest already recorded; those types were classified at the writer.
_PASS_THROUGH = frozenset({"swarm/script_base.py", "swarm/telemetry.py", "scripts/orch_status.py"})


def _emit_type_arg(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    return next((kw.value for kw in call.keywords if kw.arg == "event_type"), None)


def _emit_names(tree: ast.AST) -> set[str]:
    """`emit` plus every name bound by `from <pkg>.runlog import emit as X` (hook_trigger imports it as runlog_emit)."""
    names = {"emit"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "runlog":
            names.update(a.asname for a in node.names if a.name == "emit" and a.asname)
    return names


def _emit_sites():
    """(literal swarm types -> files, non-literal emit sites) over every emit call (direct, aliased or method) in the AST."""
    literal, other = {}, []
    for path in _SOURCES:
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        names = _emit_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not ((isinstance(f, ast.Name) and f.id in names) or (isinstance(f, ast.Attribute) and f.attr == "emit")):
                continue
            arg = _emit_type_arg(node)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                literal.setdefault(arg.value, []).append(path.name)
            elif isinstance(arg, ast.JoinedStr):
                # an f-string is a family: its leading literal names it (f"phase.{phase}" is the phase. family), and the
                # placeholder is rendered as `x` so PREFIX_KINDS resolves it like any other member of that family
                text = "".join(p.value if isinstance(p, ast.Constant) else "x" for p in arg.values)
                literal.setdefault(text, []).append(path.name)
            elif not (rel in _PASS_THROUGH and isinstance(arg, ast.Name) and arg.id == "event_type"):
                other.append((rel, node.lineno, ast.unparse(node)[:60]))
    return literal, other


def test_every_emitted_type_is_mapped_and_nothing_is_stale():
    literal, other = _emit_sites()
    assert other == [], f"emit site with a non-literal type: register it explicitly in the test and the table: {other}"
    assert len(literal) >= 12, literal
    unmapped = sorted(t for t in literal if tee_mod.kind_for(t) is None)
    assert unmapped == [], f"unmapped swarm event types (add to substrate_tee.TYPE_KINDS): {unmapped}"
    assert set(tee_mod.TYPE_KINDS) <= set(literal), "TYPE_KINDS lists a type nothing emits"
    for prefix, _ in tee_mod.PREFIX_KINDS:
        assert any(t.startswith(prefix) and len(t) > len(prefix) for t in literal), f"PREFIX_KINDS family with no emit site: {prefix}"
    assert {"script.x", "script.x.error", "phase.x", "swarm.start"} <= set(literal)


def test_kinds_are_valid_and_follow_the_documented_rules():
    assert set(tee_mod.TYPE_KINDS.values()) | {k for _, k in tee_mod.PREFIX_KINDS} <= EVENT_KINDS
    for t in ("gate.verdict.unrecorded", "gate.findings.coerced", "gate.findings.synthesized", "gate.findings.unattributed"):
        assert tee_mod.kind_for(t) == "warning"
    assert tee_mod.kind_for("plan.updated") == tee_mod.kind_for("task.transition") == "note"
    assert tee_mod.kind_for("script.orch_plan") == tee_mod.kind_for("script.qa_gate.error") == "tool.call"
    assert tee_mod.kind_for("task.claimed") == "claim"
    assert tee_mod.kind_for("made.up") is None and tee_mod.kind_for("script.") is None


def test_script_agent_ids_are_known():
    for path in (ROOT / "scripts").glob("*.py"):
        for aid in re.findall(r'AgentScript\(\s*"(A\d{2})"', path.read_text(encoding="utf-8")):
            assert aid in tee_mod.AGENT_SURFACES, path.name


@pytest.mark.parametrize("rec,agent", [
    ({"source": "A05@code_checks"}, "A05"),
    ({"source": "A15@docs_bundle"}, "A15"),
    ({"source": "A30@custody_policy_check"}, "A30"),
    ({"source": "swarm.envelope", "payload": {"source": "A08@local"}}, "A08"),
    ({"source": "swarm.envelope", "payload": {"source": "A99"}}, None),
    ({"source": "swarm.envelope", "payload": {}}, None),
    ({"source": "t"}, None),
    ({"source": "A31@x"}, None),
])
def test_agent_of(rec, agent):
    assert tee_mod.agent_of(rec) == agent


# --- Graph ID -----------------------------------------------------------------------------------------------
def test_mint_graph_id_format_and_clock():
    before = int(time.time() * 1000)
    gid = tee_mod.mint_graph_id()
    after = int(time.time() * 1000)
    m = re.fullmatch(r"ut-([0-9a-z]+)-([0-9a-f]{8})", gid)
    assert m and before <= int(m.group(1), 36) <= after
    assert tee_mod.mint_graph_id(0).startswith("ut-0-") and tee_mod.mint_graph_id(36).startswith("ut-10-")
    assert tee_mod.mint_graph_id() != tee_mod.mint_graph_id()


def test_resolve_order_explicit_env_brief_mint():
    brief = f'<ISSUES graphId="{GID_B}"> ... Graph ID: ut-zzzz-ffffffff'
    env = {"SUBSTRATE_GRAPH_ID": "ut-fromenv-00000001"}
    assert tee_mod.resolve_graph_id("c", explicit=GID_A, env=env, brief_text=brief) == GID_A
    assert tee_mod.resolve_graph_id("c", env=env, brief_text=brief) == "ut-fromenv-00000001"
    assert tee_mod.resolve_graph_id("c", env={}, brief_text=brief) == GID_B
    assert tee_mod.resolve_graph_id("c", env={}, brief_text="Graph ID: ut-q1-deadbeef.") == "ut-q1-deadbeef"
    assert tee_mod.resolve_graph_id("c", env={}, brief_text="no id here ut-bad-xyz").startswith("ut-")
    assert tee_mod.resolve_graph_id("c", explicit="garbage", env={"SUBSTRATE_GRAPH_ID": "junk"}, brief_text=None) != "garbage"


def test_bind_adopts_the_servers_winner_even_on_conflict(tmp_path, on, monkeypatch):
    f = FakeSubstrate(bindings={CORR: GID_A})
    monkeypatch.setattr(substrate_client, "_open", f)
    assert tee_mod.bind_graph(CORR, GID_B, root=tmp_path) == GID_A
    assert f.at("/mcp", "graph_bind")[0]["body"]["params"]["arguments"] == {"correlation_id": CORR, "graph_id": GID_B}
    assert tee_mod.cached_graph_id(CORR, tmp_path) == GID_A
    # a later process teeing for this correlation uses the winner without asking again
    mcp_before = len(f.at("/mcp"))
    rec = _emit(tmp_path)
    assert rec["type"] == "task.claimed"
    assert [e["graph_id"] for e in f.events] == [GID_A] and f.events[0]["session_id"] == f"A05@r0:{GID_A}"
    assert len(f.at("/mcp")) == mcp_before  # emit added only the /events request


def test_bind_without_an_answer_caches_nothing(tmp_path, on, monkeypatch):
    monkeypatch.setattr(substrate_client, "_open", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert tee_mod.bind_graph(CORR, GID_A, root=tmp_path) is None
    assert tee_mod.lookup_graph_id(CORR, root=tmp_path) is None
    assert tee_mod.cached_graph_id(CORR, tmp_path) is None


@pytest.mark.parametrize("refusal", [401, 403, "identity"])
def test_refused_graph_lookup_keeps_local_logs_and_does_not_cache_the_refusal(tmp_path, on, monkeypatch, refusal):
    def denied(req, timeout=None):
        if refusal != "identity":
            return _Resp('{"error":"unauthorized"}', refusal)
        return _Resp(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "isError": True, "content": [{"type": "text", "text": '{"error":"caller identity rejected"}'}]}}))

    monkeypatch.setattr(substrate_client, "_open", denied)
    refused = _emit(tmp_path)
    assert runlog.read_events(correlation_id=CORR, root=tmp_path) == [refused]
    assert tee_mod.cached_graph_id(CORR, tmp_path) is None
    recovered = FakeSubstrate(bindings={CORR: GID_A})
    monkeypatch.setattr(substrate_client, "_open", recovered)
    later = _emit(tmp_path)
    assert runlog.read_events(correlation_id=CORR, root=tmp_path) == [refused, later]
    assert [event["graph_id"] for event in recovered.events] == [GID_A]


def test_binding_cache_is_per_server(tmp_path, on, fake, monkeypatch):
    fake.bindings[CORR] = GID_A
    assert tee_mod.bind_graph(CORR, GID_A, root=tmp_path) == GID_A
    assert tee_mod.cached_graph_id(CORR, tmp_path) == GID_A
    monkeypatch.setenv("SUBSTRATE_URL", "http://other.test:9000")
    assert tee_mod.cached_graph_id(CORR, tmp_path) is None
    fake.bindings[CORR] = GID_B  # the other server bound this correlation elsewhere
    lookups = len(fake.at("/mcp", "graph_bind"))
    _emit(tmp_path)
    assert len(fake.at("/mcp", "graph_bind")) == lookups + 1  # asked the new server, did not reuse the old binding
    assert [e["graph_id"] for e in fake.events] == [GID_B] and tee_mod.cached_graph_id(CORR, tmp_path) == GID_B
    monkeypatch.setenv("SUBSTRATE_URL", "http://substrate.test:8787")  # same server as `on`, normalised
    assert tee_mod.cached_graph_id(CORR, tmp_path) == GID_A


def test_legacy_binding_table_is_not_read(tmp_path, on):
    tee_mod._connect(tmp_path).close()
    con = sqlite3.connect(_db(tmp_path))
    con.execute("CREATE TABLE IF NOT EXISTS bindings (correlation_id TEXT PRIMARY KEY, graph_id TEXT NOT NULL, created REAL NOT NULL)")
    con.execute("INSERT INTO bindings VALUES (?, ?, ?)", (CORR, GID_A, time.time()))
    con.commit()
    con.close()
    assert tee_mod.cached_graph_id(CORR, tmp_path) is None


# --- the tee ------------------------------------------------------------------------------------------------
def _fake_git(monkeypatch, stdout="", returncode=0, exc=None):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    monkeypatch.setattr(tee_mod.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("remote,slug", [
    ("https://github.com/owner/name.git\n", "owner/name"),
    ("https://github.com/owner/name\n", "owner/name"),
    ("git@github.com:o/r.git\n", "o/r"),
])
def test_repo_slug_parses_the_origin_remote(tmp_path, monkeypatch, remote, slug):
    calls = _fake_git(monkeypatch, stdout=remote)
    assert tee_mod.repo_slug(tmp_path) == slug
    assert tee_mod.repo_slug(tmp_path) == slug and len(calls) == 1  # memoized per root
    cmd, kw = calls[0]
    assert cmd == ["git", "-C", str(tmp_path.resolve()), "remote", "get-url", "origin"] and kw["timeout"] == 2


@pytest.mark.parametrize("kw", [{"returncode": 2}, {"stdout": "\n"}, {"exc": FileNotFoundError("git")},
                                {"exc": subprocess.TimeoutExpired("git", 2)}])
def test_repo_slug_falls_back_to_the_directory_name(tmp_path, monkeypatch, kw):
    _fake_git(monkeypatch, **kw)
    assert tee_mod.repo_slug(tmp_path) == tmp_path.resolve().name


def test_repo_slug_with_no_remote_is_the_directory_name(tmp_path):
    repo = tmp_path / "plainrepo"
    repo.mkdir()
    assert tee_mod.repo_slug(repo) == "plainrepo"


def test_build_event_carries_the_repo_slug(tmp_path, monkeypatch):
    _fake_git(monkeypatch, stdout="git@github.com:o/r.git\n")
    rec = {"type": "task.claimed", "source": "A05@x", "correlation_id": CORR, "msg_id": "m", "task_id": "T1", "payload": {}}
    assert tee_mod.build_event(rec, GID_A, root=tmp_path)["repo"] == "o/r"
    assert "repo" not in tee_mod.build_event(rec, GID_A)


def test_observability_progress_events_are_attributed_and_forwarded(tmp_path):
    from swarm import observability
    sid = "subagent-T1-abc123-1"
    recs = [
        observability.emit_gsd_omp("spawn", subagent_id=sid, task_id="T1", corr=CORR, root=tmp_path),
        observability.stream_subagent_status(sid, "spawned", task_id="T1", corr=CORR, root=tmp_path),
        observability.emit_error_agg(sid, {"errors": 1}, task_id="T1", corr=CORR, root=tmp_path),
    ]
    for rec in recs:
        assert tee_mod.agent_of(rec) == "A01", rec["source"]
        body = tee_mod.build_event(rec, GID_A)
        assert body is not None and body["surface"] == tee_mod.AGENT_SURFACES["A01"]
        assert body["payload"]["swarm_type"] == rec["type"]

def test_dry_run_gsd_progress_is_not_forwarded_but_real_progress_is(tmp_path, on, fake):
    from swarm import observability
    fake.bindings[CORR] = GID_A
    sid = "subagent-T1-abc123-1"
    simulated = observability.emit_gsd_omp("spawn", subagent_id=sid, task_id="T1", corr=CORR, root=tmp_path, dry_run=True)
    assert tee_mod.build_event(simulated, GID_A) is not None  # attributable on its own: only tee=False keeps it local
    assert fake.events == [] and fake.at("/events") == []
    real = observability.emit_gsd_omp("spawn", subagent_id=sid, task_id="T1", corr=CORR, root=tmp_path)
    (body,) = fake.events
    assert body["surface"] == tee_mod.AGENT_SURFACES["A01"] and body["payload"]["msg_id"] == real["msg_id"]
    assert body["payload"]["swarm_type"] == "gsd.autonomous.omp"


def test_tee_event_fields(tmp_path, on, fake, monkeypatch):
    monkeypatch.setenv("SWARM_REPLICA", "r7")
    fake.bindings[CORR] = GID_A
    rec = _emit(tmp_path)
    (body,) = fake.events
    assert body == {
        "kind": "claim", "summary": "task.claimed T1-be", "surface": "swarm-a05-be", "session_id": f"A05@r7:{GID_A}",
        "graph_id": GID_A, "node_id": "T1-be", "actor": "agent", "repo": tmp_path.name,
        "payload": {"correlation_id": CORR, "msg_id": rec["msg_id"], "swarm_type": "task.claimed", "status": "ok"},
    }
    call = fake.at("/events")[0]
    assert call["headers"]["authorization"] == f"Bearer {TOKEN}" and TOKEN not in json.dumps(body)


def test_tee_default_replica_and_optional_ids(tmp_path, on, fake):
    fake.bindings[CORR] = GID_A
    rec = {"type": "plan.updated", "source": "A01@orch_plan", "correlation_id": CORR, "task_id": None, "payload": {},
           "msg_id": "m1", "trace_id": "tr", "causation_id": "ca"}
    assert tee_mod.tee(rec, tmp_path) is True
    (body,) = fake.events
    assert body["session_id"] == f"A01@r0:{GID_A}" and body["kind"] == "note" and "node_id" not in body
    assert body["payload"] == {"correlation_id": CORR, "msg_id": "m1", "swarm_type": "plan.updated", "trace_id": "tr", "causation_id": "ca"}


def test_tee_script_exit_is_tool_call_with_swarm_type(tmp_path, on, fake):
    fake.bindings[CORR] = GID_A
    _emit(tmp_path, typ="script.qa_gate", source="A08@qa_gate", task_id=None)
    _emit(tmp_path, typ="script.qa_gate.error", source="A08@qa_gate", task_id=None)
    assert [(e["kind"], e["surface"], e["payload"]["swarm_type"]) for e in fake.events] == [
        ("tool.call", "swarm-a08-qa", "script.qa_gate"), ("tool.call", "swarm-a08-qa", "script.qa_gate.error")]


@pytest.mark.parametrize("rec", [
    {"type": "made.up.type", "source": "A05@x", "correlation_id": CORR, "msg_id": "m"},   # unmapped: never a silent note
    {"type": "task.claimed", "source": "t", "correlation_id": CORR, "msg_id": "m"},       # no agent identity
    {"type": "task.claimed", "source": "A05@x", "correlation_id": None, "msg_id": "m"},   # no correlation -> no graph
    {"type": "task.claimed", "source": "A05@x", "correlation_id": CORR},                  # no msg_id
])
def test_tee_skips_what_it_cannot_attribute(tmp_path, on, fake, rec):
    fake.bindings[CORR] = GID_A
    assert tee_mod.tee({"task_id": None, "payload": {}, **rec}, tmp_path) is False
    assert fake.events == []


def test_unbound_correlation_costs_one_lookup_across_tees(tmp_path, on, fake):
    _emit(tmp_path)
    _emit(tmp_path, typ="plan.updated", source="A01@orch_plan", task_id=None)
    assert fake.events == [] and len(fake.at("/mcp", "graph_bind")) == 1
    assert tee_mod.cached_graph_id(CORR, tmp_path) is None  # the negative result is never persisted


def test_unbound_negative_cache_expires_and_bind_clears_it(tmp_path, on, fake):
    _emit(tmp_path)
    _emit(tmp_path)
    assert len(fake.at("/mcp", "graph_bind")) == 1
    tee_mod._unbound[CORR] = time.monotonic() - 1  # TTL elapsed
    _emit(tmp_path)
    assert len(fake.at("/mcp", "graph_bind")) == 2
    _emit(tmp_path)
    assert len(fake.at("/mcp", "graph_bind")) == 2
    assert tee_mod.bind_graph(CORR, GID_A, root=tmp_path) == GID_A  # a bind clears the negative entry
    assert CORR not in tee_mod._unbound
    _emit(tmp_path)
    assert [e["graph_id"] for e in fake.events] == [GID_A]


def test_unbound_correlation_is_skipped_after_one_forward_lookup(tmp_path, on, fake):
    _emit(tmp_path)
    assert fake.events == []
    (lookup,) = fake.at("/mcp", "graph_bind")
    assert lookup["body"]["params"]["arguments"] == {"correlation_id": CORR}
    assert tee_mod.cached_graph_id(CORR, tmp_path) is None


def test_forward_lookup_is_cached_across_emits(tmp_path, on, fake):
    fake.bindings[CORR] = GID_B
    _emit(tmp_path)
    _emit(tmp_path, typ="plan.updated", source="A01@orch_plan", task_id=None)
    assert len(fake.at("/mcp", "graph_bind")) == 1 and [e["graph_id"] for e in fake.events] == [GID_B, GID_B]


def test_server_refusal_drops_the_event_and_a_republish_retries(tmp_path, on, fake):
    fake.bindings[CORR] = GID_A
    fake.events_status = 403
    rec = _emit(tmp_path)
    assert len(fake.events) == 1
    fake.events_status = 200
    assert tee_mod.tee(rec, tmp_path) is True and len(fake.events) == 2


# --- dedupe -------------------------------------------------------------------------------------------------
def test_double_publish_of_one_record_is_one_request(tmp_path, on, fake):
    fake.bindings[CORR] = GID_A
    rec = _emit(tmp_path)
    assert tee_mod.tee(rec, tmp_path) is False and tee_mod.tee(dict(rec), tmp_path) is False
    assert len(fake.events) == 1
    rec2 = _emit(tmp_path)  # a different message is a different key
    assert rec2["msg_id"] != rec["msg_id"] and len(fake.events) == 2


def test_dedupe_key_is_surface_plus_msg_id(tmp_path, on, fake):
    fake.bindings[CORR] = GID_A
    base = {"type": "task.claimed", "correlation_id": CORR, "task_id": "T", "payload": {}, "msg_id": "same"}
    assert tee_mod.tee({**base, "source": "A05@x"}, tmp_path) and tee_mod.tee({**base, "source": "A06@x"}, tmp_path)
    assert [e["surface"] for e in fake.events] == ["swarm-a05-be", "swarm-a06-fe"]


def test_dedupe_window_is_24h_and_pruned_on_write(tmp_path, on, fake):
    fake.bindings[CORR] = GID_A
    _emit(tmp_path)
    con = sqlite3.connect(_db(tmp_path))
    con.execute("INSERT INTO seen(surface, msg_id, first_seen) VALUES ('swarm-a05-be', 'ancient', ?)", (time.time() - 25 * 3600,))
    con.execute("INSERT INTO seen(surface, msg_id, first_seen) VALUES ('swarm-a05-be', 'recent', ?)", (time.time() - 23 * 3600,))
    con.commit()
    con.close()
    tee_mod._last_prune = None  # the first emit pruned; let the next one prune again
    _emit(tmp_path)
    con = sqlite3.connect(_db(tmp_path))
    ids = {r[0] for r in con.execute("SELECT msg_id FROM seen")}
    con.close()
    assert "ancient" not in ids and "recent" in ids and len(ids) == 3


def _put_seen(tmp_path, msg_id, state, age_s):
    tee_mod._connect(tmp_path).close()
    con = sqlite3.connect(_db(tmp_path))
    con.execute("INSERT OR REPLACE INTO seen(surface, msg_id, first_seen, state) VALUES ('swarm-a05-be', ?, ?, ?)",
                (msg_id, time.time() - age_s, state))
    con.commit()
    con.close()


def _seen_rows(tmp_path):
    con = sqlite3.connect(_db(tmp_path))
    rows = {r[0]: (r[1], r[2]) for r in con.execute("SELECT msg_id, state, first_seen FROM seen")}
    con.close()
    return rows


def test_success_marks_sent_and_failure_deletes_the_claim(tmp_path, on, fake):
    fake.bindings[CORR] = GID_A
    ok = _emit(tmp_path)
    assert _seen_rows(tmp_path)[ok["msg_id"]][0] == "sent"
    fake.events_status = 503
    failed = _emit(tmp_path)
    assert failed["msg_id"] not in _seen_rows(tmp_path) and len(fake.events) == 2


@pytest.mark.parametrize("state,age_s,reclaimed", [
    ("pending", 5, False),           # another sender is on it right now
    ("pending", 61, True),           # its sender was killed between claim and send
    ("sent", 23 * 3600, False),      # already delivered inside the window
    ("sent", 25 * 3600, True),       # past the window (even before a prune removes it)
])
def test_claim_pending_versus_sent(tmp_path, on, fake, state, age_s, reclaimed):
    fake.bindings[CORR] = GID_A
    _put_seen(tmp_path, "m-x", state, age_s)
    tee_mod._last_prune = time.monotonic()  # no prune in this claim: the claim rule alone decides
    rec = {"type": "task.claimed", "source": "A05@x", "correlation_id": CORR, "task_id": "T", "payload": {}, "msg_id": "m-x"}
    assert tee_mod.tee(rec, tmp_path) is reclaimed
    assert len(fake.events) == (1 if reclaimed else 0)
    got_state, first_seen = _seen_rows(tmp_path)["m-x"]
    assert got_state == ("sent" if reclaimed else state)
    assert (time.time() - first_seen < 60) is (reclaimed or age_s < 60)


def test_reclaim_is_exclusive(tmp_path, on):
    _put_seen(tmp_path, "m-y", "pending", 120)
    assert tee_mod._claim(tmp_path, "swarm-a05-be", "m-y") is True
    assert tee_mod._claim(tmp_path, "swarm-a05-be", "m-y") is False  # the re-claim refreshed the timestamp


def test_old_seen_table_is_migrated(tmp_path, on):
    (tmp_path / ".swarm").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_db(tmp_path))
    con.execute("CREATE TABLE seen (surface TEXT NOT NULL, msg_id TEXT NOT NULL, first_seen REAL NOT NULL, PRIMARY KEY (surface, msg_id))")
    con.execute("INSERT INTO seen VALUES ('swarm-a05-be', 'old', ?)", (time.time() - 3600,))
    con.commit()
    con.close()
    assert tee_mod._claim(tmp_path, "swarm-a05-be", "old") is False  # an old row counts as sent
    assert _seen_rows(tmp_path)["old"][0] == "sent"
    assert tee_mod._claim(tmp_path, "swarm-a05-be", "new") is True and _seen_rows(tmp_path)["new"][0] == "pending"


def test_prune_runs_at_most_once_per_interval_and_is_indexed(tmp_path, on, monkeypatch):
    statements = []
    real = tee_mod._connect

    def traced(root):
        con = real(root)
        con.set_trace_callback(statements.append)
        return con
    monkeypatch.setattr(tee_mod, "_connect", traced)

    def prunes():
        return sum(1 for s in statements if s.startswith("DELETE FROM seen WHERE first_seen"))
    assert tee_mod._claim(tmp_path, "swarm-a05-be", "p1") and tee_mod._claim(tmp_path, "swarm-a05-be", "p2")
    assert prunes() == 1
    tee_mod._last_prune -= tee_mod.PRUNE_EVERY_S + 1
    assert tee_mod._claim(tmp_path, "swarm-a05-be", "p3") and prunes() == 2
    con = sqlite3.connect(_db(tmp_path))
    plan = " ".join(str(r) for r in con.execute("EXPLAIN QUERY PLAN DELETE FROM seen WHERE first_seen < 0"))
    con.close()
    assert "seen_first_seen" in plan


# --- fail-open ----------------------------------------------------------------------------------------------
def test_tee_never_raises(tmp_path, on, fake, monkeypatch):
    fake.bindings[CORR] = GID_A
    monkeypatch.setattr(tee_mod, "_claim", lambda *a, **k: 1 / 0)
    assert tee_mod.tee({"type": "task.claimed", "source": "A05@x", "correlation_id": CORR, "msg_id": "m", "payload": {}}, tmp_path) is False
    assert tee_mod.tee(None, tmp_path) is False  # type: ignore[arg-type]
    monkeypatch.setattr(tee_mod, "tee", lambda *a, **k: 1 / 0)  # runlog.emit swallows even a broken tee
    assert _emit(tmp_path)["type"] == "task.claimed"


def test_runlog_survives_a_dead_substrate(tmp_path, on, monkeypatch, capsys):
    monkeypatch.setattr(substrate_client, "_open", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError(TOKEN)))
    rec = _emit(tmp_path)
    assert rec["type"] == "task.claimed" and len((tmp_path / ".swarm" / "events.jsonl").read_text().splitlines()) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


# --- orch_plan end to end -----------------------------------------------------------------------------------
def test_orch_plan_unchanged_when_substrate_unreachable_in_process(tmp_path, on, capsys, monkeypatch):
    attempts = []
    monkeypatch.setattr(substrate_client, "_open",
                        lambda *a, **k: attempts.append(a) or (_ for _ in ()).throw(urllib.error.URLError(f"refused {TOKEN}")))
    rc, out, raw = _orch_plan(capsys, tmp_path, "--brief-text", "billing", "--prefix", "R", "--correlation-id", CORR)
    assert rc == 0 and out["status"] == "ok" and len(out["tasks"]) == 13 and out["correlation_id"] == CORR
    assert attempts, "the unreachable substrate was never attempted"
    assert Path(out["plan_file"]).exists() and (tmp_path / ".swarm" / "tasks.db").exists()
    assert TOKEN not in raw.out + raw.err
    # the second run of the same plan still reuses
    rc, again, _ = _orch_plan(capsys, tmp_path, "--brief-text", "billing", "--prefix", "R", "--correlation-id", CORR)
    assert rc == 0 and again["reused"] is True


def test_orch_plan_unchanged_when_substrate_unreachable_subprocess(tmp_path):
    for name in ("plain", "down"):
        (tmp_path / name).mkdir()
    base = ["--json", "--brief-text", "billing", "--prefix", "R", "--correlation-id", CORR]
    plain = run_script("orch_plan.py", *base, "--root", str(tmp_path / "plain"), env={"SWARM_DIR": str(tmp_path / "plain" / ".swarm")})
    down = run_script("orch_plan.py", *base, "--root", str(tmp_path / "down"),
                      env={"SWARM_DIR": str(tmp_path / "down" / ".swarm"), "SUBSTRATE_URL": "http://127.0.0.1:1", "SUBSTRATE_TOKEN": TOKEN})
    assert plain.returncode == 0 and down.returncode == 0, plain.stdout + plain.stderr + down.stdout + down.stderr
    a, b = json.loads(plain.stdout), json.loads(down.stdout)
    assert set(a) == set(b) and [t["task_id"] for t in a["tasks"]] == [t["task_id"] for t in b["tasks"]]
    assert a["status"] == b["status"] == "ok" and TOKEN not in down.stdout + down.stderr
    assert not (tmp_path / "plain" / ".swarm" / "substrate-tee.db").exists()


def test_orch_plan_without_substrate_creates_no_tee_state(tmp_path, capsys, monkeypatch):
    calls = _no_open(monkeypatch)
    rc, out, _ = _orch_plan(capsys, tmp_path, "--brief-text", "billing", "--prefix", "R")
    assert rc == 0 and calls == [] and not _db(tmp_path).exists()


def test_orch_plan_binds_registers_and_tees(tmp_path, on, fake, capsys):
    rc, out, _ = _orch_plan(capsys, tmp_path, "--brief-text", f"billing\nGraph ID: {GID_B}", "--prefix", "R", "--correlation-id", CORR)
    assert rc == 0
    (bind,) = fake.at("/mcp", "graph_bind")
    assert bind["body"]["params"]["arguments"] == {"correlation_id": CORR, "graph_id": GID_B}  # brief text > minted
    (reg,) = fake.at("/mcp", "graph_register")
    args = reg["body"]["params"]["arguments"]
    assert args["graph_id"] == GID_B and args["repo"] == "myrepo" and args["surface"] == "swarm-a01-orch" and args["status"] == "planned"
    assert args["nodes"] == [{"node_id": t["task_id"]} for t in out["tasks"]]
    # bind happens before the first tee'd event, which are the plan snapshot and the script exit
    paths = [c["path"] + (c["body"]["params"]["name"] if c["path"] == "/mcp" else "") for c in fake.calls]
    assert paths[:2] == ["/mcpgraph_bind", "/mcpgraph_register"] and paths[2:] == ["/events", "/events"]
    assert [(e["kind"], e["payload"]["swarm_type"]) for e in fake.events] == [("note", "plan.updated"), ("tool.call", "script.orch_plan")]
    assert {e["surface"] for e in fake.events} == {"swarm-a01-orch"} and {e["session_id"] for e in fake.events} == {f"A01@r0:{GID_B}"}
    assert tee_mod.cached_graph_id(CORR, tmp_path / "myrepo") == GID_B


def test_orch_plan_graph_id_flag_and_conflict_adoption(tmp_path, on, monkeypatch, capsys):
    f = FakeSubstrate(bindings={CORR: GID_A})
    monkeypatch.setattr(substrate_client, "_open", f)
    rc, out, _ = _orch_plan(capsys, tmp_path, "--brief-text", "billing", "--prefix", "R", "--correlation-id", CORR, "--graph-id", GID_B)
    assert rc == 0
    assert f.at("/mcp", "graph_bind")[0]["body"]["params"]["arguments"]["graph_id"] == GID_B
    assert f.at("/mcp", "graph_register")[0]["body"]["params"]["arguments"]["graph_id"] == GID_A  # the stored winner
    assert {e["graph_id"] for e in f.events} == {GID_A}


def test_orch_plan_rejects_a_malformed_graph_id(tmp_path, capsys):
    rc, out, _ = _orch_plan(capsys, tmp_path, "--brief-text", "billing", "--graph-id", "not-a-graph")
    assert rc == 2 and out["error"]["code"] == "E-INPUT"


def test_replan_reuses_and_does_not_rebind(tmp_path, on, fake, capsys):
    base = ("--brief-text", "billing", "--prefix", "R", "--correlation-id", CORR)
    assert _orch_plan(capsys, tmp_path, *base)[0] == 0
    binds = len(fake.at("/mcp", "graph_bind"))
    rc, again, _ = _orch_plan(capsys, tmp_path, *base)
    assert rc == 0 and again["reused"] is True
    assert len(fake.at("/mcp", "graph_bind")) == binds and len(fake.at("/mcp", "graph_register")) == 1


def test_dry_run_plan_never_touches_substrate(tmp_path, on, fake, capsys):
    rc, out, _ = _orch_plan(capsys, tmp_path, "--dry-run")
    assert rc == 0 and out["dry_run"] is True
    assert fake.calls == []  # a dry run has no correlation on the context: no bind, no register, no tee


def _probe(tmp_path, *args):
    from swarm.script_base import AgentScript
    return AgentScript("A08", "qa_probe", lambda a, ctx: {"summary": "probe"}).main(["--json", "--root", str(tmp_path), *args])


@pytest.mark.parametrize("inherited", [False, True], ids=["flag", "env"])
def test_dry_run_with_a_bound_correlation_never_tees(tmp_path, on, fake, monkeypatch, capsys, inherited):
    fake.bindings[CORR] = GID_A
    tee_mod.remember_graph_id(CORR, GID_A, tmp_path)  # already bound and cached: only the event POST would remain
    if inherited:
        monkeypatch.setenv("SWARM_CORRELATION_ID", CORR)
    corr_args = () if inherited else ("--correlation-id", CORR)
    assert _probe(tmp_path, "--dry-run", *corr_args) == 0
    assert fake.calls == []
    (rec,) = runlog.read_events(correlation_id=CORR, root=tmp_path)  # still written to the local run log
    assert rec["type"] == "script.qa_probe"
    assert _probe(tmp_path, *corr_args) == 0  # the same run for real tees its exit record
    assert [(e["surface"], e["payload"]["swarm_type"]) for e in fake.events] == [("swarm-a08-qa", "script.qa_probe")]
    capsys.readouterr()


def test_registry_audit_rows_are_mapped_but_stay_local_only(tmp_path, on, fake, monkeypatch):
    """`swarm.tool.call` (tool_registry.call_tool, SEC-03) has a kind, so the emit-site table is complete, yet it never
    leaves the machine: source "registry" names no agent surface and the row is written with tee=False."""
    from swarm import tool_registry

    assert tee_mod.kind_for("swarm.tool.call") == "tool.call"
    fake.bindings[CORR] = GID_A
    tee_mod.remember_graph_id(CORR, GID_A, tmp_path)
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    (tmp_path / ".swarm").mkdir(exist_ok=True)  # a READ call audits only where the state dir already exists
    monkeypatch.setenv("SWARM_CORRELATION_ID", CORR)
    assert tool_registry.call_tool("orch_registry_ping", "A01", {"token": "x"})["state"] == "SUCCESS"
    (rec,) = runlog.read_events(correlation_id=CORR, root=tmp_path, event_type="swarm.tool.call")
    assert rec["source"] == "registry" and tee_mod.agent_of(rec) is None
    assert tee_mod.build_event(rec, GID_A) is None and tee_mod.tee(rec, tmp_path) is False
    assert fake.calls == [] and fake.events == []
