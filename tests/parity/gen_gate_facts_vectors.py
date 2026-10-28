#!/usr/bin/env python3
"""Vectors for the pure Rust gate facts (`crates/gsd-route/src/gate_facts.rs`), generated from the REAL `TaskStore`.

Every case seeds an in-memory store with a target task, verdict rows and gate tasks, using the real gate helpers: `make_verdict` signs a
`gate.verdict` envelope, `TaskStore.record_verdict` stores it, `TaskStore.transition` drives tasks through the state machine (a rework sets
`notes.verdicts_since` the way production does). Rows that the real flow refuses to store (a tampered body, a wrong prefix, a forged
gate, a legacy row without envelope) are inserted with the same `INSERT` that `record_verdict` runs. For several query times the oracle
answers are recorded: `_check_row` for every gate, `missing_gate_reasons`, `unaccepted_gates`, and `latest_verdicts` in its three modes.

The Rust test replays each case from the facts alone: the task row columns, the verdict rows as stored, the state of every task, and
`sig_ok`, which is what the host's signature check (`verify_envelope`) answered for each row. The Rust function never sees a key.

Determinism: the clocks of `swarm.taskstore`, `swarm.gates` and `swarm.envelope` are replaced by one fixed clock, envelope ids come from a
counter, the `live.json` observer is off, and keys are sorted. Signing uses a throwaway HMAC key derived at run time from the seed phrases
below (SHA-256 of an ASCII phrase). The key lives only in the process environment while the cases are built; the file holds envelopes
and their signatures, never a key.

`python3 tests/parity/gen_gate_facts_vectors.py` rewrites the committed file; `--check` compares without writing.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
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

from swarm import envelope, gates, taskstore  # noqa: E402
from swarm.taskstore import TaskStore  # noqa: E402
from swarm.tools import a09_tools  # noqa: E402

VECTOR_PATH = REPO / "crates" / "gsd-route" / "tests" / "vectors" / "gate_facts.json"
SCHEMA = "gsd-route.gate-facts-vectors.v1"
KEY_SEED = "agent-swarm gate-facts vectors: throwaway HMAC key, seed phrase v1"
OTHER_KEY_SEED = "agent-swarm gate-facts vectors: a different throwaway HMAC key, seed phrase v1"
KEY_ENV = ("SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_AGENT_SESSION")

GATES = ("review", "quality", "security", "release")
TARGET = "T-target"
CORR = "corr-gate-facts"
CREATED_AT = 500.0
ISSUED = 1000.25  # exactly representable, so every boundary below is exact
NOW_OK = 1001.25
NOW_LATE = 90000.0
DEFAULT_EXPIRES_S = int(inspect.signature(gates.make_verdict).parameters["expires_s"].default)
REVIEW_EXPIRES_S = int(a09_tools._MAX_EXPIRES_S)

Row = dict[str, Any]
Mutation = Callable[[dict[str, Any]], None]

# state machine paths from CREATED to each state (legal under LEGAL_TRANSITIONS)
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
        digest = hashlib.sha256(f"gate-facts-uuid-{self.count}".encode("ascii")).digest()
        return uuid.UUID(int=int.from_bytes(digest[:16], "big"))


def derive_key(phrase: str) -> str:
    """The throwaway HMAC secret: SHA-256 of an ASCII seed phrase, as hex."""
    return hashlib.sha256(phrase.encode("ascii")).hexdigest()


@contextmanager
def seeded(clock: _Clock) -> Iterator[None]:
    """The fixed clock, fixed ids, the seed-derived key and a clean signing environment, restored on exit."""
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


def ids_of(rows: dict[str, Row]) -> list[list[Any]]:
    """`[gate, id]` pairs of a latest-verdicts answer, in the dict's order."""
    return [[gate, row["id"]] for gate, row in rows.items()]


