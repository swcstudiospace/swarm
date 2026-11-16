#!/usr/bin/env python3
"""Local stdio MCP server for the Swarm read surface (14.7 W1).

Speaks newline-delimited JSON-RPC 2.0: one object per line on stdin, one object per line on stdout, diagnostics on stderr only.
Standard library only. Requests are answered synchronously in arrival order, so no request is in flight when a cancellation
arrives; cancellations are accepted and ignored here.

Protocol revisions: 2025-06-18 (structured tool output) and 2024-11-05 (text tool output only).

Tools: one read-only tool, `swarm_status`. Its answer comes from the canonical read route scripts/orch_status.py, run as a child
process with an explicit --correlation-id and --json, an environment allowlist, a timeout and a stdout cap. This module holds no
projection, lifecycle or approval logic, never writes to the task store, and reads no signing material.

`--dry-run --json` prints a status record and exits 0 without reading stdin (the repo's script contract).
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ORCH_STATUS = ROOT / "scripts" / "orch_status.py"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SWARM_PURE_PYTHON", "1")  # importing swarm.* must not load the native extension in this stdlib server
from swarm.mcp_projection import PAGE_SIZE, Page, empty_page, parse_page, project_page, success_result  # noqa: E402

STRUCTURED_REVISION = "2025-06-18"
PROTOCOL_VERSIONS = (STRUCTURED_REVISION, "2024-11-05")  # newest first; an unknown request is answered with the first
SERVER_INFO = {"name": "agent-swarm", "version": "0.1.0"}

TOOL_NAME = "swarm_status"
MAX_LINE_BYTES = 65536  # content of one request line, not counting its newline
MAX_JSON_DEPTH = 32
BACKEND_TIMEOUT_S = 10.0
BACKEND_STDOUT_MAX_BYTES = 1048576
SUMMARY_MAX_CHARS = 512

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class Session:
    """Protocol state for one client connection: the revision negotiated by initialize, or None before it."""

    def __init__(self) -> None:
        self.protocol_version: str | None = None


def _result(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _valid_id(rid: object) -> bool:
    return isinstance(rid, (str, int)) and not isinstance(rid, bool)


def _initialize(session: Session, rid: Any, params: object) -> dict[str, Any]:
    if session.protocol_version is not None:
        return _error(rid, INVALID_REQUEST, "already initialized")
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    if not isinstance(requested, str):
        return _error(rid, INVALID_PARAMS, "initialize requires a string protocolVersion")
    version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
    session.protocol_version = version
    return _result(rid, {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": dict(SERVER_INFO)})


def _tool_descriptor() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Task rows of one correlation from the Swarm task store, one page per call. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string",
                                   "description": "Correlation whose task rows to read. Never falls back to another one."},
                "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_SIZE, "description": "Rows per page."},
                "cursor": {"type": "string", "description": "next_cursor of the previous page of the same correlation."},
            },
            "required": ["correlation_id"],
            "additionalProperties": False,
        },
    }


class _Overflow(Exception):
    """The backend wrote more stdout than BACKEND_STDOUT_MAX_BYTES allows."""


def _child_env() -> dict[str, str]:
    """The backend's environment: an allowlist of names, the pure-Python authority, and no signing material. ROUTE_KEY_ENV-class
    variables are never copied, and the import path is the repository, never an inherited one."""
    env = {name: os.environ[name] for name in ("PATH", "HOME", "SWARM_DIR") if name in os.environ}
    env.setdefault("PATH", os.defpath)
    env.update({"LANG": "C.UTF-8", "SWARM_PURE_PYTHON": "1", "PYTHONPATH": str(ROOT),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
    return env


def _capture_stdout(proc: subprocess.Popen[bytes], deadline: float) -> bytes:
    """Reads the child's stdout to EOF. Raises subprocess.TimeoutExpired past the deadline and _Overflow past the stdout cap."""
    fd = proc.stdout.fileno()
    buffered = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(proc.args, BACKEND_TIMEOUT_S)
            chunk = os.read(fd, 65536)
            if not chunk:
                return bytes(buffered)
            buffered += chunk
            if len(buffered) > BACKEND_STDOUT_MAX_BYTES:
                raise _Overflow


