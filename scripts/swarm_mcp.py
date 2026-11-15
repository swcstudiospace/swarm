#!/usr/bin/env python3
"""Local stdio MCP server for the Swarm read surface (14.7 W1 protocol core).

Speaks newline-delimited JSON-RPC 2.0: one object per line on stdin, one object per line on stdout, diagnostics on stderr only.
Standard library only. Requests are answered synchronously in arrival order, so no request is in flight when a cancellation
arrives; cancellations are accepted and ignored here.

Protocol revisions: 2025-06-18 (structured tool output) and 2024-11-05 (text tool output only). The tools surface arrives in
W1 Task 3; until then tools/list and tools/call answer method not found.

`--dry-run --json` prints a status record and exits 0 without reading stdin (the repo's script contract).
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

PROTOCOL_VERSIONS = ("2025-06-18", "2024-11-05")  # newest first; an unknown request is answered with the first
SERVER_INFO = {"name": "agent-swarm", "version": "0.1.0"}

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


def _request(session: Session, rid: Any, method: str, params: object) -> dict[str, Any]:
    if method == "initialize":
        return _initialize(session, rid, params)
    if session.protocol_version is None:
        return _error(rid, INVALID_REQUEST, "server not initialized")
    if method == "ping":
        return _result(rid, {})
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


def process_line(session: Session, raw: bytes) -> dict[str, Any] | None:
    if not raw.strip():
        return None
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _error(None, PARSE_ERROR, "parse error")
    try:
        return handle(session, msg)
    except Exception:  # a bug must neither end the session nor leave a request unanswered
        traceback.print_exc(file=sys.stderr)
        if isinstance(msg, dict) and "method" in msg and "id" in msg and _valid_id(msg["id"]):
            return _error(msg["id"], INTERNAL_ERROR, "internal error")
        return None


def main(argv: list[str]) -> int:
    if "--dry-run" in argv:
        print(json.dumps({"status": "ok", "agent": "A01", "script": "swarm_mcp", "dry_run": True,
                          "protocol_versions": list(PROTOCOL_VERSIONS), "server": SERVER_INFO}))
        return 0
    session = Session()
    for raw in sys.stdin.buffer:
        reply = process_line(session, raw)
        if reply is not None:
            sys.stdout.buffer.write(json.dumps(reply).encode("ascii") + b"\n")
            sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