class Case:
    """One seeded store plus the fixed clock that drives it."""

    def __init__(self, name: str, group: str, clock: _Clock, *, risk: str = "low", notes: dict[str, Any] | None = None) -> None:
        self.name, self.group, self.clock = name, group, clock
        self.store = TaskStore(":memory:")
        clock.now = CREATED_AT
        self.store.create(task_id=TARGET, correlation_id=CORR, capability="code.backend", title="target", risk_class=risk, notes=notes)

    def at(self, now: float) -> None:
        self.clock.now = now

    def task(self, task_id: str, state: str = "CREATED", *, notes: dict[str, Any] | None = None) -> None:
        """A second task (a gate task, or another target) driven to `state`."""
        self.clock.now = CREATED_AT
        self.store.create(task_id=task_id, correlation_id=CORR, capability="gate.review", title=task_id, notes=notes)
        self.drive(task_id, state)

    def issuer(self, task_id: str, state: str) -> None:
        """A gate task in `state`; `notes.gates = []` lets it reach APPROVED without gates of its own."""
        self.task(task_id, state, notes={"gate": "review", "gates": []})

    def drive(self, task_id: str, state: str) -> None:
        self.clock.now = CREATED_AT + 1.0
        for step in PATHS[state]:
            self.store.transition(task_id, step, actor="A01", reason="seed")

    def sign(self, gate: str, *, at: float = ISSUED, task_id: str = TARGET, correlation_id: str = CORR, verdict: str | None = None,
             findings: list[dict[str, Any]] | None = None, expires_s: float = DEFAULT_EXPIRES_S, extra: dict[str, Any] | None = None) -> Row:
        """A signed envelope from the real `make_verdict`, issued at `at`."""
        self.clock.now = at
        return gates.make_verdict(gate=gate, task_id=task_id, agent_id="A08@seed", findings=findings, verdict=verdict, runs={},
                                  expires_s=expires_s, correlation_id=correlation_id, extra=extra)  # type: ignore[arg-type]

    def record(self, env: Row, at: float = ISSUED) -> None:
        """The real `record_verdict`: validates the signature, stores row and envelope."""
        self.clock.now = at
        self.store.record_verdict(env["payload"]["task_id"], env)

    def sign_record(self, gate: str, *, at: float = ISSUED, **kw: Any) -> Row:
        env = self.sign(gate, at=at, **kw)
        self.record(env, at)
        return env

    def raw(self, gate: str, verdict: str | None, text: str | None, ts: float = ISSUED, task_id: str = TARGET) -> None:
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
            (task_id, gate, verdict, "A08@seed", "[]", ts + DEFAULT_EXPIRES_S, ts, text, sig))
        self.store.conn.commit()

    def variant(self, gate: str, mutate: Mutation, *, at: float = ISSUED, resign: bool = True, row_verdict: str | None = None,
                **sign_kw: Any) -> None:
        """A raw row whose envelope is changed after signing; `resign` signs the changed envelope again (a forgery by the key holder)."""
        env = self.sign(gate, at=at, **sign_kw)
        stored = row_verdict if row_verdict is not None else env["payload"]["verdict"]
        mutate(env)
        if resign:
            envelope.sign_envelope(env)
        self.raw(gate, stored, json.dumps(env), at)

    def set_raw_notes(self, text: str | None) -> None:
        self.store.conn.execute("UPDATE tasks SET notes=? WHERE task_id=?", (text, TARGET))
        self.store.conn.commit()

    def export(self, nows: list[float]) -> Row:
        """The facts the Rust function is given, and the oracle's answers at each time in `nows`."""
        store = self.store
        task_row = store.conn.execute("SELECT task_id, correlation_id, risk_class, notes FROM tasks WHERE task_id=?", (TARGET,)).fetchone()
        rows = []
        for r in store.conn.execute("SELECT id, task_id, gate, verdict, envelope_json, ts FROM verdicts ORDER BY id"):
            rows.append({"id": r["id"], "task_id": r["task_id"], "gate": r["gate"], "verdict": r["verdict"],
                         "envelope_json": r["envelope_json"], "ts": r["ts"], "sig_ok": verifies(r["envelope_json"])})
        states = [{"task_id": r["task_id"], "state": r["state"]} for r in store.conn.execute("SELECT task_id, state FROM tasks ORDER BY task_id")]
        queries = []
        for now in nows:
            self.clock.now = now
            task = store.get(TARGET)
            latest = store._latest_rows(task)
            queries.append({
                "now": now,
                "checks": [[g, store._check_row(task, g, latest.get(g), now)] for g in GATES],
                "reasons": [[g, reason] for g, reason in store.missing_gate_reasons(TARGET).items()],
                "unaccepted": sorted(store.unaccepted_gates(TARGET)),
                "latest": {"current": ids_of(store.latest_verdicts(TARGET)),
                           "all": ids_of(store.latest_verdicts(TARGET, include_stale=True)),
                           "verified": ids_of(store.latest_verdicts(TARGET, verified_only=True)),
                           "verified_all": ids_of(store.latest_verdicts(TARGET, include_stale=True, verified_only=True))},
            })
        case = {"name": self.name, "group": self.group,
                "task": {"task_id": task_row["task_id"], "correlation_id": task_row["correlation_id"],
                         "risk_class": task_row["risk_class"], "notes": task_row["notes"]},
                "required_gates": store.required_gates(TARGET), "rows": rows, "task_states": states, "queries": queries}
        store.conn.close()
        return case


