"""Common scaffolding for every per-agent script in scripts/.

Usage pattern
-------------
    from swarm.script_base import AgentScript

    def run(args, ctx) -> dict:      # return a JSON-serialisable result
        ...

    if __name__ == "__main__":
        AgentScript("A08", "qa_gate", run, add_args=lambda p: p.add_argument("--path")).main()

Every script therefore gets: --task-id, --correlation-id, --json, --dry-run, exit codes
(0 ok / 1 finding-fail / 2 taxonomy error), taxonomy error mapping and an events.jsonl entry.
"""
from __future__ import annotations
import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import traceback
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import ErrorCode, SwarmError, classify
from .runlog import emit

ROOT = Path(__file__).resolve().parent.parent

# Canonical id shape (CR-01, WR-05) for task and correlation ids, which become file names
# (verdicts/<id>.<gate>.json, releases/<id>.plan.json, plans/<corr>.json),
# so no path separator, no "..", no leading dash or dot, no NUL. omp/src/tools.ts mirrors it as a schema pattern.
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


_ID_FIRST = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
_ID_CHARS = _ID_FIRST | frozenset("._-")


def _is_safe_id(value: str) -> bool:
    return bool(TASK_ID_RE.fullmatch(value)) and ".." not in value


def _logged_id(value: str | None) -> str | None:
    """The id as the run log may carry it: an absent id stays absent, a safe id stays, an invalid one is dropped."""
    return value if value is None or _is_safe_id(value) else None


def _broken_id_rule(value: str) -> str:
    """The first rule of the canonical id shape that `value` breaks, named without the value itself."""
    if not value:
        return "is empty"
    if len(value) > 128:
        return "is longer than 128 characters"
    if value[0] not in _ID_FIRST:
        return "does not start with a letter or digit"
    if any(c not in _ID_CHARS for c in value):
        return "contains a character outside [A-Za-z0-9._-]"
    return "contains '..'"


def check_task_id(value: str, what: str = "task id") -> str:
    """Return `value` if it is a safe task id, else E-INPUT. The message names the rule broken and the length; it never echoes the value."""
    if not _is_safe_id(value):
        raise SwarmError(ErrorCode.E_INPUT, f"invalid {what}: {len(value)} characters, {_broken_id_rule(value)}; "
                                            "expected [A-Za-z0-9._-], starting alphanumeric, no '..', at most 128 chars")
    return value


