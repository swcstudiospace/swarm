#!/usr/bin/env python3
"""A01 — return a verified canonical specialist assignment for an existing leased task.

This operation never claims a task, starts a runner, or records task/gate/log state.
Only the signed envelope conveys authority; the canonical prompt's resource and
upstream sections remain untrusted context, not additional signed permissions.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from swarm.envelope import real_key_configured, signing_config_error, verify_envelope  # noqa: E402
from swarm.errors import ErrorCode, SwarmError  # noqa: E402
from swarm.manifest import get_agent  # noqa: E402
from swarm.paths import swarm_dir  # noqa: E402
from swarm.script_base import AgentScript, Ctx, check_task_id  # noqa: E402
from swarm.taskstore import TaskStore, TaskState  # noqa: E402
from scripts.swarm_run import assignment_prompt  # noqa: E402


class ReadOnlyTaskStore(TaskStore):
    """Reuse TaskStore decoding/read APIs without its creating/migrating constructor."""

    def __init__(self, *, root: Path):
        self.path = swarm_dir(root) / "tasks.db"
        self.conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")
        self._tx_depth = 0


def _required_id(value: str | None, what: str) -> str:
    if not isinstance(value, str):
        raise SwarmError(ErrorCode.E_INPUT, f"{what} is required")
    try:
        return check_task_id(value, what)
    except SwarmError:
        raise SwarmError(ErrorCode.E_INPUT, f"invalid {what}") from None


def _assignment(prompt: str, task: dict, agent: dict) -> dict:
    # Context may contain fence/heading text. Never accept its claims of authority:
    # accept exactly one cryptographically verified, correctly bound assignment.
    matches = []
    for block in prompt.split("# task.assign (signed envelope)\n```json\n")[1:]:
        try:
            envelope = json.loads(block.split("\n```", 1)[0])
        except (ValueError, TypeError):
            continue
        if not isinstance(envelope, dict):
            continue
        payload = envelope.get("payload")
        if (envelope.get("schema") != "swarm.v1.task.assign"
                or envelope.get("type") != "task.assign"
                or envelope.get("source") != "A01@runner"
                or envelope.get("target") != agent["id"]
                or envelope.get("correlation_id") != task["correlation_id"]
                or not isinstance(payload, dict)
                or payload.get("task_id") != task["task_id"]
                or payload.get("correlation_id") != task["correlation_id"]
                or payload.get("agent_id") != agent["id"]):
            continue
        if verify_envelope(envelope):
            matches.append(envelope)
    if len(matches) != 1:
        raise SwarmError(ErrorCode.E_POLICY, "fail-closed: specialist assignment signature invalid or ambiguous")
    return matches[0]


def run(args, ctx: Ctx) -> dict:
    if ctx.agent_id != "A01" or any(os.environ.get(name) == "1"
                                     for name in ("SWARM_CHILD", "SWARM_AGENT_SESSION")):
        raise SwarmError(ErrorCode.E_POLICY, "specialist assignments require the trusted A01 controller")
    if os.environ.get("SWARM_AGENT") not in (None, "", "A01", "a01-orchestrator"):
        raise SwarmError(ErrorCode.E_POLICY, "specialist assignments require the trusted A01 controller")
    if ctx.dry_run:
        raise SwarmError(ErrorCode.E_POLICY, "authenticated specialist assignments are unavailable in dry-run")
    task_id = _required_id(ctx.task_id, "task id")
    correlation_id = _required_id(ctx.correlation_id, "correlation id")
    if signing_config_error() or not real_key_configured():
        raise SwarmError(ErrorCode.E_POLICY, "fail-closed: a real controller signer is required")
    try:
        store = ReadOnlyTaskStore(root=ctx.root)
    except (OSError, sqlite3.Error):
        raise SwarmError(ErrorCode.E_INPUT, "existing task store is unavailable") from None
    try:
        try:
            task = store.get(task_id)
        except SwarmError:
            raise SwarmError(ErrorCode.E_INPUT, "existing task is required") from None
        if task["correlation_id"] != correlation_id:
            raise SwarmError(ErrorCode.E_CONTRACT, "task correlation mismatch")
        if task["state"] != TaskState.IN_PROGRESS.value or task["attempt"] < 1:
            raise SwarmError(ErrorCode.E_POLICY, "specialist assignment requires an IN_PROGRESS leased task")
        if not task["agent_id"]:
            raise SwarmError(ErrorCode.E_CONTRACT, "task requires an assigned manifest agent")
        try:
            agent = get_agent(task["agent_id"])
        except (KeyError, ValueError, TypeError, OSError):
            raise SwarmError(ErrorCode.E_CONTRACT, "task requires an assigned manifest agent") from None
        if task["capability"] not in agent["capabilities"] or agent["id"] == "A01":
            raise SwarmError(ErrorCode.E_CONTRACT, "task requires a matching specialist capability")
        try:
            prompt = assignment_prompt(store, task, agent, ctx.root)
        except Exception:
            raise SwarmError(ErrorCode.E_CONTRACT, "canonical specialist prompt is unavailable") from None
        assignment = _assignment(prompt, task, agent)
        if not real_key_configured():
            raise SwarmError(ErrorCode.E_POLICY, "fail-closed: a real controller signer is required")
        return {"status": "ok", "task_id": task_id, "correlation_id": correlation_id,
                "agent_slug": agent["slug"], "assignment_verified": True,
                "assignment": assignment, "prompt": prompt}
    finally:
        store.conn.close()


class AssignmentScript(AgentScript):
    """Read-only AgentScript: no runlog persistence or unsafe parser/error reflection."""

    def parser(self) -> argparse.ArgumentParser:
        parser = super().parser()
        def error(_message):
            raise SwarmError(ErrorCode.E_INPUT, "invalid assignment operation arguments")
        parser.error = error
        return parser

    def main(self, argv: list[str] | None = None) -> int:
        as_json = True
        try:
            args = self.parser().parse_args(argv)
            as_json = args.json
            ctx = Ctx(self.agent_id, self.name, args.task_id, args.correlation_id,
                      args.dry_run, Path(args.root).resolve())
            result = self.run(args, ctx)
            result.update(agent=self.agent_id, script=self.name)
            self._print(result, as_json)
            return self.EXIT_OK
        except SwarmError as error:
            failure = error.to_dict()
        except Exception:
            # Persisted resource/manifest errors must not echo contents, private
            # references or exception traces into stdout or a runlog.
            failure = SwarmError(ErrorCode.E_CONTRACT, "specialist assignment could not be produced safely").to_dict()
        self._print({"agent": self.agent_id, "script": self.name, "status": "error",
                     "error": failure}, as_json)
        return self.EXIT_ERROR


# Engine selector (swarm/engine_select.py): inert while ENGINE_ROUTES is empty. A unit that gives this script a route on the Rust engine
# registers its `engine_select.Route` here; until then GSD_ENGINE=python (or unset) costs nothing and `rust` is answered as an unserved route.
ENGINE_ROUTES: tuple = ()


if __name__ == "__main__":
    script = AssignmentScript("A01", "orch_assign", run, description=__doc__)
    answer = None
    if ENGINE_ROUTES or os.environ.get("GSD_ENGINE", "python") not in ("python", ""):
        from swarm import engine_select
        answer = engine_select.select_engine(engine_select.Selector(script, ENGINE_ROUTES), sys.argv[1:])
    sys.exit(answer if answer is not None else script.main())
