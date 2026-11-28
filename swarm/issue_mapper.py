"""Issue mapping (W2.3) — 1-to-many, idempotent, TBD handling.

Design: docs/design-orchestration-SPE-38340.md §7; plan-CW2 §§1-3.
Layer: data.

Reuse:
- Idempotency pattern: ``swarm.memory.idempotency_key`` is the established
  derivation (SHA-256 hex of compact JSON, no wall-clock, no attempt in the
  tracker key per §7.2/R-P4). This module's ``derive_idempotency_key``
  implements the tracker tuple from §7.2 verbatim (correlation_id, phase,
  op, tracker, subject, body_hash) — the memory helper cannot be reused
  directly because its signature *requires* ``attempt`` in the key, which
  §7.2 explicitly forbids for tracker writes. Both share the same compact-
  JSON hashing pattern.
- DB path + transactions: ``state_store._db_path`` / ``state_store._tx``
  (same ``tasks.db``, new ``phase_issue_map`` table per §10.3).
- Fake IDs: ``fake_tracker.fake_id_for`` (single definition of the scheme).
- Errors: ``swarm.errors``.

GAPS recorded:
- G-MAP-1: No ``phase_issue_map`` table existed. Added (§10.3 columns).
- G-MAP-2: No drift/orphan/conflict classification existed for mapping.
  Added pure helpers ``classify_mapping`` / ``find_orphans`` (§8.1).
- G-MAP-3: TBD→real cutover had no representation. ``record_real_id``
  requires explicit user approval (never a flag flip, plan-CW2 §3).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .errors import ErrorCode, SwarmError
from .fake_tracker import fake_id_for
from .paths import swarm_dir  # noqa: F401  (documents the shared DB owner)
from .state_store import _db_path, _tx

__all__ = ["IssueMapper", "derive_idempotency_key", "classify_mapping", "find_orphans",
           "NOTION_PAGE_REF"]

NOTION_PAGE_REF = "https://app.notion.com/p/40abc1a0c7ae81b588b7e01197a8d77c"  # STATE:134, read-only

_PHASE_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _canonical_body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def derive_idempotency_key(*, correlation_id: str, phase_id: str, op: str,
                           tracker: str = "linear", subject: str,
                           body: dict[str, Any]) -> str:
    """§7.2: sha256(compact_json([correlation_id, phase_id, op, tracker,
    subject, body_hash])). Stable across retries; no attempt/timestamp."""
    for name, val in (("correlation_id", correlation_id), ("phase_id", phase_id),
                      ("op", op), ("tracker", tracker), ("subject", subject)):
        if not isinstance(val, str) or not val:
            raise SwarmError(ErrorCode.E_INPUT, f"{name} required", field=name)
    if not _PHASE_RE.match(phase_id):
        raise SwarmError(ErrorCode.E_INPUT, f"invalid phase_id {phase_id!r}", field="phase_id")
    fields = [correlation_id, phase_id, op, tracker, subject, _canonical_body_hash(body)]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def classify_mapping(state_verdict: str, tracker_status: str | None) -> str:
    """§5 precedence → §8.1 class. STATE wins for completion."""
    if tracker_status is None:
        return "orphan" if state_verdict != "not_run" else "ok"
    if state_verdict == "gaps_found" and tracker_status == "done":
        return "conflict"  # never auto-close (B-blocker rule)
    if state_verdict in ("passed", "passed_with_recorded_debt") and tracker_status in ("open", "in_progress", "todo"):
        return "drift"  # mirror lags; auto-correctable in fake mode
    return "ok"


def find_orphans(*, mapped_fake_ids: set[str], tracker_fake_ids: set[str],
                 phases: set[str], mapped_phases: set[str]) -> list[dict[str, Any]]:
    """§8.1 orphan: records with no counterpart. Report only; never delete."""
    out: list[dict[str, Any]] = []
    for fid in sorted(tracker_fake_ids - mapped_fake_ids):
        out.append({"class": "orphan", "field": "fake_id", "a_value": fid, "b_value": "",
                    "disposition": "reported", "candidate_parent": None})
    for ph in sorted(phases - mapped_phases):
        out.append({"class": "orphan", "field": "phase_id", "a_value": ph, "b_value": "",
                    "disposition": "reported", "phase_id": ph})
    return out


class IssueMapper:
    """Idempotent phase→Issue store over ``phase_issue_map`` (§10.3).

    Create is create-or-return (replay ⇒ same record, ``deduped: True``,
    no new row). Update is last-writer-wins on ``updated_at``; the loser is
    reported by ``classify_mapping`` as conflict, never an error.
    """

    def __init__(self, path: str | Path | None = None, *, root: str | Path | None = None,
                 tracker: Any = None):
        self.path = Path(path) if path else _db_path(root)
        self.tracker = tracker  # optional FakeTrackerAdapter (fake-only)
        self._init_schema()

    def _init_schema(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phase_issue_map (
                  phase_id TEXT PRIMARY KEY, primary_fake_id TEXT NOT NULL,
                  child_fake_ids_json TEXT NOT NULL DEFAULT '[]', tbd_real_id TEXT,
                  keys_json TEXT NOT NULL DEFAULT '{}', notion_page_ref TEXT,
                  updated_at TEXT NOT NULL
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
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {"phase_id": row["phase_id"], "primary_fake_id": row["primary_fake_id"],
                "child_fake_ids": json.loads(row["child_fake_ids_json"]),
                "tbd_real_id": row["tbd_real_id"], "idempotency_keys": json.loads(row["keys_json"]),
                "notion": {"page_ref": row["notion_page_ref"] or NOTION_PAGE_REF,
                           "mode": "read_only", "tbd_real_id": None},
                "updated_at": row["updated_at"]}

    def get_map(self, phase_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM phase_issue_map WHERE phase_id=?", (phase_id,)).fetchone()
            return self._row(row) if row else None
        finally:
            conn.close()

    def list_maps(self) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            return [self._row(r) for r in conn.execute("SELECT * FROM phase_issue_map ORDER BY phase_id")]
        finally:
            conn.close()

    def map_issue(self, *, phase_id: str, op: str, correlation_id: str,
                  child_index: int | None = None,
                  body: dict[str, Any] | None = None) -> dict[str, Any]:
        """``op`` ∈ create_primary | create_child | update_primary.

        Idempotent: the key derives from (correlation, phase, op, subject,
        body) only — a replay returns the stored record with deduped=True
        and writes nothing new. ``tbd_real_id`` stays None (fake mode).
        """
        if not _PHASE_RE.match(phase_id or ""):
            raise SwarmError(ErrorCode.E_INPUT, f"invalid phase_id {phase_id!r}", field="phase_id")
        if op not in ("create_primary", "create_child", "update_primary"):
            raise SwarmError(ErrorCode.E_INPUT, f"unknown op {op!r}", field="op")
        if not correlation_id:
            raise SwarmError(ErrorCode.E_INPUT, "correlation_id required", field="correlation_id")
        payload = dict(body or {})
        subject = fake_id_for(phase_id,
                              child_index=child_index if op == "create_child" else None)
        if op == "create_child" and subject == fake_id_for(phase_id):
            raise SwarmError(ErrorCode.E_INPUT, "create_child requires child_index>=1", field="child_index")
        key = derive_idempotency_key(correlation_id=correlation_id, phase_id=phase_id,
                                     op=op, subject=subject, body=payload)
        slot = op if op != "create_child" else f"create_child:{subject}"
        conn = self._conn()
        try:
            with _tx(conn):
                row = conn.execute("SELECT * FROM phase_issue_map WHERE phase_id=?", (phase_id,)).fetchone()
                if row is not None and json.loads(row["keys_json"]).get(slot) == key:
                    record = self._row(row)
                    record["deduped"] = True
                    return record
                if row is None:
                    if op != "create_primary":
                        raise SwarmError(ErrorCode.E_INPUT,
                                         "create_primary must precede child/update ops", field="op")
                    conn.execute(
                        "INSERT INTO phase_issue_map (phase_id, primary_fake_id, child_fake_ids_json,"
                        " tbd_real_id, keys_json, notion_page_ref, updated_at) VALUES (?,?,?,?,?,?,?)",
                        (phase_id, subject, "[]", None, json.dumps({op: key}),
                         NOTION_PAGE_REF, _now()),
                    )
                else:
                    keys = json.loads(row["keys_json"])
                    keys[slot] = key
                    children = json.loads(row["child_fake_ids_json"])
                    if op == "create_child" and subject not in children:
                        children.append(subject)
                    updates = {"keys_json": json.dumps(keys), "updated_at": _now()}
                    if op == "create_child":
                        updates["child_fake_ids_json"] = json.dumps(children)
                    if op == "update_primary" and payload.get("status_fake"):
                        # LWW on updated_at: newest write wins; conflict is a
                        # reconciliation signal, not an error (§7.2).
                        conn.execute(
                            "UPDATE phase_issue_map SET keys_json=?, child_fake_ids_json=?, updated_at=? WHERE phase_id=?",
                            (updates["keys_json"], updates.get("child_fake_ids_json", row["child_fake_ids_json"]),
                             updates["updated_at"], phase_id))
                    else:
                        conn.execute(
                            "UPDATE phase_issue_map SET keys_json=?, child_fake_ids_json=?, updated_at=? WHERE phase_id=?",
                            (updates["keys_json"], updates.get("child_fake_ids_json", row["child_fake_ids_json"]),
                             updates["updated_at"], phase_id))
                record = self._row(conn.execute(
                    "SELECT * FROM phase_issue_map WHERE phase_id=?", (phase_id,)).fetchone())
                record["deduped"] = False
                return record
        finally:
            conn.close()

    def record_real_id(self, phase_id: str, real_id: str, *, approved_by: str) -> dict[str, Any]:
        """Fake→real cutover: single-field fill, explicit user approval only."""
        if not real_id or approved_by != "user":
            raise SwarmError(ErrorCode.E_POLICY,
                             "real tracker IDs require explicit user approval", field="approved_by")
        conn = self._conn()
        try:
            with _tx(conn):
                cur = conn.execute("UPDATE phase_issue_map SET tbd_real_id=? WHERE phase_id=?",
                                   (real_id, phase_id))
                if cur.rowcount == 0:
                    raise SwarmError(ErrorCode.E_INPUT, f"no mapping for phase {phase_id!r}", field="phase_id")
        finally:
            conn.close()
        return self.get_map(phase_id) or {}
