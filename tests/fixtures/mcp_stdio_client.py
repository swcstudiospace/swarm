"""Stdlib client for the line-delimited JSON-RPC 2.0 stdio transport of the local Swarm MCP server (14.7 W0 harness).

It is the independent local client the protocol tests drive. It frames one UTF-8 JSON object per line, matches responses by id,
keeps server messages that answer nothing it asked for in `stray`, keeps stderr apart from stdout, and fails loudly on non-JSON
stdout, timeouts and early exit. It makes no protocol decisions: whether a response, an error or silence is correct is asserted by
the test that uses it. No MCP SDK and no third-party dependency.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
SERVER_ENTRY = ROOT / "scripts" / "swarm_mcp.py"
DEFAULT_TIMEOUT_S = 10.0
_AUTO_ID_START = 100


class McpTimeout(TimeoutError):
    """The server produced no complete line within the deadline."""


class McpClosed(RuntimeError):
    """The server closed its stdout or its stdin, or exited, while the client still expected traffic."""


def server_argv() -> list[str]:
    """The argv a local client launches the stdio server with. The entry point is a W1 deliverable."""
    return [sys.executable, str(SERVER_ENTRY)]


def initialize_params(protocol_version: str) -> dict[str, Any]:
    return {"protocolVersion": protocol_version, "capabilities": {},
            "clientInfo": {"name": "agent-swarm-w0-contract-client", "version": "0.0.0"}}


class McpStdioClient:
    """One server process, driven over its stdin and stdout. Use as a context manager so the process is always reaped."""

    def __init__(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path = ROOT,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s
        self.transcript: list[str] = []  # "> " for lines sent, "< " for lines received, in order
        self.stray: list[dict[str, Any]] = []  # parsed server messages that answered no request this client was awaiting
        self._next_id = _AUTO_ID_START
        self._lines: queue.Queue[bytes | None] = queue.Queue()  # one raw stdout line per item; None marks EOF
        self._stderr_chunks: list[bytes] = []
        self._proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=dict(env), cwd=str(cwd))
        self._threads = [threading.Thread(target=self._pump_stdout, daemon=True),
                         threading.Thread(target=self._pump_stderr, daemon=True)]
        for thread in self._threads:
            thread.start()

    def __enter__(self) -> McpStdioClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- outbound -------------------------------------------------------------------------------------------------

    def send(self, message: Mapping[str, Any]) -> None:
        self.send_raw(json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n")

    def send_raw(self, data: bytes) -> None:
        self.transcript.append("> " + data.decode("utf-8", "replace").rstrip("\n"))
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise McpClosed(self._closed_detail("server stopped reading its input")) from exc

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self.send(message)

    def request(self, method: str, params: Mapping[str, Any] | None = None, *, id: object = None,
                timeout_s: float | None = None) -> dict[str, Any]:
        """Sends a request and returns the first message that answers its id. An explicit `id` keeps its JSON type."""
        rid = self._allocate_id() if id is None else id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = dict(params)
        self.send(message)
        return self.await_response(rid, timeout_s=timeout_s)

    def _allocate_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    # ---- inbound --------------------------------------------------------------------------------------------------

    def await_response(self, rid: object, *, timeout_s: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s)
        while True:
            msg = self.receive(timeout_s=max(deadline - time.monotonic(), 0.01))
            mid = msg.get("id")
            if mid is not None and mid != rid and str(mid) == str(rid):
                raise AssertionError(f"response id {mid!r} does not keep the request id's JSON type {rid!r}")
            if mid == rid and ("result" in msg or "error" in msg):
                return msg
            self.stray.append(msg)

    def receive(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        timeout = self.timeout_s if timeout_s is None else timeout_s
        try:
            raw = self._lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise McpTimeout(f"no line from the server within {timeout:.1f}s\n{self.transcript_tail()}") from exc
        if raw is None:
            raise McpClosed(self._closed_detail("server closed its stdout"))
        return self._parse(raw)

    def expect_silence(self, window_s: float = 1.0) -> None:
        """Fails if the server writes anything within the window. A message that arrives is recorded before the failure."""
        try:
            raw = self._lines.get(timeout=window_s)
        except queue.Empty:
            return
        if raw is None:
            raise McpClosed(self._closed_detail("server closed its stdout"))
        self.transcript.append("< " + raw.decode("utf-8", "replace").rstrip("\n"))
        raise AssertionError(f"server sent a message where none was due: {raw[:300]!r}")

    def _parse(self, raw: bytes) -> dict[str, Any]:
        try:
            text = raw.decode("utf-8").rstrip("\n")
        except UnicodeDecodeError as exc:
            raise AssertionError(f"stdout is not UTF-8: {raw[:120]!r}") from exc
        self.transcript.append("< " + text)
        try:
            msg = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"stdout line is not JSON: {text[:300]!r}") from exc
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            raise AssertionError(f"stdout line is not a JSON-RPC 2.0 object: {text[:300]!r}")
        return msg

    # ---- lifecycle ------------------------------------------------------------------------------------------------

    def close_input(self) -> None:
        try:
            self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def wait_exit(self, timeout_s: float = 5.0) -> int:
        try:
            return self._proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise McpTimeout(f"server still running {timeout_s:.1f}s after its input closed\n"
                             f"{self.transcript_tail()}") from exc

    def stderr_text(self) -> str:
        return b"".join(self._stderr_chunks).decode("utf-8", "replace")

    def transcript_tail(self, lines: int = 40) -> str:
        return "\n".join(self.transcript[-lines:])

    def close(self) -> None:
        if self._proc.poll() is None:
            self.close_input()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        for thread in self._threads:
            thread.join(timeout=2)
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if stream is not None:
                stream.close()

    def _closed_detail(self, what: str) -> str:
        self._proc.poll()
        return (f"{what}; exit status {self._proc.returncode}\n"
                f"stderr:\n{self.stderr_text()[-4000:]}\ntranscript:\n{self.transcript_tail()}")

    def _pump_stdout(self) -> None:
        for raw in iter(self._proc.stdout.readline, b""):
            self._lines.put(raw)
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        for raw in iter(self._proc.stderr.readline, b""):
            self._stderr_chunks.append(raw)


def initialize_session(client: McpStdioClient, protocol_version: str) -> dict[str, Any]:
    """Sends initialize and, when it answers with a result, the initialized notification. Returns the initialize reply."""
    response = client.request("initialize", initialize_params(protocol_version))
    if "result" in response:
        client.notify("notifications/initialized")
    return response


def call_tool(client: McpStdioClient, name: str, arguments: Mapping[str, Any], *, id: object = None) -> dict[str, Any]:
    return client.request("tools/call", {"name": name, "arguments": dict(arguments)}, id=id)
