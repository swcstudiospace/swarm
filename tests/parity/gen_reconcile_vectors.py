#!/usr/bin/env python3
"""Vectors for the pure Rust reconcile planner (`crates/gsd-route/src/reconcile_plan.rs`), generated from the REAL `swarm.results.reconcile`.

Every case seeds an in-memory `TaskStore` through the real API: tasks are created and driven through the state machine (`create`,
`transition`), a rework runs the real `CHANGES_REQUESTED` transition (so `notes.verdicts_since` and `rework_loops` are the production ones),
and verdicts are signed with `swarm.gates.make_verdict` and stored with `TaskStore.record_verdict`. Rows the real flow refuses to store (a
forged gate name, a legacy row) are inserted with the same `INSERT`. The case then records the initial facts the Rust planner is given, runs
`reconcile(store, corr, emit, approve=...)` with an injected emitter, and records what Python did:

* `tasks_before` / `tasks_after`: every `tasks` row of the store (`SELECT *`, rowid order, JSON columns still text), so a correlation that
  must not be touched is checked too;
* `verdicts`: every verdict row with `sig_ok`, the host's answer to "does the stored envelope verify" (`verify_envelope`), which is all the
  Rust function knows about signatures;
* `transitions_before_max` and `transitions_new`: the `transitions` rows written by the call;
* `events`: every `emit(event_type, payload, **kw)` call in order, and `notes_log`, the returned list;
* `key_policy`: the three facts `TaskStore.transition` reads for an APPROVED target;
* `python_error`: instead of the after-state, the exception class (and, for a `SwarmError`, its text) of a call that raised. Python raised in
  the middle of a loop with earlier writes committed, so Rust must answer `not_started` and let Python do it again;
* `rust: "not_started"`: Python succeeded but the Rust planner must still refuse (an input whose Python meaning it does not reproduce).

Determinism: the clocks of `swarm.taskstore`, `swarm.gates` and `swarm.envelope` are one fake clock; during `reconcile` it stands still at
`NOW`, so every `updated_at`, `created_at`, audit `ts` and `verdicts_since` (`NOW + 0.001`) is exact and the Rust replay compares the whole
store without masks. Envelope ids come from a counter, the `live.json` observer is off. Signing uses a throwaway HMAC key derived at run
time from the seed phrases below; it lives in the process environment while the cases are built, and the file holds envelopes and
signatures, never a key.

`python3 tests/parity/gen_reconcile_vectors.py` rewrites the committed file; `--check` compares without writing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("SWARM_PURE_PYTHON", "1")

from swarm import envelope, gates, results, taskstore  # noqa: E402
from swarm.errors import SwarmError  # noqa: E402
from swarm.taskstore import TaskStore  # noqa: E402

VECTOR_PATH = REPO / "crates" / "gsd-route" / "tests" / "vectors" / "reconcile.json"
SCHEMA = "gsd-route.reconcile-vectors.v1"
KEY_SEED = "agent-swarm reconcile vectors: throwaway HMAC key, seed phrase v1"
KEY_ENV = ("SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_AGENT_SESSION")

CORR = "corr-a"
OTHER_CORR = "corr-b"
CREATED_AT = 500.0
REWORK_AT = 1000.0
BEFORE_REWORK = 900.25
ISSUED = 2000.25
NOW = 3000.0
SHORT_EXPIRY = 10

Row = dict[str, Any]

STATES = ["CREATED", "VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED", "DONE", "BLOCKED",
          "FAILED", "RETRY", "ESCALATED", "CANCELLED"]
_TO_CLAIMED = ["VALIDATED", "PLANNED", "CLAIMED"]
_TO_PROGRESS = [*_TO_CLAIMED, "IN_PROGRESS"]
_TO_REVIEW = [*_TO_PROGRESS, "IN_REVIEW"]
PATHS: dict[str, list[str]] = {
    "CREATED": [],
    "VALIDATED": ["VALIDATED"],
    "PLANNED": ["VALIDATED", "PLANNED"],
    "CLAIMED": _TO_CLAIMED,
    "IN_PROGRESS": _TO_PROGRESS,
    "IN_REVIEW": _TO_REVIEW,
    "CHANGES_REQUESTED": [*_TO_REVIEW, "CHANGES_REQUESTED"],
    "APPROVED": [*_TO_REVIEW, "APPROVED"],
    "DONE": [*_TO_REVIEW, "APPROVED", "DONE"],
    "BLOCKED": [*_TO_CLAIMED, "BLOCKED"],
    "FAILED": [*_TO_PROGRESS, "FAILED"],
    "RETRY": [*_TO_CLAIMED, "RETRY"],
    "ESCALATED": [*_TO_CLAIMED, "BLOCKED", "ESCALATED"],
    "CANCELLED": ["CANCELLED"],
}
GATES = ("review", "quality", "security", "release")
RERUN_STATES = ("DONE", "IN_REVIEW", "APPROVED", "CLAIMED", "IN_PROGRESS")


class _Clock:
    """Stand-in for the `time` module of the swarm modules: `time()` returns the value last set, the rest is the real module."""

    def __init__(self) -> None:
        self.now = CREATED_AT

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class _Ids:
    """Deterministic stand-in for `uuid.uuid4` (message ids and trace ids of the envelopes)."""

    def __init__(self) -> None:
        self.count = 0

    def uuid4(self) -> uuid.UUID:
        self.count += 1
        digest = hashlib.sha256(f"reconcile-uuid-{self.count}".encode("ascii")).digest()
        return uuid.UUID(int=int.from_bytes(digest[:16], "big"))


def derive_key(phrase: str) -> str:
    """The throwaway HMAC secret: SHA-256 of an ASCII seed phrase, as hex."""
    return hashlib.sha256(phrase.encode("ascii")).hexdigest()


@contextmanager
def seeded(clock: _Clock) -> Iterator[None]:
    """The fake clock, fake ids, the seed-derived key and a clean signing environment, restored on exit."""
    ids = _Ids()
    with mock.patch.dict(os.environ, {"SWARM_SIGNING_KEY": derive_key(KEY_SEED)}):
        for name in KEY_ENV:
            os.environ.pop(name, None)
        with (
            mock.patch.object(taskstore, "time", clock),
            mock.patch.object(gates, "time", clock),
            mock.patch.object(envelope, "time", clock),
            mock.patch.object(envelope, "_now_iso", lambda: "2026-01-01T00:00:00.000Z"),
            mock.patch.object(uuid, "uuid4", ids.uuid4),
            mock.patch.object(TaskStore, "_notify_committed", lambda self: None),
        ):
            yield


def verifies(text: str | None) -> bool:
    """What the host's signature check says about a stored envelope text (False for anything that is not a signed object)."""
    if not text:
        return False
    try:
        env = json.loads(text)
    except ValueError:
        return False
    return isinstance(env, dict) and envelope.verify_envelope(env)


