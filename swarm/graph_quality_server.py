"""Stdlib HTTP server for API-GsdQualityGate 1.2.0 (local harness).

Contract ``servers`` is ``http://localhost:0/v1``: the harness is local-only
and the contract declares no ``securityScheme``, so routes are intentionally
public on loopback. No auth token, secret, or body is ever logged (bodies can
carry task text); access logs record method/path/status only. The server is
stdlib ``http.server`` — no new dependency (L2).

Run:
    python3 -m swarm.graph_quality_server --host 127.0.0.1 --port 0
    python3 -m swarm.graph_quality_server --host 127.0.0.1 --port 8765
      --taskstore .swarm/tasks.db
"""

from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .graph_quality import AppContext, handle
from .taskstore import TaskStore

_LOG = logging.getLogger("swarm.graph_quality_server")


def _parse_json_body(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("request body is not valid JSON") from None
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise ValueError("request body must be a JSON object")
    return parsed


def make_handler(ctx: AppContext) -> type[BaseHTTPRequestHandler]:
    """Build a handler class bound to one AppContext (per-server state)."""

    class GraphQualityHandler(BaseHTTPRequestHandler):
        server_version = "GraphQuality/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            _LOG.info("%s %s %s", self.command, self.path, format % args)

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve(self, method: str) -> None:
            split = urlsplit(self.path)
            path = split.path.rstrip("/") or "/"
            query = {name: values[0] for name, values in parse_qs(split.query, keep_blank_values=True).items() if values}
            try:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0)) if method in ("POST", "PUT", "PATCH") else b""
                body = _parse_json_body(raw) if raw else None
            except ValueError as exc:
                self._reply(400, {"code": "E-INPUT", "message": str(exc)})
                return
            if query:
                body = {**body, **query} if isinstance(body, dict) else query
            try:
                status, payload = handle(ctx, method, path, body)
            except ValueError as exc:  # pragma: no cover - handle maps refusals
                self._reply(400, {"code": "E-INPUT", "message": str(exc)})
                return
            except Exception:  # fail-closed: never leak a traceback body
                _LOG.exception("unhandled error serving %s %s", method, path)
                self._reply(500, {"code": "E-INTERNAL", "message": "internal error"})
                return
            self._reply(status, payload)

        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._serve("POST")

    return GraphQualityHandler


def create_server(host: str, port: int, ctx: AppContext) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(ctx))


def build_context(taskstore_path: str | None, root: Path) -> AppContext:
    store: TaskStore | None = None
    if taskstore_path:
        path = Path(taskstore_path)
        if path.exists():
            store = TaskStore(path)
    return AppContext(store=store, root=root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="API-GsdQualityGate 1.2.0 local harness")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (loopback by default)")
    parser.add_argument("--port", type=int, default=0, help="bind port (0 = ephemeral)")
    parser.add_argument("--taskstore", default=None, help="path to tasks.db (optional)")
    parser.add_argument("--root", default=".", help="repository root for registry/git reads")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ctx = build_context(str(args.taskstore) if args.taskstore else None, Path(str(args.root)))
    server = create_server(str(args.host), int(args.port), ctx)
    actual_host, actual_port = server.server_address[0], server.server_address[1]
    _LOG.info("graph-quality harness listening on http://%s:%d/v1", actual_host, actual_port)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        _LOG.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
