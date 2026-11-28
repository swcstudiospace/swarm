"""Phase STATE persistence (W2.2) — atomic STATE via existing state_store.

Design: docs/design-orchestration-SPE-38340.md §§6, 10.3.
Layer: data (implements scheduler_ports.PhaseStatePort / StateFilePort).

Reuse of ``swarm/state_store.py`` (read, no duplicate):
- DB path: ``state_store._db_path`` (``<swarm_dir>/tasks.db``) — same file,
  new tables (``phase_state`` per §10.3). Never a second database.
- Transaction discipline: ``state_store._tx`` (``BEGIN IMMEDIATE``) —
  single-writer lease; loser raises instead of interleaving.
- Atomic publish: ``swarm.atomic_file.write_text_atomic`` — temp sibling +
  ``os.replace`` exactly as §6.1 requires (same helper state_store uses).
- Errors: ``swarm.errors.SwarmError`` (E-CONTRACT for illegal transitions,
  E-INPUT for malformed state).

``TriggerStore`` / ``SwarmStateStore`` in state_store cover hook firings
and the swarm-run projection (swarm-state.json) — different concerns; this
module does not reimplement them.

GAPS recorded (new code needed beyond existing state_store):
- G-STATE-1: No ``phase_state`` table / per-phase verdict store existed.
  Added here (§10.3 schema, verbatim columns).
- G-STATE-2: No STATE.md frontmatter parser/serializer existed
  (``gsd_state_path`` only resolves a path). Added minimal frontmatter
  parse/serialize here; body is preserved verbatim.
- G-STATE-3: No lifecycle guard enforcement existed. Added §4.3 guards
  (digest/debt/awaiting requirements) in ``transition_phase``.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .atomic_file import write_text_atomic
from .errors import ErrorCode, SwarmError
from .scheduler_ports import LEGAL_TRANSITIONS, PhaseVerdict
from .state_store import _db_path, _tx

__all__ = ["PhaseStateStore", "STATE_GAPS", "parse_frontmatter", "serialize_state_md"]

STATE_GAPS: tuple[str, ...] = (
    "G-STATE-1 phase_state table is new (§10.3); state_store had only trigger_firings",
    "G-STATE-2 STATE.md frontmatter parse/serialize is new; state_store had only gsd_state_path resolver",
    "G-STATE-3 lifecycle guard enforcement is new; state_store had no transition table",
)

_PHASE_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _check_phase_id(phase_id: str) -> str:
    if not isinstance(phase_id, str) or not _PHASE_RE.match(phase_id):
        raise SwarmError(ErrorCode.E_INPUT, f"invalid phase_id {phase_id!r}", field="phase_id")
    return phase_id


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split STATE.md into (frontmatter dict, body). Fails closed with
    E-INPUT on missing/malformed frontmatter — never guesses."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SwarmError(ErrorCode.E_INPUT, "STATE.md missing frontmatter block", field="state_md")
    front: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        front[key.strip()] = raw.strip().strip('"').strip("'")
    return front, text[m.end():]


def serialize_state_md(frontmatter: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


class PhaseStateStore:
    """SQLite projection (§10.3 ``phase_state``) + atomic STATE.md file.

    Single-writer via ``BEGIN IMMEDIATE`` (state_store._tx). Every write is
    read-then-guarded-then-published; the file is never appended in place.
    """

    def __init__(self, path: str | Path | None = None, *, root: str | Path | None = None):
        self.path = Path(path) if path else _db_path(root)
        self._init_schema()

    def _init_schema(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phase_state (
                  phase_id TEXT PRIMARY KEY, stage TEXT NOT NULL, verdict TEXT NOT NULL,
                  holds_json TEXT NOT NULL DEFAULT '[]', awaiting_json TEXT,
                  debt_json TEXT NOT NULL DEFAULT '[]', evidence_json TEXT NOT NULL DEFAULT '{}',
                  updated_at TEXT NOT NULL, correlation_id TEXT NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "phase_id": row["phase_id"],
            "stage": row["stage"],
            "verdict": row["verdict"],
            "holds": json.loads(row["holds_json"]),
            "awaiting": json.loads(row["awaiting_json"]) if row["awaiting_json"] else None,
            "debt": json.loads(row["debt_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "updated_at": row["updated_at"],
            "correlation_id": row["correlation_id"],
        }

    def get_phase_state(self, phase_id: str) -> dict[str, Any]:
        _check_phase_id(phase_id)
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM phase_state WHERE phase_id=?", (phase_id,)).fetchone()
            if row is None:
                return {"phase_id": phase_id, "stage": "undiscussed", "verdict": "not_run",
                        "holds": [], "awaiting": None, "debt": [], "evidence": {},
                        "updated_at": None, "correlation_id": None}
            return self._row_to_dict(row)
        finally:
            conn.close()

    def put_phase_state(self, state: dict[str, Any]) -> dict[str, Any]:
        phase_id = _check_phase_id(state.get("phase_id", ""))
        stage = state.get("stage", "undiscussed")
        verdict = state.get("verdict", "not_run")
        # Eager guard: illegal (from,to) is checked against the stored row.
        corr = state.get("correlation_id") or ""
        if not corr:
            raise SwarmError(ErrorCode.E_INPUT, "correlation_id required", field="correlation_id")
        self._check_guards(verdict, state)
        conn = self._conn()
        try:
            with _tx(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO phase_state (phase_id, stage, verdict, holds_json,"
                    " awaiting_json, debt_json, evidence_json, updated_at, correlation_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (phase_id, stage, verdict,
                     json.dumps(state.get("holds", [])),
                     json.dumps(state["awaiting"]) if state.get("awaiting") else None,
                     json.dumps(state.get("debt", [])),
                     json.dumps(state.get("evidence", {})),
                     state.get("updated_at") or _now(), corr),
                )
        finally:
            conn.close()
        return self.get_phase_state(phase_id)

    @staticmethod
    def _check_guards(verdict: str, state: dict[str, Any]) -> None:
        if verdict not in {v.value for v in PhaseVerdict}:
            raise SwarmError(ErrorCode.E_INPUT, f"unknown verdict {verdict!r}", field="verdict")
        if verdict == "passed":
            digest = (state.get("evidence") or {}).get("verifier_digest")
            if not digest or not _HEX64_RE.match(str(digest)):
                raise SwarmError(ErrorCode.E_CONTRACT, "passed requires evidence.verifier_digest (64 hex)", field="evidence")
        if verdict == "passed_with_recorded_debt":
            debt = state.get("debt") or []
            if not debt:
                raise SwarmError(ErrorCode.E_CONTRACT, "passed_with_recorded_debt requires non-empty debt[]", field="debt")
            if any(d.get("accepted_by") != "user" for d in debt):
                raise SwarmError(ErrorCode.E_CONTRACT, "debt accepted_by must be 'user'", field="debt")
        if verdict == "human_needed":
            awaiting = state.get("awaiting") or {}
            if not awaiting.get("kind") or not awaiting.get("detail"):
                raise SwarmError(ErrorCode.E_CONTRACT, "human_needed requires awaiting{kind,detail}", field="awaiting")

    def transition_phase(self, phase_id: str, to_stage: str, *, correlation_id: str,
                         evidence_digest: str | None = None, ruling_ref: str | None = None,
                         **fields: Any) -> dict[str, Any]:
        """Guarded transition (§4.3). Illegal (from,to) ⇒ E-CONTRACT.
        Leaving blocked_hold/conflict_hold requires ``ruling_ref``.
        Verdict transitions to passed require ``evidence_digest``."""
        _check_phase_id(phase_id)
        if not correlation_id:
            raise SwarmError(ErrorCode.E_INPUT, "correlation_id required", field="correlation_id")
        current = self.get_phase_state(phase_id)
        from_state = current["stage"]
        # Verdict-as-state: callers transition on stage; verdict moves live
        # in the same field pair (e.g. verification -> passed). Accept either
        # axis label as to_stage and require the table entry.
        if (from_state, to_stage) not in LEGAL_TRANSITIONS:
            raise SwarmError(ErrorCode.E_CONTRACT,
                             f"illegal transition {from_state} -> {to_stage}", field="to_stage")
        holds = current.get("holds") or []
        hold_kinds = {h.get("kind") for h in holds}
        if from_state in ("blocked_hold", "conflict_hold") and not ruling_ref:
            raise SwarmError(ErrorCode.E_CONTRACT, "leaving hold requires ruling_ref", field="ruling_ref")
        if to_stage == "executing" and hold_kinds & {"held", "blocked_hold", "conflict_hold"}:
            raise SwarmError(ErrorCode.E_CONTRACT, "held phase is never runnable", field="holds")
        nxt = dict(current)
        nxt.update(fields)
        nxt["phase_id"] = phase_id
        nxt["correlation_id"] = correlation_id
        if to_stage in {v.value for v in PhaseVerdict}:
            nxt["stage"] = to_stage
            nxt["verdict"] = to_stage
        else:
            nxt["stage"] = to_stage
        if evidence_digest:
            ev = dict(nxt.get("evidence") or {})
            ev["verifier_digest"] = evidence_digest
            nxt["evidence"] = ev
        if ruling_ref:
            ev = dict(nxt.get("evidence") or {})
            ev["ruling_ref"] = ruling_ref
            nxt["evidence"] = ev
        nxt["updated_at"] = _now()
        return self.put_phase_state(nxt)

    def list_phase_states(self) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM phase_state ORDER BY phase_id").fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # -- STATE.md file (StateFilePort) ------------------------------------
    def read_state(self, path: str | Path) -> dict[str, Any]:
        text = Path(path).read_text(encoding="utf-8")
        front, body = parse_frontmatter(text)
        front["__body__"] = body
        return front

    def write_state(self, path: str | Path, frontmatter: dict[str, Any]) -> None:
        """Atomic publish via write_text_atomic (single-writer, §6.1)."""
        front = dict(frontmatter)
        body = front.pop("__body__", "")
        front.setdefault("last_updated", _now())
        write_text_atomic(Path(path), serialize_state_md(front, body))

    def state_md_path(self, root: str | Path | None = None) -> Path:
        return Path(root or ".") / ".planning" / "STATE.md"
