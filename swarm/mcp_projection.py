"""Paging, correlation-bound cursors and the tool-result projection for the swarm_status MCP tool (14.7 W1).

Pure functions over already-read data: no subprocess, no I/O, no protocol state. scripts/swarm_mcp.py fetches the orch_status
answer and calls these to build the page and the tool result. Standard library only.
"""

from __future__ import annotations

import base64
import json
from typing import Any, NamedTuple

PAGE_SIZE = 50  # the default `limit` and its largest accepted value


class Page(NamedTuple):
    correlation_id: str
    limit: int
    offset: int


def encode_cursor(correlation_id: str, offset: int) -> str:
    body = json.dumps({"c": correlation_id, "o": offset}, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(body).decode("ascii")


def decode_cursor(cursor: str, correlation_id: str) -> int | None:
    """The offset a cursor names for this correlation, or None when it is malformed or was issued for another correlation."""
    try:
        body = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    except ValueError:  # binascii, unicode and JSON decode errors are all ValueError
        return None
    if not isinstance(body, dict) or body.get("c") != correlation_id:
        return None
    offset = body.get("o")
    return offset if type(offset) is int and offset >= 0 else None


def parse_page(arguments: object) -> Page | str:
    """The validated arguments of a swarm_status call, or the reason they are invalid."""
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    if set(arguments) - {"correlation_id", "limit", "cursor"}:
        return "arguments accept only correlation_id, limit and cursor"
    if "correlation_id" not in arguments:
        return "correlation_id is required"
    correlation_id = arguments["correlation_id"]
    if not isinstance(correlation_id, str) or "\x00" in correlation_id:
        return "correlation_id must be a string without NUL characters"
    limit = arguments.get("limit", PAGE_SIZE)
    if type(limit) is not int or not 1 <= limit <= PAGE_SIZE:
        return f"limit must be an integer from 1 to {PAGE_SIZE}"
    cursor = arguments.get("cursor")
    if cursor is None:
        return Page(correlation_id, limit, 0)
    if not isinstance(cursor, str):
        return "cursor must be a string"
    offset = decode_cursor(cursor, correlation_id)
    if offset is None:
        return "cursor was not issued for this correlation"
    return Page(correlation_id, limit, offset)


def empty_page() -> dict[str, Any]:
    """The answer for an empty correlation, which is never sent to the backend."""
    return {"correlation_id": "", "tasks": [], "next_cursor": None, "swarm_dir": None, "db": None}


def project_page(page: Page, payload: dict[str, Any]) -> dict[str, Any]:
    """One page of the orch_status task rows as structuredContent. The payload's task list is already validated by the caller."""
    rows = payload["tasks"]
    end = page.offset + page.limit
    next_cursor = encode_cursor(page.correlation_id, end) if end < len(rows) else None
    return {"correlation_id": page.correlation_id, "tasks": rows[page.offset:end], "next_cursor": next_cursor,
            "swarm_dir": payload.get("swarm_dir"), "db": payload.get("db")}


def success_result(structured: dict[str, Any], with_structured: bool) -> dict[str, Any]:
    """The tool result: the structured answer as JSON text, and as structuredContent when the negotiated revision carries it."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(structured, sort_keys=True)}], "isError": False}
    if with_structured:
        result["structuredContent"] = structured
    return result
