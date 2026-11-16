"""Cells for the host `--ingest` route (U3.5e). Each cell seeds a store through the real `TaskStore`, then
`orch_status --ingest` runs on Python and on the selector with `GSD_ENGINE=rust`. A served cell must be answered by the
binary, not handed back. Clock columns are not compared.

A transition to `IN_REVIEW` with no required gates tries to approve, and that path is fail-closed without a signing key.
The review cell therefore runs under a throwaway HMAC key generated at import and never written to a file.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "orch_status.py"
ORDER = ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW")
NAMED = ("task.claimed", "task.result.rejected", "task.result.deduplicated", "escalation.request", "task.approval.skipped")
SIGNING_KEY = secrets.token_hex(32)


@dataclass(frozen=True)
class Cell:
    """One ingest call. `served` is true when the binary must answer it; a false cell is outside the grammar and Python answers."""

    name: str
    served: bool
    expect_states: dict[str, str]
    expect_events: tuple[str, ...] = ()
    expect_code: str | None = None
    advisory: bool = False
    dry_run: bool = False
    missing: bool = False
    replay: bool = False
    signed: bool = False

CELLS = (
    Cell("heartbeat", True, {"H-1": "IN_PROGRESS"}),
    Cell("claim", True, {"H-1": "IN_PROGRESS"}, ("task.claimed",)),
    Cell("review", True, {"H-1": "DONE"}, signed=True),
    Cell("escalate", True, {"H-1": "IN_REVIEW"}, ("escalation.request",)),
    Cell("advisory-skip", True, {"H-1": "IN_REVIEW", "H-2": "IN_REVIEW"}, ("task.approval.skipped",), advisory=True),
    Cell("reject", True, {"H-1": "CREATED"}, ("task.result.rejected",), "E-CONTRACT"),
    Cell("dedup", True, {"H-1": "IN_PROGRESS"}, ("task.result.deduplicated",), replay=True),
    Cell("missing-file", False, {"H-1": "IN_PROGRESS"}, missing=True),
    Cell("dry-run", False, {"H-1": "IN_PROGRESS"}, dry_run=True),
)


def output(tid: str) -> dict[str, str]:
    return {"kind": "code.backend", "uri": f"file://{tid}", "version": "1", "digest": ""}


def result_body(tid: str, state: str) -> dict[str, Any]:
    return {"task_id": tid, "state": state, "outputs": [output(tid)]}


def _drive(store: Any, tid: str, upto: str, notes: dict[str, Any]) -> None:
    store.create(task_id=tid, correlation_id="c", capability="code.backend", risk_class="low", notes=notes)
    if upto == "CREATED":
        return
    for state in ORDER[: ORDER.index(upto) + 1]:
        store.transition(tid, state)


def seed(root: Path, cell: Cell) -> None:
    """The store the cell starts from. A replay cell has already ingested the same result once, through Python."""
    from swarm.taskstore import TaskStore

    swarm = root / ".swarm"
    swarm.mkdir(parents=True, exist_ok=True)
    store = TaskStore(swarm / "tasks.db")
    if cell.name == "claim":
        _drive(store, "H-1", "PLANNED", {"gates": []})
    elif cell.name == "reject":
        _drive(store, "H-1", "CREATED", {"gates": []})
    elif cell.name == "escalate":
        _drive(store, "H-1", "IN_PROGRESS", {})
    elif cell.name == "advisory-skip":
        _drive(store, "H-1", "IN_PROGRESS", {"gates": []})
        _drive(store, "H-2", "IN_REVIEW", {"gates": []})
    else:
        _drive(store, "H-1", "IN_PROGRESS", {"gates": []})
    store.conn.close()
    if cell.replay:
        path = root / "result.json"
        path.write_text(json.dumps(result_body("H-1", "IN_PROGRESS")), encoding="utf-8")
        done = run(root, cell, "python", path)
        if done.returncode != 0:
            raise RuntimeError(f"replay seed failed: {done.stdout} {done.stderr}")


def argv_for(cell: Cell, result: Path, root: Path) -> list[str]:
    argv = [sys.executable, str(SCRIPT), "--ingest", str(result), "--json", "--root", str(root)]
    if cell.advisory:
        argv.append("--advisory")
    if cell.dry_run:
        argv.append("--dry-run")
    return argv


def child_env(root: Path, cell: Cell, engine: str, binary: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env["SWARM_DIR"] = str(root / ".swarm")
    env["SWARM_GSD_STATE_FILE"] = ""
    env["SWARM_CLAUDE_STATE_DIR"] = str(root / "claude")
    env.pop("SWARM_ED25519_KEY", None)
    if cell.signed:
        env["SWARM_SIGNING_KEY"] = SIGNING_KEY
    else:
        env.pop("SWARM_SIGNING_KEY", None)
    if engine == "python":
        env["GSD_ENGINE"] = "python"
    else:
        env["GSD_ENGINE"] = "rust"
        env["GSD_ENGINE_BIN"] = binary or ""
        env["GSD_RUST_BACKEND"] = "host"
    return env


def run(root: Path, cell: Cell, engine: str, result: Path, binary: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv_for(cell, result, root), capture_output=True, text=True, cwd=REPO, timeout=60,
        env=child_env(root, cell, engine, binary),
    )


def snapshot(root: Path) -> dict[str, Any]:
    swarm = root / ".swarm"
    conn = sqlite3.connect(f"{(swarm / 'tasks.db').as_uri()}?mode=ro", uri=True)
    try:
        tasks = conn.execute("SELECT task_id, state FROM tasks ORDER BY task_id").fetchall()
        arts = conn.execute("SELECT task_id, kind, uri FROM artifacts ORDER BY task_id, uri").fetchall()
        trans = conn.execute("SELECT task_id, to_state FROM transitions ORDER BY id").fetchall()
    finally:
        conn.close()
    types = []
    events = swarm / "events.jsonl"
    if events.exists():
        for line in events.read_text(encoding="utf-8").splitlines():
            if line.strip():
                types.append(json.loads(line).get("type"))
    return {"tasks": tasks, "artifacts": arts, "transitions": trans, "events": types}


def named_events(types: list[str]) -> list[str]:
    return [item for item in types if item in NAMED]


def loads(text: str) -> dict[str, Any] | None:
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


def binary_answered(root: Path, cell: Cell, result: Path, binary: str) -> bool:
    """True when a direct binary call does not hand the cell back (`write_state` is not `not_started`)."""
    effects = root / "effects.json"
    effects.write_text("", encoding="utf-8")
    request = {
        "agent": "A01", "script": "orch_status", "argv": argv_for(cell, result, root)[2:],
        "root": str(REPO), "env": {"SWARM_DIR": str(root / ".swarm")}, "effects_path": str(effects),
    }
    env = {"PATH": os.environ.get("PATH", ""), "GSD_RUST_BACKEND": "host"}
    if cell.signed:
        env["SWARM_SIGNING_KEY"] = SIGNING_KEY
    done = subprocess.run(
        [binary], input=json.dumps(request), capture_output=True, text=True, cwd=REPO, timeout=60, env=env,
    )
    body = loads(done.stdout)
    if body is None:
        return False
    details = (body.get("error") or {}).get("details") or {}
    return details.get("write_state") != "not_started" and (body.get("error") or {}).get("code") != "E-DEP"