def fail_findings() -> list[dict[str, Any]]:
    """One blocking finding, so the signed verdict is `fail`."""
    return [gates.make_finding("F-1", "major", "functional", "broken")]


def key_policy() -> Row:
    """The three facts `TaskStore.transition` reads before an APPROVED, from the current environment."""
    return {"require_key": os.environ.get("SWARM_REQUIRE_KEY") == "1", "real_key": envelope.real_key_configured(),
            "dev_key": envelope.insecure_dev_key() is not None}


class World:
    """One seeded in-memory store, the fake clock that drives it, and the recorded run of `reconcile`."""

    def __init__(self, name: str, group: str, clock: _Clock) -> None:
        self.name, self.group, self.clock = name, group, clock
        self.store = TaskStore(":memory:")
        self.seq = 0
        self.rust = "plan"
        clock.now = CREATED_AT

    # ---- seeding ----------------------------------------------------------------------------------------------------------
    def task(self, task_id: str, state: str = "IN_REVIEW", *, corr: str = CORR, depth: int = 1, deps: list[str] | None = None,
             notes: dict[str, Any] | None = None, risk: str = "low", capability: str = "code.backend", title: str | None = None,
             rework: int = 0, acceptance: list[Any] | None = None, budget: dict[str, Any] | None = None, agent: str | None = "A05@seed",
             priority: str = "P2") -> None:
        """Creates a task and drives it to `state` through the real state machine; `rework` loops cycle an IN_REVIEW task back to review."""
        self.seq += 1
        self.clock.now = CREATED_AT + self.seq
        if state in ("APPROVED", "DONE") and "gates" not in (notes or {}):  # APPROVED fails closed without passing gates
            notes = {**(notes or {}), "gates": []}
        self.store.create(task_id=task_id, correlation_id=corr, capability=capability, title=task_id if title is None else title,
                          dag_depth=depth, depends_on=deps or [], notes=notes, risk_class=risk, acceptance=acceptance, budget=budget,
                          agent_id=agent, priority=priority)
        self.clock.now = CREATED_AT + self.seq + 0.5
        for step in PATHS[state]:
            self.store.transition(task_id, step, actor="A01", reason="seed")
        for _ in range(min(rework, taskstore.MAX_REWORK_LOOPS)):
            self.clock.now = REWORK_AT
            for step in ("CHANGES_REQUESTED", "IN_PROGRESS", "IN_REVIEW"):
                self.store.transition(task_id, step, actor="A01", reason="seed rework")
        if rework > taskstore.MAX_REWORK_LOOPS:  # a third real loop would escalate: set the counter the way a long-lived store holds it
            self.set_column(task_id, "rework_loops", rework)

    def gate_task(self, task_id: str, gate: str, targets: list[str], state: str = "DONE", *, deps: list[str] | None = None,
                  depth: int = 2, corr: str = CORR, notes: dict[str, Any] | None = None, title: str | None = None, **kw: Any) -> None:
        """A gate task covering `targets`; `notes.gates = []` lets it reach APPROVED without gates of its own."""
        merged = {"gate": gate, "gate_for": targets, "gates": [], **(notes or {})}
        self.task(task_id, state, corr=corr, depth=depth, deps=deps if deps is not None else list(targets), notes=merged,
                  capability=f"gate.{gate}", title=f"{gate} gate" if title is None else title, **kw)

    def sign(self, target: str, gate: str, *, at: float = ISSUED, corr: str = CORR, verdict: str | None = None,
             findings: list[dict[str, Any]] | None = None, expires_s: int = 86400, issuer: str | None = None,
             extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """A signed envelope from the real `make_verdict`, issued at `at`; `issuer` is the signed `gate_task`."""
        self.clock.now = at
        merged = {**({"gate_task": issuer} if issuer is not None else {}), **(extra or {})}
        return gates.make_verdict(gate=gate, task_id=target, agent_id="A08@seed", findings=findings, verdict=verdict, runs={},
                                  expires_s=expires_s, correlation_id=corr, extra=merged or None)

    def verdict(self, target: str, gate: str, kind: str = "pass", *, at: float = ISSUED, stored_at: float | None = None,
                corr: str = CORR, issuer: str | None = None, expires_s: int = 86400) -> None:
        """Signs and stores (`record_verdict`) a `pass`, `fail` or `waive` verdict; `stored_at` is the row's `ts` when it differs from `at`."""
        findings = fail_findings() if kind == "fail" else None
        extra = {"waived_by": "human"} if kind == "waive" else None
        env = self.sign(target, gate, at=at, corr=corr, verdict="waive" if kind == "waive" else None, findings=findings,
                        expires_s=expires_s, issuer=issuer, extra=extra)
        self.clock.now = at if stored_at is None else stored_at
        self.store.record_verdict(target, env)

    def raw(self, target: str, gate: str, verdict: str | None, text: str | None, ts: float = ISSUED) -> None:
        """The `INSERT` of `record_verdict`, for rows it would refuse to store."""
        sig = None
        if text:
            try:
                parsed = json.loads(text)
                sig = parsed.get("sig") if isinstance(parsed, dict) and isinstance(parsed.get("sig"), str) else None
            except ValueError:
                sig = None
        self.store.conn.execute(
            "INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts, envelope_json, sig) VALUES (?,?,?,?,?,?,?,?,?)",
            (target, gate, verdict, "A08@seed", "[]", ts + 86400, ts, text, sig))
        self.store.conn.commit()

    def set_column(self, task_id: str, column: str, value: object) -> None:
        """A direct `UPDATE`, for rows the real API never produces."""
        self.store.conn.execute(f"UPDATE tasks SET {column}=? WHERE task_id=?", (value, task_id))
        self.store.conn.commit()

    def passing(self, target: str, gate_list: tuple[str, ...] = ("review",), *, issuer: str | None = None) -> None:
        """A `pass` verdict for each gate in `gate_list`."""
        for gate in gate_list:
            self.verdict(target, gate, "pass", issuer=issuer)

    # ---- recording --------------------------------------------------------------------------------------------------------
    def rows(self) -> list[Row]:
        return [dict(r) for r in self.store.conn.execute("SELECT * FROM tasks ORDER BY rowid")]

    def finish(self, *, approve: bool = True, corr: str = CORR, keys: bool = True) -> Row:
        """Runs `reconcile` at `NOW` and returns the recorded case; `keys=False` removes the signing key for the call (verdict rows
        are checked first, with the key, as the host does)."""
        store = self.store
        verdicts = [{"id": r["id"], "task_id": r["task_id"], "gate": r["gate"], "verdict": r["verdict"], "envelope_json": r["envelope_json"],
                     "ts": r["ts"], "sig_ok": verifies(r["envelope_json"])} for r in store.conn.execute("SELECT * FROM verdicts ORDER BY id")]
        before = self.rows()
        max_id = store.conn.execute("SELECT COALESCE(MAX(id), 0) FROM transitions").fetchone()[0]
        events: list[Row] = []

        def emit(event_type: str, payload: dict[str, Any], **kw: Any) -> None:
            assert set(kw) <= {"task_id"}, kw
            events.append({"type": event_type, "payload": payload, "task_id": kw.get("task_id")})

        self.clock.now = NOW
        env_patch = {"SWARM_SIGNING_KEY": ""} if not keys else {}
        expect: Row
        with mock.patch.dict(os.environ, env_patch):
            if not keys:  # the conventional key file lives under HOME or XDG_CONFIG_HOME: without both there is no key anywhere
                os.environ.pop("HOME", None)
                os.environ.pop("XDG_CONFIG_HOME", None)
            policy = key_policy()
            try:
                log = results.reconcile(store, corr, emit, approve=approve)
                expect = {"notes_log": log, "events": events, "tasks": self.rows(),
                          "transitions": [dict(r) for r in store.conn.execute("SELECT * FROM transitions WHERE id > ? ORDER BY id", (max_id,))]}
            except SwarmError as exc:
                expect = {"python_error": f"{type(exc).__name__}: {exc}"}
            except Exception as exc:  # noqa: BLE001 - the oracle's crash is the recorded answer
                expect = {"python_error": type(exc).__name__}
        case = {"name": self.name, "group": self.group, "correlation_id": corr, "approve": approve, "now": NOW, "key_policy": policy,
                "rust": self.rust, "tasks_before": before, "verdicts": verdicts, "transitions_before_max": max_id, "expect": expect}
        store.conn.close()
        return case


Scenario = Callable[[_Clock], Iterator[Row]]


def _world(clock: _Clock, name: str, group: str) -> World:
    return World(name, group, clock)


# ---- rework -------------------------------------------------------------------------------------------------------------------
def _rework_basics(clock: _Clock) -> Iterator[Row]:
    w = _world(clock, "rework-fail-with-done-gate-task", "rework")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"])
    w.task("T-next", "PLANNED", depth=3, deps=["T-1"])
    w.task("T-after-gate", "PLANNED", depth=3, deps=["G-review"])
    w.verdict("T-1", "review", "fail", issuer="G-review")
    yield w.finish()

    w = _world(clock, "rework-fail-no-gate-task", "rework")
    w.task("T-1")
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    for state in STATES:
        w = _world(clock, f"rework-latest-member-{state}", "rework-state")
        w.task("T-1")
        w.gate_task("G-review", "review", ["T-1"], state)
        w.task("T-dep", "PLANNED", depth=3, deps=["G-review"])
        w.verdict("T-1", "review", "fail")
        yield w.finish()

    w = _world(clock, "rework-two-failing-gates", "rework")
    w.task("T-1", risk="medium")
    w.gate_task("G-review", "review", ["T-1"])
    w.gate_task("G-quality", "quality", ["T-1"], "IN_REVIEW")
    w.task("T-dep", "PLANNED", depth=3, deps=["G-review", "G-quality"])
    w.verdict("T-1", "review", "fail")
    w.verdict("T-1", "quality", "fail")
    yield w.finish()

    w = _world(clock, "rework-one-of-two-fails", "rework")
    w.task("T-1", risk="medium")
    w.gate_task("G-review", "review", ["T-1"])
    w.gate_task("G-quality", "quality", ["T-1"])
    w.verdict("T-1", "review", "pass")
    w.verdict("T-1", "quality", "fail")
    yield w.finish()

    w = _world(clock, "rework-fail-then-waive-latest-waives-approves", "rework")
    w.task("T-1")
    w.verdict("T-1", "review", "fail", at=ISSUED)
    w.verdict("T-1", "review", "waive", at=ISSUED + 1)
    yield w.finish()

    w = _world(clock, "rework-waive-then-fail-latest-fails", "rework")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"])
    w.verdict("T-1", "review", "waive", at=ISSUED)
    w.verdict("T-1", "review", "fail", at=ISSUED + 1)
    yield w.finish()

    w = _world(clock, "rework-advisory-still-reworks", "rework")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"])
    w.verdict("T-1", "review", "fail")
    yield w.finish(approve=False)

    w = _world(clock, "rework-duplicate-gate-name", "rework")
    w.task("T-1", notes={"gates": ["review", "review"]})
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "rework-gate-order-follows-required", "rework")
    w.task("T-1", risk="high", notes={"gates": ["security", "review", "quality"]})
    for gate in ("review", "quality", "security"):
        w.verdict("T-1", gate, "fail")
    yield w.finish()

    w = _world(clock, "rework-verdict-failing-but-unverified-ignored", "rework")
    w.task("T-1")
    env = w.sign("T-1", "review", findings=fail_findings())
    env["payload"]["verdict"] = "pass"
    w.raw("T-1", "review", "fail", json.dumps(env))
    yield w.finish()


