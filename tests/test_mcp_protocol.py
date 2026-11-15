"""MCP stdio protocol contract for the local Swarm server (14.7 W0-01, AC-14.7-04).

Every test drives the server through the stdlib line-delimited JSON-RPC client in tests/fixtures/mcp_stdio_client.py, the way an
independent local client would. Tests that need the W1 server carry strict xfail, so this file is a red contract until the server is
built. They are not skips, and a server that starts passing them makes the strict xfail fail.

Placeholders, not accepted API: TOOL and its argument shape are W0 placeholders (DA-09 and DA-SEC-10/11 are unresolved). The assertions
pin protocol behavior and the cross into the Python authority, not final tool names.

Authority: the success path reads TaskStore rows that the test seeds on a temporary SWARM_DIR. The expected projection is computed
from TaskStore itself, never from the server's own answer.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_CLIENT_PATH = ROOT / "tests" / "fixtures" / "mcp_stdio_client.py"
_client_spec = importlib.util.spec_from_file_location("mcp_stdio_client", _CLIENT_PATH)
_client = importlib.util.module_from_spec(_client_spec)
_client_spec.loader.exec_module(_client)
McpStdioClient = _client.McpStdioClient
call_tool = _client.call_tool
initialize_params = _client.initialize_params
initialize_session = _client.initialize_session
server_argv = _client.server_argv

CORR = "corr-w0-mcp-contract"
TASK_IDS = ("T-w0-alpha", "T-w0-beta")
TOOL = "swarm_status"  # placeholder name (DA-09); the assertions do not depend on the final name
PROTO_STRUCTURED = "2025-06-18"  # revision with structuredContent
PROTO_TEXT_ONLY = "2024-11-05"  # revision whose tool results are text content only
PROTO_UNKNOWN = "1999-01-01"  # a revision no server speaks
SILENCE_S = 1.0
JSONRPC_PARSE_ERROR = -32700
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
# Words that a public control surface must not expose (D-14.7-03): raw transition/ingest, signer/key, gate waiver or recording,
# arbitrary file, shell, script or registry access.
FORBIDDEN_TOOL_TOKENS = frozenset({"transition", "ingest", "sign", "signer", "key", "waive", "waiver", "record", "approve",
                                   "approval", "file", "exec", "shell", "script", "registry"})
SERVER_NOT_BUILT = pytest.mark.xfail(strict=True, reason="14.7 W1 server not built")


def _pairs(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return sorted((row["task_id"], row["state"]) for row in rows)


def _text(result: dict[str, Any]) -> str:
    return "".join(block.get("text", "") for block in result.get("content", []) if block.get("type") == "text")


def _tool_result(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response, f"tools/call answered with a protocol error, not a result: {response}"
    assert "result" in response, response
    return response["result"]


@pytest.fixture()
def server_env(swarm_dir: Path) -> dict[str, str]:
    """A minimal child environment: the temporary SWARM_DIR, the pure-Python authority, and no signing material."""
    home = swarm_dir.parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8", "HOME": str(home),
            "SWARM_DIR": str(swarm_dir), "SWARM_PURE_PYTHON": "1", "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}


@pytest.fixture()
def seeded_tasks(swarm_dir: Path) -> list[tuple[str, str]]:
    """Two TaskStore rows on the temporary SWARM_DIR. Returns their (task_id, state) pairs as TaskStore reports them."""
    from swarm.taskstore import TaskStore  # imported after the swarm_dir fixture has reset the swarm modules

    with TaskStore() as store:
        for task_id, agent_id in zip(TASK_IDS, ("A05", "A06")):
            store.create(task_id=task_id, correlation_id=CORR, capability="code", title=f"{task_id} W0 fixture",
                         agent_id=agent_id)
        return _pairs(store.list(correlation_id=CORR))


@pytest.fixture()
def mcp(server_env: dict[str, str]):
    client = McpStdioClient(server_argv(), env=server_env)
    yield client
    client.close()


def test_authority_cli_projection_matches_taskstore_fixture(seeded_tasks, server_env):
    """Green oracle: the real orch_status.py reads the same SWARM_DIR and reports the rows the fixture wrote."""
    completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "orch_status.py"), "--correlation-id", CORR, "--json"],
                               env=server_env, cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["correlation_id"] == CORR
    assert _pairs(payload["tasks"]) == seeded_tasks


@SERVER_NOT_BUILT
@pytest.mark.parametrize(("method", "params"), [
    ("tools/list", {}),
    ("tools/call", {"name": TOOL, "arguments": {"correlation_id": CORR}}),
])
def test_request_before_initialize_is_rejected_without_a_result(mcp, seeded_tasks, method, params):
    """No tool may run before the initialize handshake: the answer is an error object and carries no result."""
    response = mcp.request(method, params)
    assert "error" in response and "result" not in response, response
    assert isinstance(response["error"].get("code"), int), response


@SERVER_NOT_BUILT
def test_initialize_echoes_a_supported_version_and_advertises_tools(mcp):
    response = mcp.request("initialize", initialize_params(PROTO_STRUCTURED))
    assert "result" in response, response
    result = response["result"]
    assert result["protocolVersion"] == PROTO_STRUCTURED
    assert "tools" in result["capabilities"], result["capabilities"]
    assert isinstance(result["serverInfo"]["name"], str) and result["serverInfo"]["name"]
    assert isinstance(result["serverInfo"]["version"], str)


@SERVER_NOT_BUILT
def test_initialize_with_unknown_version_answers_with_a_revision_the_server_speaks(mcp):
    response = mcp.request("initialize", initialize_params(PROTO_UNKNOWN))
    assert "result" in response, response
    offered = response["result"]["protocolVersion"]
    assert offered != PROTO_UNKNOWN
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", offered), offered


@SERVER_NOT_BUILT
def test_initialized_notification_gets_no_reply(mcp):
    initialize = mcp.request("initialize", initialize_params(PROTO_STRUCTURED))
    assert "result" in initialize, initialize
    mcp.notify("notifications/initialized")
    mcp.expect_silence(SILENCE_S)
    pong = mcp.request("ping")
    assert "result" in pong, pong
    assert not mcp.stray, mcp.stray


@SERVER_NOT_BUILT
def test_notification_with_unknown_method_gets_no_reply(mcp):
    initialize_session(mcp, PROTO_STRUCTURED)
    mcp.notify("notifications/w0-no-such-notification", {"detail": "w0"})
    mcp.expect_silence(SILENCE_S)
    pong = mcp.request("ping")
    assert "result" in pong, pong
    assert not mcp.stray, mcp.stray


@SERVER_NOT_BUILT
def test_unknown_method_returns_method_not_found_with_the_request_id(mcp):
    initialize_session(mcp, PROTO_STRUCTURED)
    response = mcp.request("w0/no-such-method", {}, id=7)
    assert response["id"] == 7
    assert "result" not in response
    assert response["error"]["code"] == JSONRPC_METHOD_NOT_FOUND, response


@SERVER_NOT_BUILT
def test_unknown_tool_name_is_an_invalid_params_protocol_error(mcp):
    """Under the 2025-06-18 revision an unknown tool is a protocol error (-32602), not a tool result."""
    initialize_session(mcp, PROTO_STRUCTURED)
    response = call_tool(mcp, "w0_no_such_tool", {}, id=8)
    assert response["id"] == 8
    assert "result" not in response, response
    assert response["error"]["code"] == JSONRPC_INVALID_PARAMS, response


@SERVER_NOT_BUILT
def test_malformed_json_line_gets_parse_error_and_session_continues(mcp):
    initialize_session(mcp, PROTO_STRUCTURED)
    mcp.send_raw(b'{"jsonrpc":"2.0","id":9,\n')
    error = mcp.receive()
    assert error["error"]["code"] == JSONRPC_PARSE_ERROR, error
    assert error.get("id") is None, error
    pong = mcp.request("ping")
    assert "result" in pong, pong


@SERVER_NOT_BUILT
@pytest.mark.parametrize("rid", ["req-w0-str", 4242])
def test_request_id_type_is_preserved_in_the_response(mcp, rid):
    initialize_session(mcp, PROTO_STRUCTURED)
    response = mcp.request("ping", id=rid)
    assert "result" in response, response
    assert response["id"] == rid and type(response["id"]) is type(rid), response


@SERVER_NOT_BUILT
def test_each_request_is_answered_once(mcp):
    initialize_session(mcp, PROTO_STRUCTURED)
    first = mcp.request("ping", id=11)
    assert first["id"] == 11 and "result" in first, first
    mcp.expect_silence(SILENCE_S)  # no second answer for an id that already completed
    assert not mcp.stray, mcp.stray


@SERVER_NOT_BUILT
def test_cancel_for_unknown_request_id_gets_no_reply(mcp):
    initialize_session(mcp, PROTO_STRUCTURED)
    mcp.notify("notifications/cancelled", {"requestId": 987654, "reason": "w0 contract: nothing in flight"})
    mcp.expect_silence(SILENCE_S)
    pong = mcp.request("ping")
    assert "result" in pong, pong
    assert not mcp.stray, mcp.stray


@SERVER_NOT_BUILT
def test_cancel_of_initialize_does_not_drop_its_response(mcp):
    """The initialize request must not be cancelled. A cancel that arrives for it must not erase the negotiated result."""
    mcp.send({"jsonrpc": "2.0", "id": 13, "method": "initialize", "params": initialize_params(PROTO_STRUCTURED)})
    mcp.notify("notifications/cancelled", {"requestId": 13, "reason": "w0 contract: initialize is not cancellable"})
    response = mcp.await_response(13)
    assert "result" in response, response
    assert response["result"]["protocolVersion"] == PROTO_STRUCTURED


@SERVER_NOT_BUILT
def test_cancelled_tool_call_gets_at_most_one_reply(mcp, seeded_tasks):
    initialize_session(mcp, PROTO_STRUCTURED)
    mcp.send({"jsonrpc": "2.0", "id": 77, "method": "tools/call",
              "params": {"name": TOOL, "arguments": {"correlation_id": CORR}}})
    mcp.notify("notifications/cancelled", {"requestId": 77, "reason": "w0 contract: racing completion"})
    pong = mcp.request("ping", id=78)
    assert "result" in pong, pong
    replies_to_77 = [message for message in mcp.stray if message.get("id") == 77]
    assert len(replies_to_77) <= 1, replies_to_77


@SERVER_NOT_BUILT
def test_tools_list_names_tools_with_object_input_schemas(mcp):
    initialize_session(mcp, PROTO_STRUCTURED)
    tools = mcp.request("tools/list")["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert len(names) == len(set(names)), names
    assert TOOL in names, names
    for tool in tools:
        assert isinstance(tool["name"], str) and tool["name"], tool
        if "description" in tool:  # optional in the MCP tool schema
            assert isinstance(tool["description"], str), tool
        assert tool["inputSchema"]["type"] == "object", tool


@SERVER_NOT_BUILT
def test_tools_list_exposes_no_raw_control_surface(mcp):
    initialize_session(mcp, PROTO_STRUCTURED)
    names = [tool["name"] for tool in mcp.request("tools/list")["result"]["tools"]]
    for name in names:
        tokens = {token for token in re.split(r"[^a-z0-9]+", name.lower()) if token}
        assert not tokens & FORBIDDEN_TOOL_TOKENS, f"{name!r} exposes a forbidden control surface"


@SERVER_NOT_BUILT
def test_tools_call_crosses_into_taskstore_and_returns_structured_rows(mcp, seeded_tasks):
    initialize_session(mcp, PROTO_STRUCTURED)
    result = _tool_result(call_tool(mcp, TOOL, {"correlation_id": CORR}, id=20))
    assert not result.get("isError"), result
    assert "structuredContent" in result, result
    assert _pairs(result["structuredContent"]["tasks"]) == seeded_tasks
    text = _text(result)
    for task_id in TASK_IDS:
        assert task_id in text, text


@SERVER_NOT_BUILT
def test_text_only_revision_returns_text_without_structured_content(mcp, seeded_tasks):
    initialize = initialize_session(mcp, PROTO_TEXT_ONLY)
    assert initialize["result"]["protocolVersion"] == PROTO_TEXT_ONLY, initialize
    result = _tool_result(call_tool(mcp, TOOL, {"correlation_id": CORR}))
    assert not result.get("isError"), result
    assert "structuredContent" not in result, result
    text = _text(result)
    assert text, result
    for task_id, _state in seeded_tasks:
        assert task_id in text, text


@SERVER_NOT_BUILT
def test_tools_call_for_a_correlation_with_no_tasks_is_an_explicit_empty_result(mcp, seeded_tasks):
    """An unknown correlation is an empty answer, not an error, and not a silent fallback to the latest correlation."""
    initialize_session(mcp, PROTO_STRUCTURED)
    result = _tool_result(call_tool(mcp, TOOL, {"correlation_id": "corr-w0-nowhere"}))
    assert not result.get("isError"), result
    assert result["structuredContent"]["tasks"] == [], result


@SERVER_NOT_BUILT
def test_authority_failure_is_a_tool_error_result_not_a_protocol_error(mcp, swarm_dir):
    """A directory where tasks.db should be makes the real TaskStore fail to open. The failure is a tool result with isError."""
    swarm_dir.mkdir(parents=True, exist_ok=True)
    (swarm_dir / "tasks.db").mkdir()
    initialize_session(mcp, PROTO_STRUCTURED)
    response = call_tool(mcp, TOOL, {"correlation_id": CORR}, id=22)
    assert "error" not in response, f"authority failure leaked as a protocol error: {response}"
    result = response["result"]
    assert result["isError"] is True, result
    text = _text(result)
    assert text.strip(), result
    assert "Traceback" not in text, text


@SERVER_NOT_BUILT
def test_stdout_carries_only_jsonrpc_and_server_exits_zero_on_stdin_eof(mcp):
    """Every stdout line parses as a JSON-RPC 2.0 object (the client enforces it), and closing stdin ends the server cleanly."""
    initialize_session(mcp, PROTO_STRUCTURED)
    mcp.request("ping")
    mcp.request("tools/list")
    mcp.close_input()
    exit_status = mcp.wait_exit(timeout_s=5.0)
    assert exit_status == 0, f"exit {exit_status}; stderr:\n{mcp.stderr_text()}"
    assert not mcp.stray, mcp.stray