def _fail_finding() -> list[dict[str, Any]]:
    return [gates.make_finding("F-1", "major", "functional", "broken")]


def _boundary(issued: float, expires_s: float) -> list[float]:
    end = issued + expires_s
    return [issued - 1.0, issued, end - 0.5, math.nextafter(end, 0.0), end, math.nextafter(end, math.inf), end + 0.5]


def _ok_cases(clock: _Clock) -> Iterator[Row]:
    c = Case("ok-low-pass", "ok", clock)
    c.sign_record("review")
    yield c.export([NOW_OK])

    c = Case("ok-waive", "ok", clock)
    c.sign_record("review", verdict="waive", extra={"waived_by": "human"})
    yield c.export([NOW_OK])

    c = Case("ok-medium-both", "ok", clock, risk="medium")
    c.sign_record("review")
    c.sign_record("quality")
    yield c.export([NOW_OK])

    c = Case("ok-high-all-four", "ok", clock, risk="high")
    for gate in GATES:
        c.sign_record(gate)
    yield c.export([NOW_OK])


def _fail_cases(clock: _Clock) -> Iterator[Row]:
    c = Case("fail-finding", "fail", clock)
    c.sign_record("review", findings=_fail_finding())
    yield c.export([NOW_OK])

    c = Case("fail-forced-verdict", "fail", clock)
    c.sign_record("review", verdict="fail")
    yield c.export([NOW_OK])

    c = Case("fail-expired-reports-expired", "fail", clock)
    c.sign_record("review", findings=_fail_finding(), expires_s=10)
    yield c.export([NOW_OK, ISSUED + 10.0, NOW_LATE])


def _expiry_cases(clock: _Clock) -> Iterator[Row]:
    for label, expires_s in (("86400", DEFAULT_EXPIRES_S), ("172800", REVIEW_EXPIRES_S), ("10", 10), ("float-2.5", 2.5), ("zero", 0)):
        c = Case(f"expiry-{label}", "expired", clock)
        c.sign_record("review", expires_s=expires_s)
        yield c.export(_boundary(ISSUED, float(expires_s)))
    c = Case("expiry-waive-expires", "expired", clock)
    c.sign_record("review", verdict="waive", extra={"waived_by": "human"}, expires_s=10)
    yield c.export([NOW_OK, ISSUED + 10.0])


def _absent_cases(clock: _Clock) -> Iterator[Row]:
    for risk in ("low", "medium", "high"):
        yield Case(f"absent-no-rows-{risk}", "absent", clock, risk=risk).export([NOW_OK])

    c = Case("absent-other-gate-only", "absent", clock, risk="medium")
    c.sign_record("security")
    yield c.export([NOW_OK])

    c = Case("absent-other-task-rows", "absent", clock)
    c.task("T-other", "IN_REVIEW")
    c.sign_record("review", task_id="T-other")
    yield c.export([NOW_OK])


def _unsigned_cases(clock: _Clock) -> Iterator[Row]:
    c = Case("unsigned-null-envelope", "unsigned", clock)
    c.raw("review", "pass", None)
    yield c.export([NOW_OK])

    c = Case("unsigned-empty-envelope", "unsigned", clock, risk="medium")
    c.raw("review", "pass", "")
    c.raw("quality", "pass", None)
    yield c.export([NOW_OK])