def _rework_loops(clock: _Clock) -> Iterator[Row]:
    for loops in range(4):
        w = _world(clock, f"cap-rework-loops-{loops}", "cap")
        w.task("T-1", rework=loops)
        w.gate_task("G-review", "review", ["T-1"])
        w.task("T-dep", "PLANNED", depth=3, deps=["G-review"])
        w.verdict("T-1", "review", "fail")
        yield w.finish()

    stash: dict[str, dict[str, Any]] = {
        "result-and-meta": {"result": {"status": "failed", "n": 1}, "meta": {"ingest": "/x/y.md"}},
        "result-only": {"result": {"status": "failed"}},
        "meta-only": {"meta": {"ingest": "/x/y.md"}},
        "empty-result": {"result": {}, "meta": None},
        "history-list": {"result": {"a": 1}, "rework_evidence": [{"result": {"old": True}}]},
        "history-not-a-list": {"result": {"a": 1}, "rework_evidence": "text"},
        "key-order": {"zeta": 1, "result": {"a": 1}, "alpha": 2, "verdicts_since": 5},
    }
    for label, notes in stash.items():
        for loops in (0, 2):
            w = _world(clock, f"cap-stash-{label}-loops-{loops}", "cap-stash")
            w.task("T-1", rework=loops, notes=notes)
            w.verdict("T-1", "review", "fail")
            yield w.finish()

    w = _world(clock, "cap-escalated-with-gate-tasks-and-dependants", "cap")
    w.task("T-1", risk="medium", rework=2)
    w.gate_task("G-review", "review", ["T-1"])
    w.gate_task("G-quality", "quality", ["T-1"], "IN_REVIEW")
    w.task("T-dep", "PLANNED", depth=3, deps=["G-review"])
    w.verdict("T-1", "review", "fail")
    w.verdict("T-1", "quality", "fail")
    yield w.finish()

    w = _world(clock, "cap-two-targets-one-escalates", "cap")
    w.task("T-1", rework=2)
    w.task("T-2", depth=2, rework=0)
    w.gate_task("G-review", "review", ["T-1", "T-2"], depth=3)
    w.verdict("T-1", "review", "fail")
    w.verdict("T-2", "review", "fail")
    yield w.finish()


