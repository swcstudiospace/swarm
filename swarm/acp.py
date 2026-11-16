"""In-repo ACP (acp.v1).

The envelope is transport-agnostic. This module is the in-process transport:
routing and the default-deny matrix are derived from agents.json, never from a
hardcoded peer list. Local tool calls do not consult the peer circuit breaker.
"""
from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from typing import Any, Callable

from swarm.tools.support import Busy, StillRunning, Waiting, invoke_bounded

SCHEMA = "acp.v1"
REQUIRED = (
    "schema",
    "sender",
    "recipient",
    "correlation_id",
    "message_type",
    "payload",
    "timestamp",
    "timeout_hint_s",
)
TERMINAL = {"RESPONDED", "FAILED", "TIMED_OUT", "DEAD_LETTERED", "INVALID_INPUT", "UNAUTHORIZED"}


class ACPStatus(Exception):
    """A handler outcome that must not be retried."""

    def __init__(self, state: str, message: str):
        self.state = state
        super().__init__(message)


def load_agents(path: Path | None = None) -> dict[str, Any]:
    from swarm.tool_registry import load_document

    return load_document(path)


def routing_table(data: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """One row per agent in agents.json. Endpoints are derived from the agent id."""
    data = data if data is not None else load_agents()
    table: dict[str, dict[str, Any]] = {}
    for agent in data.get("agents") or []:
        if not isinstance(agent, dict) or "id" not in agent:
            continue
        agent_id = agent["id"]
        table[agent_id] = {
            "endpoint": f"inproc://{agent_id}",
            "accepts": list(agent.get("consumes") or []),
            "produces": list(agent.get("produces") or []),
        }
    return table


def message_allowed(data: dict[str, Any], sender: str, recipient: str, message_type: str) -> bool:
    table = routing_table(data)
    source = table.get(sender)
    target = table.get(recipient)
    if source is None or target is None or sender == recipient:
        return False
    return message_type in source["produces"] and message_type in target["accepts"]


def tool_allowed(data: dict[str, Any], caller: str, tool_name: str) -> bool:
    from swarm.tool_registry import iter_entries

    for _container, tool in iter_entries(data):
        if isinstance(tool, dict) and tool.get("name") == tool_name:
            return caller in (tool.get("permitted_callers") or [])
    return False


def validate_envelope(envelope: object) -> list[str]:
    if not isinstance(envelope, dict):
        return ["envelope must be an object"]
    errors = []
    missing = [key for key in REQUIRED if key not in envelope]
    if missing:
        errors.append("missing " + ", ".join(missing))
    if "schema" in envelope and envelope["schema"] != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    for key in ("sender", "recipient", "correlation_id", "message_type", "timestamp"):
        value = envelope.get(key)
        if key in envelope and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{key} must be a non-empty string")
    if "payload" in envelope and not isinstance(envelope["payload"], dict):
        errors.append("payload must be an object")
    hint = envelope.get("timeout_hint_s")
    if "timeout_hint_s" in envelope and (isinstance(hint, bool) or not isinstance(hint, (int, float)) or hint <= 0):
        errors.append("timeout_hint_s must be a number > 0")
    return errors


class Bus:
    """Deliver one envelope and record its lifecycle. `sleep` and `now` are injectable."""

    def __init__(
        self,
        *,
        data: dict[str, Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        backoff_s: float = 0.01,
        max_backoff_s: float = 1.0,
        breaker_threshold: int = 3,
    ):
        self.data = data if data is not None else load_agents()
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.breaker_threshold = breaker_threshold
        self.handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self.dead: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self._failures: dict[str, int] = {}
        self.open_circuits: set[str] = set()
        self._lock = threading.Lock()

    def register(self, agent_id: str, handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.handlers[agent_id] = handler

    def invoke_tool(self, caller: str, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Local tool call. Peer circuit state does not apply."""
        from swarm.tool_registry import call_tool

        if not tool_allowed(self.data, caller, tool_name):
            with self._lock:
                self.audit.append({
                    "caller": caller,
                    "target": tool_name,
                    "reason": "caller is not permitted to invoke this tool",
                })
            return {
                "state": "UNAUTHORIZED",
                "caller": caller,
                "target": tool_name,
                "reason": "caller is not permitted to invoke this tool",
            }
        return call_tool(tool_name, caller, payload, data=self.data)

    def request(self, envelope: dict[str, Any]) -> dict[str, Any]:
        errors = validate_envelope(envelope)
        if errors:
            return {
                "ok": False,
                "state": "INVALID_INPUT",
                "errors": errors,
                "attempts": 0,
                "history": ["INVALID_INPUT"],
            }
        sender = envelope["sender"]
        recipient = envelope["recipient"]
        message_type = envelope["message_type"]
        if not message_allowed(self.data, sender, recipient, message_type):
            reason = "message type is not permitted for this sender and recipient"
            with self._lock:
                self.audit.append({"caller": sender, "target": recipient, "reason": reason, "message_type": message_type})
            return {
                "ok": False,
                "state": "UNAUTHORIZED",
                "caller": sender,
                "target": recipient,
                "reason": reason,
                "attempts": 1,
                "history": ["UNAUTHORIZED"],
            }
        with self._lock:
            circuit_open = recipient in self.open_circuits
        if circuit_open:
            return self._terminal(
                envelope,
                "FAILED",
                attempts=0,
                history=["FAILED"],
                reason="circuit open",
            )
        history = ["PENDING", "DELIVERED", "IN_PROGRESS"]
        handler = self.handlers.get(recipient)
        last_error = "peer unreachable"
        # Handlers can mutate the dict they receive. Keep one untouched copy for
        # retries and the dead letter, and hand each attempt its own copy.
        original = copy.deepcopy(envelope)
        for attempt in range(1, self.max_attempts + 1):
            try:
                if handler is None:
                    raise ConnectionError("no in-process handler for recipient")
                response = invoke_bounded(handler, copy.deepcopy(original), float(original["timeout_hint_s"]))
            except ACPStatus as exc:
                history.append(exc.state)
                return self._terminal(
                    original,
                    exc.state,
                    attempts=attempt,
                    history=history,
                    reason=str(exc),
                    retryable=False,
                )
            except Waiting:
                # This attempt never reached the peer. Retry it like any other timeout.
                # Exhaustion keeps the envelope and does not count as a peer failure.
                last_error = "previous call is still running"
                if attempt < self.max_attempts:
                    history.append("TIMED_OUT")
                    self.sleep(self._delay(attempt))
                    continue
                history.append("DEAD_LETTERED")
                return self._save(original, "DEAD_LETTERED", attempt, history, last_error)
            except (StillRunning, Busy) as exc:
                # The handler started and did not finish. Another attempt would run it twice.
                history.append("DEAD_LETTERED")
                return self._fail_peer(original, "DEAD_LETTERED", attempt, history, str(exc) or "timeout")
            except TimeoutError as exc:
                last_error = str(exc) or "timeout"
                if attempt < self.max_attempts:
                    history.append("TIMED_OUT")
                    self.sleep(self._delay(attempt))
                    continue
                history.append("DEAD_LETTERED")
                return self._fail_peer(original, "DEAD_LETTERED", attempt, history, last_error)
            except (ConnectionError, OSError) as exc:
                last_error = str(exc) or "peer unreachable"
                if attempt < self.max_attempts:
                    history.append("FAILED")
                    self.sleep(self._delay(attempt))
                    continue
                history.append("DEAD_LETTERED")
                return self._fail_peer(original, "DEAD_LETTERED", attempt, history, last_error)
            except Exception as exc:
                history.append("FAILED")
                return self._terminal(
                    original,
                    "FAILED",
                    attempts=attempt,
                    history=history,
                    reason=f"handler failed: {exc}",
                    retryable=False,
                )
            if not isinstance(response, dict):
                history.append("FAILED")
                return self._terminal(
                    original,
                    "FAILED",
                    attempts=attempt,
                    history=history,
                    reason="handler did not return an object",
                    retryable=False,
                )
            echoed = response.get("correlation_id")
            if echoed != original["correlation_id"]:
                history.append("FAILED")
                return self._terminal(
                    original,
                    "FAILED",
                    attempts=attempt,
                    history=history,
                    reason="response correlation_id does not match the request",
                    retryable=False,
                )
            with self._lock:
                self._failures[recipient] = 0
            history.append("RESPONDED")
            return {
                "ok": True,
                "state": "RESPONDED",
                "attempts": attempt,
                "history": history,
                "envelope": original,
                "response": response,
                "correlation_id": original["correlation_id"],
            }
        history.append("DEAD_LETTERED")
        return self._fail_peer(original, "DEAD_LETTERED", self.max_attempts, history, last_error)

    def _delay(self, attempt: int) -> float:
        return min(self.backoff_s * float(2 ** (attempt - 1)), self.max_backoff_s)

    def _save(self, envelope: dict[str, Any], state: str, attempts: int, history: list[str], reason: str) -> dict[str, Any]:
        """Keep the envelope for replay. The handler never started, so the peer did not fail."""
        with self._lock:
            self.dead.append({"envelope": envelope, "reason": reason, "state": state})
        return self._terminal(envelope, state, attempts=attempts, history=history, reason=reason)

    def _fail_peer(self, envelope: dict[str, Any], state: str, attempts: int, history: list[str], reason: str) -> dict[str, Any]:
        recipient = envelope["recipient"]
        with self._lock:
            self._failures[recipient] = self._failures.get(recipient, 0) + 1
            if self._failures[recipient] >= self.breaker_threshold:
                self.open_circuits.add(recipient)
            self.dead.append({"envelope": envelope, "reason": reason, "state": state})
        return self._terminal(envelope, state, attempts=attempts, history=history, reason=reason)

    def _terminal(self, envelope: dict[str, Any], state: str, *, attempts: int, history: list[str], reason: str, retryable: bool = True) -> dict[str, Any]:
        return {
            "ok": False,
            "state": state,
            "attempts": attempts,
            "history": history,
            "reason": reason,
            "envelope": envelope,
            "correlation_id": envelope["correlation_id"],
            "retryable": retryable,
        }