def _bad_sig_cases(clock: _Clock) -> Iterator[Row]:
    def tamper(env: dict[str, Any]) -> None:
        env["payload"]["runs"] = {"tampered": True}

    def tamper_verdict(env: dict[str, Any]) -> None:
        env["payload"]["verdict"] = "pass"

    def tamper_expiry(env: dict[str, Any]) -> None:
        env["payload"]["expires_s"] = 9999999

    signature: dict[str, Mutation] = {
        "tampered-runs": tamper,
        "tampered-verdict": tamper_verdict,
        "tampered-expiry": tamper_expiry,
        "prefix-hmac2": lambda e: e.update(sig="hmac2:" + e["sig"][5:]),
        "prefix-ed25519-garbage": lambda e: e.update(sig="ed25519:QUJD"),
        "prefix-unknown": lambda e: e.update(sig="rsa:" + e["sig"][5:]),
        "no-prefix": lambda e: e.update(sig=e["sig"][5:]),
        "hmac-not-base64": lambda e: e.update(sig="hmac:!!not base64!!"),
        "hmac-truncated": lambda e: e.update(sig=e["sig"][:-8]),
        "sig-empty": lambda e: e.update(sig=""),
        "sig-null": lambda e: e.update(sig=None),
        "sig-number": lambda e: e.update(sig=5),
        "sig-removed": lambda e: e.pop("sig"),
    }
    for label, mutate in signature.items():
        c = Case(f"bad-sig-{label}", "bad-sig", clock)
        failing = label == "tampered-verdict"
        c.variant("review", mutate, resign=False, verdict="fail" if failing else None, row_verdict="fail" if failing else None)
        yield c.export([NOW_OK])

    c = Case("bad-sig-wrong-key", "bad-sig", clock)
    with mock.patch.dict(os.environ, {"SWARM_SIGNING_KEY": derive_key(OTHER_KEY_SEED)}):
        c.raw("review", "pass", json.dumps(c.sign("review")))
    yield c.export([NOW_OK])

    texts = {"not-json": "{not json", "json-null": "null", "json-list": "[1, 2]", "json-string": '"text"', "json-number": "123",
             "json-empty-object": "{}", "truncated": '{"msg_id": "x", "payload": {'}
    for label, text in texts.items():
        c = Case(f"bad-sig-text-{label}", "bad-sig", clock)
        c.raw("review", "pass", text)
        yield c.export([NOW_OK])

    # structure the key holder signed correctly, which validate_verdict still refuses (sig_ok is true, the answer is still bad-sig)
    structure: dict[str, Mutation] = {
        "missing-findings": lambda e: e["payload"].pop("findings"),
        "missing-gate": lambda e: e["payload"].pop("gate"),
        "missing-task-id": lambda e: e["payload"].pop("task_id"),
        "missing-verdict": lambda e: e["payload"].pop("verdict"),
        "missing-expires": lambda e: e["payload"].pop("expires_s"),
        "wrong-type": lambda e: e.update(type="task.assign", schema="swarm.v1.task.assign"),
        "schema-mismatch": lambda e: e.update(schema="swarm.v1.other"),
        "schema-no-prefix": lambda e: e.update(schema="gate.verdict"),
        "schema-not-string": lambda e: e.update(schema=7),
        "type-not-string": lambda e: e.update(type=7),
        "type-no-dot": lambda e: e.update(type="gateverdict", schema="swarm.v1.gateverdict"),
        "payload-list": lambda e: e.update(payload=[1]),
        "payload-null": lambda e: e.update(payload=None),
        "priority-bad": lambda e: e.update(priority="P9"),
        "priority-not-string": lambda e: e.update(priority=1),
        "risk-class-bad": lambda e: e.update(risk_class="extreme"),
        "missing-trace-id": lambda e: e.pop("trace_id"),
        "missing-correlation": lambda e: e.pop("correlation_id"),
        "missing-ttl": lambda e: e.pop("ttl_s"),
    }
    for label, mutate in structure.items():
        c = Case(f"bad-sig-structure-{label}", "bad-sig", clock)
        c.variant("review", mutate, verdict="pass")
        yield c.export([NOW_OK])

    c = Case("bad-sig-structure-extra-fields-ok", "bad-sig", clock)
    c.variant("review", lambda e: e.update(extra_field=[1, {"a": None}], risk_class="high", priority="P0"), verdict="pass")
    yield c.export([NOW_OK])


def _mismatch_cases(clock: _Clock) -> Iterator[Row]:
    c = Case("mismatch-envelope-gate", "mismatch", clock)
    c.raw("review", "pass", json.dumps(c.sign("quality")))
    yield c.export([NOW_OK])

    c = Case("mismatch-envelope-task", "mismatch", clock)
    c.raw("review", "pass", json.dumps(c.sign("review", task_id="T-other")))
    yield c.export([NOW_OK])

    c = Case("mismatch-correlation", "mismatch", clock)
    c.raw("review", "pass", json.dumps(c.sign("review", correlation_id="corr-other")))
    yield c.export([NOW_OK])

    c = Case("mismatch-row-pass-envelope-fail", "mismatch", clock)
    c.raw("review", "pass", json.dumps(c.sign("review", verdict="fail")))
    yield c.export([NOW_OK])

    c = Case("mismatch-row-fail-envelope-pass", "mismatch", clock)
    c.raw("review", "fail", json.dumps(c.sign("review")))
    yield c.export([NOW_OK])

    c = Case("mismatch-row-verdict-null", "mismatch", clock)
    c.raw("review", None, json.dumps(c.sign("review")))
    yield c.export([NOW_OK])

    c = Case("mismatch-envelope-verdict-null", "mismatch", clock)
    c.variant("review", lambda e: e["payload"].update(verdict=None), row_verdict="pass")
    yield c.export([NOW_OK])

    c = Case("fail-envelope-and-row-verdict-null", "fail", clock)
    c.variant("review", lambda e: e["payload"].update(verdict=None))
    c.store.conn.execute("UPDATE verdicts SET verdict=NULL")
    c.store.conn.commit()
    yield c.export([NOW_OK])

    c = Case("mismatch-envelope-verdict-number", "mismatch", clock)
    c.variant("review", lambda e: e["payload"].update(verdict=5), row_verdict="5")
    yield c.export([NOW_OK])

    for label, key, value in (("gate", "gate", 5), ("task-id", "task_id", ["T-target"]), ("correlation", "correlation_id", None)):
        c = Case(f"mismatch-not-a-string-{label}", "mismatch", clock)
        if key == "correlation_id":
            c.variant("review", lambda e, v=value: e.update(correlation_id=v), verdict="pass")
        else:
            c.variant("review", lambda e, k=key, v=value: e["payload"].update({k: v}), verdict="pass")
        yield c.export([NOW_OK])