def _lineages(clock: _Clock) -> Iterator[Row]:
    for label, members in {
        "two-members": [("G-review", "DONE"), ("G-review.r1", "DONE")],
        "three-members": [("G-review", "DONE"), ("G-review.r1", "DONE"), ("G-review.r2", "IN_REVIEW")],
        "created-out-of-order": [("G-review.r2", "DONE"), ("G-review", "DONE"), ("G-review.r1", "DONE")],
        "superseded-failed-latest": [("G-review", "DONE"), ("G-review.r1", "FAILED")],
        "superseded-cancelled-latest": [("G-review", "DONE"), ("G-review.r1", "CANCELLED")],
        "planned-latest": [("G-review", "DONE"), ("G-review.r1", "PLANNED")],
        "claimed-latest": [("G-review", "DONE"), ("G-review.r1", "CLAIMED")],
        "tie-same-index": [("G-review", "DONE"), ("G-review.r1", "DONE"), ("G-review.r1.r1", "IN_REVIEW")],
        "index-ten": [("G-review", "DONE"), ("G-review.r9", "DONE"), ("G-review.r10", "DONE")],
        "nested-suffix": [("G-review.r1.r2", "DONE")],
        "lookalike-id": [("G-review.rx", "DONE"), ("G-review.r1x", "DONE")],
    }.items():
        w = _world(clock, f"lineage-{label}", "lineage")
        w.task("T-1")
        for index, (member, state) in enumerate(members):
            w.gate_task(member, "review", ["T-1"], state, depth=2 + index % 2)
        w.task("T-dep", "PLANNED", depth=5, deps=[members[0][0]])
        w.verdict("T-1", "review", "fail")
        yield w.finish()

    w = _world(clock, "lineage-two-gates-independent-numbering", "lineage")
    w.task("T-1", risk="medium")
    for member, gate, state in (("G-review", "review", "DONE"), ("G-review.r1", "review", "DONE"), ("G-review.r2", "review", "DONE"),
                                ("G-quality", "quality", "IN_REVIEW")):
        w.gate_task(member, gate, ["T-1"], state)
    w.task("T-dep", "PLANNED", depth=5, deps=["G-review.r1", "G-quality"])
    w.verdict("T-1", "review", "fail")
    w.verdict("T-1", "quality", "pass")
    yield w.finish()

    w = _world(clock, "lineage-multi-target-both-fail", "lineage")
    w.task("T-1", depth=1)
    w.task("T-2", depth=2)
    w.gate_task("G-review", "review", ["T-1", "T-2"], depth=3)
    w.verdict("T-1", "review", "fail")
    w.verdict("T-2", "review", "fail")
    yield w.finish()

    w = _world(clock, "lineage-multi-target-one-fails-other-passes", "lineage")
    w.task("T-1", depth=1)
    w.task("T-2", depth=2)
    w.gate_task("G-review", "review", ["T-1", "T-2"], depth=3)
    w.verdict("T-1", "review", "fail")
    w.verdict("T-2", "review", "pass", issuer="G-review")
    yield w.finish()

    w = _world(clock, "lineage-rerun-covers-later-target-stall", "lineage")
    w.task("T-1", depth=1)
    w.task("T-2", depth=2)
    w.gate_task("G-review", "review", ["T-1", "T-2"], depth=3)
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "lineage-gate-task-not-covering-later-target-stalls", "lineage")
    w.task("T-1", depth=1)
    w.task("T-2", depth=2)
    w.gate_task("G-review", "review", ["T-1"], depth=3)
    w.verdict("T-1", "review", "fail")
    yield w.finish()


