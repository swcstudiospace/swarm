"""SQLite Task Store implementing swarm.task.v1 and the lifecycle state machine.

Only A01 (orchestrator) may call `transition`
other agents call `report_status`,
which is validated and applied by the orchestrator scripts.
"""
from __future__ import annotations
import builtins
import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, cast

from .envelope import insecure_dev_key, real_key_configured
from .errors import SwarmError, ErrorCode
from .paths import swarm_dir
from .gates import validate_verdict


class TaskState(str, Enum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    PLANNED = "PLANNED"
    CLAIMED = "CLAIMED"
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    RETRY = "RETRY"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"


S = TaskState
LEGAL_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    S.CREATED: {S.VALIDATED, S.CANCELLED},
    S.VALIDATED: {S.PLANNED, S.BLOCKED, S.CANCELLED},
    S.PLANNED: {S.CLAIMED, S.BLOCKED, S.FAILED, S.CANCELLED},
    S.CLAIMED: {S.IN_PROGRESS, S.FAILED, S.RETRY, S.BLOCKED, S.CANCELLED},
    S.IN_PROGRESS: {S.IN_REVIEW, S.FAILED, S.RETRY, S.BLOCKED, S.CANCELLED},
    S.IN_REVIEW: {S.APPROVED, S.CHANGES_REQUESTED, S.FAILED, S.CANCELLED},
    S.CHANGES_REQUESTED: {S.IN_PROGRESS, S.ESCALATED, S.CANCELLED},
    S.APPROVED: {S.DONE, S.CANCELLED},
    S.BLOCKED: {S.PLANNED, S.CLAIMED, S.CANCELLED, S.ESCALATED},
    S.FAILED: {S.RETRY, S.ESCALATED, S.CANCELLED},
    S.RETRY: {S.PLANNED, S.CLAIMED, S.ESCALATED},
    S.ESCALATED: {S.PLANNED, S.CANCELLED},
    S.DONE: set(),
    S.CANCELLED: set(),
}
MAX_REWORK_LOOPS = 2
SATISFIED_STATES = {S.IN_REVIEW.value, S.APPROVED.value, S.DONE.value}
# A gate task in one of these states had its result accepted by apply_result (never refused, FAILED or BLOCKED).
ACCEPTED_ISSUER_STATES = frozenset({S.IN_REVIEW.value, S.APPROVED.value, S.DONE.value})
DEFAULT_MAX_ATTEMPTS = 3
GATES_BY_RISK = {"low": ["review"], "medium": ["review", "quality"],
                 "high": ["review", "quality", "security", "release"]}