def _number_cases(clock: _Clock) -> Iterator[Row]:
    """issued_at and expires_s of every JSON type and of the numeric strings Python's `float()` takes."""
    missing = object()
    values: list[tuple[str, object]] = [
        ("missing", missing), ("null", None), ("true", True), ("false", False), ("list", []), ("object", {}), ("empty-string", ""),
        ("text", "abc"), ("blank", "   "), ("int-1000", 1000), ("float-1000.5", 1000.5), ("str-1000.5", "1000.5"),
        ("str-padded", " 1000.5\n"), ("str-exp", "1e3"), ("str-plus", "+1000.5"), ("str-dot", ".5e3"), ("str-trailing-dot", "1000."),
        ("str-neg", "-1000.5"), ("str-inf", "inf"), ("str-infinity-signed", "-Infinity"), ("str-nan", "nan"), ("str-hex", "0x10"),
        ("str-exp-bare", "1e"), ("str-two-dots", "1.5.5"), ("str-int", "1000"), ("str-huge", "1e999"), ("float-huge", 1e300),
        ("int-neg", -5), ("float-tiny", 5e-324),
    ]
    for field in ("issued_at", "expires_s"):
        for label, value in values:
            c = Case(f"number-{field}-{label}", "mismatch", clock)

            def mutate(env: dict[str, Any], f: str = field, v: object = value) -> None:
                if v is missing:
                    env["payload"].pop(f, None)
                else:
                    env["payload"][f] = v

            c.variant("review", mutate, verdict="pass")
            yield c.export([NOW_OK, NOW_LATE])


def _stale_cases(clock: _Clock) -> Iterator[Row]:
    def reworked(c: Case, at: float) -> None:
        c.drive(TARGET, "IN_REVIEW")
        c.at(at)
        c.store.transition(TARGET, "CHANGES_REQUESTED", actor="A01", reason="needs work")

    c = Case("stale-reinserted-after-rework", "stale", clock)
    env = c.sign_record("review")
    reworked(c, 2000.0)
    c.record(env, 2500.0)  # the old signed envelope stored again after the rework: the row passes the ts filter, issued_at does not
    yield c.export([2501.0])

    c = Case("stale-old-row-filtered", "stale", clock)
    c.sign_record("review")
    reworked(c, 2000.0)
    yield c.export([2501.0])

    c = Case("stale-then-fresh", "stale", clock, risk="medium")
    old = c.sign_record("review")
    c.sign_record("quality")
    reworked(c, 2000.0)
    c.record(old, 2500.0)
    c.sign_record("quality", at=3000.0)
    yield c.export([3001.0])

    c = Case("stale-rework-twice", "stale", clock)
    c.sign_record("review")
    reworked(c, 2000.0)
    c.sign_record("review", at=2500.0)
    c.at(2600.0)
    c.store.transition(TARGET, "IN_PROGRESS", actor="A01", reason="again")
    c.store.transition(TARGET, "IN_REVIEW", actor="A01", reason="again")
    c.at(2700.0)
    c.store.transition(TARGET, "CHANGES_REQUESTED", actor="A01", reason="again")
    yield c.export([2800.0])

    inside = ISSUED
    above = math.nextafter(ISSUED, math.inf)
    below = math.nextafter(ISSUED, 0.0)
    for label, since, ts in (("equal", inside, inside), ("just-above", above, 2000.0), ("just-above-row-ts-equal", above, above),
                             ("just-below", below, 2000.0), ("row-ts-just-below", inside, below)):
        c = Case(f"since-{label}", "stale", clock)
        c.store.set_notes(TARGET, verdicts_since=since)
        env = c.sign("review")
        c.record(env, ts)
        yield c.export([NOW_OK, 2001.0])

    for label, notes in (("int", {"verdicts_since": 1500}), ("null", {"verdicts_since": None}), ("zero", {"verdicts_since": 0}),
                         ("false", {"verdicts_since": False}), ("empty-string", {"verdicts_since": ""}), ("float", {"verdicts_since": 999.5})):
        c = Case(f"since-notes-{label}", "stale", clock, notes=notes)
        c.sign_record("review")
        yield c.export([NOW_OK])