def _scans_and_release(clock: _Clock) -> Iterator[Row]:
    for state in ("DONE", "IN_REVIEW", "APPROVED", "CLAIMED", "IN_PROGRESS", "PLANNED", "FAILED"):
        w = _world(clock, f"scan-{state}-joins-lineage", "scan")
        w.task("T-1")
        w.task("T-scan", state, depth=2, deps=["T-1"], notes={"gates": []}, capability="scan.net")
        w.task("T-consumer", "PLANNED", depth=3, deps=["T-scan"])
        w.verdict("T-1", "review", "fail")
        yield w.finish()

    w = _world(clock, "scan-with-required-gates-is-a-consumer-not-a-member", "scan")
    w.task("T-1")
    w.task("T-consumer", "CLAIMED", depth=2, deps=["T-1"])
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "scan-with-gate-note-is-not-a-scan", "scan")
    w.task("T-1")
    w.task("T-other-gate", "DONE", depth=2, deps=["T-1"], notes={"gate": "quality", "gates": []})
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "release-rerun-waits-for-other-reruns", "release")
    w.task("T-1", risk="high")
    w.task("T-scan", "DONE", depth=2, deps=["T-1"], notes={"gates": []}, capability="scan.net")
    for gate in GATES:
        deps = ["T-1", "T-scan"] if gate == "release" else ["T-1"]
        w.gate_task(f"G-{gate}", gate, ["T-1"], "DONE", deps=deps)
    w.task("T-post", "DONE", depth=3, deps=["T-1", "G-release"], notes={"gates": []}, capability="scan.pol")
    w.task("T-ship", "PLANNED", depth=4, deps=["G-release"])
    w.task("T-docs", "PLANNED", depth=4, deps=["T-1"])
    w.verdict("T-1", "review", "fail")
    for gate in GATES[1:]:
        w.verdict("T-1", gate, "pass")
    yield w.finish()

    w = _world(clock, "release-rerun-without-other-gate-reruns", "release")
    w.task("T-1", risk="high")
    w.gate_task("G-release", "release", ["T-1"], "DONE")
    w.gate_task("G-review", "review", ["T-1"], "FAILED")
    w.verdict("T-1", "release", "fail")
    yield w.finish()

    w = _world(clock, "release-chain-upstream-scans", "release")
    w.task("T-1", risk="high")
    w.task("T-scan-a", "DONE", depth=2, deps=["T-1"], notes={"gates": []})
    w.task("T-scan-b", "IN_REVIEW", depth=3, deps=["T-scan-a"], notes={"gates": []})
    w.gate_task("G-release", "release", ["T-1"], "DONE", deps=["T-1", "T-scan-b"], depth=4)
    w.verdict("T-1", "release", "fail")
    yield w.finish()

    w = _world(clock, "release-gate-numbered-lineage", "release")
    w.task("T-1", risk="high", rework=1)
    w.gate_task("G-release", "release", ["T-1"], "DONE")
    w.gate_task("G-release.r1", "release", ["T-1"], "DONE", deps=["T-1", "G-release"])
    w.gate_task("G-quality", "quality", ["T-1"], "DONE")
    w.verdict("T-1", "release", "fail")
    yield w.finish()


def _dependants(clock: _Clock) -> Iterator[Row]:
    w = _world(clock, "dependants-done-is-skipped", "dependants")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"])
    w.task("T-done", "DONE", depth=3, deps=["G-review"])
    w.task("T-cancelled", "CANCELLED", depth=3, deps=["G-review"])
    w.task("T-live", "PLANNED", depth=3, deps=["G-review"])
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "dependants-already-lists-the-new-id", "dependants")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"])
    w.task("T-dep", "PLANNED", depth=3, deps=["G-review", "G-review.r1"])
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "dependants-of-several-lineages-keep-creation-order", "dependants")
    w.task("T-1", risk="medium")
    w.gate_task("G-review", "review", ["T-1"])
    w.gate_task("G-quality", "quality", ["T-1"])
    w.task("T-dep", "PLANNED", depth=3, deps=["G-quality", "G-review", "T-1"])
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "dependants-rerun-copies-dependencies-at-creation", "dependants")
    w.task("T-1")
    w.task("T-pre", "DONE", depth=1, deps=[])
    w.gate_task("G-review", "review", ["T-1"], deps=["T-1", "T-pre"])
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "rerun-clone-fields", "clone")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"], "IN_REVIEW", title="Review gate (rerun)", priority="P0", agent=None,
                acceptance=[{"id": "AC-1", "text": "é"}], budget={"max_tokens": 10, "max_wall_s": 5, "max_cost_usd": 0.5},
                notes={"result": {"x": 1}, "meta": {"y": 2}, "lease_id": "L-1", "running": True, "b": [1, 2.5, None]})
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "rerun-clone-drops-ingest-keys", "clone")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"], "IN_REVIEW",
                notes={"ingest_keys": [{"task_id": "G-review", "attempt": 1, "digest": "d" * 64, "reply": {"state": "IN_REVIEW"}},
                                       {"task_id": "G-review", "attempt": 2, "digest": "e" * 64, "reply": {"state": "IN_REVIEW"}}],
                       "keep_first": 1, "result": {"x": 1}, "feedback": [{"n": 1}], "meta": {"y": 2}, "keep_last": "z"})
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "rerun-clone-empty-acceptance-and-budget", "clone")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"], "DONE", title="", budget={})
    w.set_column("G-review", "budget", "")
    w.set_column("G-review", "acceptance", None)
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "rerun-clone-title-suffix-only-once", "clone")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"], title="x (rerun) (rerun)")
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "rerun-sorts-by-dag-depth-after-old-rows", "clone")
    w.task("T-1", depth=1)
    w.gate_task("G-review", "review", ["T-1"], depth=4)
    w.gate_task("G-quality", "quality", ["T-1"], depth=0)
    w.task("T-late", "PLANNED", depth=4, deps=["G-review"])
    w.verdict("T-1", "review", "fail")
    yield w.finish()