_LOG = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL, parent_id TEXT,
  dag_depth INTEGER DEFAULT 0, capability TEXT NOT NULL, agent_id TEXT,
  title TEXT, inputs TEXT DEFAULT '[]', outputs TEXT DEFAULT '[]',
  acceptance TEXT DEFAULT '[]', budget TEXT DEFAULT '{}', risk_class TEXT DEFAULT 'low',
  priority TEXT DEFAULT 'P2', state TEXT NOT NULL, attempt INTEGER DEFAULT 0,
  max_attempts INTEGER DEFAULT 3, rework_loops INTEGER DEFAULT 0,
  depends_on TEXT DEFAULT '[]', assigned_to TEXT, notes TEXT,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, from_state TEXT, to_state TEXT,
  actor TEXT, reason TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS verdicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, gate TEXT, verdict TEXT,
  agent_id TEXT, findings TEXT, expires_at REAL, ts REAL, envelope_json TEXT, sig TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, uri TEXT,
  version TEXT, digest TEXT, producer TEXT, ts REAL
);
"""

_VERIFIED = {"ok", "fail", "expired"}  # a verified, current envelope; reconcile acts only on these
_JSON_COLS = ("inputs", "outputs", "acceptance", "budget", "depends_on")


# A task row as read from SQLite. JSON columns hold per-column types, so their values stay Any.
TaskRow = dict[str, Any]


def _verdicts_since(task: TaskRow) -> float:
    """Rework cut-off that transition(CHANGES_REQUESTED) writes to notes.verdicts_since; 0 before any rework."""
    return float(task["notes_json"].get("verdicts_since") or 0)


def _gates_of(task: TaskRow) -> list[str]:
    override = (task.get("notes_json") or {}).get("gates")
    return list(override) if override is not None else GATES_BY_RISK[task["risk_class"]]


class TaskStore:
    def __init__(self, path: str | Path | None = None, *, root: str | Path | None = None):
        self.path = Path(path) if path else swarm_dir(root, create=True) / "tasks.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)  # contenders wait for the write lock, not "database is locked"
        self.conn.row_factory = sqlite3.Row
        self._tx_depth = 0
        self._live_dirty = False  # a transition committed-in-intent inside transaction(): refresh live.json after the outermost commit
        self._tune_journal()
        self.conn.executescript(_SCHEMA)
        self._migrate_verdicts()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> TaskStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _tune_journal(self) -> None:
        """Opt-in (SWARM_TASKSTORE_WAL=1, read at every open): WAL + synchronous=NORMAL, so a commit costs no fsync of
        the main file instead of about two (measured 17% of a dry run). Off by default: default-on changes the scheduler's
        retry-versus-priority ordering in timing-sensitive scenarios (faster commits) and makes read-only openers leave
        tasks.db-wal and tasks.db-shm behind."""
        if os.environ.get("SWARM_TASKSTORE_WAL") != "1":
            return
        try:
            mode = str(self.conn.execute("PRAGMA journal_mode").fetchone()[0])
            if mode.lower() != "wal":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:
            # read-only filesystem, no shared memory, or a concurrent opener holds the lock: keep the current mode, only slower
            _LOG.debug("journal_mode WAL not applied (%s); keeping the current mode", exc)

    def _migrate_verdicts(self) -> None:
        """D-14: in-place, idempotent add of the signed-envelope columns to a v1 verdicts table.
        No backup is taken; legacy rows keep NULL envelope_json and count as missing."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(verdicts)")}
        for col, ddl in (("envelope_json", "ALTER TABLE verdicts ADD COLUMN envelope_json TEXT"),
                         ("sig", "ALTER TABLE verdicts ADD COLUMN sig TEXT")):
            if col in cols:
                continue
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError as e:  # concurrent opener added it first
                if "duplicate column" not in str(e):
                    raise
        self._commit()

    def _commit(self) -> None:
        """Commit unless inside transaction(); the outermost transaction() commits."""
        if self._tx_depth == 0:
            self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[TaskStore]:
        """All-or-nothing block: BEGIN IMMEDIATE (write lock) on outermost entry,
        commit on success, rollback on exception. Nested entries join the outer one."""
        if self._tx_depth == 0:
            if self.conn.in_transaction:
                self.conn.commit()
            self.conn.execute("BEGIN IMMEDIATE")
        self._tx_depth += 1
        try:
            yield self
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self._live_dirty = False  # rolled back: nothing to publish
                self.conn.rollback()
            raise
        self._tx_depth -= 1
        if self._tx_depth == 0:
            try:
                self.conn.commit()
            except BaseException:
                self._live_dirty = False  # not committed: nothing to publish
                self.conn.rollback()
                raise
            if self._live_dirty:  # after the write lock is released
                self._live_dirty = False
                self._notify_committed()

    # ---- CRUD -------------------------------------------------------------
    def create(self, *, task_id: str, correlation_id: str, capability: str, title: str = "",
               parent_id: str | None = None, dag_depth: int = 0, inputs: object = None, acceptance: object = None,
               budget: object = None, risk_class: str = "low", priority: str = "P2", depends_on: object = None,
               agent_id: str | None = None, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
               notes: dict[str, Any] | None = None) -> TaskRow:
        now = time.time()
        self.conn.execute(
            "INSERT INTO tasks (task_id, correlation_id, parent_id, dag_depth, capability, agent_id, title,"
            " inputs, acceptance, budget, risk_class, priority, state, depends_on, max_attempts,"
            " notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, correlation_id, parent_id, dag_depth, capability, agent_id, title,
             json.dumps(inputs or []), json.dumps(acceptance or []),
             json.dumps(budget or {"max_tokens": 200000, "max_wall_s": 1800, "max_cost_usd": 5}),
             risk_class, priority, S.CREATED.value, json.dumps(depends_on or []), max_attempts,
             json.dumps(notes or {}), now, now))
        self._log(task_id, None, S.CREATED, "system", "created")
        self._commit()
        return self.get(task_id)

    def get(self, task_id: str) -> TaskRow:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise SwarmError(ErrorCode.E_INPUT, f"unknown task {task_id}")
        return self._row(row)

    def list(self, *, correlation_id: str | None = None, state: str | None = None) -> builtins.list[TaskRow]:
        q, args = "SELECT * FROM tasks", []
        conds = []
        if correlation_id:
            conds.append("correlation_id=?")
            args.append(correlation_id)
        if state:
            conds.append("state=?")
            args.append(state)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY dag_depth, created_at"
        return [self._row(r) for r in self.conn.execute(q, args)]

    def update(self, task_id: str, **fields: object) -> TaskRow:
        cols: list[str] = []
        args: list[Any] = []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            args.append(json.dumps(v) if k in _JSON_COLS else v)
        cols.append("updated_at=?")
        args.append(time.time())
        args.append(task_id)
        self.conn.execute(f"UPDATE tasks SET {', '.join(cols)} WHERE task_id=?", args)
        self._commit()
        return self.get(task_id)

    # ---- state machine (A01 only) ----------------------------------------
    def transition(self, task_id: str, to_state: TaskState | str, *, actor: str = "A01",
                   reason: str = "") -> TaskRow:
        to_state = TaskState(to_state)
        # WR-02: read, legality/cap checks and every write run under one BEGIN IMMEDIATE write lock, so
        # concurrent callers serialize and a state change never commits without its audit row.
        with self.transaction():
            task = self.get(task_id)
            from_state = TaskState(task["state"])
            if to_state not in LEGAL_TRANSITIONS[from_state]:
                raise SwarmError(ErrorCode.E_CONTRACT,
                                 f"illegal transition {from_state.value} → {to_state.value} for {task_id}",
                                 task_id=task_id)
            extra = {}
            if to_state is S.CHANGES_REQUESTED:
                loops = task["rework_loops"] + 1
                notes = task["notes_json"]
                # T-06-10: the next dispatch replaces notes result/meta and reuses the same attempt
                # number, so stash this attempt's failing evidence before rework supersedes it. One
                # entry per rework loop, bounded by MAX_REWORK_LOOPS. Inside this transaction, so the
                # stash commits or rolls back with the state change itself.
                prior = {k: notes[k] for k in ("result", "meta") if notes.get(k)}
                if prior:
                    history = notes.get("rework_evidence")
                    if not isinstance(history, list):
                        history = notes["rework_evidence"] = []
                    history.append(prior)
                if loops > MAX_REWORK_LOOPS:
                    to_state, reason = S.ESCALATED, f"rework loops exhausted ({loops-1}); {reason}"
                    if prior:
                        extra["notes"] = json.dumps(notes)
                    if S.ESCALATED not in LEGAL_TRANSITIONS[from_state]:
                        # IN_REVIEW → CHANGES_REQUESTED → ESCALATED in one audited step
                        self._apply(task_id, from_state, S.CHANGES_REQUESTED, actor, "rework cap reached")
                        from_state = S.CHANGES_REQUESTED
                else:
                    extra["rework_loops"] = loops
                    notes["verdicts_since"] = time.time() + 0.001
                    extra["notes"] = json.dumps(notes)
            if to_state in (S.CLAIMED,) and from_state in (S.PLANNED, S.RETRY, S.BLOCKED):
                attempt = task["attempt"] + 1
                if attempt > task["max_attempts"]:
                    raise SwarmError(ErrorCode.E_TIMEOUT, f"max_attempts exceeded for {task_id}", task_id=task_id)
                extra["attempt"] = attempt
            if to_state is S.APPROVED:
                if os.environ.get("SWARM_REQUIRE_KEY") == "1" and not real_key_configured():
                    raise SwarmError(ErrorCode.E_POLICY,
                                     "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured", task_id=task_id)
                if not real_key_configured() and insecure_dev_key() is None:
                    raise SwarmError(ErrorCode.E_POLICY, "fail-closed: no signing key configured", task_id=task_id)
                missing = self.missing_gates(task_id)
                if missing:
                    raise SwarmError(ErrorCode.E_POLICY,
                                     f"fail-closed: {task_id} lacks passing gates {missing}", task_id=task_id)
                # T-05-31: every path (reconcile, `orch_status --transition`, the runner) applies the issuer rule, not
                # only reconcile: a passing row whose gate task's result was never accepted approves nothing
                unaccepted = self.unaccepted_gates(task_id)
                if unaccepted:
                    raise SwarmError(ErrorCode.E_POLICY,
                                     f"fail-closed: {task_id} has unaccepted gates {sorted(unaccepted)}", task_id=task_id)
            self._apply(task_id, from_state, to_state, actor, reason, **extra)
        self._notify_committed()
        return self.get(task_id)

    def _notify_committed(self) -> None:
        """Post-commit observer: refresh `live.json` (best effort). Skipped inside an enclosing transaction() (not yet
        committed) and never raises; it runs after the write lock is released."""
        if self._tx_depth:
            self._live_dirty = True  # refreshed by the outermost transaction() once it has committed
            return
        try:
            from . import telemetry  # lazy: keeps taskstore import cost unchanged
            telemetry.refresh_live(db_path=self.path, conn=self.conn)  # the commit is visible to the connection that made it
        except Exception:  # noqa: BLE001 - observer must never fail a committed transition
            import logging
            logging.getLogger("swarm.telemetry").warning("live.json refresh failed", exc_info=True)

    def _apply(self, task_id: str, from_state: TaskState, to_state: TaskState, actor: str, reason: str,
               **extra: object) -> None:
        """State write conditional on from_state plus its audit row; the caller's transaction() commits both."""
        cols = ["state=?", *(f"{k}=?" for k in extra), "updated_at=?"]
        args: list[Any] = [to_state.value, *extra.values(), time.time(), task_id, from_state.value]
        cur = self.conn.execute(f"UPDATE tasks SET {', '.join(cols)} WHERE task_id=? AND state=?", args)
        if cur.rowcount != 1:
            raise SwarmError(ErrorCode.E_CONTRACT, f"concurrent transition on {task_id}: expected {from_state.value}",
                             task_id=task_id)
        self._log(task_id, from_state, to_state, actor, reason)

    def _log(self, task_id: str, from_state: TaskState | None, to_state: TaskState, actor: str, reason: str) -> None:
        self.conn.execute("INSERT INTO transitions (task_id, from_state, to_state, actor, reason, ts)"
                          " VALUES (?,?,?,?,?,?)",
                          (task_id, from_state.value if from_state else None, to_state.value,
                           actor, reason, time.time()))

    def history(self, task_id: str) -> builtins.list[TaskRow]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM transitions WHERE task_id=? ORDER BY id", (task_id,))]

    # ---- gates ------------------------------------------------------------
    def record_verdict(self, task_id: str, envelope: dict[str, Any]) -> None:
        """Store a signed gate.verdict envelope as a row on task_id (the envelope's own task_id)."""
        p = validate_verdict(envelope)
        if p["task_id"] != task_id:
            raise SwarmError(ErrorCode.E_CONTRACT, f"verdict names {p['task_id']!r}, recorded on {task_id!r}", task_id=task_id)
        self.get(task_id)
        now = time.time()
        self.conn.execute("INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts,"
                          " envelope_json, sig) VALUES (?,?,?,?,?,?,?,?,?)",
                          (task_id, p["gate"], p["verdict"], envelope["source"], json.dumps(p["findings"]),
                           now + p["expires_s"], now, json.dumps(envelope), envelope["sig"]))
        self._commit()

    def latest_verdicts(self, task_id: str, *, include_stale: bool = False,
                        verified_only: bool = False) -> dict[str, TaskRow]:
        """Latest verdict per gate. Verdicts issued before the task's last rework loop are stale
        (the producer changed the artifact) and are ignored unless include_stale=True.
        verified_only=True drops rows whose signed envelope does not verify or disagrees with the row or task."""
        task = self.get(task_id)
        out = self._latest_rows(task, include_stale=include_stale)
        if verified_only:
            now = time.time()
            out = {g: d for g, d in out.items() if self._check_row(task, g, d, now) in _VERIFIED}
        return out

    def _latest_rows(self, task: TaskRow, *, include_stale: bool = False) -> dict[str, TaskRow]:
        """Highest-id row per gate of `task`, inserted at or after its last rework unless include_stale."""
        since = 0.0 if include_stale else _verdicts_since(task)
        out: dict[str, TaskRow] = {}
        for r in self.conn.execute("SELECT * FROM verdicts WHERE task_id=? AND ts>=? ORDER BY id",
                                   (task["task_id"], since)):
            d = dict(r)
            d["findings"] = json.loads(d["findings"])
            out[d["gate"]] = d
        return out

    def required_gates(self, task_id: str) -> builtins.list[str]:
        """Gates from risk class, unless the plan overrides them via notes.gates (e.g. [] for
        non-code tasks such as requirements or docs, or gate tasks themselves)."""
        return _gates_of(self.get(task_id))

    def _check_row(self, task: TaskRow, gate: str, row: TaskRow | None, now: float) -> str:
        """Verify one verdict row of `task` against its signed envelope. Returns ok | fail | expired
        (signature verified and current) or absent | unsigned | bad-sig | mismatch | stale | dry-run (not
        trustworthy for this task). mismatch: the signed gate/task_id/verdict disagree with the row, or the
        envelope's correlation_id is not the task's. stale: the signed issued_at precedes the task's last rework
        (notes.verdicts_since), even when the row itself was re-inserted after it. dry-run: a signed dry_run
        verdict on a task that A01 did not dispatch in a runner dry-run (notes.dry_run)."""
        if row is None:
            return "absent"
        if not row.get("envelope_json"):
            return "unsigned"
        try:
            env = json.loads(row["envelope_json"])
            p = validate_verdict(env)
        except Exception:  # any corrupt/forged envelope is untrusted, never an abort of missing_gates/reconcile
            return "bad-sig"
        if p["gate"] != gate or p["task_id"] != task["task_id"] or p["verdict"] != row["verdict"]:
            return "mismatch"
        if env["correlation_id"] != task["correlation_id"]:
            return "mismatch"
        try:
            if float(p["issued_at"]) < _verdicts_since(task):
                return "stale"
        except (KeyError, TypeError, ValueError):
            return "mismatch"
        if p.get("dry_run") and not task["notes_json"].get("dry_run"):
            return "dry-run"
        try:
            if not float(p["issued_at"]) + float(p["expires_s"]) > now:
                return "expired"
        except (KeyError, TypeError, ValueError):
            return "mismatch"
        if p["verdict"] not in ("pass", "waive"):
            return "fail"
        return "ok"

    def missing_gate_reasons(self, task_id: str) -> dict[str, str]:
        """Required gates lacking a verified, current, unexpired pass/waive verdict → reason, in required order."""
        task = self.get(task_id)
        latest = self._latest_rows(task)
        now = time.time()
        out = {}
        for g in _gates_of(task):
            reason = self._check_row(task, g, latest.get(g), now)
            if reason != "ok":
                out[g] = reason
        return out

    def missing_gates(self, task_id: str) -> builtins.list[str]:
        return list(self.missing_gate_reasons(task_id))

    def unaccepted_gates(self, task_id: str, reasons: dict[str, str] | None = None) -> dict[str, str]:
        """T-05-11/T-05-31: required gates of `task_id` whose current row passes (a gate in `reasons`, default
        missing_gate_reasons, is already reported as missing and skipped) but was recorded by a gate task (the signed
        `gate_task`) whose result has not been accepted: leased, refused (review verdict mismatch), FAILED, BLOCKED, RETRY,
        ESCALATED or CANCELLED. → {gate: "unaccepted"}. Such a row may be contradicted by its own gate agent, so it never
        approves the target. A row with no `gate_task` (written outside a gate task) counts as accepted; an unreadable
        envelope or a gate task that does not exist fails closed."""
        if reasons is None:
            reasons = self.missing_gate_reasons(task_id)
        latest = self.latest_verdicts(task_id)
        out = {}
        for g in self.required_gates(task_id):
            if g in reasons or g not in latest:
                continue
            try:
                issuer = validate_verdict(json.loads(latest[g]["envelope_json"])).get("gate_task")
                accepted = issuer is None or self.get(issuer)["state"] in ACCEPTED_ISSUER_STATES
            except Exception:  # noqa: BLE001 - any unreadable envelope or unknown issuer is untrusted
                accepted = False
            if not accepted:
                out[g] = "unaccepted"
        return out

    def gate_verdict_since(self, target: str, *, gate: str, gate_task_id: str, since: float) -> bool:
        """True when a `gate` row on `target` holds a verifying signed envelope for that gate and target that
        gate task `gate_task_id`'s script issued at or after `since` (its lease start): proof the script ran for
        this lease. Pass/fail does not matter; malformed or forged rows are skipped."""
        return self.gate_verdict_value_since(target, gate=gate, gate_task_id=gate_task_id, since=since) is not None

    def gate_verdict_value_since(self, target: str, *, gate: str, gate_task_id: str, since: float) -> str | None:
        """The signed verdict of the latest row gate_verdict_since accepts (same filtering), else None."""
        for r in self.conn.execute("SELECT envelope_json FROM verdicts WHERE task_id=? AND gate=? ORDER BY id DESC",
                                   (target, gate)):
            try:
                p = validate_verdict(json.loads(r["envelope_json"]))
                if (p["gate"] == gate and p["task_id"] == target and p.get("gate_task") == gate_task_id
                        and float(p["issued_at"]) >= since):
                    return cast(str, p["verdict"])  # validate_verdict only checks that the key exists; a signed verdict is a string
            except Exception:
                continue
        return None

    # ---- artifacts --------------------------------------------------------
    def add_artifact(self, task_id: str, *, kind: str, uri: str, version: str = "1",
                     digest: str = "", producer: str = "") -> None:
        self.conn.execute("INSERT INTO artifacts (task_id, kind, uri, version, digest, producer, ts)"
                          " VALUES (?,?,?,?,?,?,?)", (task_id, kind, uri, version, digest, producer, time.time()))
        task = self.get(task_id)
        outputs = task["outputs"] + [{"kind": kind, "uri": uri, "version": version, "digest": digest}]
        self.update(task_id, outputs=outputs)

    # ---- DAG helpers ------------------------------------------------------
    def deps_satisfied(self, task: TaskRow, tasks: builtins.list[TaskRow] | None = None) -> bool:
        """A dependency is satisfied once its work is complete (IN_REVIEW/APPROVED/DONE).
        Gates are a separate ledger: gate tasks consume IN_REVIEW work, and a failing gate
        pulls the producer back to CHANGES_REQUESTED, which un-satisfies its dependants."""
        tasks = tasks or self.list(correlation_id=task["correlation_id"])
        ok = {t["task_id"] for t in tasks if t["state"] in SATISFIED_STATES}
        return all(d in ok for d in task["depends_on"])

    def ready(self, correlation_id: str | None = None) -> builtins.list[TaskRow]:
        """Schedulable tasks: PLANNED/RETRY (or CREATED/VALIDATED) with all dependencies satisfied."""
        tasks = self.list(correlation_id=correlation_id)
        return [t for t in tasks
                if t["state"] in (S.PLANNED.value, S.RETRY.value, S.CREATED.value, S.VALIDATED.value)
                and self.deps_satisfied(t, tasks)]

    def _row(self, row: sqlite3.Row) -> TaskRow:
        d = dict(row)
        for c in _JSON_COLS:
            d[c] = json.loads(d[c] or ("{}" if c == "budget" else "[]"))
        try:
            d["notes_json"] = json.loads(d["notes"]) if d.get("notes") else {}
            if not isinstance(d["notes_json"], dict):
                d["notes_json"] = {}
        except json.JSONDecodeError:
            d["notes_json"] = {}
        return d

    def set_notes(self, task_id: str, **kv: object) -> TaskRow:
        """Merge kv into notes; re-read and write under one write lock so concurrent writers keep each other's keys."""
        with self.transaction():
            notes = self.get(task_id)["notes_json"]
            notes.update(kv)
            self.update(task_id, notes=json.dumps(notes))
        return self.get(task_id)

    def append_feedback(self, task_id: str, entry: dict[str, Any]) -> TaskRow:
        """Append entry to notes.feedback in one transaction; parallel gate scripts and runner threads lose none."""
        with self.transaction():
            notes = self.get(task_id)["notes_json"]
            notes.setdefault("feedback", []).append(entry)
            self.update(task_id, notes=json.dumps(notes))
        return self.get(task_id)