def _dry_run_cases(clock: _Clock) -> Iterator[Row]:
    payload_values: list[object] = [True, False, 0, 1, "", "no", [], [0], {}, {"a": 1}, None, 0.0, 2.5, -1]
    for index, value in enumerate(payload_values):
        for notes_label, notes in (("no-notes", None), ("notes-true", {"dry_run": True})):
            c = Case(f"dry-run-payload-{index}-{notes_label}", "dry-run", clock, notes=notes)
            c.sign_record("review", extra={"dry_run": value})
            yield c.export([NOW_OK])
    notes_values: list[tuple[str, object]] = [
        ("true", True), ("one", 1), ("text", "yes"), ("list", [1]), ("dict", {"a": 1}), ("negative", -0.5),
        ("false", False), ("zero", 0), ("float-zero", 0.0), ("empty", ""), ("empty-list", []), ("empty-dict", {}), ("null", None),
    ]
    for label, value in notes_values:
        c = Case(f"dry-run-notes-{label}", "dry-run", clock, notes={"dry_run": value})
        c.sign_record("review", extra={"dry_run": True})
        yield c.export([NOW_OK])

    c = Case("dry-run-order-expired-first-not", "dry-run", clock)
    c.sign_record("review", extra={"dry_run": True}, expires_s=10)
    yield c.export([NOW_OK, NOW_LATE])

    c = Case("dry-run-allowed-then-expires", "dry-run", clock, notes={"dry_run": True})
    c.sign_record("review", extra={"dry_run": True}, expires_s=10)
    yield c.export([NOW_OK, NOW_LATE])

    c = Case("dry-run-stale-wins", "dry-run", clock)
    old = c.sign("review", extra={"dry_run": True})
    c.drive(TARGET, "IN_REVIEW")
    c.at(2000.0)
    c.store.transition(TARGET, "CHANGES_REQUESTED", actor="A01", reason="needs work")
    c.record(old, 2500.0)
    yield c.export([2501.0])

    c = Case("dry-run-fail-verdict", "dry-run", clock, notes={"dry_run": True})
    c.sign_record("review", extra={"dry_run": True}, findings=_fail_finding())
    yield c.export([NOW_OK])


def _unaccepted_cases(clock: _Clock) -> Iterator[Row]:
    for state in PATHS:
        c = Case(f"issuer-{state}", "unaccepted", clock)
        c.issuer("T-gate", state)
        c.sign_record("review", extra={"gate_task": "T-gate"})
        yield c.export([NOW_OK])

    c = Case("issuer-unknown-task", "unaccepted", clock)
    c.sign_record("review", extra={"gate_task": "T-nowhere"})
    yield c.export([NOW_OK])

    c = Case("issuer-empty-string", "unaccepted", clock)
    c.sign_record("review", extra={"gate_task": ""})
    yield c.export([NOW_OK])

    c = Case("issuer-case-sensitive-name", "unaccepted", clock)
    c.issuer("T-gate", "DONE")
    c.sign_record("review", extra={"gate_task": "t-gate"})
    yield c.export([NOW_OK])

    c = Case("issuer-absent-field-accepted", "unaccepted", clock)
    c.sign_record("review")
    yield c.export([NOW_OK])

    c = Case("issuer-null-field-accepted", "unaccepted", clock)
    c.sign_record("review", extra={"gate_task": None})
    yield c.export([NOW_OK])

    c = Case("issuer-is-the-target", "unaccepted", clock)
    c.sign_record("review", extra={"gate_task": TARGET})
    yield c.export([NOW_OK])

    c = Case("issuer-failing-row-not-reported", "unaccepted", clock)
    c.issuer("T-gate", "FAILED")
    c.sign_record("review", findings=_fail_finding(), extra={"gate_task": "T-gate"})
    yield c.export([NOW_OK])

    c = Case("issuer-expired-row-not-reported", "unaccepted", clock)
    c.issuer("T-gate", "BLOCKED")
    c.sign_record("review", expires_s=10, extra={"gate_task": "T-gate"})
    yield c.export([NOW_OK, NOW_LATE])

    c = Case("issuer-bad-sig-row-not-reported", "unaccepted", clock)
    c.issuer("T-gate", "CANCELLED")
    c.variant("review", lambda e: e["payload"].update(gate_task="T-gate"), resign=False, verdict="pass")
    yield c.export([NOW_OK])

    c = Case("issuer-latest-row-decides", "unaccepted", clock)
    c.issuer("T-gate-a", "DONE")
    c.issuer("T-gate-b", "FAILED")
    c.sign_record("review", at=1000.25, extra={"gate_task": "T-gate-a"})
    c.sign_record("review", at=1000.5, extra={"gate_task": "T-gate-b"})
    yield c.export([NOW_OK])

    c = Case("issuer-state-changes-after-row", "unaccepted", clock)
    c.issuer("T-gate", "IN_PROGRESS")
    c.sign_record("review", extra={"gate_task": "T-gate"})
    c.at(1100.0)
    c.store.transition("T-gate", "IN_REVIEW", actor="A01", reason="accepted later")
    yield c.export([NOW_OK])