# ---- approval and stalls ------------------------------------------------------------------------------------------------------
def _approval(clock: _Clock) -> Iterator[Row]:
    for risk, gate_list in (("low", ("review",)), ("medium", ("review", "quality")), ("high", GATES)):
        for approve in (True, False):
            w = _world(clock, f"approve-{risk}-{'apply' if approve else 'advisory'}", "approve")
            w.task("T-1", risk=risk)
            w.passing("T-1", gate_list)
            yield w.finish(approve=approve)

    w = _world(clock, "approve-no-required-gates", "approve")
    w.task("T-1", notes={"gates": []})
    yield w.finish()

    w = _world(clock, "approve-no-required-gates-advisory", "approve")
    w.task("T-1", notes={"gates": []})
    yield w.finish(approve=False)

    w = _world(clock, "approve-waive", "approve")
    w.task("T-1")
    w.verdict("T-1", "review", "waive")
    yield w.finish()

    w = _world(clock, "approve-gate-task-in-review-approved-in-same-loop", "approve")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"], "IN_REVIEW")
    w.passing("T-1", issuer="G-review")
    yield w.finish()

    w = _world(clock, "approve-order-follows-depth-then-creation", "approve")
    w.task("T-b", depth=2)
    w.task("T-a", depth=1)
    w.task("T-c", depth=2)
    for target in ("T-a", "T-b", "T-c"):
        w.passing(target)
    yield w.finish()

    w = _world(clock, "approve-only-in-review-tasks-are-visited", "approve")
    for index, state in enumerate(s for s in STATES if s != "IN_REVIEW"):
        w.task(f"T-{state}", state, depth=index)
    w.task("T-1", depth=20)
    w.passing("T-1")
    yield w.finish()

    w = _world(clock, "approve-mixed-fail-pass-stall", "approve")
    w.task("T-pass", depth=1)
    w.task("T-fail", depth=2)
    w.task("T-stall", depth=3)
    w.gate_task("G-review", "review", ["T-fail"], depth=4)
    w.passing("T-pass")
    w.verdict("T-fail", "review", "fail")
    yield w.finish()

    w = _world(clock, "approve-only-in-review-no-tasks", "approve")
    w.task("T-1", "IN_PROGRESS")
    yield w.finish()


def _stalls(clock: _Clock) -> Iterator[Row]:
    for state in STATES:
        for covered in (True, False):
            w = _world(clock, f"issuer-{state}-{'covering' if covered else 'not-covering'}", "issuer")
            w.task("T-1")
            w.gate_task("G-review", "review", ["T-1"] if covered else ["T-other"], state)
            w.passing("T-1", issuer="G-review")
            yield w.finish()

    w = _world(clock, "issuer-in-other-correlation", "issuer")
    w.task("T-1")
    w.task("G-b", "DONE", corr=OTHER_CORR, notes={"gate": "review", "gates": []})
    w.passing("T-1", issuer="G-b")
    yield w.finish()

    w = _world(clock, "issuer-in-other-correlation-failed", "issuer")
    w.task("T-1")
    w.task("G-b", "FAILED", corr=OTHER_CORR, notes={"gate": "review", "gates": []})
    w.passing("T-1", issuer="G-b")
    yield w.finish()

    w = _world(clock, "issuer-unknown-task", "issuer")
    w.task("T-1")
    w.passing("T-1", issuer="G-nowhere")
    yield w.finish()

    w = _world(clock, "issuer-names-the-target-itself", "issuer")
    w.task("T-1")
    w.passing("T-1", issuer="T-1")
    yield w.finish()

    w = _world(clock, "leased-gate-task-holds-target", "leased")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"], "IN_PROGRESS")
    w.passing("T-1", issuer="G-review")
    yield w.finish()

    for state in ("CLAIMED", "IN_PROGRESS"):
        for approve in (True, False):
            w = _world(clock, f"leased-rerun-holds-a-target-whose-row-is-accepted-{state}-{'apply' if approve else 'advisory'}", "leased")
            w.task("T-1")
            w.gate_task("G-review", "review", ["T-1"], "DONE")
            w.gate_task("G-review.r1", "review", ["T-1"], state)
            w.passing("T-1", issuer="G-review")
            yield w.finish(approve=approve)

    w = _world(clock, "leased-gate-of-another-gate-name-does-not-hold", "leased")
    w.task("T-1")
    w.gate_task("G-security", "security", ["T-1"], "CLAIMED")
    w.passing("T-1")
    yield w.finish()

    w = _world(clock, "leased-gate-for-another-target-does-not-hold", "leased")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-other"], "CLAIMED")
    w.passing("T-1")
    yield w.finish()

    w = _world(clock, "leased-while-failing-still-reworks", "leased")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"], "IN_PROGRESS")
    w.verdict("T-1", "review", "fail")
    yield w.finish()

    w = _world(clock, "stall-order-gate-done-before-later-target", "stall-order")
    w.task("T-1", depth=1)
    w.task("T-2", depth=3)
    w.gate_task("G-review", "review", ["T-1", "T-2"], "IN_REVIEW", depth=2)
    w.passing("T-1", issuer="G-review")
    yield w.finish()

    w = _world(clock, "stall-order-gate-still-live-for-earlier-target", "stall-order")
    w.task("T-1", depth=3)
    w.task("T-2", depth=1)
    w.gate_task("G-review", "review", ["T-1", "T-2"], "IN_REVIEW", depth=2)
    w.passing("T-1", issuer="G-review")
    yield w.finish()

    w = _world(clock, "stall-live-gate-task-covers", "stall")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"], "PLANNED")
    yield w.finish()

    w = _world(clock, "stall-gate-name-must-match", "stall")
    w.task("T-1", risk="medium")
    w.gate_task("G-review", "review", ["T-1"], "PLANNED")
    w.verdict("T-1", "review", "pass")
    yield w.finish()

    for risk in ("low", "medium", "high"):
        w = _world(clock, f"stall-absent-{risk}", "stall")
        w.task("T-1", risk=risk)
        yield w.finish()
        w = _world(clock, f"stall-absent-{risk}-advisory", "stall")
        w.task("T-1", risk=risk)
        yield w.finish(approve=False)