@dataclass
class Ctx:
    agent_id: str
    script: str
    task_id: str | None
    correlation_id: str | None
    dry_run: bool
    root: Path = field(default_factory=lambda: Path.cwd())

    def emit(self, event_type: str, payload: dict[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
        # a dry run (this record and the script's exit record, which AgentScript.main emits through here) stays local
        return emit(event_type, payload, source=f"{self.agent_id}@{self.script}", correlation_id=self.correlation_id,
                    task_id=task_id or self.task_id or payload.get("task_id"), root=self.root, tee=not self.dry_run)


class AgentScript:
    EXIT_OK, EXIT_FAIL, EXIT_ERROR = 0, 1, 2

    def __init__(self, agent_id: str, name: str, run: Callable[[argparse.Namespace, Ctx], dict[str, Any]],
                 *, description: str = "", add_args: Callable[[argparse.ArgumentParser], None] | None = None):
        self.agent_id, self.name, self.run, self.description, self.add_args = agent_id, name, run, description, add_args

    def parser(self) -> argparse.ArgumentParser:
        # T-05-23: no prefix abbreviations (`--ing` for --ingest, `--tr` for --transition): only exact flags, so a
        # policy that matches flag spellings cannot be sidestepped
        p = argparse.ArgumentParser(prog=self.name, description=self.description, allow_abbrev=False)
        # an exported-but-empty env var means unset (WR-06); an explicit empty flag still fails check_task_id
        p.add_argument("--task-id", default=os.environ.get("SWARM_TASK_ID") or None)
        p.add_argument("--correlation-id", default=os.environ.get("SWARM_CORRELATION_ID") or None)
        p.add_argument("--json", action="store_true", help="print machine-readable JSON only")
        p.add_argument("--dry-run", action="store_true", help="deterministic canned output, no side effects")
        p.add_argument("--root", default=".", help="repository root to operate on")
        if self.add_args:
            self.add_args(p)
        return p

    def main(self, argv: list[str] | None = None) -> int:
        args = self.parser().parse_args(argv)
        ctx = Ctx(self.agent_id, self.name, args.task_id, args.correlation_id, args.dry_run, Path(args.root).resolve())
        try:
            if ctx.task_id is not None:
                check_task_id(ctx.task_id)
            if ctx.correlation_id is not None:  # plans/<corr>.json (WR-05)
                check_task_id(ctx.correlation_id, "correlation id")
            result = self.run(args, ctx)
            result.setdefault("agent", self.agent_id)
            result.setdefault("script", self.name)
            result.setdefault("status", "ok")
            ctx.emit(f"script.{self.name}", result)
            self._print(result, args.json)
            findings = result.get("findings") or []
            policy_fail = any(f.get("severity") in ("blocker", "major") for f in findings)
            return self.EXIT_FAIL if result.get("status") == "fail" or policy_fail else self.EXIT_OK
        except SwarmError as e:
            # the run log carries task_id and correlation_id verbatim: an id that failed the shape check is never written to it
            ctx.task_id, ctx.correlation_id = _logged_id(ctx.task_id), _logged_id(ctx.correlation_id)
            err = {"agent": self.agent_id, "script": self.name, "status": "error", "error": e.to_dict()}
            ctx.emit(f"script.{self.name}.error", err)
            self._print(err, args.json)
            return self.EXIT_ERROR
        except Exception as e:  # noqa: BLE001 - must map to taxonomy
            code = classify(e)
            err = {"agent": self.agent_id, "script": self.name, "status": "error",
                   "error": {"code": code.value, "message": f"{type(e).__name__}: {e}",
                             "handling": code.standard_handling, "trace": traceback.format_exc(limit=3)}}
            ctx.emit(f"script.{self.name}.error", err)
            self._print(err, args.json)
            return self.EXIT_ERROR

    def record_external_reply(self, argv: list[str], reply: dict[str, Any], exit_code: int) -> None:
        """Writes the run-log record `main` would have written for this call, for a reply another engine produced (D1).

        `argv` is parsed with this script's own parser (usage text is swallowed: a rejected argv, `--help` included, raises
        SwarmError E-INPUT and writes nothing), `Ctx` is built as in `main`, and the record is emitted through `ctx.emit`
        (so `tee=not dry_run`, redaction and the run-log format are the Python path's own) as `script.<name>`, or
        `script.<name>.error` when `exit_code` is 2. `reply` is not mutated: a copy gets `setdefault` of agent, script
        and status, so a key the reply already carries is kept.

        Returns None once the record is written. It raises, and has written nothing, for a rejected argv, an exit code
        other than 0, 1, 2, or (exit 0 or 1 only) a task or correlation id that fails `check_task_id`; `main` answers those
        with its own error record, so the caller is expected to fall back to it. For exit 2 an invalid id is dropped from
        the record as `main` does. The only write is the one `emit` makes, a single appended line, so a raise from
        `emit` (state dir not writable) leaves no half-written record that this method could undo; errors are never swallowed.
        """
        if exit_code not in (self.EXIT_OK, self.EXIT_FAIL, self.EXIT_ERROR):
            raise SwarmError(ErrorCode.E_INPUT, f"exit code {exit_code} has no run-log record; expected 0, 1 or 2")
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                args = self.parser().parse_args(argv)
        except SystemExit as exc:
            raise SwarmError(ErrorCode.E_INPUT, f"argv rejected by the {self.name} parser (exit {exc.code})") from exc
        ctx = Ctx(self.agent_id, self.name, args.task_id, args.correlation_id, args.dry_run, Path(args.root).resolve())
        record = dict(reply)
        if exit_code == self.EXIT_ERROR:
            ctx.task_id, ctx.correlation_id = _logged_id(ctx.task_id), _logged_id(ctx.correlation_id)
        else:
            if ctx.task_id is not None:
                check_task_id(ctx.task_id)
            if ctx.correlation_id is not None:
                check_task_id(ctx.correlation_id, "correlation id")
        record.setdefault("agent", self.agent_id)
        record.setdefault("script", self.name)
        record.setdefault("status", "error" if exit_code == self.EXIT_ERROR else "ok")
        if exit_code == self.EXIT_ERROR:
            ctx.emit(f"script.{self.name}.error", record)
        else:
            ctx.emit(f"script.{self.name}", record)

    @staticmethod
    def _print(obj: dict[str, Any], as_json: bool) -> None:
        if as_json:
            print(json.dumps(obj, indent=2, default=str))
            return
        status = obj.get("status", "ok").upper()
        print(f"[{obj.get('agent')}/{obj.get('script')}] {status}")
        summary = obj.get("summary")
        if summary:
            print(summary)
        for f in obj.get("findings", []) or []:
            print(f"  - [{f.get('severity','?')}] {f.get('id','')} {f.get('summary','')}"
                  + (f"  ({f['location']})" if f.get("location") else ""))
        if obj.get("error"):
            print("  error:", json.dumps(obj["error"], indent=2, default=str))


def sh(cmd: list[str] | str, *, cwd: Path | None = None, timeout: int = 600, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output; never raises unless check=True."""
    shell = isinstance(cmd, str)
    return subprocess.run(cmd, cwd=cwd, shell=shell, capture_output=True, text=True, timeout=timeout, check=check)


def which(binary: str) -> bool:
    from shutil import which as _w
    return _w(binary) is not None


def iter_files(root: Path, *, exts: tuple[str, ...] = (), skip_dirs: tuple[str, ...] = (".git", "node_modules", ".venv", "venv",
                                                                                         "dist", "build", "__pycache__", ".swarm")) -> Iterator[Path]:
    for p in root.rglob("*"):
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.is_file() and (not exts or p.suffix in exts):
            yield p