def _several_cases(clock: _Clock) -> Iterator[Row]:
    c = Case("several-mixed-four", "several", clock, risk="high")
    c.issuer("T-g-review", "DONE")
    c.issuer("T-g-quality", "FAILED")
    c.issuer("T-g-security", "IN_REVIEW")
    c.sign_record("review", extra={"gate_task": "T-g-review"})
    c.sign_record("quality", findings=_fail_finding(), extra={"gate_task": "T-g-quality"})
    c.sign_record("security", expires_s=10, extra={"gate_task": "T-g-security"})
    yield c.export([NOW_OK, NOW_LATE])

    c = Case("several-unaccepted-two-of-four", "several", clock, risk="high")
    c.issuer("T-g-a", "DONE")
    c.issuer("T-g-b", "RETRY")
    c.issuer("T-g-c", "IN_PROGRESS")
    c.issuer("T-g-d", "APPROVED")
    for gate, issuer in zip(GATES, ("T-g-a", "T-g-b", "T-g-c", "T-g-d"), strict=True):
        c.sign_record(gate, extra={"gate_task": issuer})
    yield c.export([NOW_OK])

    c = Case("several-every-reason", "several", clock, risk="high")
    c.sign_record("review", findings=_fail_finding())
    c.raw("quality", "pass", None)
    c.variant("security", lambda e: e["payload"].update(runs={"x": 1}), resign=False, verdict="pass")
    c.sign_record("release", expires_s=10)
    yield c.export([NOW_OK, NOW_LATE])

    c = Case("several-reasons-bad-sig-mismatch-dry-stale", "several", clock, risk="high")
    c.variant("review", lambda e: e["payload"].update(runs={"x": 1}), resign=False, verdict="pass")
    c.raw("quality", "pass", json.dumps(c.sign("review")))
    c.sign_record("security", extra={"dry_run": True})
    c.variant("release", lambda e: e["payload"].update(issued_at=500.0), verdict="pass")
    yield c.export([NOW_OK])


def _latest_cases(clock: _Clock) -> Iterator[Row]:
    c = Case("latest-pass-then-fail", "latest", clock)
    c.sign_record("review", at=1000.25)
    c.sign_record("review", at=1000.5, findings=_fail_finding())
    yield c.export([NOW_OK])

    c = Case("latest-fail-then-pass", "latest", clock)
    c.sign_record("review", at=1000.25, findings=_fail_finding())
    c.sign_record("review", at=1000.5)
    yield c.export([NOW_OK])

    c = Case("latest-garbage-over-ok", "latest", clock)
    c.sign_record("review", at=1000.25)
    c.raw("review", "pass", "{garbage", 1000.5)
    yield c.export([NOW_OK])

    c = Case("latest-unsigned-over-ok", "latest", clock)
    c.sign_record("review", at=1000.25)
    c.raw("review", "pass", None, 1000.5)
    yield c.export([NOW_OK])

    c = Case("latest-ok-over-garbage", "latest", clock)
    c.raw("review", "pass", "{garbage", 1000.25)
    c.sign_record("review", at=1000.5)
    yield c.export([NOW_OK])

    c = Case("latest-gate-order-first-seen", "latest", clock, risk="high")
    c.sign_record("review", at=1000.25)
    c.sign_record("quality", at=1000.5)
    c.sign_record("review", at=1000.75, findings=_fail_finding())
    c.sign_record("release", at=1001.0)
    yield c.export([NOW_OK + 1.0])

    c = Case("latest-id-order-not-ts-order", "latest", clock)
    c.raw("review", "pass", json.dumps(c.sign("review")), ts=2000.0)
    c.raw("review", "fail", json.dumps(c.sign("review", verdict="fail")), ts=1500.0)
    yield c.export([2001.0])

    c = Case("latest-id-order-not-ts-order-reversed", "latest", clock)
    c.raw("review", "fail", json.dumps(c.sign("review", verdict="fail")), ts=2000.0)
    c.raw("review", "pass", json.dumps(c.sign("review")), ts=1500.0)
    yield c.export([2001.0])

    c = Case("latest-other-task-rows-later-ids", "latest", clock)
    c.task("T-other", "IN_REVIEW")
    c.sign_record("review")
    c.sign_record("review", task_id="T-other", findings=_fail_finding())
    yield c.export([NOW_OK])

    c = Case("latest-unrequired-gate-row-ignored", "latest", clock)
    c.sign_record("security", findings=_fail_finding())
    c.sign_record("review")
    yield c.export([NOW_OK])