def _stall_reasons(clock: _Clock) -> Iterator[Row]:
    w = _world(clock, "stall-unsigned", "stall-reason")
    w.task("T-1")
    w.raw("T-1", "review", "pass", None)
    yield w.finish()

    w = _world(clock, "stall-bad-sig", "stall-reason")
    w.task("T-1")
    env = w.sign("T-1", "review")
    env["payload"]["runs"] = {"tampered": True}
    w.raw("T-1", "review", "pass", json.dumps(env))
    yield w.finish()

    w = _world(clock, "stall-mismatch-envelope-gate", "stall-reason")
    w.task("T-1")
    w.raw("T-1", "review", "pass", json.dumps(w.sign("T-1", "quality")))
    yield w.finish()

    w = _world(clock, "stall-mismatch-correlation", "stall-reason")
    w.task("T-1")
    w.raw("T-1", "review", "pass", json.dumps(w.sign("T-1", "review", corr="corr-elsewhere")))
    yield w.finish()

    w = _world(clock, "stall-dry-run", "stall-reason")
    w.task("T-1")
    w.raw("T-1", "review", "pass", json.dumps(w.sign("T-1", "review", extra={"dry_run": True})))
    yield w.finish()

    w = _world(clock, "stall-stale-reinserted-after-rework", "stall-reason")
    w.task("T-1", rework=1)
    w.verdict("T-1", "review", "pass", at=BEFORE_REWORK, stored_at=ISSUED)
    yield w.finish()

    w = _world(clock, "stall-expired", "stall-reason")
    w.task("T-1")
    w.verdict("T-1", "review", "pass", expires_s=SHORT_EXPIRY)
    yield w.finish()

    w = _world(clock, "stall-expired-fail-verdict-still-reworks", "stall-reason")
    w.task("T-1")
    w.verdict("T-1", "review", "fail", expires_s=SHORT_EXPIRY)
    yield w.finish()

    w = _world(clock, "stall-nonstandard-verdict-reports-fail-without-stall", "stall-reason")
    w.task("T-1")
    env = w.sign("T-1", "review")
    env["payload"]["verdict"] = "reject"
    envelope.sign_envelope(env)
    w.raw("T-1", "review", "reject", json.dumps(env))
    yield w.finish()

    w = _world(clock, "stall-several-gates-sorted", "stall-reason")
    w.task("T-1", risk="high")
    w.verdict("T-1", "review", "pass", expires_s=SHORT_EXPIRY)
    w.raw("T-1", "security", "pass", None)
    yield w.finish()

    w = _world(clock, "stall-mixed-live-and-uncovered", "stall-reason")
    w.task("T-1", risk="high")
    w.gate_task("G-quality", "quality", ["T-1"], "FAILED")
    w.gate_task("G-security", "security", ["T-1"], "ESCALATED")
    w.verdict("T-1", "review", "pass")
    yield w.finish()

    w = _world(clock, "stall-two-targets-both-recorded", "stall-reason")
    w.task("T-1", depth=1)
    w.task("T-2", depth=2, risk="medium")
    yield w.finish()

    recorded: dict[str, object] = {
        "equal": ["review:absent"],
        "different": ["quality:absent"],
        "empty-list": [],
        "empty-string": "",
        "zero": 0,
        "text": "review:absent",
        "number": 7,
        "null": None,
        "list-with-number": [1],
        "longer": ["review:absent", "quality:absent"],
        "dict": {"a": 1},
    }
    for label, value in recorded.items():
        w = _world(clock, f"stall-recorded-{label}", "stall-recorded")
        w.task("T-1", notes={"gate_stall": value, "other": 1})
        yield w.finish()
        w = _world(clock, f"stall-recorded-{label}-no-stall-now", "stall-recorded")
        w.task("T-1", notes={"other": 1, "gate_stall": value})
        w.gate_task("G-review", "review", ["T-1"], "PLANNED")
        yield w.finish()


def _waived(clock: _Clock) -> Iterator[Row]:
    w = _world(clock, "waive-unexpired-after-rework", "waive")
    w.task("T-1", rework=1)
    w.verdict("T-1", "review", "waive")
    yield w.finish()

    w = _world(clock, "waive-expired", "waive")
    w.task("T-1")
    w.verdict("T-1", "review", "waive", expires_s=SHORT_EXPIRY)
    yield w.finish()

    w = _world(clock, "waive-before-last-rework-is-gone", "waive")
    w.task("T-1", rework=1)
    w.verdict("T-1", "review", "waive", at=BEFORE_REWORK)
    yield w.finish()

    w = _world(clock, "waive-issued-before-rework-stored-after-is-stale", "waive")
    w.task("T-1", rework=1)
    w.verdict("T-1", "review", "waive", at=BEFORE_REWORK, stored_at=ISSUED)
    yield w.finish()

    w = _world(clock, "waive-issued-after-rework-stored-after", "waive")
    w.task("T-1", rework=2)
    w.verdict("T-1", "review", "waive", at=ISSUED, stored_at=ISSUED + 5)
    yield w.finish()

    w = _world(clock, "fail-before-rework-is-stale-so-no-rework-again", "waive")
    w.task("T-1", rework=1)
    w.verdict("T-1", "review", "fail", at=BEFORE_REWORK)
    yield w.finish()

    w = _world(clock, "fail-before-then-pass-after-rework", "waive")
    w.task("T-1", rework=1)
    w.verdict("T-1", "review", "fail", at=BEFORE_REWORK)
    w.verdict("T-1", "review", "pass", at=ISSUED)
    yield w.finish()

    w = _world(clock, "waive-both-gates-medium", "waive")
    w.task("T-1", risk="medium")
    w.verdict("T-1", "review", "waive")
    w.verdict("T-1", "quality", "pass")
    yield w.finish()

    w = _world(clock, "waive-one-expired-one-valid-medium", "waive")
    w.task("T-1", risk="medium")
    w.verdict("T-1", "review", "waive", expires_s=SHORT_EXPIRY)
    w.verdict("T-1", "quality", "waive")
    yield w.finish()


def _several_correlations(clock: _Clock) -> Iterator[Row]:
    def build(name: str) -> World:
        w = _world(clock, name, "correlations")
        w.task("T-a1", corr=CORR, depth=1)
        w.gate_task("G-a", "review", ["T-a1"], corr=CORR)
        w.task("T-a2", corr=CORR, depth=2)
        w.task("T-b1", corr=OTHER_CORR, depth=1)
        w.gate_task("G-b", "review", ["T-b1"], corr=OTHER_CORR)
        w.task("T-b2", corr=OTHER_CORR, depth=2)
        w.task("T-b3", corr=OTHER_CORR, depth=3, risk="medium")
        w.verdict("T-a1", "review", "fail", corr=CORR, issuer="G-a")
        w.passing("T-a2")
        w.verdict("T-b1", "review", "fail", corr=OTHER_CORR, issuer="G-b")
        w.verdict("T-b2", "review", "pass", corr=OTHER_CORR)
        return w

    yield build("correlations-reconcile-a-only").finish(corr=CORR)
    yield build("correlations-reconcile-b-only").finish(corr=OTHER_CORR)
    yield build("correlations-reconcile-an-empty-one").finish(corr="corr-empty")

    w = _world(clock, "correlations-rerun-id-taken-in-another-correlation", "correlations")
    w.task("T-a1", corr=CORR)
    w.gate_task("G-a", "review", ["T-a1"], corr=CORR)
    w.task("G-a.r1", "PLANNED", corr=OTHER_CORR)
    w.verdict("T-a1", "review", "fail")
    yield w.finish(corr=CORR)

    w = _world(clock, "correlations-same-gate-name-other-correlation-ignored", "correlations")
    w.task("T-a1", corr=CORR)
    w.gate_task("G-b", "review", ["T-a1"], "PLANNED", corr=OTHER_CORR)
    yield w.finish(corr=CORR)


