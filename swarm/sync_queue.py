"""Durable sync queue (W2.4) — ordered enqueue/replay, offline, DLQ.

Design: docs/design-orchestration-SPE-38340.md §9; plan-CW2 §5.6.
Layer: service (implements scheduler_ports.SyncPort).

- Transactional outbox: ``sync_outbox`` + ``sync_dlq`` tables (§10.3) in
  the shared ``tasks.db`` (state_store._db_path), written with
  state_store._tx (BEGIN IMMEDIATE) — enqueue succeeds offline (202).
- Transient-only retry (D5/§9.2): retryable iff code ∈
  {E-TIMEOUT, E-DEP (transport detail only), E-CAPACITY}. Everything else
  (E-INPUT/E-CONTRACT/E-INTERNAL, live-deny E-DEP) is terminal → DLQ on
  attempt 1, no backoff. Max 5 attempts (1 + 4 retries).
- Backoff ``min(30, 1*2^attempt)`` ± 300ms jitter, jitter deterministic in
  fake mode (seeded from the idempotency key via SHA-256).
- FIFO replay; redrive re-enqueues the SAME idempotency key with a new
  attempt series (still create-or-return downstream); ``discarded`` only
  via explicit user action.

Existing reuse / gaps:
- Reused: ``state_store._db_path/_tx``, ``errors.SwarmError``,
  ``clock``-style wall timestamps via time (display only).
- G-SYNC-1: No outbox/DLQ existed. Added (§10.3 schema).
- G-SYNC-2: ``errors.ErrorHandler`` implements generic 5-attempt retry
  over arbitrary callables but has no durable queue/DLQ/determinism —
  this module adds durability + determinism, not a second retry loop.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .errors import ErrorCode, SwarmError
from .state_store import _db_path, _tx

__all__ = ["SyncQueue", "MAX_ATTEMPTS", "TRANSIENT_CODES", "backoff_seconds"]

MAX_ATTEMPTS = 5
TRANSIENT_CODES = frozenset({"E-TIMEOUT", "E-DEP", "E-CAPACITY"})
TERMINAL_DETAIL = "live tracker transport denied"  # E-DEP but terminal (host-deny)

Transport = Callable[[dict[str, Any]], None]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def backoff_seconds(attempt: int, idempotency_key: str) -> float:
    """min(30, 1*2^attempt) ± deterministic jitter in [0, 0.3)."""
    base = min(30.0, 1.0 * (2.0 ** max(0, attempt - 1)))
    seed = int.from_bytes(hashlib.sha256(idempotency_key.encode()).digest()[:4], "big")
    jitter = (seed / 0xFFFFFFFF) * 0.3
    return base + jitter


def _is_transient(err: SwarmError) -> bool:
    if err.code.value not in TRANSIENT_CODES:
        return False
    if err.code.value == "E-DEP" and TERMINAL_DETAIL in str(err):
        return False  # fake-host-deny is terminal (T-NONTRANSIENT)
    return True


class SyncQueue:
    """Durable FIFO outbox with transient-only retry + DLQ (§§9.1-9.3)."""

    def __init__(self, path: str | Path | None = None, *, root: str | Path | None = None,
                 transport: Transport | None = None):
        self.path = Path(path) if path else _db_path(root)
        self.transport = transport
        self._init_schema()

    def _init_schema(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_outbox (
                  intent_id TEXT PRIMARY KEY, phase_id TEXT NOT NULL, op TEXT NOT NULL,
                  tracker TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                  state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
                  last_error TEXT, enqueued_at TEXT NOT NULL, correlation_id TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_dlq (
                  dlq_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES sync_outbox(intent_id),
                  idempotency_key TEXT NOT NULL, last_error TEXT NOT NULL,
                  entry_state TEXT NOT NULL DEFAULT 'pending_redrive',
                  enqueued_at TEXT NOT NULL, terminal_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_fifo ON sync_outbox(enqueued_at, intent_id)")
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _outbox_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"intent_id": row["intent_id"], "phase_id": row["phase_id"], "op": row["op"],
                "tracker": row["tracker"], "idempotency_key": row["idempotency_key"],
                "state": row["state"], "attempt": row["attempt"], "last_error": row["last_error"],
                "enqueued_at": row["enqueued_at"], "correlation_id": row["correlation_id"]}

    def enqueue(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Durable enqueue (offline-safe). Re-enqueue of the same key ⇒
        the existing row (deduped), never a duplicate."""
        key = intent.get("idempotency_key", "")
        if not key:
            raise SwarmError(ErrorCode.E_INPUT, "idempotency_key required", field="idempotency_key")
        for field in ("phase_id", "op", "tracker", "correlation_id"):
            if not intent.get(field):
                raise SwarmError(ErrorCode.E_INPUT, f"{field} required", field=field)
        intent_id = intent.get("intent_id") or f"intent-{uuid.uuid4().hex[:12]}"
        conn = self._conn()
        try:
            with _tx(conn):
                exists = conn.execute("SELECT * FROM sync_outbox WHERE idempotency_key=?", (key,)).fetchone()
                if exists is not None:
                    rec = self._outbox_row(exists)
                    rec["deduped"] = True
                    return rec
                conn.execute(
                    "INSERT INTO sync_outbox (intent_id, phase_id, op, tracker, idempotency_key,"
                    " state, attempt, last_error, enqueued_at, correlation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (intent_id, intent["phase_id"], intent["op"], intent["tracker"], key,
                     "enqueued", 1, None, intent.get("enqueued_at") or _now(), intent["correlation_id"]),
                )
        finally:
            conn.close()
        conn = self._conn()
        try:
            rec = self._outbox_row(conn.execute(
                "SELECT * FROM sync_outbox WHERE idempotency_key=?", (key,)).fetchone())
            rec["deduped"] = False
            return rec
        finally:
            conn.close()

    def pending(self) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM sync_outbox WHERE state IN ('enqueued','failed_transient')"
                " ORDER BY rowid").fetchall()
            return [self._outbox_row(r) for r in rows]
        finally:
            conn.close()

    def _to_dlq(self, conn: sqlite3.Connection, row: sqlite3.Row, error: str) -> None:
        dlq_id = f"dlq-{row['intent_id']}"
        conn.execute(
            "UPDATE sync_outbox SET state='dead_lettered', last_error=? WHERE intent_id=?",
            (error, row["intent_id"]))
        conn.execute(
            "INSERT INTO sync_dlq (dlq_id, intent_id, idempotency_key, last_error, entry_state,"
            " enqueued_at, terminal_at) VALUES (?,?,?,?,?,?,?)",
            (dlq_id, row["intent_id"], row["idempotency_key"], error, "pending_redrive",
             row["enqueued_at"], _now()),
        )

    def deliver_all(self, transport: Transport | None = None) -> list[dict[str, Any]]:
        """Deliver pending intents FIFO via ``transport``.

        Returns one record per processed intent: {intent_id, outcome,
        attempts, backoffs}. ``outcome`` ∈ delivered | dead_lettered.
        Offline (transient failure) keeps the intent pending without
        burning attempts on every replay — only retryable deliveries that
        reach the transport consume an attempt; a failed attempt that is
        still retryable stays pending; attempt 5 ⇒ DLQ.
        """
        fn = transport or self.transport
        if fn is None:
            raise SwarmError(ErrorCode.E_INPUT, "a transport is required to deliver", field="transport")
        results: list[dict[str, Any]] = []
        for item in self.pending():
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM sync_outbox WHERE intent_id=?",
                                   (item["intent_id"],)).fetchone()
            finally:
                conn.close()
            backoffs: list[float] = []
            outcome = "delivered"
            attempt = row["attempt"]
            for _ in range(MAX_ATTEMPTS - attempt + 1):
                try:
                    fn(self._outbox_row(row))
                    conn = self._conn()
                    try:
                        with _tx(conn):
                            conn.execute("UPDATE sync_outbox SET state='delivered', attempt=? WHERE intent_id=?",
                                         (attempt, row["intent_id"]))
                    finally:
                        conn.close()
                    outcome = "delivered"
                    break
                except SwarmError as err:
                    if not _is_transient(err):
                        conn = self._conn()
                        try:
                            with _tx(conn):
                                self._to_dlq(conn, row, str(err))
                        finally:
                            conn.close()
                        outcome = "dead_lettered"
                        return results + [{"intent_id": item["intent_id"], "outcome": outcome,
                                           "attempts": 1, "backoffs": []}]
                    backoffs.append(backoff_seconds(attempt, row["idempotency_key"]))
                    if attempt >= MAX_ATTEMPTS:
                        conn = self._conn()
                        try:
                            with _tx(conn):
                                conn.execute("UPDATE sync_outbox SET attempt=? WHERE intent_id=?",
                                             (attempt, row["intent_id"]))
                                self._to_dlq(conn, row, str(err))
                        finally:
                            conn.close()
                        outcome = "dead_lettered"
                        break
                    conn = self._conn()
                    try:
                        with _tx(conn):
                            conn.execute(
                                "UPDATE sync_outbox SET state='failed_transient', attempt=?, last_error=?"
                                " WHERE intent_id=?",
                                (attempt + 1, str(err), row["intent_id"]))
                    finally:
                        conn.close()
                    attempt += 1
            results.append({"intent_id": item["intent_id"], "outcome": outcome,
                            "attempts": attempt, "backoffs": backoffs})
        return results

    def list_dead_letter(self) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM sync_dlq WHERE entry_state='pending_redrive'"
                                " ORDER BY terminal_at, dlq_id").fetchall()
            return [{"dlq_id": r["dlq_id"], "intent_id": r["intent_id"],
                     "idempotency_key": r["idempotency_key"], "last_error": r["last_error"],
                     "entry_state": r["entry_state"], "enqueued_at": r["enqueued_at"],
                     "terminal_at": r["terminal_at"]} for r in rows]
        finally:
            conn.close()

    def redrive(self, dlq_id: str) -> dict[str, Any]:
        """Re-enqueue same key, new attempt series (still dedupes downstream)."""
        conn = self._conn()
        try:
            with _tx(conn):
                row = conn.execute("SELECT * FROM sync_dlq WHERE dlq_id=?", (dlq_id,)).fetchone()
                if row is None or row["entry_state"] != "pending_redrive":
                    raise SwarmError(ErrorCode.E_INPUT, f"no pending_redrive entry {dlq_id!r}", field="dlq_id")
                conn.execute("UPDATE sync_dlq SET entry_state='redriven' WHERE dlq_id=?", (dlq_id,))
                conn.execute(
                    "UPDATE sync_outbox SET state='enqueued', attempt=1, last_error=NULL WHERE intent_id=?",
                    (row["intent_id"],))
                conn.execute("DELETE FROM sync_dlq WHERE dlq_id=?", (dlq_id,))
        finally:
            conn.close()
        conn = self._conn()
        try:
            rec = self._outbox_row(conn.execute(
                "SELECT * FROM sync_outbox WHERE intent_id=?", (row["intent_id"],)).fetchone())
            return rec
        finally:
            conn.close()

    def discard(self, dlq_id: str, *, approved_by: str) -> dict[str, Any]:
        """Explicit user discard — the only path that removes a DLQ row."""
        if approved_by != "user":
            raise SwarmError(ErrorCode.E_POLICY, "discard requires explicit user action", field="approved_by")
        conn = self._conn()
        try:
            with _tx(conn):
                row = conn.execute("SELECT * FROM sync_dlq WHERE dlq_id=?", (dlq_id,)).fetchone()
                if row is None:
                    raise SwarmError(ErrorCode.E_INPUT, f"no dlq entry {dlq_id!r}", field="dlq_id")
                conn.execute("UPDATE sync_dlq SET entry_state='discarded' WHERE dlq_id=?", (dlq_id,))
        finally:
            conn.close()
        return {"dlq_id": dlq_id, "entry_state": "discarded"}