def _run_backend(correlation_id: str) -> tuple[int, bytes] | str:
    """Runs the read route for one correlation: (exit status, stdout), or the reason it could not finish. The child's stderr is
    inherited, so its diagnostics go to this server's stderr and never to the protocol stdout."""
    try:
        proc = subprocess.Popen([sys.executable, str(ORCH_STATUS), f"--correlation-id={correlation_id}", "--json"],
                                env=_child_env(), cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    except OSError as exc:
        return f"orch_status could not start ({type(exc).__name__})"
    deadline = time.monotonic() + BACKEND_TIMEOUT_S
    try:
        stdout = _capture_stdout(proc, deadline)
        proc.wait(timeout=max(deadline - time.monotonic(), 0.001))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return f"orch_status did not finish within {BACKEND_TIMEOUT_S:g} s"
    except _Overflow:
        proc.kill()
        proc.wait()
        return f"orch_status wrote more than {BACKEND_STDOUT_MAX_BYTES} bytes"
    finally:
        proc.stdout.close()
    return proc.returncode, stdout


def _clean(text: str) -> str:
    """A diagnostic that is safe to return to a client: no control characters, bounded length."""
    return "".join(ch if ch.isprintable() else " " for ch in text)[:SUMMARY_MAX_CHARS]


def _read_payload(correlation_id: str) -> dict[str, Any] | str:
    """The orch_status --json answer for one correlation, or a sanitized reason the answer is unavailable."""
    ran = _run_backend(correlation_id)
    if isinstance(ran, str):
        return ran
    code, stdout = ran
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        payload = None
    failed = code != 0 or (isinstance(payload, dict) and payload.get("status") == "error")
    if failed:
        summary = payload.get("summary") if isinstance(payload, dict) else None
        if isinstance(summary, str) and summary.strip() and "Traceback" not in summary:
            return _clean(summary)
        return f"orch_status failed with exit status {code}"
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        return "orch_status answered without a task list"
    return payload


def _swarm_status(page: Page) -> dict[str, Any] | str:
    """One page of task rows as structuredContent, or the reason the read failed. An empty correlation is answered without the
    backend, so it can never reach another correlation."""
    if page.correlation_id == "":
        return empty_page()
    payload = _read_payload(page.correlation_id)
    if isinstance(payload, str):
        return payload
    return project_page(page, payload)


def _error_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _tools_call(session: Session, rid: Any, params: object) -> dict[str, Any]:
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _error(rid, INVALID_PARAMS, "tools/call requires a string name")
    if params["name"] != TOOL_NAME:
        return _error(rid, INVALID_PARAMS, "unknown tool")
    page = parse_page(params.get("arguments", {}))
    if isinstance(page, str):
        return _error(rid, INVALID_PARAMS, page)
    answer = _swarm_status(page)
    if isinstance(answer, str):
        return _result(rid, _error_result(answer))  # an authority or backend failure is a tool result, not a protocol error
    return _result(rid, success_result(answer, session.protocol_version == STRUCTURED_REVISION))


def _request(session: Session, rid: Any, method: str, params: object) -> dict[str, Any]:
    if method == "initialize":
        return _initialize(session, rid, params)
    if session.protocol_version is None:
        return _error(rid, INVALID_REQUEST, "server not initialized")
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": [_tool_descriptor()]})
    if method == "tools/call":
        return _tools_call(session, rid, params)
    return _error(rid, METHOD_NOT_FOUND, f"method not found: {method}")


def handle(session: Session, msg: object) -> dict[str, Any] | None:
    """The reply for one parsed message, or None when none is due."""
    if not isinstance(msg, dict):
        return _error(None, INVALID_REQUEST, "request must be a JSON object")
    if "method" not in msg:
        return None  # a response from the client: this server issues no requests, so there is nothing to match
    if "id" not in msg:
        return None  # a notification (initialized, cancelled, or unknown): never answered
    rid = msg["id"]
    if msg.get("jsonrpc") != "2.0" or not isinstance(msg["method"], str) or not _valid_id(rid):
        return _error(rid if _valid_id(rid) else None, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    return _request(session, rid, msg["method"], msg.get("params"))


def _json_depth(value: object) -> int:
    """The number of containers nested in value, the outermost included. Iterative, so a hostile input cannot exhaust the stack."""
    deepest = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children: Any = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        deepest = max(deepest, depth)
        stack.extend((child, depth + 1) for child in children)
    return deepest


def _refused_for_depth(msg: object) -> dict[str, Any] | None:
    """The reply to a message nested past MAX_JSON_DEPTH: an error for a request, none for a notification or a response."""
    if isinstance(msg, dict) and ("method" not in msg or "id" not in msg):
        return None
    rid = msg.get("id") if isinstance(msg, dict) else None
    return _error(rid if _valid_id(rid) else None, INVALID_REQUEST, f"JSON nesting exceeds {MAX_JSON_DEPTH} levels")


def process_line(session: Session, raw: bytes) -> dict[str, Any] | None:
    if not raw.strip():
        return None
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _error(None, PARSE_ERROR, "parse error")
    if _json_depth(msg) > MAX_JSON_DEPTH:
        return _refused_for_depth(msg)
    try:
        return handle(session, msg)
    except Exception:  # a bug must neither end the session nor leave a request unanswered
        traceback.print_exc(file=sys.stderr)
        if isinstance(msg, dict) and "method" in msg and "id" in msg and _valid_id(msg["id"]):
            return _error(msg["id"], INTERNAL_ERROR, "internal error")
        return None


_OVERSIZED = object()  # sentinel: a request line whose content exceeds MAX_LINE_BYTES


def read_request(stream: Any) -> Any:
    """The next line from stream: its bytes, None at EOF, or _OVERSIZED when its content exceeds MAX_LINE_BYTES. The rest of an
    oversized line is discarded without being held in memory."""
    raw = stream.readline(MAX_LINE_BYTES + 2)  # content, its newline, and one byte that proves overflow
    if not raw:
        return None
    if raw.endswith(b"\n"):
        return raw if len(raw) - 1 <= MAX_LINE_BYTES else _OVERSIZED
    if len(raw) <= MAX_LINE_BYTES:  # end of input after a short final line
        return raw
    while True:
        rest = stream.readline(MAX_LINE_BYTES)
        if not rest or rest.endswith(b"\n"):
            return _OVERSIZED


def main(argv: list[str]) -> int:
    if "--dry-run" in argv:
        print(json.dumps({"status": "ok", "agent": "A01", "script": "swarm_mcp", "dry_run": True,
                          "protocol_versions": list(PROTOCOL_VERSIONS), "server": SERVER_INFO}))
        return 0
    session = Session()
    while True:
        raw = read_request(sys.stdin.buffer)
        if raw is None:
            break
        if raw is _OVERSIZED:
            reply = _error(None, INVALID_REQUEST, f"request line exceeds {MAX_LINE_BYTES} bytes")
        else:
            reply = process_line(session, raw)
        if reply is not None:
            sys.stdout.buffer.write(json.dumps(reply).encode("ascii") + b"\n")
            sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