def _policy_and_refusals(clock: _Clock) -> Iterator[Row]:
    w = _world(clock, "policy-no-key-approval-raises", "policy")
    w.task("T-1", notes={"gates": []})
    yield w.finish(keys=False)

    w = _world(clock, "policy-no-key-after-an-earlier-approval", "policy")
    w.task("T-1", depth=1, notes={"gates": []})
    w.task("T-2", depth=2, notes={"gates": []})
    yield w.finish(keys=False)

    w = _world(clock, "policy-no-key-nothing-to-approve", "policy")
    w.task("T-1")
    yield w.finish(keys=False)

    w = _world(clock, "policy-no-key-advisory-needs-no-key", "policy")
    w.task("T-1", notes={"gates": []})
    yield w.finish(approve=False, keys=False)

    w = _world(clock, "policy-dev-key-opt-in-approves", "policy")
    w.task("T-1", notes={"gates": []})
    with mock.patch.dict(os.environ, {"SWARM_ALLOW_INSECURE_DEV_KEY": "1"}):
        yield w.finish(keys=False)

    w = _world(clock, "policy-require-key-without-real-key-raises", "policy")
    w.task("T-1", notes={"gates": []})
    with mock.patch.dict(os.environ, {"SWARM_ALLOW_INSECURE_DEV_KEY": "1", "SWARM_REQUIRE_KEY": "1"}):
        yield w.finish(keys=False)

    w = _world(clock, "policy-require-key-with-real-key-approves", "policy")
    w.task("T-1", notes={"gates": []})
    with mock.patch.dict(os.environ, {"SWARM_REQUIRE_KEY": "1"}):
        yield w.finish()

    w = _world(clock, "refused-gate-name-needs-escaped-repr", "refused")
    w.task("T-1", notes={"gates": ["re'view"]})
    env = w.sign("T-1", "review", findings=fail_findings())
    env["payload"]["gate"] = "re'view"
    envelope.sign_envelope(env)
    w.raw("T-1", "re'view", "fail", json.dumps(env))
    w.rust = "not_started"
    yield w.finish()

    w = _world(clock, "refused-non-ascii-digit-in-a-task-id", "refused")
    w.task("T-1")
    w.task("T-\u0663", "PLANNED", depth=2)
    w.passing("T-1")
    w.rust = "not_started"
    yield w.finish()

    w = _world(clock, "refused-newline-in-a-task-id", "refused")
    w.task("T-1")
    w.task("T-nl\n", "PLANNED", depth=2)
    w.passing("T-1")
    w.rust = "not_started"
    yield w.finish()

    w = _world(clock, "refused-gate-for-is-text", "refused")
    w.task("T-1")
    w.task("G-text", "DONE", depth=2, notes={"gate": "review", "gate_for": "T-1 and more", "gates": []})
    w.passing("T-1")
    w.rust = "not_started"
    yield w.finish()

    w = _world(clock, "refused-notes-gate-is-a-list", "refused")
    w.task("T-1")
    w.task("G-odd", "DONE", depth=2, notes={"gate": ["review"], "gates": []})
    w.passing("T-1")
    w.rust = "not_started"
    yield w.finish()

    w = _world(clock, "refused-gate-task-title-is-null", "refused")
    w.task("T-1")
    w.gate_task("G-review", "review", ["T-1"])
    w.set_column("G-review", "title", None)
    w.verdict("T-1", "review", "fail")
    w.rust = "not_started"
    yield w.finish()

    w = _world(clock, "refused-unknown-risk-class", "refused")
    w.task("T-1")
    w.set_column("T-1", "risk_class", "extreme")
    yield w.finish()

    w = _world(clock, "refused-notes-gates-not-a-list", "refused")
    w.task("T-1", notes={"gates": "review"})
    w.rust = "not_started"
    yield w.finish()

    w = _world(clock, "refused-garbage-notes-on-another-task", "refused")
    w.task("T-1")
    w.task("T-other", "PLANNED", depth=2)
    w.set_column("T-other", "notes", "garbage")
    w.passing("T-1")
    w.rust = "not_started"
    yield w.finish()


def constants() -> Row:
    """Python's constants the Rust module must match."""
    scratch = TaskStore(":memory:")
    scratch.create(task_id="T-scratch", correlation_id="c", capability="code.backend")
    default_budget = json.loads(scratch.conn.execute("SELECT budget FROM tasks").fetchone()[0])
    scratch.conn.close()
    return {"max_rework_loops": taskstore.MAX_REWORK_LOOPS, "default_budget": default_budget, "states": STATES,
            "rerun_states": list(RERUN_STATES), "now": NOW}


def build_cases() -> list[Row]:
    """Run every case against the real `reconcile` and return the recorded cases, in the fixed order."""
    clock = _Clock()
    groups: tuple[Scenario, ...] = (_rework_basics, _rework_loops, _lineages, _scans_and_release, _dependants, _approval, _stalls, _stall_reasons,
                                    _waived, _several_correlations, _policy_and_refusals)
    cases: list[Row] = []
    with seeded(clock):
        for group in groups:
            cases.extend(group(clock))
    return cases


def render() -> str:
    """The committed file text: constants first, then one case per line (stable and reviewable in a diff)."""
    lines = ",\n".join("  " + json.dumps(case) for case in build_cases())
    head = json.dumps({"schema": SCHEMA, "constants": constants()})
    return head[:-1] + ',\n "cases": [\n' + lines + "\n ]\n}\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = VECTOR_PATH.read_text(encoding="utf-8") if VECTOR_PATH.exists() else ""
        print("up to date" if current == text else "STALE: rerun without --check")
        return 0 if current == text else 1
    VECTOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTOR_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {VECTOR_PATH.relative_to(REPO)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