def _risk_cases(clock: _Clock) -> Iterator[Row]:
    for risk in ("low", "medium", "high"):
        c = Case(f"risk-{risk}-review-only", "risk", clock, risk=risk)
        c.sign_record("review")
        yield c.export([NOW_OK])

    c = Case("risk-high-partial", "risk", clock, risk="high")
    c.sign_record("review")
    c.sign_record("quality")
    c.sign_record("security", findings=_fail_finding())
    yield c.export([NOW_OK])

    overrides: list[tuple[str, dict[str, Any] | None, str]] = [
        ("security-only", {"gates": ["security"]}, "low"),
        ("empty-list-low", {"gates": []}, "low"),
        ("empty-list-high", {"gates": []}, "high"),
        ("order-release-review", {"gates": ["release", "review"]}, "low"),
        ("order-quality-review", {"gates": ["quality", "review"]}, "high"),
        ("duplicate", {"gates": ["review", "review", "quality", "review"]}, "low"),
        ("duplicate-missing-gate", {"gates": ["quality", "review", "quality", "security", "quality"]}, "low"),
        ("null-uses-risk", {"gates": None}, "medium"),
        ("unknown-gate-name", {"gates": ["bogus", "review"]}, "low"),
        ("empty-string-gate", {"gates": [""]}, "low"),
        ("absent-uses-risk", {"other": 1}, "high"),
    ]
    for label, notes, risk in overrides:
        c = Case(f"override-{label}", "override", clock, risk=risk, notes=notes)
        c.sign_record("review")
        c.sign_record("security", findings=_fail_finding())
        yield c.export([NOW_OK])

    raw_notes: list[tuple[str, str | None]] = [
        ("sql-null", None), ("empty", ""), ("garbage", "garbage"), ("list", "[1]"), ("string", '"text"'), ("number", "5"),
        ("null-literal", "null"), ("gates-in-object", '{"gates": ["security"]}'),
    ]
    for label, text in raw_notes:
        c = Case(f"notes-raw-{label}", "override", clock, risk="medium")
        c.set_raw_notes(text)
        c.sign_record("review")
        yield c.export([NOW_OK])


def constants() -> Row:
    """Python's tables and constants the Rust module must match."""
    return {
        "accepted_issuer_states": sorted(taskstore.ACCEPTED_ISSUER_STATES),
        "default_expires_s": DEFAULT_EXPIRES_S,
        "gates": list(GATES),
        "gates_by_risk": taskstore.GATES_BY_RISK,
        "outcomes": ["ok", "fail", "expired", "absent", "unsigned", "bad-sig", "mismatch", "stale", "dry-run"],
        "review_expires_s": REVIEW_EXPIRES_S,
        "verified_outcomes": sorted(taskstore._VERIFIED),
    }


def build_cases() -> list[Row]:
    """Run every case against the real `TaskStore` and return the recorded cases, in the fixed order."""
    clock = _Clock()
    groups = (_ok_cases, _fail_cases, _expiry_cases, _absent_cases, _unsigned_cases, _bad_sig_cases, _mismatch_cases, _number_cases,
              _stale_cases, _dry_run_cases, _unaccepted_cases, _several_cases, _latest_cases, _risk_cases)
    cases: list[Row] = []
    with seeded(clock):
        for group in groups:
            cases.extend(group(clock))
    return cases


def render() -> str:
    """The committed file text: constants first, then one case per line (stable and reviewable in a diff)."""
    lines = ",\n".join("  " + json.dumps(case, sort_keys=True) for case in build_cases())
    head = json.dumps({"schema": SCHEMA, "constants": constants()}, sort_keys=True)
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
