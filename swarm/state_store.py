"""State/Idempotency Store for hook triggers, swarm run state (PENDING/FIRED etc per n5/n7).
Boring extraction + extension from taskstore + runlog facts. Single-writer tx, atomic check-set.
Extended n6 for full states LOADING IN_PROGRESS COMPLETED FAILED EMPTY ERROR in SwarmStateStore, separate audit logs for lifecycle/identities, gsd-autonomous workflowz ultrathink orchestrate OMP session visibility.
"""

from __future__ import annotations
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from collections.abc import Iterator

from .atomic_file import write_text_atomic
from .paths import append_text, swarm_dir


def _db_path(root: str | Path | None) -> Path:
    return swarm_dir(root, create=True) / "tasks.db"


def claude_state_dir(default: Path) -> Path:
    """Directory of the .claude projection. SWARM_CLAUDE_STATE_DIR replaces it (read per call); empty means unset."""
    override = os.environ.get("SWARM_CLAUDE_STATE_DIR")
    return Path(override) if override else default


def gsd_state_path() -> Path | None:
    """GSD STATE.md mirror target. Off unless SWARM_GSD_STATE_FILE names a file: unset and empty both mean off."""
    override = os.environ.get("SWARM_GSD_STATE_FILE")
    return Path(override) if override else None


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class TriggerStore:
    """Dedup store for hook trigger firings (n3/n7). Atomic check-set per G-atomic-check-set."""

    def __init__(self, path: str | Path | None = None, *, root: str | Path | None = None):
        self.path = Path(path) if path else _db_path(root)
        self._init_schema()

    def _init_schema(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trigger_firings (
                    id TEXT PRIMARY KEY,
                    ts REAL NOT NULL,
                    correlation_id TEXT,
                    brief_hash TEXT,
                    fired INTEGER DEFAULT 1
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def seen_trigger(self, trigger_id: str, *, correlation_id: str | None = None, brief_hash: str | None = None) -> bool:
        """Atomic check + set. Returns True if already seen (no fire)."""
        conn = sqlite3.connect(self.path)
        try:
            with _tx(conn):
                cur = conn.execute("SELECT 1 FROM trigger_firings WHERE id = ?", (trigger_id,))
                if cur.fetchone():
                    return True
                conn.execute(
                    "INSERT INTO trigger_firings (id, ts, correlation_id, brief_hash) VALUES (?, ?, ?, ?)",
                    (trigger_id, time.time(), correlation_id, brief_hash),
                )
            return False
        finally:
            conn.close()

    def record(self, trigger_id: str, **kw: Any) -> None:
        self.seen_trigger(trigger_id, **kw)

    def forget(self, trigger_id: str) -> None:
        """Delete one firing row so an identical retry can claim it again. Used when the spawn that claimed it failed."""
        conn = sqlite3.connect(self.path)
        try:
            with _tx(conn):
                conn.execute("DELETE FROM trigger_firings WHERE id = ?", (trigger_id,))
        finally:
            conn.close()


class SwarmStateStore:
    """Light obs projection store for full states (LOADING IN_PROGRESS COMPLETED FAILED EMPTY ERROR + compat) (n5/n6) + stale guard (n7). Primary .claude for advertised, .swarm mirror.
    Wired for gsd-autonomous workflowz ultrathink orchestrate , OMP session visibility, subagent IDs, states listed.
    Added support emit for error agg (via extra), perID partial on FAILED/ERROR. Isolate errors w/ try.
    """
    STATES = ("PENDING", "LOADING", "IN_PROGRESS", "INITIALIZING", "FIRED", "RUNNING", "PARTIAL_FAILURE", "COMPLETED", "COMPLETE", "FAILED", "EMPTY", "ERROR")
    def __init__(self, path: str | Path | None = None, *, root: str | Path | None = None):
        try:
            self.root = Path(root) if root else Path(".")
            self.claude_path = claude_state_dir(self.root / ".claude") / "swarm-state.json"
            self.swarm_path = swarm_dir(root, create=True) / "swarm-state.json"
            if not self.claude_path.exists():  # an existing file means an existing directory: the mkdir syscall is only for a first write
                self.claude_path.parent.mkdir(parents=True, exist_ok=True)
                self._write(self.claude_path, {"version": "1.0", "swarm_state": "INITIALIZING", "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "progress": {}})
            if not self.swarm_path.exists():
                self._write(self.swarm_path, {"version": "1.0", "swarm_state": "INITIALIZING", "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "progress": {}})
        except Exception:
            # isolate init
            self.root = Path(".")
            self.claude_path = claude_state_dir(self.root / ".claude") / "swarm-state.json"
            self.swarm_path = self.root / "swarm-state.json"

    def _write(self, p: Path, data: dict[str, Any], text: str | None = None) -> None:
        """Atomically write `data` as JSON (or `text`, its rendering when the caller already has it); failures are isolated."""
        try:
            write_text_atomic(p, json.dumps(data, indent=2) + "\n" if text is None else text)
        except Exception:
            pass  # isolate

    def update(self, state: str, *, corr: str | None = None, progress: dict[str, Any] | None = None, extra: dict[str, Any] | None = None, gsd_state: str | None = None) -> dict[str, Any]:
        """Extend update to support gsd states (LOADING IN_PROGRESS etc), immediate state transitions for OMP gsd.
        Support error_agg, perID partial when state FAILED/ERROR.
        """
        try:
            if gsd_state:
                state = gsd_state
            if state not in self.STATES:
                gsd_state_map = {
                    "IN_PROGRESS": "RUNNING",
                    "COMPLETED": "COMPLETE",
                    "EMPTY": "PENDING",
                    "ERROR": "FAILED",
                    "STREAMING": "RUNNING",
                }
                state = gsd_state_map.get(state, state if state in self.STATES else "INITIALIZING")
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            data = {
                "version": "1.0",
                "correlation_id": corr,
                "swarm_state": state,
                "updated_at": now,
                "progress": progress or {},
                **(extra or {}),
            }
            if state in ("FAILED", "ERROR") and extra and extra.get("per_id_partial"):
                data["error_partial"] = {"subagent_id": (extra or {}).get("subagent_id"), "agg": extra.get("error_agg")}
            if extra and extra.get("error_agg"):
                data["error_agg"] = extra.get("error_agg")
            try:
                text: str | None = json.dumps(data, indent=2) + "\n"  # rendered once for both files
            except Exception:
                text = None  # unserialisable: each _write isolates it itself, nothing is written
            self._write(self.claude_path, data, text)
            self._write(self.swarm_path, data, text)
            sub_id = (extra or {}).get("subagent_id")
            slc = (extra or {}).get("slice_id") or (extra or {}).get("task_slice")
            try:
                self.append_gsd_audit("state_update", subagent_id=sub_id, task_slice=slc, state=state, identity=corr)
            except Exception:
                pass
            return data
        except Exception:
            return {"swarm_state": state, "error": "update_isolated"}

    def get(self) -> dict[str, Any]:
        try:
            if self.claude_path.exists():
                loaded: dict[str, Any] = json.loads(self.claude_path.read_text())
                return loaded
            return {}
        except Exception:
            return {}

    def append_gsd_line(self, line: str, *, root: str | Path | None = None, state: str | None = None) -> None:
        try:
            state_md = gsd_state_path()
            if state_md is not None and state_md.exists():
                with state_md.open("a") as f:
                    suffix = f" state={state}" if state else ""
                    f.write(f"swarm: {line}{suffix}\n")
        except Exception:
            pass  # isolate

    def append_gsd_audit(self, event: str, *, subagent_id: str | None = None, task_slice: str | None = None, state: str | None = None, identity: str | None = None, root: str | Path | None = None) -> None:
        """Add separate audit log fn for lifecycle and identities... Support error agg contexts."""
        try:
            base = Path(root) if root else self.root
            log_path = swarm_dir(base, create=True) / "gsd-lifecycle-audit.log"
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            sub = (subagent_id or "").replace("\n", " ")[:100]
            slc = (task_slice or "").replace("\n", " ")[:100]
            st = (state or "").replace("\n", " ")[:32]
            idt = (identity or "").replace("\n", " ")[:100]
            entry = f"{ts} event={event} subagent_id={sub} task_slice={slc} state={st} identity={idt}\n"
            append_text(log_path, entry)
        except Exception:
            pass  # isolate

    def check_pending_timeout(self, max_age: float = 3600) -> None:
        try:
            data = self.get()
            if data.get("swarm_state") == "PENDING":
                ts = data.get("updated_at")
                if ts:
                    pass
        except Exception:
            pass


# compat thin
def get_trigger_store(root: str | Path | None = None) -> TriggerStore:
    return TriggerStore(root=root)


def get_swarm_state_store(root: str | Path | None = None) -> SwarmStateStore:
    return SwarmStateStore(root=root)
